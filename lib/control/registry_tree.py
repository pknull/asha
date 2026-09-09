"""Exact legacy registry snapshots and replayable permission fencing.

These internal operations do not select a backend. Activation must durably save
the returned fence entries and a preparing marker before calling apply_modes.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
from pathlib import PurePosixPath
import stat

from .store import StoreError, _directory_fd, _managed_start, _validate_open_file


ROOTS = ("tasks", "rooms", "initiatives", "transactions", "authorities", "prunes", "repository-inits")
ARTIFACT_ROOTS = ("artifacts", "materialization-ownership")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 1_000_000


def _fact(metadata):
    return {"dev": metadata.st_dev, "ino": metadata.st_ino,
            "uid": metadata.st_uid, "mode": stat.S_IMODE(metadata.st_mode)}


def _file_digest(fd):
    size, digest = 0, hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise StoreError("legacy registry file exceeds snapshot capacity")
        digest.update(chunk)
    return size, digest.hexdigest()


def _directory_metadata(fd):
    metadata = os.fstat(fd)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o700, 0o500}):
        raise StoreError("legacy registry directory ownership or mode changed")
    return metadata


@contextmanager
def _parent(config):
    root = config.tasks_dir.parent
    with _directory_fd(root, create=False,
            managed_start=_managed_start(root, ("state", "control"))) as fd:
        if fd is None:
            raise StoreError("Control root is missing")
        yield fd


def capture_tree(config, *, roots=ROOTS):
    """Capture exact owned directory/file identities and bytes, without writes."""
    entries = []
    if not roots or len(set(roots)) != len(roots) or any(root not in (*ROOTS, *ARTIFACT_ROOTS) for root in roots):
        raise StoreError("invalid registry snapshot roots")
    def visit(parent_fd, name, parts):
        if len(entries) >= MAX_ENTRIES or len(parts) > 4:
            raise StoreError("legacy registry tree exceeds snapshot capacity")
        try:
            visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if len(parts) != 1:
                raise StoreError("legacy registry entry disappeared during snapshot") from None
            entries.append({"path": name, "kind": "missing"})
            return
        directory = stat.S_ISDIR(visible.st_mode)
        if not directory and not stat.S_ISREG(visible.st_mode):
            raise StoreError("symlink or non-file rejected in legacy registry snapshot")
        try:
            fd = os.open(name, _DIR_FLAGS if directory else _FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise StoreError(f"cannot open legacy registry snapshot entry: {exc}") from exc
        try:
            metadata = _directory_metadata(fd) if directory else _validate_open_file(
                fd, "legacy registry snapshot file", required_mode=stat.S_IMODE(visible.st_mode))
            if (metadata.st_dev, metadata.st_ino) != (visible.st_dev, visible.st_ino):
                raise StoreError("legacy registry entry changed identity during snapshot")
            entry = {"path": "/".join(parts), "kind": "directory" if directory else "file", **_fact(metadata)}
            entries.append(entry)
            if directory:
                names = sorted(os.listdir(fd))
                for child in names:
                    visit(fd, child, (*parts, child))
                if sorted(os.listdir(fd)) != names:
                    raise StoreError("legacy registry directory changed during snapshot")
            else:
                entry["bytes"], entry["digest"] = _file_digest(fd)
                if entry["bytes"] != metadata.st_size:
                    raise StoreError("legacy registry file changed size during snapshot")
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _fact(current) != _fact(metadata):
                raise StoreError("legacy registry entry changed during snapshot")
        finally:
            os.close(fd)
    with _parent(config) as parent_fd:
        for root in roots:
            visit(parent_fd, root, (root,))
    return sorted(entries, key=lambda entry: entry["path"])


def validate_entries(entries):
    if not isinstance(entries, list) or not len(ROOTS) <= len(entries) <= MAX_ENTRIES:
        raise StoreError("invalid legacy registry snapshot entries")
    paths = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise StoreError("invalid legacy registry snapshot entry")
        path, kind = entry.get("path"), entry.get("kind")
        if not isinstance(path, str):
            raise StoreError("invalid legacy registry snapshot path")
        parts = PurePosixPath(path).parts
        if (not parts or parts[0] not in ROOTS or "/".join(parts) != path
                or any(part in {".", ".."} for part in parts) or "\x00" in path
                or len(parts) > 4 or path in paths):
            raise StoreError("unsafe or duplicate legacy registry snapshot path")
        fields = {"path", "kind"}
        if kind in {"file", "directory"}:
            fields |= {"dev", "ino", "uid", "mode"}
            if any(type(entry.get(key)) is not int or entry[key] < 0 for key in ("dev", "ino", "uid", "mode")):
                raise StoreError("invalid legacy registry inode facts")
            if entry["uid"] != os.geteuid() or entry["mode"] > 0o7777:
                raise StoreError("invalid legacy registry ownership facts")
            if kind == "directory" and entry["mode"] != 0o700:
                raise StoreError("original legacy registry directory must be private and writable")
        if kind == "file":
            fields |= {"bytes", "digest"}
            if (type(entry.get("bytes")) is not int or not 0 <= entry["bytes"] <= MAX_FILE_BYTES
                    or not isinstance(entry.get("digest"), str) or len(entry["digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in entry["digest"])):
                raise StoreError("invalid legacy registry file digest or size")
        elif kind == "missing":
            if len(parts) != 1:
                raise StoreError("only legacy registry roots may be absent")
        elif kind != "directory":
            raise StoreError("invalid legacy registry entry kind")
        if set(entry) != fields:
            raise StoreError("invalid legacy registry snapshot fields")
        paths[path] = entry
    for root in ROOTS:
        if root not in paths or paths[root]["kind"] not in {"directory", "missing"}:
            raise StoreError("legacy registry snapshot is missing a root")
    for path in paths:
        parent = str(PurePosixPath(path).parent)
        if parent != "." and (parent not in paths or paths[parent]["kind"] != "directory"):
            raise StoreError("legacy registry snapshot has an unbound parent")
    return paths


def frozen_mode(entry):
    if entry["kind"] == "directory":
        return 0o500
    if entry["path"].startswith("transactions/") and entry["path"].endswith(".ownership"):
        if entry["mode"] != 0o600:
            raise StoreError("ownership sidecar mode is not its original private mode")
        return 0o600
    return 0o400


def verify_tree(config, expected, *, transitional=False, frozen=False, allow_extra=False):
    expected = validate_entries(expected)
    observed = {entry["path"]: entry for entry in capture_tree(config)}
    if (not set(expected) <= set(observed) or (not allow_extra and set(observed) != set(expected))):
        raise StoreError("legacy registry tree membership changed")
    for path, original in expected.items():
        current = observed[path]
        if original["kind"] == "missing":
            if current != original:
                raise StoreError("absent legacy registry root appeared")
            continue
        mode = current.get("mode")
        allowed = {original["mode"], frozen_mode(original)} if transitional else {
            frozen_mode(original) if frozen else original["mode"]}
        if mode not in allowed or {**current, "mode": original["mode"]} != original:
            raise StoreError(f"legacy registry snapshot changed: {path}")


def prepare_missing_roots(config, expected):
    """Create empty private placeholders before the preparing transaction.

    The caller must save the returned inode facts before changing permissions.
    Failure before that transaction only leaves empty ordinary registry roots.
    """
    expected_map = validate_entries(expected)
    try:
        verify_tree(config, expected)
    except StoreError as exc:
        raise StoreError(f"source changed; create a fresh registry stage before activation: {exc}") from exc
    replacements = {}
    with _parent(config) as parent_fd:
        for root in ROOTS:
            if expected_map[root]["kind"] != "missing":
                continue
            try:
                os.mkdir(root, 0o700, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise StoreError("absent legacy registry root appeared during preparation") from exc
            fd = os.open(root, _DIR_FLAGS, dir_fd=parent_fd)
            try:
                os.fchmod(fd, 0o700)
                metadata = _directory_metadata(fd)
                if os.listdir(fd):
                    raise StoreError("new legacy registry placeholder is not empty")
                replacements[root] = {"path": root, "kind": "directory", **_fact(metadata)}
                os.fsync(fd)
                os.fsync(parent_fd)
            finally:
                os.close(fd)
    prepared = [replacements.get(entry["path"], entry) for entry in expected]
    verify_tree(config, prepared)
    return prepared


@contextmanager
def _entry_fd(parent_fd, entry):
    with ExitStack() as stack:
        fd = parent_fd
        parts = entry["path"].split("/")
        for index, name in enumerate(parts):
            directory = index < len(parts) - 1 or entry["kind"] == "directory"
            child = os.open(name, _DIR_FLAGS if directory else _FILE_FLAGS, dir_fd=fd)
            stack.callback(os.close, child)
            if directory:
                _directory_metadata(child)
            if index == len(parts) - 1:
                yield fd, child, name
            fd = child


@contextmanager
def fence_locks(config, entries):
    """Refuse a busy source; never block while holding a partial lock set."""
    validate_entries(entries)
    with _parent(config) as parent_fd, ExitStack() as stack:
        held = set()
        def acquire(more):
            validate_entries(more)
            for entry in more:
                lock_entry(entry)
        def lock_entry(entry):
            if entry["kind"] == "missing":
                return
            if entry["kind"] != "directory" and not entry["path"].endswith(".lock"):
                return
            identity = entry["dev"], entry["ino"]
            if identity in held:
                return
            _, fd, _ = stack.enter_context(_entry_fd(parent_fd, entry))
            if _fact(os.fstat(fd)) != {key: entry[key] for key in ("dev", "ino", "uid", "mode")}:
                # Recovery may encounter files/directories already frozen.
                actual = _fact(os.fstat(fd))
                if actual != {key: frozen_mode(entry) if key == "mode" else entry[key]
                              for key in ("dev", "ino", "uid", "mode")}:
                    raise StoreError("legacy registry lock identity changed")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StoreError("legacy registry writer is still active; quiesce before activation") from exc
            held.add(identity)
        acquire(entries)
        yield acquire


def apply_modes(config, entries, *, frozen, after_change=None, allow_extra=False):
    """Replay freeze or thaw using durably recorded, exact original inode facts."""
    if frozen and allow_extra:
        raise StoreError("activation cannot accept extra source files")
    validate_entries(entries)
    if any(entry["kind"] == "missing" for entry in entries):
        raise StoreError("prepare all missing legacy roots before permission fencing")
    verify_tree(config, entries, transitional=True, allow_extra=allow_extra)
    ordered = sorted(entries, key=lambda entry: (entry["path"].count("/"), entry["path"]), reverse=frozen)
    with _parent(config) as parent_fd:
        for entry in ordered:
            with _entry_fd(parent_fd, entry) as (directory_fd, fd, name):
                actual = _fact(os.fstat(fd))
                if ({**actual, "mode": entry["mode"]} != {key: entry[key] for key in ("dev", "ino", "uid", "mode")}
                        or actual["mode"] not in {entry["mode"], frozen_mode(entry)}):
                    raise StoreError("legacy registry inode changed before permission update")
                if entry["kind"] == "file" and _file_digest(fd) != (entry["bytes"], entry["digest"]):
                    raise StoreError("legacy registry bytes changed before permission update")
                os.fchmod(fd, frozen_mode(entry) if frozen else entry["mode"])
                os.fsync(fd)
                os.fsync(directory_fd)
                visible = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (visible.st_dev, visible.st_ino) != (entry["dev"], entry["ino"]):
                    raise StoreError("legacy registry inode changed during permission update")
            if after_change is not None:
                after_change(entry["path"])
    verify_tree(config, entries, frozen=frozen, allow_extra=allow_extra)
