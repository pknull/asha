"""Descriptor-relative, per-initiative durable orchestration record storage."""

from __future__ import annotations

import copy
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..store import (
    StoreCommittedError,
    StoreError,
    _directory_fd,
    _managed_start,
    _open_existing_file,
    _registry_lock,
    _task_lock,
    _validate_open_file,
)
from .config import OrchestrationConfig
from .model import (
    ACTION_TRANSITIONS,
    APPROVAL_TRANSITIONS,
    ATTEMPT_TRANSITIONS,
    BUNDLE_TRANSITIONS,
    NODE_TRANSITIONS,
    REVIEW_TRANSITIONS,
    COORDINATOR_TERMINAL_STATES,
    COORDINATOR_TRANSITIONS,
    RESULT_INGESTION_TRANSITIONS,
    RESULT_PUBLICATION_TRANSITIONS,
    VERIFICATION_TRANSITIONS,
    ModelError,
    _validate_retained_plan_observation,
    canonical_uuid,
    plan_digest,
    record_digest,
    require_transition,
    validate_action,
    validate_approval,
    validate_attempt,
    validate_bundle,
    validate_coordinator,
    validate_coordinator_checkpoint,
    validate_evidence,
    validate_event,
    validate_initiative,
    validate_link,
    validate_node,
    validate_plan_record,
    validate_result,
    validate_result_ingestion,
    validate_result_publication,
    validate_review,
    validate_seal,
    validate_seal_preparation,
    validate_slug,
    validate_verification,
)


MAX_RECORD_BYTES = 256 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC

_LAYOUT_DIRECTORIES = (
    "plans", "nodes", "attempts", "assignments", "links", "result-ingestions",
    "result-publications", "results",
    "seal-preparations", "seals", "reviews", "verifications", "bundles", "approvals", "actions",
    "evidence", "outputs", "events", "locks", "coordinators", "checkpoints",
)
_INVENTORY_CLASSES = ("initiative",) + _LAYOUT_DIRECTORIES


class _DuplicateJsonKey(ValueError):
    pass


