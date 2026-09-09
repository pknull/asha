"""Read-only, descriptor-checked access to current and retained artifacts.

Only the legacy artifact reader accepts frozen owner-only modes. It never creates
directories, takes file-registry locks, or changes an artifact's inode or mode.
"""
from contextlib import ExitStack, contextmanager
import os
import stat

from ..store import _directory_fd, _managed_start, _validate_open_file
from .model import canonical_uuid, ModelError
from .store import StoreError


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_CLASSES = {"assignments": (".md", 32768), "outputs": (".bin", 1048576)}


class ArtifactFiles:
    def __init__(self, control_config, *, retained=False):
        self.parent = control_config.tasks_dir.parent
        self.retained = retained
        self.root = self.parent / ("initiatives" if retained else "artifacts")

    def path(self, initiative_id, directory, identity):
        try:
            canonical_uuid(initiative_id, "initiative_id")
            canonical_uuid(identity, "artifact_id")
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        if directory not in _CLASSES:
            raise StoreError("invalid artifact class")
        return self.root / initiative_id / directory / (identity + _CLASSES[directory][0])

    @contextmanager
    def directory(self, initiative_id, directory):
        self.path(initiative_id, directory, "00000000-0000-4000-8000-000000000000")
        with _directory_fd(self.parent, create=False,
                managed_start=_managed_start(self.parent, ("control",))) as parent_fd, ExitStack() as stack:
            if parent_fd is None:
                yield None
                return
            fd = parent_fd
            for name in (self.root.name, initiative_id, directory):
                try:
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    yield None
                    return
                except OSError as exc:
                    raise StoreError(f"cannot open artifact directory {name}: {exc}") from exc
                stack.callback(os.close, child)
                metadata = os.fstat(child)
                modes = {0o700, 0o500} if self.retained else {0o700}
                if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) not in modes):
                    raise StoreError(f"artifact directory ownership or mode changed: {name}")
                fd = child
            yield fd

    @contextmanager
    def _file(self, directory_fd, name, directory):
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            yield None
            return
        except OSError as exc:
            raise StoreError(f"cannot open artifact {name}: {exc}") from exc
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            allowed = {0o600, 0o400} if self.retained else {0o600}
            if mode not in allowed:
                raise StoreError(f"artifact ownership or mode changed: {name}")
            metadata = _validate_open_file(fd, "artifact", required_mode=mode)
            if metadata.st_size > _CLASSES[directory][1]:
                raise StoreError(f"artifact exceeds its byte limit: {name}")
            yield fd, metadata
        finally:
            os.close(fd)

    def inspect(self, initiative_id, directory, identity, *, read=False):
        path = self.path(initiative_id, directory, identity)
        with self.directory(initiative_id, directory) as directory_fd:
            if directory_fd is None:
                return None
            with self._file(directory_fd, path.name, directory) as opened:
                if opened is None:
                    return None
                fd, metadata = opened
                content = None
                if read:
                    remaining, chunks = metadata.st_size, []
                    while remaining:
                        chunk = os.read(fd, min(65536, remaining))
                        if not chunk:
                            raise StoreError("artifact shortened during read")
                        remaining -= len(chunk)
                        chunks.append(chunk)
                    if os.read(fd, 1):
                        raise StoreError("artifact grew during read")
                    content = b"".join(chunks)
                return {"path": path, "bytes": metadata.st_size, "content": content}

    def inventory(self, initiative_id):
        result = {name: {"bytes": 0, "inodes": 0} for name in _CLASSES}
        for directory in _CLASSES:
            with self.directory(initiative_id, directory) as directory_fd:
                if directory_fd is None:
                    continue
                for name in os.listdir(directory_fd):
                    # Residue is charged to storage, never silently swept here.
                    with self._file(directory_fd, name, directory) as opened:
                        if opened is None:
                            raise StoreError("artifact disappeared during inventory")
                        result[directory]["bytes"] += opened[1].st_size
                        result[directory]["inodes"] += 1
        return result
