"""SQLite implementation of the Room registry; activation is owned by migration."""
from __future__ import annotations

from contextlib import contextmanager
import copy

from .database import ControlDatabase
from .model import canonical_uuid
from .record_registry import RecordRegistry
from .registry_guards import mutation_guard
from .rooms import RoomError, RoomStore
from .store import StoreError, _directory_fd, _managed_start, _registry_lock


class SQLiteRoomStore(RoomStore):
    def __init__(self, config):
        from .registry_backend import control_config
        config = control_config(config)
        super().__init__(config)
        self.config = config
        self.records = RecordRegistry("rooms")
        self.lock_root = config.tasks_dir.parent / "registry-locks" / "rooms"

    @contextmanager
    def transaction(self, *, create):
        # Lifecycle methods may call tmux between saves. Serialize those methods
        # with a domain lock; each SQLite write remains a separate short commit.
        try:
            with mutation_guard(self.config), _directory_fd(self.lock_root, create=True,
                               managed_start=_managed_start(self.lock_root, ("control", "registry-locks", "rooms"))) as fd:
                with _registry_lock(fd):
                    yield
        except StoreError as exc:
            raise RoomError(str(exc)) from exc

    def _record(self, c, room_id):
        self._path(room_id)
        row = self.records.read(c, room_id)
        if row is None:
            raise RoomError("room was not found")
        if len(row["raw"]) > 64 * 1024:
            raise RoomError("room record exceeds the bounded size")
        value = self._validate(row["value"])
        if value["room_id"] != room_id:
            raise RoomError("room key and record identity differ")
        return row

    def create(self, record):
        value = self._validate(copy.deepcopy(dict(record)))
        raw = self._raw(value)
        if len(raw) > 64 * 1024:
            raise RoomError("room record exceeds the bounded size")
        with self.transaction(create=True), ControlDatabase(self.config) as db:
            with db.transaction(write=True) as c:
                # Unicode casefold matches the established Room name contract.
                # The lifecycle lock and write transaction cover uniqueness and
                # publication together, including competing controller processes.
                for row in c.execute("SELECT record_key FROM records WHERE domain='rooms' AND scope='registry'"):
                    if self._record(c, row[0])["value"]["name"].casefold() == value["name"].casefold():
                        raise RoomError(f"room name {value['name']!r} already exists")
                self.records.put(c, value["room_id"], raw,
                                 state=value["lifecycle"], updated_at=value["updated_at"])
        return value

    def save(self, record, *, expected_digest=None):
        value = self._validate(copy.deepcopy(dict(record)))
        raw = self._raw(value)
        if len(raw) > 64 * 1024:
            raise RoomError("room record exceeds the bounded size")
        with self.transaction(create=False), ControlDatabase(self.config) as db:
            with db.transaction(write=True) as c:
                previous = self._record(c, value["room_id"])
                if expected_digest is not None and expected_digest != previous["digest"]:
                    raise RoomError("room record changed concurrently; stale save refused")
                self.records.put(c, value["room_id"], raw, expected_digest=previous["digest"],
                                 state=value["lifecycle"], updated_at=value["updated_at"])
        return value

    def read(self, room_id):
        try:
            with ControlDatabase(self.config) as db, db.transaction() as c:
                return self._record(c, room_id)["value"]
        except StoreError as exc:
            raise RoomError(str(exc)) from exc

    def list(self):
        result, after = [], ""
        try:
            with ControlDatabase(self.config) as db:
                while True:
                    with db.transaction() as c:
                        keys = self.records.keys(c, after=after)
                        result.extend(self._record(c, key)["value"] for key in keys)
                    if len(keys) < 100:
                        return result
                    after = keys[-1]
        except StoreError as exc:
            raise RoomError(str(exc)) from exc

    def bounded_snapshots(self, budget):
        return self._bounded_snapshots(budget)

    def bounded_active_snapshots(self, budget):
        return self._bounded_snapshots(budget, states=("open", "creating"))

    def _bounded_snapshots(self, budget, *, states=None):
        result = []
        try:
            with ControlDatabase(self.config) as db, db.transaction() as c:
                cap = min(1000, max(1, budget.limit - budget.scanned))
                keys = (self.records.keys(c, limit=cap) if states is None
                        else self.records.active_root_keys(c, states, limit=cap))
                for key in keys:
                    if not budget.ready():
                        break
                    budget.scanned += 1
                    try:
                        value = self._record(c, canonical_uuid(key))["value"]
                        if states is not None and value["lifecycle"] not in states:
                            raise StoreError("Room lifecycle disagrees with its activity index")
                        result.append(value)
                    except (ValueError, StoreError):
                        budget.unavailable += 1
                if len(keys) == cap:
                    budget.truncated = True
        except (OSError, StoreError):
            budget.unavailable += 1
        return result