class ObservationOnlyPlanError(StoreError):
    """A retained plan is readable evidence but lacks execution authority."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _directory_error(name: str, exc: OSError) -> StoreError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return StoreError(f"symlink or non-directory rejected in initiative path: {name}")
    return StoreError(f"cannot open initiative directory {name}: {exc}")


def _open_directory(parent_fd: int, name: str, *, create: bool) -> int | None:
    if not name or "/" in name or name in {".", ".."}:
        raise StoreError("unsafe initiative directory component")
    created = False
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileExistsError:
            try:
                fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise _directory_error(name, exc) from exc
        except OSError as exc:
            raise _directory_error(name, exc) from exc
    except OSError as exc:
        raise _directory_error(name, exc) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise StoreError(f"initiative path component is not a directory: {name}")
        if metadata.st_uid != os.geteuid():
            raise StoreError(f"initiative directory is not owned by the effective user: {name}")
        if created:
            os.fchmod(fd, 0o700)
            metadata = os.fstat(fd)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise StoreError(f"initiative directory must have mode 0700: {name}")
        if create:
            os.fsync(fd)
            os.fsync(parent_fd)
        return fd
    except Exception:
        _close(fd)
        raise


def _canonical_bytes(validator: Callable[[Any], dict[str, Any]], record: Any) -> tuple[dict[str, Any], bytes]:
    try:
        validated = validator(copy.deepcopy(record))
    except ModelError as exc:
        raise StoreError(str(exc)) from exc
    raw = json.dumps(
        validated, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    if len(raw) > MAX_RECORD_BYTES:
        raise StoreError(f"record exceeds {MAX_RECORD_BYTES} bytes")
    return validated, raw


class InitiativeStore:
    """One locked record tree per initiative beneath the Control state root."""

    def __init__(self, config: OrchestrationConfig, *, lock_wait_hook=None):
        self.config = config
        self._lock_wait_hook = lock_wait_hook
        self._root_managed_start = _managed_start(
            config.initiatives_dir, ("control", "initiatives")
        )
        self.skipped: list[dict[str, str]] = []

    @contextmanager
    def _initiative_directory(
        self,
        initiative_id: str,
        *,
        create_root: bool,
        create_initiative: bool,
    ) -> Iterator[tuple[int, int]]:
        try:
            initiative_id = canonical_uuid(initiative_id, "initiative_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with _directory_fd(
            self.config.initiatives_dir,
            create=create_root,
            managed_start=self._root_managed_start,
        ) as root_fd:
            if root_fd is None:
                raise StoreError(f"initiative not found: {initiative_id}")
            if create_initiative:
                with _registry_lock(root_fd):
                    initiative_fd = _open_directory(
                        root_fd, initiative_id, create=True
                    )
            else:
                initiative_fd = _open_directory(root_fd, initiative_id, create=False)
            if initiative_fd is None:
                raise StoreError(f"initiative not found: {initiative_id}")
            try:
                yield root_fd, initiative_fd
            finally:
                _close(initiative_fd)

    def _ensure_layout(self, initiative_fd: int) -> None:
        for name in _LAYOUT_DIRECTORIES:
            fd = _open_directory(initiative_fd, name, create=True)
            assert fd is not None
            _close(fd)

    @contextmanager
    def _locked_fds(
        self,
        initiative_id: str,
        *,
        create: bool = False,
    ) -> Iterator[tuple[int, int]]:
        with self._initiative_directory(
            initiative_id, create_root=create, create_initiative=create
        ) as (root_fd, initiative_fd):
            # Increment 2b added record classes to initiatives already created
            # by 2a. The first locked use upgrades only the missing 0700
            # directories; immutable records and mutable snapshots are untouched.
            self._ensure_layout(initiative_fd)
            locks_fd = _open_directory(initiative_fd, "locks", create=True)
            if locks_fd is None:
                raise StoreError("initiative lock directory is missing")
            try:
                with _task_lock(locks_fd, "initiative", self._lock_wait_hook):
                    yield root_fd, initiative_fd
            finally:
                _close(locks_fd)

    @contextmanager
    def transaction_lock(self, initiative_id: str) -> Iterator[None]:
        """Hold the re-entrant production lock for one existing initiative."""
        with self._locked_fds(initiative_id):
            yield

    @contextmanager
    def result_ingestion_lock(
        self, initiative_id: str, ingestion_id: str,
    ) -> Iterator[None]:
        """Single-flight one ingestion without blocking the whole initiative."""
        try:
            ingestion_id = canonical_uuid(ingestion_id, "ingestion_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with self._initiative_directory(
            initiative_id, create_root=False, create_initiative=False,
        ) as (_root_fd, initiative_fd):
            self._ensure_layout(initiative_fd)
            locks_fd = _open_directory(initiative_fd, "locks", create=True)
            if locks_fd is None:
                raise StoreError("initiative lock directory is missing")
            try:
                with _task_lock(
                    locks_fd, f"result-ingestion-{ingestion_id}",
                    self._lock_wait_hook,
                ):
                    yield
            finally:
                _close(locks_fd)

    @staticmethod
    def _subdirectory(initiative_fd: int, name: str) -> int:
        fd = _open_directory(initiative_fd, name, create=False)
        if fd is None:
            raise StoreError(f"initiative storage directory is missing: {name}")
        return fd

    @staticmethod
    def _read_raw(directory_fd: int, name: str, label: str) -> Any:
        try:
            fd = _open_existing_file(directory_fd, name, label)
        except FileNotFoundError:
            raise StoreError(f"{label} not found: {name}") from None
        try:
            metadata = os.fstat(fd)
            if metadata.st_size > MAX_RECORD_BYTES:
                raise StoreError(f"{label} exceeds {MAX_RECORD_BYTES} bytes: {name}")
            remaining = MAX_RECORD_BYTES + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_RECORD_BYTES:
                raise StoreError(f"{label} exceeds {MAX_RECORD_BYTES} bytes: {name}")
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError(f"cannot read {label} {name}: {exc}") from exc
        finally:
            _close(fd)
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        except _DuplicateJsonKey as exc:
            raise StoreError(f"duplicate JSON key in {label} {name}") from exc
        except RecursionError as exc:
            raise StoreError(f"{label} nesting exceeds supported limit: {name}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StoreError(f"invalid JSON in {label} {name}: {exc}") from exc

    @staticmethod
    def _validated_read(
        directory_fd: int,
        name: str,
        label: str,
        validator: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        value = InitiativeStore._read_raw(directory_fd, name, label)
        try:
            return validator(value)
        except ModelError as exc:
            raise StoreError(f"invalid {label} {name}: {exc}") from exc

    @staticmethod
    def _read_if_exists(
        directory_fd: int,
        name: str,
        label: str,
        validator: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any] | None:
        try:
            return InitiativeStore._validated_read(
                directory_fd, name, label, validator
            )
        except StoreError as exc:
            if str(exc) == f"{label} not found: {name}":
                return None
            raise

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        written = 0
        while written < len(raw):
            count = os.write(fd, raw[written:])
            if count <= 0:
                raise StoreError("short write while saving orchestration record")
            written += count

    @staticmethod
    def _write_mutable(directory_fd: int, name: str, raw: bytes) -> None:
        temporary = f".{name}.tmp.{secrets.token_hex(8)}"
        fd = -1
        replaced = False
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(fd, 0o600)
            InitiativeStore._write_all(fd, raw)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            replaced = True
            os.fsync(directory_fd)
        except (StoreError, StoreCommittedError):
            raise
        except OSError as exc:
            if replaced:
                raise StoreCommittedError(
                    f"record {name} is visible but durability is indeterminate: {exc}"
                ) from exc
            raise StoreError(f"atomic record save failed for {name}: {exc}") from exc
        finally:
            if fd >= 0:
                _close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass

    @staticmethod
    def _write_once(directory_fd: int, name: str, raw: bytes) -> None:
        InitiativeStore._sweep_write_residue(directory_fd)
        temporary = f".{name}.tmp.{secrets.token_hex(8)}"
        fd = -1
        linked = False
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(fd, 0o600)
            _validate_open_file(fd, "temporary orchestration record")
            InitiativeStore._write_all(fd, raw)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            try:
                os.link(
                    temporary, name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise StoreError(f"write-once record already exists: {name}") from exc
            linked = True
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except (StoreError, StoreCommittedError):
            raise
        except OSError as exc:
            if linked:
                raise StoreCommittedError(
                    f"write-once record {name} is visible but durability is indeterminate: {exc}"
                ) from exc
            raise StoreError(f"write-once record save failed for {name}: {exc}") from exc
        finally:
            if fd >= 0:
                _close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass

    @staticmethod
    def _sweep_write_residue(directory_fd: int) -> None:
        """Remove abandoned atomic-write temporaries while holding the initiative lock."""
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise StoreError(f"cannot inspect abandoned write residue: {exc}") from exc
        abandoned = [
            name for name in names
            if re.fullmatch(r"\..+\.tmp\..+", name) is not None
        ]
        removed = False
        for name in sorted(abandoned):
            try:
                os.unlink(name, dir_fd=directory_fd)
                removed = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StoreError(
                    f"cannot remove abandoned write residue {name}: {exc}"
                ) from exc
        if removed:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                raise StoreError(
                    f"cannot durably remove abandoned write residue: {exc}"
                ) from exc

    @staticmethod
    def _check_expected(
        current: dict[str, Any] | None,
        expected_digest: str | None,
    ) -> None:
        if current is None:
            if expected_digest is not None:
                raise StoreError("expected digest supplied for a new record")
            return
        if expected_digest is None:
            raise StoreError("expected digest is required to update an existing record")
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise StoreError("expected digest must be 64 lowercase hexadecimal characters")
        if not hmac.compare_digest(record_digest(current), expected_digest):
            raise StoreError("record digest mismatch; reload the current record")

    @staticmethod
    def _same_fields(
        current: Mapping[str, Any], requested: Mapping[str, Any], fields: tuple[str, ...]
    ) -> None:
        for field in fields:
            if current[field] != requested[field]:
                raise StoreError(f"immutable record field changed: {field}")

    @staticmethod
    def _time(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00")

    def save_initiative(
        self, record: Any, *, expected_digest: str | None = None
    ) -> Path:
        validated, raw = _canonical_bytes(validate_initiative, record)
        initiative_id = validated["initiative_id"]
        with self._locked_fds(initiative_id, create=True) as (_, initiative_fd):
            current = self._read_if_exists(
                initiative_fd, "initiative.json", "initiative record", validate_initiative
            )
            self._check_expected(current, expected_digest)
            if current is None:
                # Core v1 creation begins before any journal event or approved
                # plan exists.  The initial state revision is exactly zero.
                if (
                    validated["last_event_sequence"] != 0
                    or validated["state"] != "draft"
                    or validated["active_plan"] is not None
                    or validated["state_revision"] != 0
                ):
                    raise StoreError(
                        "new initiative requires draft state, null active_plan, "
                        "state_revision 0, and last_event_sequence 0"
                    )
            else:
                self._same_fields(
                    current, validated,
                    (
                        "contract", "initiative_id", "slug", "label", "created_at",
                        "scope", "coordinator", "forbidden_action_classes",
                    ),
                )
                if validated["state"] != current["state"]:
                    try:
                        require_transition("initiative", current["state"], validated["state"])
                    except ModelError as exc:
                        raise StoreError(str(exc)) from exc
                if validated["state_revision"] != current["state_revision"] + 1:
                    raise StoreError("initiative state_revision must advance by exactly one")
                if validated["last_event_sequence"] < current["last_event_sequence"]:
                    raise StoreError("initiative last_event_sequence must not move backward")
                if self._time(validated["updated_at"]) < self._time(current["updated_at"]):
                    raise StoreError("initiative updated_at must not move backward")
            self._write_mutable(initiative_fd, "initiative.json", raw)
        return self.config.initiatives_dir / initiative_id / "initiative.json"

    def _read_initiative_unlocked(self, initiative_fd: int, initiative_id: str) -> dict[str, Any]:
        record = self._validated_read(
            initiative_fd, "initiative.json", "initiative record", validate_initiative
        )
        if record["initiative_id"] != initiative_id:
            raise StoreError("initiative ID does not match its directory")
        return record

    def read_initiative(self, initiative_id: str) -> dict[str, Any]:
        try:
            initiative_id = canonical_uuid(initiative_id, "initiative_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            return self._read_initiative_unlocked(initiative_fd, initiative_id)

    def peek(self, initiative_id: str) -> dict[str, Any]:
        """Lock-free read of one atomic initiative snapshot."""
        try:
            initiative_id = canonical_uuid(initiative_id, "initiative_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with self._initiative_directory(
            initiative_id, create_root=False, create_initiative=False
        ) as (_, initiative_fd):
            return self._read_initiative_unlocked(initiative_fd, initiative_id)

    def list_initiatives(self) -> list[dict[str, Any]]:
        self.skipped = []
        with _directory_fd(
            self.config.initiatives_dir,
            create=False,
            managed_start=self._root_managed_start,
        ) as root_fd:
            if root_fd is None:
                return []
            try:
                with _registry_lock(root_fd):
                    names = sorted(os.listdir(root_fd))
            except OSError as exc:
                raise StoreError(f"cannot list initiative registry: {exc}") from exc
            records: list[dict[str, Any]] = []
            for name in names:
                if name.startswith("."):
                    continue
                try:
                    initiative_id = canonical_uuid(name, "initiative directory")
                    initiative_fd = _open_directory(root_fd, name, create=False)
                    if initiative_fd is None:
                        raise StoreError("initiative directory disappeared")
                    try:
                        records.append(
                            self._read_initiative_unlocked(initiative_fd, initiative_id)
                        )
                    finally:
                        _close(initiative_fd)
                except (ModelError, StoreError, OSError) as exc:
                    self.skipped.append({"name": name, "reason": str(exc)})
            return records

    list = list_initiatives
    save = save_initiative
    read = read_initiative

    def _save_subrecord(
        self,
        initiative_id: str,
        directory: str,
        name: str,
        record: Any,
        validator: Callable[[Any], dict[str, Any]],
        *,
        immutable: bool,
        expected_digest: str | None = None,
        transition_machine: Mapping[str, frozenset[str]] | None = None,
        immutable_fields: tuple[str, ...] = (),
        bind_once_fields: tuple[str, ...] = (),
        mutable_while_states: Mapping[str, frozenset[str]] | None = None,
        terminal_states: frozenset[str] = frozenset(),
    ) -> Path:
        validated, raw = _canonical_bytes(validator, record)
        if "initiative_id" in validated and validated["initiative_id"] != initiative_id:
            raise StoreError("record initiative_id does not match destination initiative")
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            directory_fd = self._subdirectory(initiative_fd, directory)
            try:
                if immutable:
                    if expected_digest is not None:
                        raise StoreError("write-once records do not accept expected_digest")
                    self._write_once(directory_fd, name, raw)
                else:
                    current = self._read_if_exists(
                        directory_fd, name, f"{directory} record", validator
                    )
                    self._check_expected(current, expected_digest)
                    if current is not None:
                        if current.get("state") in terminal_states:
                            raise StoreError(f"write-once terminal record already exists: {name}")
                        self._same_fields(current, validated, immutable_fields)
                        for field in bind_once_fields:
                            # An additive optional field is absent from records
                            # written before it existed; absent binds like null.
                            if (
                                current.get(field) is not None
                                and current.get(field) != validated.get(field)
                            ):
                                raise StoreError(f"immutable record field changed: {field}")
                        for field, states in (mutable_while_states or {}).items():
                            if (
                                current[field] != validated[field]
                                and (
                                    current.get("state") not in states
                                    or validated.get("state") not in states
                                )
                            ):
                                raise StoreError(f"immutable record field changed: {field}")
                        if transition_machine is not None and validated["state"] != current["state"]:
                            try:
                                require_transition(transition_machine, current["state"], validated["state"])
                            except ModelError as exc:
                                raise StoreError(str(exc)) from exc
                        if (
                            "updated_at" in current
                            and self._time(validated["updated_at"])
                            < self._time(current["updated_at"])
                        ):
                            raise StoreError("record updated_at must not move backward")
                    self._write_mutable(directory_fd, name, raw)
            finally:
                _close(directory_fd)
        return self.config.initiatives_dir / initiative_id / directory / name

    def save_plan(self, initiative_id: str, record: Any) -> Path:
        try:
            value = validate_plan_record(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        if value["status"] != "proposed":
            raise StoreError("new plan status must be proposed")
        digest = plan_digest(value)
        if value["digest"] is not None and value["digest"] != digest:
            raise StoreError("plan digest does not match canonical plan bytes")
        value["digest"] = digest
        validated, raw = _canonical_bytes(validate_plan_record, value)
        if validated["initiative_id"] != initiative_id:
            raise StoreError("record initiative_id does not match destination initiative")
        name = f"{validated['revision']:04d}.json"
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            plans_fd = self._subdirectory(initiative_fd, "plans")
            try:
                self._sweep_write_residue(plans_fd)
                try:
                    names = sorted(
                        candidate
                        for candidate in os.listdir(plans_fd)
                        if not candidate.startswith(".")
                    )
                except OSError as exc:
                    raise StoreError(f"cannot list plan revisions: {exc}") from exc
                revisions: list[int] = []
                for candidate in names:
                    match = re.fullmatch(r"([0-9]+)\.json", candidate)
                    if match is None:
                        raise StoreError(f"invalid plan revision filename: {candidate}")
                    revision = int(match.group(1))
                    if candidate != f"{revision:04d}.json" or revision <= 0:
                        raise StoreError(f"invalid plan revision filename: {candidate}")
                    revisions.append(revision)
                if revisions != list(range(1, len(revisions) + 1)):
                    raise StoreError("stored plan revisions contain a gap")
                expected_revision = len(revisions) + 1
                if validated["revision"] != expected_revision:
                    raise StoreError(
                        f"plan revision must be exactly {expected_revision}"
                    )
                self._write_once(plans_fd, name, raw)
            finally:
                _close(plans_fd)
        return self.config.initiatives_dir / initiative_id / "plans" / name

    def read_plan(self, initiative_id: str, revision: int) -> dict[str, Any]:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise StoreError("plan revision must be a positive integer")
        return self._read_subrecord(
            initiative_id, "plans", f"{revision:04d}.json", self._validate_stored_plan,
            identity_field="revision", identity_value=revision,
        )

    def read_plan_snapshot(self, initiative_id: str, revision: int) -> dict[str, Any]:
        """Read one retained plan without locks, writes, or residue cleanup."""
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise StoreError("plan revision must be a positive integer")
        with self._initiative_directory(
            initiative_id, create_root=False, create_initiative=False,
        ) as (_, initiative_fd):
            plans_fd = self._subdirectory(initiative_fd, "plans")
            try:
                record = self._validated_read(
                    plans_fd, f"{revision:04d}.json", "plans record",
                    self._validate_stored_plan_observation,
                )
            finally:
                _close(plans_fd)
        if record["initiative_id"] != initiative_id:
            raise StoreError("record initiative_id does not match destination initiative")
        if record["revision"] != revision:
            raise StoreError("record revision does not match its filename")
        return record

    @staticmethod
    def _validated_plan_content_digest(value: dict[str, Any]) -> str:
        content = dict(value)
        content.pop("digest")
        content.pop("status")
        raw = json.dumps(
            content, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _validate_stored_plan_observation(cls, record: Any) -> dict[str, Any]:
        value = _validate_retained_plan_observation(record)
        if (
            value["digest"] is None
            or value["digest"] != cls._validated_plan_content_digest(value)
        ):
            raise ModelError("stored plan digest does not match canonical plan bytes")
        return value

    @classmethod
    def _validate_stored_plan(cls, record: Any) -> dict[str, Any]:
        try:
            value = validate_plan_record(record)
        except ModelError as strict_error:
            try:
                retained = _validate_retained_plan_observation(record)
            except ModelError:
                raise strict_error
            if (
                retained["digest"] is None
                or retained["digest"] != cls._validated_plan_content_digest(retained)
            ):
                raise ModelError(
                    "stored plan digest does not match canonical plan bytes"
                ) from strict_error
            raise ObservationOnlyPlanError(
                f"retained asha.orchestration-plan.v1 revision "
                f"{retained['revision']} is observation-only: one or more historical "
                "verification gates lack immutable commands and environment_policy; "
                "execution authority cannot be inferred"
            ) from strict_error
        if (
            value["digest"] is None
            or value["digest"] != cls._validated_plan_content_digest(value)
        ):
            raise ModelError("stored plan digest does not match canonical plan bytes")
        return value

    def save_node(self, initiative_id: str, record: Any, *, expected_digest: str | None = None) -> Path:
        try:
            node_id = validate_node(record)["node_id"]
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        return self._save_subrecord(
            initiative_id, "nodes", f"{node_id}.json", record, validate_node,
            immutable=False, expected_digest=expected_digest,
            transition_machine=NODE_TRANSITIONS,
            immutable_fields=tuple(field for field in validate_node(record) if field != "state"),
            terminal_states=frozenset({"failed", "cancelled", "superseded", "stale"}),
        )

    def read_node(self, initiative_id: str, node_id: str) -> dict[str, Any]:
        try:
            node_id = validate_slug(node_id, "node_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        return self._read_subrecord(
            initiative_id, "nodes", f"{node_id}.json", validate_node,
            identity_field="node_id", identity_value=node_id,
        )

    def save_attempt(self, initiative_id: str, record: Any, *, expected_digest: str | None = None) -> Path:
        try:
            attempt_id = validate_attempt(record)["attempt_id"]
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        mutable = {
            "action_id", "state", "result_publication_id", "result_id", "seal_id",
            "updated_at",
        }
        return self._save_subrecord(
            initiative_id, "attempts", f"{attempt_id}.json", record, validate_attempt,
            immutable=False, expected_digest=expected_digest,
            transition_machine=ATTEMPT_TRANSITIONS,
            immutable_fields=tuple(field for field in validate_attempt(record) if field not in mutable),
            mutable_while_states={"action_id": frozenset({"allocated"})},
            terminal_states=frozenset({
                "sealed-success", "sealed-failure", "sealed-paused", "completed-readonly",
                "launch-failed", "failed-no-artifact", "cancelled", "stale",
            }),
        )

    def write_assignment(
        self, initiative_id: str, attempt_id: str, content: bytes
    ) -> Path:
        """Publish one immutable bounded worker assignment with mode 0600.

        Replaying the exact bytes is a read-only success.  A changed file or a
        changed requested body is a conflict rather than an overwrite.
        """
        try:
            attempt_id = canonical_uuid(attempt_id, "attempt_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        if not isinstance(content, bytes) or not content:
            raise StoreError("assignment must contain bounded UTF-8 bytes")
        if len(content) > 32 * 1024:
            raise StoreError("assignment exceeds 32768 bytes")
        try:
            content.decode("utf-8")
        except UnicodeError as exc:
            raise StoreError("assignment must be UTF-8") from exc
        name = f"{attempt_id}.md"
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            directory_fd = _open_directory(initiative_fd, "assignments", create=True)
            if directory_fd is None:
                raise StoreError("cannot create assignment storage directory")
            try:
                self._sweep_write_residue(directory_fd)
                try:
                    fd = _open_existing_file(directory_fd, name, "assignment")
                except FileNotFoundError:
                    self._write_once(directory_fd, name, content)
                else:
                    try:
                        metadata = os.fstat(fd)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_nlink != 1
                        ):
                            raise StoreError("retained assignment ownership or mode changed")
                        chunks: list[bytes] = []
                        remaining = 32 * 1024 + 1
                        while remaining:
                            chunk = os.read(fd, min(65536, remaining))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        current = b"".join(chunks)
                    finally:
                        _close(fd)
                    if current != content:
                        raise StoreError("retained assignment differs from the action reservation")
            finally:
                _close(directory_fd)
        return self.config.initiatives_dir / initiative_id / "assignments" / name

    def read_attempt(self, initiative_id: str, attempt_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "attempts", attempt_id, validate_attempt)

    def save_link(self, initiative_id: str, record: Any) -> Path:
        try:
            attempt_id = validate_link(record)["attempt_id"]
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        return self._save_subrecord(
            initiative_id, "links", f"{attempt_id}.json", record, validate_link,
            immutable=True,
        )

    def save_result_publication(
        self,
        initiative_id: str,
        record: Any,
        *,
        expected_digest: str | None = None,
    ) -> Path:
        try:
            publication_id = validate_result_publication(record)["publication_id"]
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        mutable = {"state", "refusal", "updated_at"}
        return self._save_subrecord(
            initiative_id, "result-publications", f"{publication_id}.json", record,
            validate_result_publication, immutable=False, expected_digest=expected_digest,
            transition_machine=RESULT_PUBLICATION_TRANSITIONS,
            immutable_fields=tuple(
                field
                for field in validate_result_publication(record)
                if field not in mutable
            ),
            terminal_states=frozenset({"completed", "refused"}),
        )

    def save_result_ingestion(
        self,
        initiative_id: str,
        record: Any,
        *,
        expected_digest: str | None = None,
    ) -> Path:
        try:
            value = validate_result_ingestion(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        mutable = {
            "state", "candidate_digest", "publication_id", "result_id",
            "claimed_commit_id", "claimed_tree_digest", "verification_evidence_ids",
            "commit_creator", "ingester", "refusal", "updated_at",
        }
        return self._save_subrecord(
            initiative_id, "result-ingestions", f"{value['ingestion_id']}.json",
            value, validate_result_ingestion, immutable=False,
            expected_digest=expected_digest,
            transition_machine=RESULT_INGESTION_TRANSITIONS,
            immutable_fields=(
                "staging_token_digest",
                *(field for field in value
                  if field not in mutable and field != "staging_token_digest"),
            ),
            bind_once_fields=(
                "candidate_digest", "publication_id", "result_id",
                "claimed_commit_id", "claimed_tree_digest", "commit_creator", "ingester",
            ),
            terminal_states=frozenset({"completed", "refused"}),
        )

    def save_result(self, initiative_id: str, record: Any) -> Path:
        return self._save_uuid_immutable(initiative_id, "results", record, validate_result, "result_id")

    def save_seal(self, initiative_id: str, record: Any) -> Path:
        return self._save_uuid_immutable(initiative_id, "seals", record, validate_seal, "seal_id")

    def save_seal_preparation(
        self,
        initiative_id: str,
        record: Any,
        *,
        expected_digest: str | None = None,
    ) -> Path:
        try:
            value = validate_seal_preparation(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        mutable = {"state", "refusal", "updated_at"}
        return self._save_subrecord(
            initiative_id, "seal-preparations", f"{value['seal_id']}.json", value,
            validate_seal_preparation, immutable=False,
            expected_digest=expected_digest,
            transition_machine={
                "preparing": frozenset({"indeterminate", "completed"}),
                "indeterminate": frozenset({"preparing", "completed"}),
                "completed": frozenset(),
            },
            immutable_fields=tuple(field for field in value if field not in mutable),
            terminal_states=frozenset({"completed"}),
        )

    def save_review(
        self,
        initiative_id: str,
        record: Any,
        *,
        expected_digest: str | None = None,
    ) -> Path:
        try:
            value = validate_review(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        try:
            current = self.read_review(initiative_id, value["review_id"])
        except StoreError as exc:
            if "not found" not in str(exc):
                raise
        else:
            if current["state"] in {
                "accepted-pass", "accepted-findings", "failed", "indeterminate",
            }:
                unchanged = copy.deepcopy(current)
                unchanged.update({
                    "state": "stale", "verdict": None, "findings": [],
                    "updated_at": value["updated_at"],
                })
                if value != unchanged:
                    raise StoreError(
                        "terminal review evidence is immutable; only state may become stale"
                    )
        mutable = {
            "attempt_id", "task_id", "run_id", "state", "verdict", "findings",
            "updated_at",
        }
        return self._save_subrecord(
            initiative_id, "reviews", f"{value['review_id']}.json", value,
            validate_review, immutable=False, expected_digest=expected_digest,
            transition_machine=REVIEW_TRANSITIONS,
            immutable_fields=tuple(field for field in value if field not in mutable),
            bind_once_fields=("attempt_id", "task_id", "run_id"),
            terminal_states=frozenset({"stale"}),
        )

    def save_verification(
        self,
        initiative_id: str,
        record: Any,
        *,
        expected_digest: str | None = None,
    ) -> Path:
        try:
            value = validate_verification(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        try:
            current = self.read_verification(initiative_id, value["verification_id"])
        except StoreError as exc:
            if "not found" not in str(exc):
                raise
        else:
            if current["state"] in {"passed", "failed", "indeterminate"}:
                unchanged = copy.deepcopy(current)
                unchanged.update({
                    "state": "stale", "outcome": None,
                    "updated_at": value["updated_at"],
                })
                if value != unchanged:
                    raise StoreError(
                        "terminal verification evidence is immutable; only state may become stale"
                    )
        mutable = {"state", "commands", "evidence_ids", "outcome", "updated_at"}
        return self._save_subrecord(
            initiative_id, "verifications", f"{value['verification_id']}.json", value,
            validate_verification, immutable=False, expected_digest=expected_digest,
            transition_machine=VERIFICATION_TRANSITIONS,
            immutable_fields=tuple(field for field in value if field not in mutable),
            terminal_states=frozenset({"stale"}),
        )

    def save_bundle(
        self,
        initiative_id: str,
        record: Any,
        *,
        expected_digest: str | None = None,
    ) -> Path:
        try:
            value = validate_bundle(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        mutable = {"state", "controller_evidence_ids", "outcome", "bound_at"}
        return self._save_subrecord(
            initiative_id, "bundles", f"{value['bundle_id']}.json", value,
            validate_bundle, immutable=False, expected_digest=expected_digest,
            transition_machine=BUNDLE_TRANSITIONS,
            immutable_fields=tuple(field for field in value if field not in mutable),
            terminal_states=frozenset({"compatible", "incompatible", "indeterminate"}),
        )

    def save_approval(self, initiative_id: str, record: Any, *, expected_digest: str | None = None) -> Path:
        try:
            request_id = validate_approval(record)["request_id"]
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        # `decided_by` is absent until a decision lands, so it cannot be
        # derived as immutable from the requested record; bind-once lets it go
        # absent -> signer exactly once and never be rewritten (#83).
        mutable = {"state", "rationale", "updated_at", "decided_by"}
        return self._save_subrecord(
            initiative_id, "approvals", f"{request_id}.json", record, validate_approval,
            immutable=False, expected_digest=expected_digest,
            transition_machine=APPROVAL_TRANSITIONS,
            bind_once_fields=("decided_by",),
            immutable_fields=tuple(field for field in validate_approval(record) if field not in mutable),
            terminal_states=frozenset({
                "rejected", "expired", "cancelled", "consumed",
                "revoked-before-use",
            }),
        )

    def save_action(self, initiative_id: str, record: Any, *, expected_digest: str | None = None) -> Path:
        try:
            action_id = validate_action(record)["action_id"]
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        mutable = {"state", "outcome", "updated_at"}
        return self._save_subrecord(
            initiative_id, "actions", f"{action_id}.json", record, validate_action,
            immutable=False, expected_digest=expected_digest,
            transition_machine=ACTION_TRANSITIONS,
            immutable_fields=tuple(field for field in validate_action(record) if field not in mutable),
            terminal_states=frozenset({"completed", "refused"}),
        )

    _COORDINATOR_MUTABLE_FIELDS = frozenset({
        "state", "event_cursor", "last_accepted_action_id", "updated_at",
    })

    def save_coordinator(
        self, initiative_id: str, record: Any, *, expected_digest: str | None = None
    ) -> Path:
        """Persist one coordinator generation; identity, generation, and anchor are fixed.

        A new record must carry exactly the next generation and name an existing
        predecessor (or none). Mutable fields: state, event_cursor,
        last_accepted_action_id, updated_at. Terminal generations are write-once.
        """
        try:
            value = validate_coordinator(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with self._locked_fds(initiative_id):
            existing = self.list_coordinators_snapshot(initiative_id)
            if value["coordinator_id"] not in {item["coordinator_id"] for item in existing}:
                self._check_new_coordinator(existing, value)
            return self._save_subrecord(
                initiative_id, "coordinators", f"{value['coordinator_id']}.json", record,
                validate_coordinator, immutable=False, expected_digest=expected_digest,
                transition_machine=COORDINATOR_TRANSITIONS,
                immutable_fields=tuple(
                    field for field in value if field not in self._COORDINATOR_MUTABLE_FIELDS
                ),
                terminal_states=COORDINATOR_TERMINAL_STATES,
            )

    @staticmethod
    def _check_new_coordinator(existing: list[dict[str, Any]], value: dict[str, Any]) -> None:
        highest = max((item["generation"] for item in existing), default=0)
        if value["generation"] != highest + 1:
            raise StoreError(f"coordinator generation must be {highest + 1}")
        known = {item["coordinator_id"] for item in existing}
        predecessor = value["predecessor_coordinator_id"]
        if predecessor is not None and predecessor not in known:
            raise StoreError("coordinator predecessor_coordinator_id is unknown")
        if predecessor is None and existing:
            raise StoreError("a replacement coordinator must name its predecessor")

    def read_coordinator(self, initiative_id: str, coordinator_id: str) -> dict[str, Any]:
        return self._read_uuid_record(
            initiative_id, "coordinators", coordinator_id, validate_coordinator
        )

    def list_coordinators_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        records = self._list_subrecords_snapshot(
            initiative_id, "coordinators", validate_coordinator,
            re.compile(r"([0-9a-f-]{36})\.json"), "coordinator_id",
        )
        return sorted(records, key=lambda item: item["generation"])

    def current_coordinator(self, initiative_id: str) -> dict[str, Any] | None:
        """The highest-generation coordinator record, or None when never claimed."""
        records = self.list_coordinators_snapshot(initiative_id)
        return records[-1] if records else None

    def save_checkpoint(
        self, initiative_id: str, record: Any, *, expected_digest: str | None = None
    ) -> Path:
        """Replace a generation's checkpoint; identity fields are fixed, content is CAS-guarded."""
        try:
            value = validate_coordinator_checkpoint(record)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        return self._save_subrecord(
            initiative_id, "checkpoints", f"{value['coordinator_id']}.json", record,
            validate_coordinator_checkpoint, immutable=False, expected_digest=expected_digest,
            immutable_fields=("contract", "initiative_id", "coordinator_id", "generation"),
        )

    def read_checkpoint(self, initiative_id: str, coordinator_id: str) -> dict[str, Any]:
        return self._read_uuid_record(
            initiative_id, "checkpoints", coordinator_id, validate_coordinator_checkpoint
        )

    def save_evidence(self, initiative_id: str, record: Any) -> Path:
        return self._save_uuid_immutable(
            initiative_id, "evidence", record, validate_evidence, "evidence_id"
        )

    def save_output(
        self, initiative_id: str, output_id: str, content: bytes,
    ) -> Path:
        """Retain one bounded command-output artifact as private write-once bytes."""
        try:
            output_id = canonical_uuid(output_id, "output_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        if not isinstance(content, bytes) or len(content) > 1024 * 1024:
            raise StoreError("command output must contain at most 1048576 bytes")
        name = f"{output_id}.bin"
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            directory_fd = self._subdirectory(initiative_fd, "outputs")
            try:
                self._write_once(directory_fd, name, content)
            finally:
                _close(directory_fd)
        return self.config.initiatives_dir / initiative_id / "outputs" / name

    def reserve_output(self, initiative_id: str, output_id: str) -> Path:
        """Create one private output destination before a command starts."""
        try:
            output_id = canonical_uuid(output_id, "output_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        name = f"{output_id}.bin"
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            evidence_fd = self._subdirectory(initiative_fd, "evidence")
            directory_fd = self._subdirectory(initiative_fd, "outputs")
            try:
                if self._read_if_exists(
                    evidence_fd, f"{output_id}.json", "evidence record",
                    validate_evidence,
                ) is not None:
                    raise StoreError("cannot reserve output after evidence publication")
                self._write_once(directory_fd, name, b"")
            finally:
                _close(directory_fd)
                _close(evidence_fd)
        return self.config.initiatives_dir / initiative_id / "outputs" / name

    def finalize_reserved_output(
        self, initiative_id: str, output_id: str, content: bytes,
    ) -> Path:
        """Durably finish a reserved output before immutable evidence binds it."""
        try:
            output_id = canonical_uuid(output_id, "output_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        if not isinstance(content, bytes) or len(content) > 1024 * 1024:
            raise StoreError("command output must contain at most 1048576 bytes")
        name = f"{output_id}.bin"
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            evidence_fd = self._subdirectory(initiative_fd, "evidence")
            directory_fd = self._subdirectory(initiative_fd, "outputs")
            descriptor = -1
            try:
                if self._read_if_exists(
                    evidence_fd, f"{output_id}.json", "evidence record",
                    validate_evidence,
                ) is not None:
                    raise StoreError("retained output is immutable after evidence publication")
                descriptor = os.open(
                    name, os.O_WRONLY | _NONBLOCK | _NOFOLLOW | _CLOEXEC,
                    dir_fd=directory_fd,
                )
                _validate_open_file(descriptor, "reserved command output")
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                self._write_all(descriptor, content)
                os.fsync(descriptor)
                os.fsync(directory_fd)
            except FileNotFoundError as exc:
                raise StoreError(f"reserved command output not found: {name}") from exc
            except StoreError:
                raise
            except OSError as exc:
                raise StoreError(f"reserved command output save failed: {exc}") from exc
            finally:
                _close(descriptor)
                _close(directory_fd)
                _close(evidence_fd)
        return self.config.initiatives_dir / initiative_id / "outputs" / name

    def read_output(self, initiative_id: str, output_id: str) -> bytes:
        """Read one private retained output with its ownership invariants checked."""
        try:
            output_id = canonical_uuid(output_id, "output_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        name = f"{output_id}.bin"
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            directory_fd = self._subdirectory(initiative_fd, "outputs")
            descriptor = -1
            try:
                descriptor = _open_existing_file(
                    directory_fd, name, "command output",
                )
                metadata = os.fstat(descriptor)
                if metadata.st_size > 1024 * 1024:
                    raise StoreError("command output exceeds 1048576 bytes")
                remaining = metadata.st_size
                chunks: list[bytes] = []
                while remaining:
                    chunk = os.read(descriptor, min(65536, remaining))
                    if not chunk:
                        raise StoreError("command output shortened during read")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise StoreError("command output grew during read")
                content = b"".join(chunks)
            except FileNotFoundError as exc:
                raise StoreError(f"command output not found: {name}") from exc
            finally:
                _close(descriptor)
                _close(directory_fd)
        return content

    def _save_uuid_immutable(
        self,
        initiative_id: str,
        directory: str,
        record: Any,
        validator: Callable[[Any], dict[str, Any]],
        id_field: str,
    ) -> Path:
        try:
            record_id = canonical_uuid(validator(record)[id_field], id_field)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        return self._save_subrecord(
            initiative_id, directory, f"{record_id}.json", record, validator,
            immutable=True,
        )

    def _read_subrecord(
        self,
        initiative_id: str,
        directory: str,
        name: str,
        validator: Callable[[Any], dict[str, Any]],
        *,
        identity_field: str | None = None,
        identity_value: Any = None,
    ) -> dict[str, Any]:
        path = self.config.initiatives_dir / initiative_id / directory / name
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            directory_fd = self._subdirectory(initiative_fd, directory)
            try:
                self._sweep_write_residue(directory_fd)
                try:
                    record = self._validated_read(
                        directory_fd, name, f"{directory} record", validator
                    )
                except StoreError as exc:
                    raise type(exc)(
                        f"initiative {initiative_id} record {path}: {exc}"
                    ) from exc
            finally:
                _close(directory_fd)
        if "initiative_id" in record and record["initiative_id"] != initiative_id:
            raise StoreError(
                f"initiative {initiative_id} record {path}: field initiative_id "
                "does not match its destination initiative: "
                f"destination={initiative_id!r}, record value={record['initiative_id']!r}"
            )
        if identity_field is not None and record[identity_field] != identity_value:
            raise StoreError(
                f"initiative {initiative_id} record {path}: field {identity_field} "
                "does not match its filename: "
                f"filename stem={identity_value!r}, record value={record[identity_field]!r}"
            )
        return record

    def _list_subrecords_snapshot(
        self,
        initiative_id: str,
        directory: str,
        validator: Callable[[Any], dict[str, Any]],
        filename_pattern: re.Pattern[str],
        identity_field: str,
        identity_parser: Callable[[str], Any] = lambda value: value,
        *,
        problems: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """List validated regular records without locks, writes, or cleanup.

        Strict by default: the first record that does not read back as itself
        fails the whole listing, because a reader that cannot trust one record
        cannot trust the set.  A caller that passes `problems` instead collects
        those records by name and continues.  That tolerance never widens what
        counts as a valid record -- an unreadable record is excluded from the
        result and reported, never repaired, adopted, renamed or removed --
        and it is opt-in per call site, so every other reader keeps failing
        closed.
        """
        def _reject(name: str, reason: str, **detail: str) -> None:
            path = self.config.initiatives_dir / initiative_id / directory / name
            contextual = f"initiative {initiative_id} record {path}: {reason}"
            if problems is None:
                raise StoreError(contextual)
            problems.append({
                "directory": directory, "name": name, "path": str(path),
                "reason": contextual, **detail,
            })

        with self._initiative_directory(
            initiative_id, create_root=False, create_initiative=False
        ) as (_, initiative_fd):
            directory_fd = self._subdirectory(initiative_fd, directory)
            try:
                try:
                    names = sorted(name for name in os.listdir(directory_fd) if not name.startswith("."))
                except OSError as exc:
                    raise StoreError(f"cannot list {directory} records: {exc}") from exc
                records: list[dict[str, Any]] = []
                for name in names:
                    match = filename_pattern.fullmatch(name)
                    if match is None:
                        _reject(name, f"invalid {directory} record filename: {name}")
                        continue
                    identity = identity_parser(match.group(1))
                    try:
                        record = self._validated_read(
                            directory_fd, name, f"{directory} record", validator
                        )
                    except StoreError as exc:
                        _reject(
                            name, str(exc), filename_identity=str(identity),
                        )
                        continue
                    if record.get("initiative_id", initiative_id) != initiative_id:
                        detail = {
                            "filename_identity": str(identity),
                            "record_initiative_id": str(record["initiative_id"]),
                        }
                        if "task_id" in record:
                            detail["task_id"] = str(record["task_id"])
                        if identity_field in record:
                            detail["record_identity"] = str(record[identity_field])
                        _reject(
                            name,
                            "field initiative_id does not match its destination "
                            f"initiative: destination={initiative_id!r}, "
                            f"record value={record['initiative_id']!r}",
                            **detail,
                        )
                        continue
                    if record[identity_field] != identity:
                        detail = {
                            "filename_identity": str(identity),
                            "record_identity": str(record[identity_field]),
                        }
                        if "task_id" in record:
                            detail["task_id"] = str(record["task_id"])
                        _reject(
                            name,
                            f"field {identity_field} does not match its filename: "
                            f"filename stem={identity!r}, "
                            f"record value={record[identity_field]!r}",
                            **detail,
                        )
                        continue
                    records.append(record)
                return records
            finally:
                _close(directory_fd)

    def list_plans_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        records = self._list_subrecords_snapshot(
            initiative_id, "plans", self._validate_stored_plan_observation,
            re.compile(r"([0-9]{4})\.json"), "revision", int,
        )
        if [record["revision"] for record in records] != list(range(1, len(records) + 1)):
            raise StoreError("stored plan revisions contain a gap")
        return records

    def list_nodes_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "nodes", validate_node,
            re.compile(r"([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)\.json"), "node_id",
        )

    def list_attempts_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "attempts", validate_attempt,
            re.compile(r"([0-9a-f-]{36})\.json"), "attempt_id",
        )

    def list_links_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "links", validate_link,
            re.compile(r"([0-9a-f-]{36})\.json"), "attempt_id",
        )

    def list_approvals_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "approvals", validate_approval,
            re.compile(r"([0-9a-f-]{36})\.json"), "request_id",
        )

    def list_actions_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "actions", validate_action,
            re.compile(r"([0-9a-f-]{36})\.json"), "action_id",
        )

    def list_seals_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "seals", validate_seal,
            re.compile(r"([0-9a-f-]{36})\.json"), "seal_id",
        )

    def list_result_publications_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "result-publications", validate_result_publication,
            re.compile(r"([0-9a-f-]{36})\.json"), "publication_id",
        )

    def list_result_ingestions_snapshot(
        self,
        initiative_id: str,
        *,
        problems: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """List ingestion reservations; `problems` surveys instead of raising.

        Only reconciliation passes `problems`, so that a single unreadable
        record cannot wedge every pass of an initiative.  Identity resolution
        and the direct-publication refusal gate stay strict.
        """
        return self._list_subrecords_snapshot(
            initiative_id, "result-ingestions", validate_result_ingestion,
            re.compile(r"([0-9a-f-]{36})\.json"), "ingestion_id",
            problems=problems,
        )

    def list_results_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "results", validate_result,
            re.compile(r"([0-9a-f-]{36})\.json"), "result_id",
        )

    def list_seal_preparations_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "seal-preparations", validate_seal_preparation,
            re.compile(r"([0-9a-f-]{36})\.json"), "seal_id",
        )

    def list_reviews_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "reviews", validate_review,
            re.compile(r"([0-9a-f-]{36})\.json"), "review_id",
        )

    def list_verifications_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "verifications", validate_verification,
            re.compile(r"([0-9a-f-]{36})\.json"), "verification_id",
        )

    def list_evidence_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "evidence", validate_evidence,
            re.compile(r"([0-9a-f-]{36})\.json"), "evidence_id",
        )

    def list_bundles_snapshot(self, initiative_id: str) -> list[dict[str, Any]]:
        return self._list_subrecords_snapshot(
            initiative_id, "bundles", validate_bundle,
            re.compile(r"([0-9a-f-]{36})\.json"), "bundle_id",
        )

    def _read_uuid_record(
        self,
        initiative_id: str,
        directory: str,
        record_id: str,
        validator: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            record_id = canonical_uuid(record_id)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        identity_fields = {
            "attempts": "attempt_id",
            "links": "attempt_id",
            "result-ingestions": "ingestion_id",
            "result-publications": "publication_id",
            "results": "result_id",
            "seal-preparations": "seal_id",
            "seals": "seal_id",
            "reviews": "review_id",
            "verifications": "verification_id",
            "bundles": "bundle_id",
            "approvals": "request_id",
            "actions": "action_id",
            "evidence": "evidence_id",
            "coordinators": "coordinator_id",
            "checkpoints": "coordinator_id",
        }
        return self._read_subrecord(
            initiative_id, directory, f"{record_id}.json", validator,
            identity_field=identity_fields[directory], identity_value=record_id,
        )

    # Symmetric readers for immutable and journal records.
    def read_link(self, initiative_id: str, attempt_id: str) -> dict[str, Any]:
        return self._read_uuid_record(
            initiative_id, "links", attempt_id, validate_link
        )

    def read_result_publication(
        self, initiative_id: str, publication_id: str
    ) -> dict[str, Any]:
        return self._read_uuid_record(
            initiative_id, "result-publications", publication_id,
            validate_result_publication,
        )

    def read_result_ingestion(
        self, initiative_id: str, ingestion_id: str
    ) -> dict[str, Any]:
        return self._read_uuid_record(
            initiative_id, "result-ingestions", ingestion_id,
            validate_result_ingestion,
        )

    def read_result(self, initiative_id: str, result_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "results", result_id, validate_result)

    def read_seal(self, initiative_id: str, seal_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "seals", seal_id, validate_seal)

    def read_seal_preparation(self, initiative_id: str, seal_id: str) -> dict[str, Any]:
        return self._read_uuid_record(
            initiative_id, "seal-preparations", seal_id, validate_seal_preparation,
        )

    def read_review(self, initiative_id: str, review_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "reviews", review_id, validate_review)

    def read_verification(self, initiative_id: str, verification_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "verifications", verification_id, validate_verification)

    def read_bundle(self, initiative_id: str, bundle_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "bundles", bundle_id, validate_bundle)

    def read_approval(self, initiative_id: str, request_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "approvals", request_id, validate_approval)

    def read_action(self, initiative_id: str, action_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "actions", action_id, validate_action)

    def read_evidence(self, initiative_id: str, evidence_id: str) -> dict[str, Any]:
        return self._read_uuid_record(initiative_id, "evidence", evidence_id, validate_evidence)

    def _event_records(
        self, events_fd: int, initiative_id: str
    ) -> list[dict[str, Any]]:
        try:
            names = sorted(name for name in os.listdir(events_fd) if not name.startswith("."))
        except OSError as exc:
            raise StoreError(f"cannot list initiative events: {exc}") from exc
        records: list[dict[str, Any]] = []
        seen_sequences: set[int] = set()
        for name in names:
            match = re.fullmatch(r"([0-9]{6})-([0-9a-f-]{36})\.json", name)
            if match is None:
                raise StoreError(f"invalid event filename: {name}")
            sequence = int(match.group(1))
            if sequence in seen_sequences:
                raise StoreError(f"duplicate event sequence: {sequence}")
            seen_sequences.add(sequence)
            record = self._validated_read(events_fd, name, "event record", validate_event)
            if (
                record["sequence"] != sequence
                or record["event_id"] != match.group(2)
                or record["initiative_id"] != initiative_id
            ):
                raise StoreError(f"event identity does not match filename: {name}")
            records.append(record)
        sequences = [record["sequence"] for record in records]
        if sequences != list(range(1, len(records) + 1)):
            raise StoreError("event sequence contains a gap")
        return records

    def _event_tail(
        self,
        events_fd: int,
        initiative_id: str,
        expected_count: int,
    ) -> dict[str, Any] | None:
        """Check journal count and its single tail record without replaying history."""
        try:
            names = [name for name in os.listdir(events_fd) if not name.startswith(".")]
        except OSError as exc:
            raise StoreError(f"cannot list initiative events: {exc}") from exc
        if len(names) != expected_count:
            raise StoreError("event sequence disagrees with initiative snapshot")
        if expected_count == 0:
            return None
        name = max(names)
        match = re.fullmatch(r"([0-9]{6})-([0-9a-f-]{36})\.json", name)
        if match is None or int(match.group(1)) != expected_count:
            raise StoreError("event tail disagrees with initiative snapshot")
        record = self._validated_read(events_fd, name, "event record", validate_event)
        if (
            record["sequence"] != expected_count
            or record["event_id"] != match.group(2)
            or record["initiative_id"] != initiative_id
        ):
            raise StoreError(f"event identity does not match filename: {name}")
        return record

    def append_event(self, initiative_id: str, event: Any) -> Path:
        try:
            validated, raw = _canonical_bytes(validate_event, event)
            initiative_id = canonical_uuid(initiative_id, "initiative_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        if validated["initiative_id"] != initiative_id:
            raise StoreError("event initiative_id does not match destination initiative")
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            initiative_record = self._read_initiative_unlocked(initiative_fd, initiative_id)
            events_fd = self._subdirectory(initiative_fd, "events")
            try:
                self._sweep_write_residue(events_fd)
                self._event_tail(
                    events_fd,
                    initiative_id,
                    initiative_record["last_event_sequence"],
                )
                expected = initiative_record["last_event_sequence"] + 1
                if validated["sequence"] != expected:
                    raise StoreError(
                        f"event sequence must be exactly {expected}, got {validated['sequence']}"
                    )
                if self._time(validated["recorded_at"]) < self._time(
                    initiative_record["updated_at"]
                ):
                    raise StoreError("event recorded_at must not precede initiative updated_at")
                updated = copy.deepcopy(initiative_record)
                updated["last_event_sequence"] = expected
                updated["state_revision"] += 1
                updated["updated_at"] = validated["recorded_at"]
                _, initiative_raw = _canonical_bytes(validate_initiative, updated)
                name = f"{expected:06d}-{validated['event_id']}.json"
                self._write_once(events_fd, name, raw)
            finally:
                _close(events_fd)
            self._write_mutable(initiative_fd, "initiative.json", initiative_raw)
        return self.config.initiatives_dir / initiative_id / "events" / name

    def list_events(self, initiative_id: str) -> list[dict[str, Any]]:
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            events_fd = self._subdirectory(initiative_fd, "events")
            try:
                self._sweep_write_residue(events_fd)
                return self._event_records(events_fd, initiative_id)
            finally:
                _close(events_fd)

    def list_events_snapshot(
        self, initiative_id: str, *, after: int = 0
    ) -> list[dict[str, Any]]:
        """Read the immutable journal without acquiring locks or sweeping residue."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise StoreError("event sequence cursor must be a nonnegative integer")
        with self._initiative_directory(
            initiative_id, create_root=False, create_initiative=False
        ) as (_, initiative_fd):
            events_fd = self._subdirectory(initiative_fd, "events")
            try:
                return [
                    event for event in self._event_records(events_fd, initiative_id)
                    if event["sequence"] > after
                ]
            finally:
                _close(events_fd)

    def record_counts_snapshot(self, initiative_id: str) -> dict[str, int]:
        """Count canonical retained fact records without locks or residue cleanup."""
        uuid_filename = re.compile(r"([0-9a-f-]{36})\.json")
        classes = {
            "result-publications": (validate_result_publication, "publication_id"),
            "results": (validate_result, "result_id"),
            "seals": (validate_seal, "seal_id"),
            "seal-preparations": (validate_seal_preparation, "seal_id"),
            "reviews": (validate_review, "review_id"),
            "verifications": (validate_verification, "verification_id"),
            "bundles": (validate_bundle, "bundle_id"),
            "approvals": (validate_approval, "request_id"),
            "actions": (validate_action, "action_id"),
            "evidence": (validate_evidence, "evidence_id"),
            "coordinators": (validate_coordinator, "coordinator_id"),
            "checkpoints": (validate_coordinator_checkpoint, "coordinator_id"),
        }
        counts = {
            "links": len(self.list_links_snapshot(initiative_id)),
            "events": len(self.list_events_snapshot(initiative_id)),
        }
        for directory, (validator, identity_field) in classes.items():
            counts[directory] = len(self._list_subrecords_snapshot(
                initiative_id, directory, validator, uuid_filename, identity_field,
            ))
        return counts

    def verify_events(self, initiative_id: str) -> list[dict[str, Any]]:
        """Replay and validate the complete journal against its initiative snapshot."""
        with self._locked_fds(initiative_id) as (_, initiative_fd):
            events_fd = self._subdirectory(initiative_fd, "events")
            try:
                self._sweep_write_residue(events_fd)
                records = self._event_records(events_fd, initiative_id)
            finally:
                _close(events_fd)
            snapshot = self._read_initiative_unlocked(initiative_fd, initiative_id)
            if len(records) != snapshot["last_event_sequence"]:
                raise StoreError("event sequence disagrees with initiative snapshot")
            return records

    def inventory(
        self, initiative_id: str, *, locked: bool = True
    ) -> dict[str, Any]:
        """Inventory retained files; snapshot mode never locks or sweeps residue."""
        result: dict[str, Any] = {
            name: {"bytes": 0, "inodes": 0} for name in _INVENTORY_CLASSES
        }

        def scan(directory_fd: int, class_name: str) -> None:
            if locked:
                self._sweep_write_residue(directory_fd)
            try:
                names = sorted(os.listdir(directory_fd))
            except OSError as exc:
                raise StoreError(f"cannot inventory {class_name}: {exc}") from exc
            for name in names:
                try:
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise StoreError(f"cannot inspect retained path {name}: {exc}") from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise StoreError(f"symlink rejected during inventory: {name}")
                if stat.S_ISREG(metadata.st_mode):
                    fd = _open_existing_file(directory_fd, name, "retained file")
                    try:
                        pinned = os.fstat(fd)
                    finally:
                        _close(fd)
                    result[class_name]["bytes"] += pinned.st_size
                    result[class_name]["inodes"] += 1
                elif stat.S_ISDIR(metadata.st_mode):
                    child = _open_directory(directory_fd, name, create=False)
                    if child is None:
                        raise StoreError(f"retained directory disappeared: {name}")
                    try:
                        scan(child, class_name)
                    finally:
                        _close(child)
                else:
                    raise StoreError(f"non-file retained path rejected during inventory: {name}")

        manager = (
            self._locked_fds(initiative_id)
            if locked
            else self._initiative_directory(
                initiative_id, create_root=False, create_initiative=False
            )
        )
        with manager as (_, initiative_fd):
            try:
                names = sorted(os.listdir(initiative_fd))
            except OSError as exc:
                raise StoreError(f"cannot inventory initiative: {exc}") from exc
            for name in names:
                try:
                    metadata = os.stat(name, dir_fd=initiative_fd, follow_symlinks=False)
                except OSError as exc:
                    raise StoreError(f"cannot inspect retained path {name}: {exc}") from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise StoreError(f"symlink rejected during inventory: {name}")
                if name == "initiative.json" and stat.S_ISREG(metadata.st_mode):
                    fd = _open_existing_file(initiative_fd, name, "initiative record")
                    try:
                        pinned = os.fstat(fd)
                    finally:
                        _close(fd)
                    result["initiative"]["bytes"] += pinned.st_size
                    result["initiative"]["inodes"] += 1
                elif name in _LAYOUT_DIRECTORIES and stat.S_ISDIR(metadata.st_mode):
                    child = _open_directory(initiative_fd, name, create=False)
                    if child is None:
                        raise StoreError(f"retained directory disappeared: {name}")
                    try:
                        scan(child, name)
                    finally:
                        _close(child)
                else:
                    raise StoreError(f"unexpected retained path rejected during inventory: {name}")

        total_bytes = sum(value["bytes"] for value in result.values())
        total_inodes = sum(value["inodes"] for value in result.values())
        result["totals"] = {"bytes": total_bytes, "inodes": total_inodes}
        result["pause_recommended"] = (
            total_bytes >= self.config.max_retained_bytes_before_pause
            or total_inodes >= self.config.max_retained_inodes_before_pause
        )
        return result

__all__ = [
    "MAX_RECORD_BYTES", "InitiativeStore", "ObservationOnlyPlanError",
    "StoreError", "StoreCommittedError",
]
