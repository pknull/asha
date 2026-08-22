"""Typed Control start prerequisites and the explicit apply-only repair."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import ControlConfig
from .context import DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES, read_published_snapshot
from .jj import (
    ContextCompatibilityError, DefaultBaseResolution, JjAdapter, JjError,
    MissingPositiveIgnoreEvidence, RepositoryPreEnableBinding,
    MAX_TRACKED_BLOB_BYTES, inspect_pre_enable_binding,
    require_pre_enable_binding,
)
from .prepare import PreparationPrerequisiteError
from .store import TransactionCoordinator


WORKER_REFUSAL_CONTRACT = "asha.control-task-start-worker-refusal.v1"
CONTROL_IGNORE_TARGET = ".gitignore"
CONTROL_IGNORE_RULE = "/.asha/control-task.json"
CONTROL_IGNORE_MARKER = "# Asha Control private context (managed)"
CONTROL_IGNORE_BLOCK = f"{CONTROL_IGNORE_MARKER}\n{CONTROL_IGNORE_RULE}\n"
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_MAX_WIRE_BYTES = 64 * 1024
_NO_IGNORE_OVERRIDE = object()


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly the v1 fields")
    return value


def _clean_string(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _canonical_root(value: Any) -> Path:
    root = Path(_clean_string(value, "repository root"))
    if not root.is_absolute() or os.path.realpath(root) != str(root):
        raise ValueError("repository root is not an exact canonical absolute path")
    return root


def _binding_record(binding: RepositoryPreEnableBinding) -> dict[str, Any]:
    return {
        "root": str(binding.root), "root_fact": dict(binding.root_fact),
        "git_binding": binding.git_binding.record(),
    }


def _binding_digest(binding: RepositoryPreEnableBinding) -> str:
    raw = json.dumps(
        _binding_record(binding), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"asha-control-prerequisite-binding-v1\0" + raw).hexdigest()


@dataclass(frozen=True)
class IgnorePreimage:
    state: str
    sha256: str
    size: int
    mode: int
    uid: int
    dev: int
    ino: int
    nlink: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class StartPrerequisiteOffer:
    root: Path
    project_id: str
    binding: RepositoryPreEnableBinding
    binding_digest: str
    requested_base: str
    base_explicit: bool
    existing_jj: bool
    base_commit_id: str
    default_base_resolution: DefaultBaseResolution | None
    evidence: MissingPositiveIgnoreEvidence
    proof_origin: str
    pr_remote_config_digest: str | None
    target: str
    rules: tuple[str, ...]
    preimage: IgnorePreimage
    working_ignore_digest: str
    already_covered: bool


class StartPrerequisiteRefusal(ValueError):
    def __init__(
        self, offer: StartPrerequisiteOffer, *, task_id: str | None = None,
        tui_worker: bool = False,
    ) -> None:
        self.offer = offer
        self.task_id = task_id
        self.tui_worker = tui_worker
        super().__init__(
            "selected immutable base does not positively ignore "
            ".asha/control-task.json; add /.asha/control-task.json to .gitignore, "
            "commit the rule or select a commit that contains it, then retry "
            f"(selected base {offer.base_commit_id}; repository and task state "
            "were unchanged)"
        )


class PrerequisiteApplyIndeterminate(ValueError):
    pass


class ControlTermination(Exception):
    """Control-flow termination that must cross repository transactions intact."""


_INDETERMINATE_MESSAGE = (
    "the .gitignore replacement became visible but durable verification is "
    "indeterminate; inspect .gitignore before retrying"
)


def _read_ignore_preimage(root: Path) -> tuple[IgnorePreimage, bytes]:
    path = root / CONTROL_IGNORE_TARGET
    try:
        before = path.lstat()
    except FileNotFoundError:
        return IgnorePreimage(
            "absent", hashlib.sha256(b"").hexdigest(), 0, 0o644,
            os.geteuid(), 0, 0, 0, 0, 0,
        ), b""
    except OSError as exc:
        raise ValueError(f"cannot inspect .gitignore: {exc}") from exc
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(".gitignore must be one regular file, not a symlink or special path")
    if before.st_uid != os.geteuid() or before.st_nlink != 1:
        raise ValueError(".gitignore must be one file owned by the effective user")
    if mode & 0o133:
        raise ValueError(".gitignore must not be executable or group/other writable")
    if before.st_size > MAX_TRACKED_BLOB_BYTES:
        raise ValueError(".gitignore exceeds the bounded repair size")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open .gitignore safely: {exc}") from exc
    try:
        opened = os.fstat(fd)
        remaining = MAX_TRACKED_BLOB_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size",
              "st_mtime_ns", "st_ctime_ns")
    if any(getattr(opened, item) != getattr(after, item) for item in fields):
        raise ValueError(".gitignore changed during inspection")
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError(".gitignore changed during inspection")
    if len(raw) != opened.st_size or len(raw) > MAX_TRACKED_BLOB_BYTES:
        raise ValueError(".gitignore changed during bounded read")
    return IgnorePreimage(
        "file", hashlib.sha256(raw).hexdigest(), len(raw), mode, opened.st_uid,
        opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_mtime_ns,
        opened.st_ctime_ns,
    ), raw


def _working_ignore_state(
    root: Path, *, root_override: bytes | object = _NO_IGNORE_OVERRIDE,
) -> tuple[str, bool]:
    files: list[tuple[str, bytes]] = []
    digest = hashlib.sha256(b"asha-control-working-ignore-v1\0")
    for relative in (".gitignore", ".asha/.gitignore"):
        if relative == ".gitignore" and root_override is not _NO_IGNORE_OVERRIDE:
            if not isinstance(root_override, bytes):
                raise ValueError("intended root ignore bytes are invalid")
            if len(root_override) > MAX_TRACKED_BLOB_BYTES:
                raise ValueError("intended .gitignore exceeds the bounded repair size")
            digest.update(
                relative.encode() + b"\0" + hashlib.sha256(root_override).digest()
            )
            files.append((relative, root_override))
            continue
        path = root / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(relative.encode() + b"\0absent\0")
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{relative} must be a regular non-symlink ignore file")
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
            raise ValueError(f"{relative} has unsafe ownership or link count")
        if stat.S_IMODE(metadata.st_mode) & 0o133:
            raise ValueError(f"{relative} is executable or group/other writable")
        if metadata.st_size > MAX_TRACKED_BLOB_BYTES:
            raise ValueError(f"{relative} exceeds the bounded ignore size")
        try:
            fd = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise ValueError(f"cannot open {relative} safely: {exc}") from exc
        try:
            opened = os.fstat(fd)
            chunks: list[bytes] = []
            remaining = MAX_TRACKED_BLOB_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        observed = (
            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
            opened.st_nlink, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
        )
        if observed != (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino) \
                or len(raw) != opened.st_size or len(raw) > MAX_TRACKED_BLOB_BYTES:
            raise ValueError(f"{relative} changed during inspection")
        digest.update(relative.encode() + b"\0" + hashlib.sha256(raw).digest())
        files.append((relative, raw))
    with tempfile.TemporaryDirectory(prefix="asha-control-ignore-") as temporary:
        temporary_root = Path(temporary)
        work = temporary_root / "work"
        metadata = temporary_root / "git"
        work.mkdir(mode=0o700)
        metadata.mkdir(mode=0o700)
        for relative in ("objects", "refs", "info"):
            (metadata / relative).mkdir(mode=0o700)
        (metadata / "HEAD").write_text("ref: refs/heads/probe\n", encoding="ascii")
        (metadata / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
            encoding="ascii",
        )
        (metadata / "info/exclude").write_bytes(b"")
        for relative, raw in files:
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        marker = work / ".asha/control-task.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"{}\n")
        result = subprocess.run(
            ["/usr/bin/git", f"--git-dir={metadata}", f"--work-tree={work}",
             "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index",
             "--quiet", "--", ".asha/control-task.json"],
            cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=False, env={
                "PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            },
        )
    if result.returncode not in {0, 1}:
        raise ValueError("working-tree ignore proof failed")
    return digest.hexdigest(), result.returncode == 0


def capture_prerequisite_offer(
    config: ControlConfig, error: Exception,
) -> StartPrerequisiteOffer:
    if not isinstance(error, PreparationPrerequisiteError):
        raise error
    if error.evidence.missing_paths != (".asha/control-task.json",):
        raise error
    binding = error.source_binding
    preimage, _raw = _read_ignore_preimage(binding.root)
    working_digest, covered = _working_ignore_state(binding.root)
    return StartPrerequisiteOffer(
        root=binding.root, project_id=error.evidence.project_id,
        binding=binding, binding_digest=_binding_digest(binding),
        requested_base=error.request.requested_base,
        base_explicit=error.base_explicit, existing_jj=error.existing_jj,
        base_commit_id=error.resolved_base_commit_id,
        default_base_resolution=error.default_base_resolution,
        evidence=error.evidence,
        proof_origin=(
            "source" if error.source_object_available else "quarantine"
        ),
        pr_remote_config_digest=error.pr_remote_config_digest,
        target=CONTROL_IGNORE_TARGET,
        rules=(CONTROL_IGNORE_RULE,), preimage=preimage,
        working_ignore_digest=working_digest, already_covered=covered,
    )


def _fact(value: Any, label: str) -> dict[str, int]:
    obj = _exact(value, {"dev", "ino", "mode", "uid"}, label)
    if any(type(obj[key]) is not int or obj[key] < 0 for key in obj):
        raise ValueError(f"{label} contains an invalid integer")
    return dict(obj)


def _offer_record(offer: StartPrerequisiteOffer, task_id: str) -> dict[str, Any]:
    default = None if offer.default_base_resolution is None else {
        "tier": offer.default_base_resolution.tier,
        "references": list(offer.default_base_resolution.references),
        "commit_id": offer.default_base_resolution.commit_id,
    }
    evidence = asdict(offer.evidence)
    for name in (
        "planned_context_paths", "private_directory_paths", "reused_paths",
        "required_ignored_paths", "missing_paths",
    ):
        evidence[name] = list(evidence[name])
    return {
        "contract": WORKER_REFUSAL_CONTRACT, "task_id": task_id,
        "kind": "missing-positive-ignore",
        "repository": {
            "root": str(offer.root), "project_id": offer.project_id,
            "binding_digest": offer.binding_digest,
            "binding": _binding_record(offer.binding),
        },
        "base": {
            "requested": offer.requested_base, "explicit": offer.base_explicit,
            "existing_jj": offer.existing_jj, "commit_id": offer.base_commit_id,
            "default": default, "proof_origin": offer.proof_origin,
            "remote_config_digest": offer.pr_remote_config_digest,
        },
        "proof": evidence,
        "repair": {
            "target": offer.target, "rules": list(offer.rules),
            "preimage": asdict(offer.preimage),
            "working_ignore_digest": offer.working_ignore_digest,
            "already_covered": offer.already_covered,
        },
    }


def encode_worker_refusal(offer: StartPrerequisiteOffer, task_id: str) -> bytes:
    try:
        parsed = uuid.UUID(task_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("worker refusal task identity is invalid") from exc
    if str(parsed) != task_id:
        raise ValueError("worker refusal task identity is invalid")
    raw = json.dumps(
        _offer_record(offer, task_id), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(raw) > _MAX_WIRE_BYTES:
        raise ValueError("worker refusal exceeds the bounded wire size")
    return raw


def _string_tuple(
    value: Any, label: str, *, maximum: int = 64, allow_empty: bool = False,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list) or len(value) > maximum
        or (not value and not allow_empty)
    ):
        raise ValueError(f"{label} is invalid")
    result = tuple(_clean_string(item, label) for item in value)
    if len(set(result)) != len(result) or result != tuple(sorted(result)):
        raise ValueError(f"{label} must be unique and sorted")
    return result


def _context_tuple(
    value: Any, label: str, *, directories: bool | None = None,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    result = _string_tuple(value, label, allow_empty=allow_empty)
    for item in result:
        candidate = item[:-1] if item.endswith("/") else item
        if (
            item.startswith("/") or "//" in item or ".." in Path(candidate).parts
            or os.path.normpath(candidate) != candidate
            or (directories is True and not item.endswith("/"))
            or (directories is False and item.endswith("/"))
        ):
            raise ValueError(f"{label} contains a non-canonical path")
    return result


def decode_worker_refusal(raw: bytes, expected_task_id: str) -> StartPrerequisiteOffer:
    if not isinstance(raw, bytes) or len(raw) > _MAX_WIRE_BYTES:
        raise ValueError("worker refusal output is oversized or invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (_DuplicateKey, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("worker refusal is not strict UTF-8 JSON") from exc
    root_obj = _exact(
        value, {"contract", "task_id", "kind", "repository", "base", "proof", "repair"},
        "worker refusal",
    )
    if root_obj["contract"] != WORKER_REFUSAL_CONTRACT or root_obj["kind"] != "missing-positive-ignore":
        raise ValueError("worker refusal contract or kind is unsupported")
    if root_obj["task_id"] != expected_task_id:
        raise ValueError("worker refusal task identity does not match the supervised worker")
    try:
        if str(uuid.UUID(root_obj["task_id"])) != root_obj["task_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("worker refusal task identity is invalid") from exc
    repository = _exact(
        root_obj["repository"], {"root", "project_id", "binding_digest", "binding"},
        "worker refusal repository",
    )
    root = _canonical_root(repository["root"])
    project_id = _clean_string(repository["project_id"], "project identity", maximum=256)
    binding_digest = repository["binding_digest"]
    if not isinstance(binding_digest, str) or _DIGEST.fullmatch(binding_digest) is None:
        raise ValueError("worker refusal binding digest is invalid")
    binding_obj = _exact(repository["binding"], {"root", "root_fact", "git_binding"}, "binding")
    if binding_obj["root"] != str(root):
        raise ValueError("worker refusal binding root differs")
    git_obj = _exact(
        binding_obj["git_binding"], {"kind", "marker_fact", "marker_digest", "target", "target_fact"},
        "Git binding",
    )
    from .jj import GitMarkerBinding
    marker_digest = git_obj["marker_digest"]
    if marker_digest is not None and (
        not isinstance(marker_digest, str) or _DIGEST.fullmatch(marker_digest) is None
    ):
        raise ValueError("Git binding marker digest is invalid")
    binding = RepositoryPreEnableBinding(
        root, _fact(binding_obj["root_fact"], "root fact"),
        GitMarkerBinding(
            _clean_string(git_obj["kind"], "Git marker kind", maximum=16),
            _fact(git_obj["marker_fact"], "Git marker fact"), marker_digest,
            _canonical_root(git_obj["target"]),
            _fact(git_obj["target_fact"], "Git target fact"),
        ),
    )
    if _binding_digest(binding) != binding_digest:
        raise ValueError("worker refusal binding digest differs from its facts")
    base = _exact(
        root_obj["base"], {
            "requested", "explicit", "existing_jj", "commit_id", "default",
            "proof_origin", "remote_config_digest",
        },
        "worker refusal base",
    )
    requested = _clean_string(base["requested"], "requested base")
    if type(base["explicit"]) is not bool or type(base["existing_jj"]) is not bool:
        raise ValueError("worker refusal base flags must be booleans")
    if not isinstance(base["commit_id"], str) or _OID.fullmatch(base["commit_id"]) is None:
        raise ValueError("worker refusal base commit is invalid")
    default_value = base["default"]
    default = None
    if default_value is not None:
        item = _exact(default_value, {"tier", "references", "commit_id"}, "default base")
        references = _string_tuple(item["references"], "default references")
        if any(not reference.startswith("refs/") for reference in references):
            raise ValueError("default base references are invalid")
        if not isinstance(item["commit_id"], str) or _OID.fullmatch(item["commit_id"]) is None:
            raise ValueError("default base commit is invalid")
        tier = _clean_string(item["tier"], "default tier", maximum=64)
        if tier not in {"attached-local", "remote-head", "conventional-local"}:
            raise ValueError("default base tier is unsupported")
        default = DefaultBaseResolution(references, item["commit_id"], tier)
    if base["explicit"] == (default is not None):
        raise ValueError("worker refusal explicit/default base binding is inconsistent")
    if default is not None and default.commit_id != base["commit_id"]:
        raise ValueError("worker refusal default commit differs from selected base")
    proof_origin = base["proof_origin"]
    remote_config_digest = base["remote_config_digest"]
    if proof_origin not in {"source", "quarantine"}:
        raise ValueError("worker refusal proof origin is unsupported")
    if proof_origin == "source":
        if remote_config_digest is not None:
            raise ValueError("source proof unexpectedly carries remote configuration")
    elif (
        not isinstance(remote_config_digest, str)
        or _DIGEST.fullmatch(remote_config_digest) is None
        or not base["explicit"] or base["requested"] != base["commit_id"]
    ):
        raise ValueError("quarantine proof lacks its exact remote/base binding")
    proof = _exact(root_obj["proof"], {
        "base_commit_id", "materialization_digest", "project_id",
        "planned_context_paths", "private_directory_paths", "reused_paths",
        "required_ignored_paths", "missing_paths", "info_exclude_digest", "digest",
    }, "worker refusal proof")
    for name in ("base_commit_id",):
        if not isinstance(proof[name], str) or _OID.fullmatch(proof[name]) is None:
            raise ValueError(f"worker refusal proof {name} is invalid")
    for name in ("materialization_digest", "info_exclude_digest", "digest"):
        if not isinstance(proof[name], str) or _DIGEST.fullmatch(proof[name]) is None:
            raise ValueError(f"worker refusal proof {name} is invalid")
    evidence = MissingPositiveIgnoreEvidence(
        proof["base_commit_id"], proof["materialization_digest"],
        _clean_string(proof["project_id"], "proof project identity", maximum=256),
        _context_tuple(proof["planned_context_paths"], "planned context paths", directories=False),
        _context_tuple(proof["private_directory_paths"], "private directory paths", directories=True),
        _context_tuple(proof["reused_paths"], "reused paths", directories=False, allow_empty=True),
        _context_tuple(proof["required_ignored_paths"], "required ignored paths"),
        _context_tuple(proof["missing_paths"], "missing paths", directories=False),
        proof["info_exclude_digest"], proof["digest"],
    )
    if evidence.missing_paths != (".asha/control-task.json",):
        raise ValueError("worker refusal is not the supported marker omission")
    if (evidence.base_commit_id != base["commit_id"] or evidence.project_id != project_id):
        raise ValueError("worker refusal proof differs from repository/base binding")
    if not set(evidence.missing_paths).issubset(evidence.required_ignored_paths):
        raise ValueError("worker refusal missing paths are not required paths")
    failure_digest = hashlib.sha256(
        b"asha-control-context-missing-positive-ignore-v1\0"
    )
    for item in (
        evidence.base_commit_id, evidence.materialization_digest,
        evidence.project_id, *evidence.planned_context_paths,
        *evidence.private_directory_paths, *evidence.reused_paths,
        *evidence.required_ignored_paths, *evidence.missing_paths,
        evidence.info_exclude_digest,
    ):
        failure_digest.update(item.encode("utf-8") + b"\0")
    if failure_digest.hexdigest() != evidence.digest:
        raise ValueError("worker refusal proof digest differs from its facts")
    repair = _exact(
        root_obj["repair"], {"target", "rules", "preimage", "working_ignore_digest", "already_covered"},
        "worker refusal repair",
    )
    if repair["target"] != CONTROL_IGNORE_TARGET or repair["rules"] != [CONTROL_IGNORE_RULE]:
        raise ValueError("worker refusal repair is not the supported exact patch")
    pre = _exact(repair["preimage"], {
        "state", "sha256", "size", "mode", "uid", "dev", "ino", "nlink", "mtime_ns", "ctime_ns",
    }, "worker refusal preimage")
    if pre["state"] not in {"absent", "file"} or not isinstance(pre["sha256"], str) or _DIGEST.fullmatch(pre["sha256"]) is None:
        raise ValueError("worker refusal preimage state or digest is invalid")
    for name in ("size", "mode", "uid", "dev", "ino", "nlink", "mtime_ns", "ctime_ns"):
        if type(pre[name]) is not int or pre[name] < 0:
            raise ValueError("worker refusal preimage contains an invalid integer")
    preimage = IgnorePreimage(**pre)
    if preimage.size > MAX_TRACKED_BLOB_BYTES or preimage.mode > 0o7777:
        raise ValueError("worker refusal preimage exceeds its bounded range")
    if preimage.state == "absent" and (
        preimage.size != 0 or preimage.sha256 != hashlib.sha256(b"").hexdigest()
        or preimage.nlink != 0 or any((preimage.dev, preimage.ino,
                                      preimage.mtime_ns, preimage.ctime_ns))
    ):
        raise ValueError("worker refusal absent preimage contains file facts")
    if not isinstance(repair["working_ignore_digest"], str) or _DIGEST.fullmatch(repair["working_ignore_digest"]) is None or type(repair["already_covered"]) is not bool:
        raise ValueError("worker refusal working ignore evidence is invalid")
    return StartPrerequisiteOffer(
        root, project_id, binding, binding_digest, requested, base["explicit"],
        base["existing_jj"], base["commit_id"], default, evidence,
        proof_origin, remote_config_digest,
        CONTROL_IGNORE_TARGET, (CONTROL_IGNORE_RULE,), preimage,
        repair["working_ignore_digest"], repair["already_covered"],
    )


def _intended_ignore_bytes(existing: bytes) -> bytes:
    if existing.endswith(CONTROL_IGNORE_BLOCK.encode("utf-8")):
        return existing
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    return existing + separator + CONTROL_IGNORE_BLOCK.encode("utf-8")


def _open_bound_root_directory(offer: StartPrerequisiteOffer) -> int:
    flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(offer.root, flags)
    try:
        metadata = os.fstat(fd)
        expected = offer.binding.root_fact
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != expected["dev"]
            or metadata.st_ino != expected["ino"]
            or metadata.st_mode != expected["mode"]
            or metadata.st_uid != expected["uid"]
        ):
            raise ValueError("repository root changed after prerequisite review")
    except Exception:
        os.close(fd)
        raise
    return fd


def _create_temporary_at(directory_fd: int, mode: int) -> tuple[int, str]:
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(32):
        name = f".gitignore.asha-control.{secrets.token_hex(16)}"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, mode)
        except Exception:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        return fd, name
    raise ValueError("could not allocate a unique prerequisite temporary file")


def _fsync_directory(fd: int) -> None:
    os.fsync(fd)


def _revalidate_offer_repository(offer: StartPrerequisiteOffer) -> None:
    root = offer.root
    require_pre_enable_binding(root, offer.binding)
    if _binding_digest(inspect_pre_enable_binding(root)) != offer.binding_digest:
        raise ValueError("repository binding changed after prerequisite review")
    snapshot = read_published_snapshot(root)
    if snapshot.project_id != offer.project_id:
        raise ValueError("project identity changed after prerequisite review")
    adapter = JjAdapter()
    if offer.default_base_resolution is not None:
        if adapter.resolve_default_base(root) != offer.default_base_resolution:
            raise ValueError("default base changed after prerequisite review")
    else:
        try:
            resolved = (
                adapter.resolve_base(root, offer.requested_base)
                if offer.existing_jj else
                adapter.resolve_git_commit(root, offer.requested_base)
            )
        except JjError:
            # A remote-only PR refusal deliberately leaves its selected object
            # outside the source. The exact OID and closed failure digest are
            # the immutable action-time proof; root, Git, project, and mutable
            # ignore facts are still re-read under the source lock.
            if (
                offer.proof_origin != "quarantine" or
                offer.requested_base != offer.base_commit_id
                or _OID.fullmatch(offer.requested_base) is None
            ):
                raise
            resolved = offer.base_commit_id
        if resolved != offer.base_commit_id:
            raise ValueError("explicit base changed after prerequisite review")
    try:
        plan = adapter.materialization_plan(
            offer.binding.git_binding.target, offer.base_commit_id, exact_root=root,
        )
    except JjError:
        if (
            offer.proof_origin != "quarantine" or
            offer.requested_base != offer.base_commit_id
            or _OID.fullmatch(offer.requested_base) is None
        ):
            raise
        if offer.pr_remote_config_digest is None:
            raise ValueError("quarantine prerequisite proof lacks remote binding")
        configured = adapter.git_remote_configuration(root)
        if configured.config_digest != offer.pr_remote_config_digest:
            raise ValueError("Git remote configuration changed after prerequisite review")
        plan = None
    if plan is None:
        failure_digest = hashlib.sha256(
            b"asha-control-context-missing-positive-ignore-v1\0"
        )
        for value in (
            offer.evidence.base_commit_id, offer.evidence.materialization_digest,
            offer.evidence.project_id, *offer.evidence.planned_context_paths,
            *offer.evidence.private_directory_paths, *offer.evidence.reused_paths,
            *offer.evidence.required_ignored_paths, *offer.evidence.missing_paths,
            offer.evidence.info_exclude_digest,
        ):
            failure_digest.update(value.encode("utf-8") + b"\0")
        if failure_digest.hexdigest() != offer.evidence.digest:
            raise ValueError("immutable prerequisite evidence changed after review")
        return
    if plan.digest != offer.evidence.materialization_digest:
        raise ValueError("immutable base materialization changed after prerequisite review")
    try:
        adapter.prove_context_compatibility(
            root, offer.binding.git_binding.target, plan,
            project_id=offer.project_id,
            planned_context_paths=offer.evidence.planned_context_paths,
            private_directory_paths=offer.evidence.private_directory_paths,
        )
    except ContextCompatibilityError as exc:
        if exc.evidence != offer.evidence:
            raise ValueError("immutable prerequisite evidence changed after review") from exc
    else:
        raise ValueError("immutable prerequisite no longer fails; review the new base")


def apply_ignore_prerequisite(config: ControlConfig, offer: StartPrerequisiteOffer) -> str:
    if not isinstance(offer, StartPrerequisiteOffer):
        raise ValueError("Control prerequisite offer is invalid")
    root = offer.root
    with TransactionCoordinator(config).source_lock(root):
        _revalidate_offer_repository(offer)
        current_preimage, existing = _read_ignore_preimage(root)
        if current_preimage != offer.preimage:
            raise ValueError(".gitignore changed after prerequisite review")
        working_digest, covered = _working_ignore_state(root)
        if (working_digest, covered) != (
            offer.working_ignore_digest, offer.already_covered,
        ):
            raise ValueError("working ignore policy changed after prerequisite review")
        if covered:
            return (
                "The working tree already ignores .asha/control-task.json. Commit "
                "the covering rule or select a commit that contains it; the previous "
                "base remains unauthorized. No task state was created."
            )
        intended = _intended_ignore_bytes(existing)
        if intended == existing:
            raise ValueError("managed ignore patch did not change ineffective policy")
        if len(intended) > MAX_TRACKED_BLOB_BYTES:
            raise ValueError("intended .gitignore exceeds the bounded repair size")
        _intended_digest, intended_covered = _working_ignore_state(
            root, root_override=intended,
        )
        if not intended_covered:
            raise ValueError(
                "the root-only patch cannot effectively ignore the marker because "
                "a nested .asha/.gitignore policy overrides it"
            )
        directory_fd = _open_bound_root_directory(offer)
        temporary_name: str | None = None
        try:
            fd, temporary_name = _create_temporary_at(
                directory_fd, current_preimage.mode,
            )
            try:
                offset = 0
                while offset < len(intended):
                    written = os.write(fd, intended[offset:])
                    if written <= 0:
                        raise OSError("prerequisite temporary write made no progress")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            second_preimage, _ = _read_ignore_preimage(root)
            if second_preimage != offer.preimage:
                raise ValueError(".gitignore changed immediately before replacement")
            second_working = _working_ignore_state(root)
            if second_working != (
                offer.working_ignore_digest, offer.already_covered,
            ):
                raise ValueError("working ignore policy changed immediately before replacement")
            _second_digest, second_intended_covered = _working_ignore_state(
                root, root_override=intended,
            )
            if not second_intended_covered:
                raise ValueError(
                    "nested ignore policy changed and now overrides the intended patch"
                )
            _revalidate_offer_repository(offer)
            try:
                # From this point onward the kernel may have made the rename
                # visible even if Python observes an exception. Every outcome
                # until durable final verification is therefore indeterminate.
                os.replace(
                    temporary_name, CONTROL_IGNORE_TARGET,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                temporary_name = None
                _fsync_directory(directory_fd)
                final_preimage, final = _read_ignore_preimage(root)
                if (
                    final != intended or final_preimage.sha256 != hashlib.sha256(intended).hexdigest()
                    or final_preimage.mode != current_preimage.mode
                ):
                    raise ValueError("final .gitignore differs from intended managed patch")
                _digest, final_covered = _working_ignore_state(root)
                if not final_covered:
                    raise ValueError("patched .gitignore does not effectively ignore the marker")
            except ControlTermination as exc:
                # Preserve the exact shutdown object/signum so run_tui returns
                # 128+signal, while attaching the possibly-visible warning for
                # its terminal diagnostic.
                if getattr(exc, "detail", None) is None:
                    exc.detail = _INDETERMINATE_MESSAGE
                raise
            except Exception as exc:
                raise PrerequisiteApplyIndeterminate(
                    _INDETERMINATE_MESSAGE
                ) from exc
            except BaseException:
                # KeyboardInterrupt, SystemExit, and any other process control
                # exception retain identity and semantics. The dirfd-owned
                # finally cleanup still runs before propagation.
                raise
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)
    return (
        "Patched .gitignore; no task state was created. Commit this rule to the "
        "branch or select a commit that contains it, then retry. The previous "
        "base remains unauthorized."
    )
