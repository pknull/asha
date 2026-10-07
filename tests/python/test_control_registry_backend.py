import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid

from lib.control.config import load_config
from lib.control.database import ControlDatabase, DATABASE_NAME
from lib.control.record_registry import RecordRegistry
from lib.control.registry_backend import BACKEND_CONTRACT, selected_backend
from lib.control.rooms import RoomStore
from lib.control.sqlite_rooms import SQLiteRoomStore
from lib.control.store import StoreError


def install_marker_fixture(config, **overrides):
    """Selection fixture only; does not test or perform production activation."""
    value = {"contract": BACKEND_CONTRACT, "backend": "sqlite", "state": "active",
        "activation_id": str(uuid.uuid4()), "source_root": str(config.asha_home),
        "stage_digest": "a" * 64, "activated_at": "2026-09-08T12:00:00Z", **overrides}
    with ControlDatabase(config, create=True) as db, db.transaction(write=True) as c:
        RecordRegistry("registry-backend", scope="control").put(c, "active", json.dumps(value).encode())
    return value


class RegistryBackendTests(unittest.TestCase):
    """Rooms are the registry left on the selectable backend after L-b."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        home = root / "home"
        home.mkdir()
        self.config = load_config({
            "HOME": str(home), "ASHA_CONFIG": str(root / "missing.json"),
            "ASHA_HOME": str(root / "asha"), "XDG_RUNTIME_DIR": str(root / "runtime"),
        })

    def test_missing_database_and_unselected_database_keep_file_stores(self):
        for create in (False, True):
            if create:
                with ControlDatabase(self.config, create=True):
                    pass
            self.assertIs(type(RoomStore(self.config)), RoomStore)
            self.assertEqual((self.config.tasks_dir.parent / DATABASE_NAME).exists(), create)

    def test_the_room_constructor_selects_sqlite_and_runs_its_subclass_initializer(self):
        install_marker_fixture(self.config)
        instance = RoomStore(self.config)
        self.assertIs(type(instance), SQLiteRoomStore)
        self.assertIs(instance.__class__, SQLiteRoomStore)

    def test_small_room_configuration_uses_the_same_backend_root(self):
        install_marker_fixture(self.config)
        small = SimpleNamespace(asha_home=self.config.asha_home)
        room = RoomStore(small)
        self.assertIs(type(room), SQLiteRoomStore)
        self.assertEqual(room.config.tasks_dir, self.config.tasks_dir)

    def test_incomplete_transition_and_offline_stage_refuse_construction(self):
        with ControlDatabase(self.config, create=True) as db, db.transaction(write=True) as c:
            RecordRegistry("registry-backend", scope="control").put(c, "transition", b'{"state":"preparing"}')
        with self.assertRaisesRegex(StoreError, "incomplete"):
            RoomStore(self.config)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-backend'")
            RecordRegistry("registry-migration", scope="control").put(c, "attempt", b'{"state":"incomplete"}')
        with self.assertRaisesRegex(StoreError, "offline"):
            RoomStore(self.config)

    def test_foreign_or_malformed_marker_never_falls_back_to_files(self):
        install_marker_fixture(self.config, source_root="/tmp/foreign-control-root")
        with self.assertRaisesRegex(StoreError, "another root"):
            RoomStore(self.config)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-backend'")
            RecordRegistry("registry-backend", scope="control").put(c, "active", b'{"state":"active"}')
        with self.assertRaises(StoreError):
            selected_backend(self.config)


if __name__ == "__main__":
    unittest.main()
