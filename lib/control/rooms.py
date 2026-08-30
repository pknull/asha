"""Project-bound, persistent tmux conversations carrying the Asha persona.

Rooms are deliberately smaller than Control tasks: they run in the project's
canonical checkout, create no workspace, and have only open/attach/list/close.
The durable record is the authority; readable tmux names are never ownership
evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .config import is_canonical_absolute_path
from .harness import HarnessError, validate_harness
from .model import ModelError, canonical_uuid
from .store import (
    StoreError, _CLOEXEC, _NOFOLLOW, _directory_fd, _managed_start,
    _open_existing_file, _registry_lock,
)
from .tmux import TmuxAdapter, TmuxError
from .orchestration.projects import display_name, list_projects_across, resolve_roots


ROOM_CONTRACT = "asha.room.v1"
ROOM_LIST_CONTRACT = "asha.room-list.v1"
ROOM_OPEN_CONTRACT = "asha.room-open.v1"
ROOM_ATTACH_CONTRACT = "asha.room-attach.v1"
ROOM_CLOSE_CONTRACT = "asha.room-close.v1"
SESSION_ROOM_OPTION = "@asha_room_session_id"
PANE_ROOM_OPTION = "@asha_room_id"
PANE_PROJECT_OPTION = "@asha_room_project_id"
ROOM_ENV = "ASHA_ROOM_ID"
ROOM_GUIDANCE_ATTR = "asha_room_guidance"
SCRUBBED_ROLE_ENV = (
    "ASHA_SEAT", "ASHA_COORDINATOR_LAUNCH", "ASHA_CONTROL_MANAGED",
    "ASHA_CONTROL_TASK_ID", "ASHA_CONTROL_RUN_ID", "ASHA_CONTROL_STATE_DIR",
    "ASHA_CONTROL_RESULT_TOKEN", "ASHA_CONTROL_RESULT_OUTBOX",
    "ASHA_CONTROL_RESULT_INGESTION_ID",
    "ASHA_ORCHESTRATION_INITIATIVE_ID",
    "ASHA_ORCHESTRATION_COORDINATOR_ID",
    "ASHA_ORCHESTRATION_COORDINATOR_GENERATION",
    "ASHA_VERIFICATION_PROCESS_V1",
)
HARNESS_COMMAND_ENV = {
    "claude": "ASHA_CLAUDE_CMD", "codex": "ASHA_CODEX_CMD",
    "copilot": "ASHA_COPILOT_CMD", "opencode": "ASHA_OPENCODE_CMD",
}
MAX_NAME_BYTES = 120
MAX_PROMPT_BYTES = 32 * 1024
_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_TMUX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_PANE_ID = re.compile(r"%[0-9]+", re.ASCII)
_SESSION_ID = re.compile(r"\$[0-9]+", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ROOM_KEYS = {
    "contract", "room_id", "name", "slug", "project_id", "project_root",
    "project_name", "harness", "tmux", "created_at", "updated_at",
    "lifecycle", "prompt_digest",
}
_ROOM_CHILD_EXEC = (
    "(__import__('os').execv(__import__('sys').argv[2],"
    "__import__('sys').argv[2:]+[__import__('base64').b64decode("
    "__import__('sys').argv[1]).decode('utf-8')]))"
)


class RoomError(ValueError):
    """A room request was invalid, ambiguous, or unsafe to perform."""


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum
            or not value.isprintable() or any(
                unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value
            )):
        raise RoomError(f"room {label} is invalid")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RoomError(f"room {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RoomError(f"room {label} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise RoomError(f"room {label} is invalid")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z",
    )


def _room_name(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise RoomError("room name must be text")
    name = " ".join(value.split())
    if (not name or len(name.encode("utf-8")) > MAX_NAME_BYTES or
            not name.isprintable() or any(
                unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in name
            )):
        raise RoomError(f"room name must be printable and at most {MAX_NAME_BYTES} UTF-8 bytes")
    slug = _SLUG_CHARS.sub("-", name.casefold()).strip("-")[:48].rstrip("-")
    if not slug:
        raise RoomError("room name does not contain a usable ASCII slug")
    return name, slug


def _prompt(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoomError("room prompt must not be empty")
    if len(value.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise RoomError(f"room prompt exceeds {MAX_PROMPT_BYTES} UTF-8 bytes")
    if any(
        unicodedata.category(char) in {"Cf", "Cs"}
        or (unicodedata.category(char) == "Cc" and char not in {"\n", "\t"})
        for char in value
    ):
        raise RoomError("room prompt contains unsupported control text")
    return value


def room_launch_argv(asha_root: Path, harness: str, prompt: str) -> list[str]:
    """Exact full-persona interactive argv for each supported harness."""
    try:
        selected = validate_harness(harness)
    except HarnessError as exc:
        raise RoomError(str(exc)) from exc
    text = _prompt(prompt)
    root = Path(asha_root)
    if not root.is_absolute() or root.resolve() != root:
        raise RoomError("Asha root must be an exact canonical absolute path")
    launcher = root / "bin" / "asha"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RoomError("Asha launcher is missing or not executable")
    tail = {
        "claude": [text],
        "codex": [text],
        "copilot": ["--interactive", text],
        "opencode": ["--prompt", text],
    }[selected]
    return [str(launcher), selected, *tail]


def room_tmux_argv(asha_root: Path, harness: str, prompt: str) -> list[str]:
    """Transport a prompt through tmux without exposing its grammar to tmux."""
    logical = room_launch_argv(asha_root, harness, prompt)
    encoded = base64.b64encode(logical[-1].encode("utf-8")).decode("ascii")
    return [
        sys.executable, "-I", "-S", "-c", _ROOM_CHILD_EXEC,
        encoded, *logical[:-1],
    ]


def room_harness_command(harness: str, env: Mapping[str, str]) -> tuple[str, str]:
    """Return the registry-compatible environment key and exact executable."""
    try:
        selected = validate_harness(harness)
    except HarnessError as exc:
        raise RoomError(str(exc)) from exc
    key = HARNESS_COMMAND_ENV[selected]
    value = env.get(key) or selected
    if (not isinstance(value, str) or not value or len(value) > 4096
            or not value.isprintable()):
        raise RoomError(f"{key} is not a valid executable name or path")
    return key, value


def room_harness_available(
    harness: str, env: Mapping[str, str],
    *, executable_finder: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Use the same command override for TUI/CLI preflight as the launcher."""
    try:
        _key, command = room_harness_command(harness, env)
    except RoomError:
        return False
    return executable_finder(command) is not None


def _project_config(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / ".asha/config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid_project(entry: Mapping[str, Any]) -> bool:
    root = Path(str(entry.get("root") or ""))
    config = _project_config(root)
    return bool(
        root.is_dir() and (root / "Memory").is_dir()
        and config.get("initialized") is True
        and config.get("memory_version") == 2
        and isinstance(config.get("project_id"), str)
        and config["project_id"].strip()
        and entry.get("project_id") == config["project_id"]
    )


def resolve_project(
    selector: str, *, env: Mapping[str, str], cwd: Path | None = None,
) -> dict[str, Any]:
    """Resolve one exact path/name/directory/project-id from the shared index."""
    if not isinstance(selector, str) or not selector.strip():
        raise RoomError("--project requires an exact project path or name")
    raw = Path(selector).expanduser()
    looks_pathlike = raw.is_absolute() or "/" in selector or selector.startswith(".")
    if looks_pathlike:
        requested = raw.resolve()
        config = _project_config(requested)
        direct = {
            "name": display_name(requested, requested.name),
            "directory": requested.name, "root": str(requested),
            "project_id": config.get("project_id"), "role": None,
            "declared": False, "asha_project": True, "jj_colocated": False,
        }
        if not _valid_project(direct):
            raise RoomError(
                f"project {requested} is not exactly one initialized Memory v2 Asha "
                "project; run session-init there"
            )
        matches = [direct]
    else:
        roots, source = resolve_roots(env=env, cwd=cwd)
        payload = list_projects_across(roots, depth=3, source_of_roots=source)
        candidates = [entry for entry in payload["projects"] if _valid_project(entry)]
        needle = selector.strip().casefold()
        matches = [entry for entry in candidates if needle in {
            str(entry.get("name") or "").casefold(),
            str(entry.get("directory") or "").casefold(),
            Path(entry["root"]).name.casefold(),
            str(entry.get("project_id") or "").casefold(),
        }]
    if not matches:
        raise RoomError(
            f"project {selector!r} is not an initialized Memory v2 Asha project "
            "in the configured project index"
        )
    if len(matches) != 1:
        roots = ", ".join(sorted(entry["root"] for entry in matches))
        raise RoomError(f"project {selector!r} is ambiguous: {roots}")
    selected = dict(matches[0])
    selected["root"] = str(Path(selected["root"]).resolve())
    return selected


class RoomStore:
    """Small atomic JSON registry beneath the single Asha state root."""

    def __init__(self, config: Any):
        self.root = Path(config.asha_home) / "state/control/rooms"
        self._managed_start = _managed_start(self.root, ("control", "rooms"))

    def _path(self, room_id: str) -> Path:
        try:
            canonical = canonical_uuid(room_id)
        except ModelError as exc:
            raise RoomError("room id must be a canonical UUID") from exc
        return self.root / f"{canonical}.json"

    @staticmethod
    def _raw(value: Mapping[str, Any]) -> bytes:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n").encode("utf-8")

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        written = 0
        while written < len(raw):
            count = os.write(fd, raw[written:])
            if count <= 0:
                raise RoomError("short write while saving room record")
            written += count

    @staticmethod
    def _write_record(
        directory_fd: int, name: str, raw: bytes, *, create_only: bool,
    ) -> None:
        temporary = f".{name}.tmp.{secrets.token_hex(8)}"
        fd = -1
        committed = False
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600, dir_fd=directory_fd,
            )
            os.fchmod(fd, 0o600)
            RoomStore._write_all(fd, raw)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            if create_only:
                try:
                    os.link(
                        temporary, name, src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise RoomError("room id already exists") from exc
                os.unlink(temporary, dir_fd=directory_fd)
            else:
                os.replace(
                    temporary, name, src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            committed = True
            os.fsync(directory_fd)
        except RoomError:
            raise
        except OSError as exc:
            phase = "after commit" if committed else "before commit"
            raise RoomError(f"room record save failed {phase}: {exc}") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass

    def _open_directory(self, *, create: bool):
        return _directory_fd(
            self.root, create=create, managed_start=self._managed_start,
        )

    @staticmethod
    def _validate(record: Any) -> dict[str, Any]:
        if not isinstance(record, dict) or record.get("contract") != ROOM_CONTRACT:
            raise RoomError("room record contract is invalid")
        if set(record) != _ROOM_KEYS:
            raise RoomError("room record fields do not match asha.room.v1")
        try:
            canonical_uuid(record["room_id"])
            validate_harness(record["harness"])
        except (ModelError, HarnessError) as exc:
            raise RoomError("room record identity is invalid") from exc
        name, slug = _room_name(record["name"])
        if record["name"] != name or record["slug"] != slug:
            raise RoomError("room name and slug identity differ")
        project_id = _bounded_text(record["project_id"], "project id", 200)
        if ";" in project_id or "#{" in project_id:
            raise RoomError("room project id is not safe for a tmux marker")
        _bounded_text(record["project_name"], "project name", 200)
        root = record["project_root"]
        if not isinstance(root, str) or not is_canonical_absolute_path(root, resolved=True):
            raise RoomError("room project root is not an exact canonical path")
        tmux = record["tmux"]
        if not isinstance(tmux, dict) or set(tmux) != {
            "session", "session_id", "window", "pane_id",
        }:
            raise RoomError("room tmux identity is invalid")
        if _TMUX_NAME.fullmatch(str(tmux["session"])) is None or tmux["window"] != "room":
            raise RoomError("room tmux identity is invalid")
        if tmux["pane_id"] is not None and _PANE_ID.fullmatch(str(tmux["pane_id"])) is None:
            raise RoomError("room pane identity is invalid")
        if tmux["session_id"] is not None and _SESSION_ID.fullmatch(
            str(tmux["session_id"])
        ) is None:
            raise RoomError("room session identity is invalid")
        if (tmux["pane_id"] is None) != (tmux["session_id"] is None):
            raise RoomError("room tmux immutable identities are incomplete")
        if record["lifecycle"] not in {"creating", "open", "ended"}:
            raise RoomError("room lifecycle is invalid")
        created = _timestamp(record["created_at"], "created_at")
        updated = _timestamp(record["updated_at"], "updated_at")
        if updated < created:
            raise RoomError("room updated_at precedes created_at")
        if _DIGEST.fullmatch(str(record["prompt_digest"])) is None:
            raise RoomError("room prompt digest is invalid")
        return dict(record)

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        value = self._validate(dict(record))
        try:
            with self._open_directory(create=True) as directory_fd:
                if directory_fd is None:
                    raise RoomError("room registry could not be created")
                with _registry_lock(directory_fd):
                    if any(
                        current["name"].casefold() == value["name"].casefold()
                        for current in self.list()
                    ):
                        raise RoomError(f"room name {value['name']!r} already exists")
                    self._write_record(
                        directory_fd, f"{value['room_id']}.json", self._raw(value),
                        create_only=True,
                    )
        except StoreError as exc:
            raise RoomError(str(exc)) from exc
        return value

    @staticmethod
    def digest(record: Mapping[str, Any]) -> str:
        value = RoomStore._validate(dict(record))
        return hashlib.sha256(RoomStore._raw(value)).hexdigest()

    @contextmanager
    def transaction(self, *, create: bool) -> Iterator[None]:
        """Serialize each complete Room lifecycle mutation on the registry."""
        try:
            with self._open_directory(create=create) as directory_fd:
                if directory_fd is None:
                    raise RoomError("room registry does not exist")
                with _registry_lock(directory_fd):
                    yield
        except StoreError as exc:
            raise RoomError(str(exc)) from exc

    def save(
        self, record: Mapping[str, Any], *, expected_digest: str | None = None,
    ) -> dict[str, Any]:
        value = self._validate(dict(record))
        try:
            with self._open_directory(create=False) as directory_fd:
                if directory_fd is None:
                    raise RoomError("room record does not exist")
                with _registry_lock(directory_fd):
                    existing = -1
                    try:
                        existing = _open_existing_file(
                            directory_fd, f"{value['room_id']}.json", "room record",
                        )
                        if os.fstat(existing).st_size > 64 * 1024:
                            raise RoomError("room record exceeds the bounded size")
                        chunks = []
                        while True:
                            chunk = os.read(existing, 65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        if expected_digest is not None and hashlib.sha256(
                            b"".join(chunks)
                        ).hexdigest() != expected_digest:
                            raise RoomError("room record changed concurrently; stale save refused")
                    except FileNotFoundError as exc:
                        raise RoomError("room record does not exist") from exc
                    finally:
                        if existing >= 0:
                            os.close(existing)
                    self._write_record(
                        directory_fd, f"{value['room_id']}.json", self._raw(value),
                        create_only=False,
                    )
        except StoreError as exc:
            raise RoomError(str(exc)) from exc
        return value

    def read(self, room_id: str) -> dict[str, Any]:
        path = self._path(room_id)
        try:
            with self._open_directory(create=False) as directory_fd:
                if directory_fd is None:
                    raise RoomError("room was not found")
                try:
                    fd = _open_existing_file(directory_fd, path.name, "room record")
                except FileNotFoundError as exc:
                    raise RoomError("room was not found") from exc
                try:
                    metadata = os.fstat(fd)
                    if metadata.st_size > 64 * 1024:
                        raise RoomError("room record exceeds the bounded size")
                    chunks = []
                    remaining = 64 * 1024 + 1
                    while remaining:
                        chunk = os.read(fd, min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                finally:
                    os.close(fd)
            if len(raw) > 64 * 1024:
                raise RoomError("room record exceeds the bounded size")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        except RoomError:
            raise
        except (StoreError, OSError, UnicodeError, ValueError, _DuplicateJsonKey) as exc:
            raise RoomError(f"room record is unreadable: {exc}") from exc
        record = self._validate(value)
        if record["room_id"] != path.stem:
            raise RoomError("room filename and record identity differ")
        return record

    def list(self) -> list[dict[str, Any]]:
        try:
            with self._open_directory(create=False) as directory_fd:
                if directory_fd is None:
                    return []
                entries = os.listdir(directory_fd)
                json_names = [name for name in entries if name.endswith(".json")]
                invalid = [
                    name for name in json_names
                    if re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json",
                        name,
                    ) is None
                ]
                if invalid:
                    raise RoomError(f"room registry contains an invalid record name: {invalid[0]}")
                names = sorted(json_names)
        except (StoreError, OSError) as exc:
            raise RoomError(f"room registry is unreadable: {exc}") from exc
        return [self.read(Path(name).stem) for name in names]

    def resolve(self, selector: str) -> dict[str, Any]:
        try:
            return self.read(canonical_uuid(selector))
        except (ModelError, RoomError):
            pass
        needle = selector.strip().casefold() if isinstance(selector, str) else ""
        matches = [room for room in self.list() if room["name"].casefold() == needle]
        if not matches:
            raise RoomError(f"room {selector!r} was not found")
        if len(matches) != 1:
            raise RoomError(f"room name {selector!r} is ambiguous; use its UUID")
        return matches[0]


def _project_marker(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()


def _set_recovery_guidance(exc: BaseException, guidance: str) -> None:
    """Annotate an interrupt without changing its type, identity, or exit code."""
    try:
        setattr(exc, ROOM_GUIDANCE_ATTR, guidance)
    except BaseException:
        pass


def _owned_state(record: Mapping[str, Any], tmux: TmuxAdapter) -> tuple[str, str]:
    tmux_record = record["tmux"]
    session, pane = tmux_record["session"], tmux_record.get("pane_id")
    session_id = tmux_record.get("session_id")
    if bool(pane) != bool(session_id):
        return "mismatch", "record has no immutable tmux identity"
    if not pane:
        try:
            if tmux.has_session(session):
                return "mismatch", "readable session name is occupied without immutable identity"
        except TmuxError as exc:
            return "unavailable", str(exc)
        return "missing", "no immutable tmux identity or readable session is present"
    try:
        facts = tmux.pane_facts(pane)
        if tmux.session_id(pane) != session_id:
            return "mismatch", "immutable session identity mismatch"
        if tmux.session_option(session_id, SESSION_ROOM_OPTION) != record["room_id"]:
            return "mismatch", "session ownership mismatch"
        if tmux.pane_option(pane, PANE_ROOM_OPTION) != record["room_id"]:
            return "mismatch", "pane ownership mismatch"
        if tmux.pane_option(pane, PANE_PROJECT_OPTION) != _project_marker(
            record["project_id"]
        ):
            return "mismatch", "pane project ownership mismatch"
        if facts.dead:
            return "ended", "harness process exited"
        return "open", "exact room ownership verified"
    except TmuxError as exc:
        try:
            if tmux.has_session(session):
                return "mismatch", "recorded pane is absent but its readable name is occupied"
        except TmuxError as collision_exc:
            return "unavailable", str(collision_exc)
        diagnostic = str(exc).casefold()
        if any(marker in diagnostic for marker in (
            "can't find", "no server", "no sessions", "missing pane",
        )):
            return "missing", "exact tmux pane is absent"
        return "unavailable", str(exc)


def _prelaunch_owned_pane(
    record: Mapping[str, Any], tmux: TmuxAdapter,
) -> tuple[str, str] | None:
    """Recover a pane identity only when both durable ownership markers match."""
    session = record["tmux"]["session"]
    try:
        if not tmux.has_session(session):
            return None
        if tmux.session_option(session, SESSION_ROOM_OPTION) != record["room_id"]:
            return None
        pane = record["tmux"].get("pane_id")
        facts = (
            tmux.pane_facts(pane) if pane
            else tmux.window_pane_facts(session, record["tmux"]["window"])
        )
        if facts.session != session or facts.window != record["tmux"]["window"]:
            return None
        if tmux.pane_option(facts.pane_id, PANE_ROOM_OPTION) != record["room_id"]:
            return None
        if tmux.pane_option(facts.pane_id, PANE_PROJECT_OPTION) != _project_marker(
            record["project_id"]
        ):
            return None
        session_id = tmux.session_id(facts.pane_id)
        recorded_session_id = record["tmux"].get("session_id")
        if recorded_session_id is not None and recorded_session_id != session_id:
            return None
        return facts.pane_id, session_id
    except TmuxError:
        return None


def _action_identity(record: Mapping[str, Any]) -> dict[str, str]:
    tmux_record = record["tmux"]
    if not tmux_record.get("pane_id") or not tmux_record.get("session_id"):
        raise RoomError("room has no immutable tmux identity")
    return {
        "room_id": record["room_id"],
        "project_marker": _project_marker(record["project_id"]),
        "pane_id": tmux_record["pane_id"],
        "session_id": tmux_record["session_id"],
    }


def _result(
    record: Mapping[str, Any], *, state: str, detail: str, tmux: TmuxAdapter,
) -> dict[str, Any]:
    attach_argv = None
    attach = None
    if record["tmux"].get("pane_id") and record["tmux"].get("session_id"):
        attach_argv = tmux.room_attach_argv(**_action_identity(record))
        attach = shlex.join(attach_argv)
    return {
        "room_id": record["room_id"], "name": record["name"],
        "slug": record["slug"], "project_id": record["project_id"],
        "project_root": record["project_root"],
        "project_name": record["project_name"], "harness": record["harness"],
        "session": record["tmux"]["session"],
        "window": record["tmux"]["window"],
        "pane_id": record["tmux"].get("pane_id"),
        "session_id": record["tmux"].get("session_id"), "state": state,
        "detail": detail, "attach": attach,
        "attach_argv": attach_argv,
        "created_at": record["created_at"], "updated_at": record["updated_at"],
    }


def open_room(
    *, name: str, project: str, harness: str, prompt: str, config: Any,
    env: Mapping[str, str], tmux: TmuxAdapter, asha_root: Path,
    executable_finder: Callable[[str], str | None] = shutil.which,
    room_id: str | None = None,
) -> dict[str, Any]:
    room_name, slug = _room_name(name)
    text = _prompt(prompt)
    try:
        selected_harness = validate_harness(harness)
    except HarnessError as exc:
        raise RoomError(str(exc)) from exc
    command_key, harness_command = room_harness_command(selected_harness, env)
    if executable_finder(harness_command) is None:
        raise RoomError(
            f"{selected_harness} harness command {harness_command!r} is not installed"
        )
    selected_project = resolve_project(project, env=env)
    store = RoomStore(config)
    identity = str(uuid.uuid4()) if room_id is None else room_id
    try:
        identity = canonical_uuid(identity)
    except ModelError as exc:
        raise RoomError("room id must be a canonical UUID") from exc
    session = f"{config.session_prefix}room-{identity.replace('-', '')[:8]}"
    at = _now()
    record = {
        "contract": ROOM_CONTRACT, "room_id": identity, "name": room_name,
        "slug": slug, "project_id": selected_project["project_id"],
        "project_root": selected_project["root"],
        "project_name": selected_project["name"], "harness": selected_harness,
        "tmux": {
            "session": session, "session_id": None,
            "window": "room", "pane_id": None,
        },
        "created_at": at, "updated_at": at, "lifecycle": "creating",
        "prompt_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    crossed_respawn = False
    with store.transaction(create=True):
        store.create(record)  # durable intent precedes the first tmux mutation
        expected_digest = store.digest(record)
        try:
            pane = tmux.create_task_session(
            session=session, window="room",
            start_directory=Path(selected_project["root"]),
            environment={
                "ASHA_HOME": str(config.asha_home), "ASHA_PERSONA": "1",
                "ASHA_ORCHESTRATOR_STANCE": "0", ROOM_ENV: identity,
                command_key: harness_command,
            },
            holder_argv=["sleep", "3600"],
            session_options={SESSION_ROOM_OPTION: identity},
            pane_options={
                PANE_ROOM_OPTION: identity,
                PANE_PROJECT_OPTION: _project_marker(selected_project["project_id"]),
            },
            pane_title=f"asha:room:{slug}:{selected_harness}",
            )
            record["tmux"]["pane_id"] = pane
            record["tmux"]["session_id"] = tmux.session_id(pane)
            record["updated_at"] = _now()
            store.save(record, expected_digest=expected_digest)
            expected_digest = store.digest(record)
            state, detail = _owned_state(record, tmux)
            if state not in {"open", "ended"}:
                raise RoomError(f"new room ownership could not be verified: {detail}")
            argv = ["env"]
            for key in SCRUBBED_ROLE_ENV:
                argv.extend(["-u", key])
            argv.extend(room_tmux_argv(asha_root, selected_harness, text))
            crossed_respawn = True
            tmux.respawn(pane, argv)
            record["lifecycle"] = "open"
            record["updated_at"] = _now()
            store.save(record, expected_digest=expected_digest)
        except BaseException as exc:
            if not crossed_respawn:
                exact: tuple[str, str] | None = None
                recovery_failure: BaseException | None = None
                cleanup_failure: BaseException | None = None
                persistence_failure: BaseException | None = None
                try:
                    exact = _prelaunch_owned_pane(record, tmux)
                except BaseException as recovery_exc:
                    recovery_failure = recovery_exc
                if exact is not None:
                    exact_pane, exact_session_id = exact
                    # Never write the in-memory one-ID intermediate state. The
                    # recovered pair is durable recovery identity even after a
                    # successful cleanup, making list/close deterministic.
                    record["tmux"]["pane_id"] = exact_pane
                    record["tmux"]["session_id"] = exact_session_id
                    try:
                        tmux.kill_owned_room(
                            room_id=identity,
                            project_marker=_project_marker(record["project_id"]),
                            pane_id=exact_pane, session_id=exact_session_id,
                        )
                    except BaseException as cleanup_exc:
                        cleanup_failure = cleanup_exc
                elif bool(record["tmux"].get("pane_id")) != bool(
                    record["tmux"].get("session_id")
                ):
                    record["tmux"]["pane_id"] = None
                    record["tmux"]["session_id"] = None
                record["lifecycle"] = "ended"
                record["updated_at"] = _now()
                try:
                    store.save(record, expected_digest=expected_digest)
                except BaseException as save_exc:
                    persistence_failure = save_exc
                guidance = (
                    f"room {identity} launch stopped before harness start; "
                    f"inspect with 'asha room list --json' and confirm cleanup with "
                    f"'asha room close {identity} --yes'"
                )
                failures = [
                    item for item in (
                        recovery_failure, cleanup_failure, persistence_failure,
                    ) if item is not None
                ]
                if failures:
                    guidance += "; recovery detail: " + "; ".join(
                        str(item) or type(item).__name__ for item in failures
                    )
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    _set_recovery_guidance(exc, guidance)
                    raise
                if not isinstance(exc, (RoomError, TmuxError, OSError)):
                    _set_recovery_guidance(exc, guidance)
                    raise
                recovery_details = [
                    item for item in (recovery_failure, cleanup_failure)
                    if item is not None
                ]
                if persistence_failure is not None:
                    raise RoomError(
                        f"room launch failed before harness start: {exc}; "
                        f"record update also failed: {persistence_failure}"
                        + (
                            "; recovery detail: " + "; ".join(
                                str(item) or type(item).__name__
                                for item in recovery_details
                            ) if recovery_details else ""
                        )
                    ) from exc
                detail = ""
                if recovery_details:
                    detail = "; recovery detail: " + "; ".join(
                        str(item) or type(item).__name__
                        for item in recovery_details
                    )
                raise RoomError(
                    f"room launch failed before harness start: {exc}{detail}"
                ) from exc
            # The harness may have started. Never mask this uncertainty with a
            # second persistence attempt, and give the UUID-bound recovery path.
            guidance = (
                f"room launch outcome is uncertain after harness start for {identity}: "
                f"{exc}; run 'asha room list --json', inspect room {identity}, then "
                f"run 'asha room close {identity} --yes'"
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                _set_recovery_guidance(exc, guidance)
                raise
            if not isinstance(exc, (RoomError, TmuxError, OSError)):
                _set_recovery_guidance(exc, guidance)
                raise
            raise RoomError(guidance) from exc
    return {"contract": ROOM_OPEN_CONTRACT, **_result(
        record, state="open", detail="room launched detached", tmux=tmux,
    )}


def list_rooms(store: RoomStore, *, tmux: TmuxAdapter) -> dict[str, Any]:
    rooms = []
    for record in store.list():
        state, detail = _owned_state(record, tmux)
        tmux_record = record["tmux"]
        if not tmux_record.get("pane_id") and not tmux_record.get("session_id"):
            recovered = _prelaunch_owned_pane(record, tmux)
            if recovered is not None:
                state, detail = (
                    "ended",
                    "failed launch holder is exact-marker-owned and recoverable by confirmed close",
                )
            elif state == "mismatch":
                detail = (
                    "failed launch identity recovery is unavailable or the readable "
                    "session name is foreign; retry confirmed close when tmux recovers"
                )
        if record["lifecycle"] == "ended" and state == "missing":
            state, detail = "ended", "room was closed"
        rooms.append(_result(record, state=state, detail=detail, tmux=tmux))
    counts: dict[str, int] = {}
    for room in rooms:
        counts[room["project_id"]] = counts.get(room["project_id"], 0) + (
            1 if room["state"] == "open" else 0
        )
    for room in rooms:
        room["shared_working_tree"] = (
            room["state"] == "open" and counts[room["project_id"]] > 1
        )
    return {"contract": ROOM_LIST_CONTRACT, "rooms": rooms}


def attach_room(store: RoomStore, selector: str, *, tmux: TmuxAdapter) -> dict[str, Any]:
    record = store.resolve(selector)
    state, detail = _owned_state(record, tmux)
    if record["lifecycle"] == "ended" and state == "missing":
        state, detail = "ended", "room was closed"
    if state != "open":
        if state == "mismatch":
            raise RoomError(f"room ownership mismatch: {detail}")
        raise RoomError(f"room {record['name']} is {state}: {detail}")
    return {"contract": ROOM_ATTACH_CONTRACT, **_result(
        record, state=state, detail=detail, tmux=tmux,
    )}


def close_room(store: RoomStore, selector: str, *, tmux: TmuxAdapter) -> dict[str, Any]:
    with store.transaction(create=False):
        record = store.resolve(selector)
        expected_digest = store.digest(record)
        tmux_record = record["tmux"]
        zero_identity = (
            not tmux_record.get("pane_id") and not tmux_record.get("session_id")
        )
        if zero_identity:
            recovered = _prelaunch_owned_pane(record, tmux)
            if recovered is not None:
                recovered_pane, recovered_session = recovered
                record["tmux"]["pane_id"] = recovered_pane
                record["tmux"]["session_id"] = recovered_session
                record["updated_at"] = _now()
                # This CAS must commit the kill identity before the irreversible
                # tmux action. A repeated close can then finish safely even if
                # kill succeeds but the final lifecycle save is interrupted.
                store.save(record, expected_digest=expected_digest)
                expected_digest = store.digest(record)
            else:
                recovery_state, recovery_detail = _owned_state(record, tmux)
                if recovery_state != "missing":
                    raise RoomError(
                        f"room immutable identity recovery is unavailable: "
                        f"{recovery_detail}; retry 'asha room close "
                        f"{record['room_id']} --yes' when tmux recovers; "
                        "no session was killed"
                    )
        state, detail = _owned_state(record, tmux)
        if state == "mismatch":
            raise RoomError(f"room ownership mismatch: {detail}; foreign session was not killed")
        if state == "unavailable":
            raise RoomError(f"room ownership is unavailable: {detail}; no session was killed")
        already = record["lifecycle"] == "ended" and state == "missing"
        if state in {"open", "ended"}:
            try:
                tmux.kill_owned_room(**_action_identity(record))
            except TmuxError as exc:
                changed_state, changed_detail = _owned_state(record, tmux)
                if changed_state != "missing":
                    raise RoomError(
                        f"room close refused because ownership changed: {exc}; "
                        f"{changed_detail}; no session was killed"
                    ) from exc
        record["lifecycle"] = "ended"
        record["updated_at"] = _now()
        store.save(record, expected_digest=expected_digest)
    return {
        "contract": ROOM_CLOSE_CONTRACT,
        **_result(record, state="ended", detail="room closed", tmux=tmux),
        "already_closed": already,
    }


__all__ = [
    "PANE_PROJECT_OPTION", "PANE_ROOM_OPTION", "ROOM_ATTACH_CONTRACT",
    "ROOM_CLOSE_CONTRACT", "ROOM_CONTRACT", "ROOM_LIST_CONTRACT",
    "ROOM_OPEN_CONTRACT", "SESSION_ROOM_OPTION", "RoomError", "RoomStore",
    "attach_room", "close_room", "list_rooms", "open_room", "resolve_project",
    "room_harness_available", "room_harness_command", "room_launch_argv",
    "room_tmux_argv",
]
