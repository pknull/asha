from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import stat
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path
from typing import Any

from lib.control.config import load_config
from lib.control.database import (
    APPLICATION_ID, DATABASE_NAME, ControlDatabase, DatabaseBusyError,
    DatabaseError, SCHEMA_VERSION,
)
from lib.control.session_store import SessionStore, digest
from lib.control.store import StoreError


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class ControlDatabaseTests(unittest.TestCase):
    def test_connection_inspection_preserves_an_existing_reader_lock(self):
        def inspect():
            with ControlDatabase(self.config):
                pass
        self._assert_reader_lock_preserved(inspect)

    def test_restore_source_inspection_preserves_an_existing_reader_lock(self):
        target = load_config({"HOME": str(self.home), "ASHA_HOME": str(self.root / "restored")})
        self._assert_reader_lock_preserved(lambda: ControlDatabase.restore(target, self.path))

    def _assert_reader_lock_preserved(self, inspect):
        with ControlDatabase(self.config, create=True) as first:
            with first.transaction(write=True) as c:
                c.execute("CREATE TABLE lock_probe(value INTEGER)")
                c.execute("INSERT INTO lock_probe VALUES(1)")
            with first.transaction() as reader:
                self.assertEqual(reader.execute("SELECT value FROM lock_probe").fetchone()[0], 1)
                # A second handle must not drop the first handle's POSIX locks.
                inspect()
                child = subprocess.run([sys.executable, "-c", '''import sqlite3,sys,json
c=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=0.1)
c.execute('UPDATE lock_probe SET value=2')
print(json.dumps(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()))
c.close()
''', str(self.path)], capture_output=True, text=True, timeout=5)
                self.assertEqual(child.returncode, 0, child.stderr)
                import json
                checkpoint = json.loads(child.stdout)
                self.assertEqual(checkpoint[0], 1, "checkpoint bypassed an active SQLite reader")
                self.assertEqual(reader.execute("SELECT value FROM lock_probe").fetchone()[0], 1)


    def _restored_config(self):
        backup = self.root / "snapshot.sqlite3"
        with ControlDatabase(self.config, create=True) as db:
            db.backup(backup)
        target = load_config({"HOME": str(self.home), "ASHA_HOME": str(self.root / "restored")})
        ControlDatabase.restore(target, backup)
        return target

    def test_cold_restored_root_can_be_inspected_without_changing_its_journal(self):
        target = self._restored_config()
        for options in ({"read_only": True}, {"allow_legacy_reads": True}):
            with ControlDatabase(target, **options) as db:
                self.assertEqual(db.health()["journal_mode"], "delete")
                with self.assertRaisesRegex(DatabaseError, "read-only"):
                    with db.transaction(write=True):
                        pass

    def test_backup_cannot_create_a_database_at_its_own_reserved_sidecar_path(self):
        target = self._restored_config()
        with ControlDatabase(target) as db:
            wal = db.path.with_name(DATABASE_NAME + "-wal")
            self.assertFalse(wal.exists())
            for suffix in ("-wal", "-shm", "-journal"):
                with self.assertRaisesRegex(DatabaseError, "reserved"):
                    db.backup(db.path.with_name(DATABASE_NAME + suffix))
            self.assertFalse(wal.exists())

    def test_process_death_during_restore_publication_recovers_in_a_fresh_root(self):
        backup = self.root / "snapshot.sqlite3"
        with ControlDatabase(self.config, create=True) as db:
            db.put("fixture", "project", "one", {"text": "retained"})
            db.backup(backup)
        original = backup.read_bytes()
        interrupted = load_config({"HOME": str(self.home), "ASHA_HOME": str(self.root / "interrupted")})
        child = subprocess.run([sys.executable, "-c", '''import os,sys
from unittest.mock import patch
from lib.control.config import load_config
from lib.control.database import ControlDatabase
from pathlib import Path
link=os.link
def publish_then_die(*args,**kwargs):
    link(*args,**kwargs)
    os._exit(77)
config=load_config({"HOME":sys.argv[1],"ASHA_HOME":sys.argv[2]})
with patch("lib.control.database.os.link",side_effect=publish_then_die):
    ControlDatabase.restore(config,Path(sys.argv[3]))
''', str(self.home), str(interrupted.asha_home), str(backup)],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 77, child.stderr)
        with self.assertRaises(DatabaseError):
            ControlDatabase(interrupted, read_only=True)
        fresh = load_config({"HOME": str(self.home), "ASHA_HOME": str(self.root / "recovered")})
        ControlDatabase.restore(fresh, backup)
        with ControlDatabase(fresh, read_only=True) as db:
            self.assertEqual(db.get("fixture", "project", "one"), {"text": "retained"})
            with db.transaction() as c:
                self.assertEqual(c.execute("SELECT mode FROM control_runtime").fetchone()[0], "paused")
        self.assertEqual(backup.read_bytes(), original)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        self.control = self.config.asha_home / "state/control"
        self.path = self.control / DATABASE_NAME

    def tearDown(self) -> None:
        self.temp.cleanup()

    def raw(self, timeout: float = 0.1) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None)
        self.addCleanup(connection.close)
        return connection

    # -- layout and identity ------------------------------------------------

    def test_read_only_snapshot_sees_committed_wal_and_refuses_write_and_create(self):
        with ControlDatabase(self.config, create=True) as writer:
            writer.put("fixture", "project", "one", {"text": "committed in WAL"})
            with ControlDatabase(self.config, read_only=True) as reader:
                self.assertEqual(reader.get("fixture", "project", "one"), {"text": "committed in WAL"})
                with self.assertRaisesRegex(DatabaseError, "read-only"):
                    reader.put("fixture", "project", "two", {"text": "refused"})
                self.assertIsNone(writer.get("fixture", "project", "two"))
        with self.assertRaisesRegex(DatabaseError, "cannot create"):
            ControlDatabase(self.config, read_only=True, create=True)

    def test_v4_scoped_activity_upgrade_is_atomic_and_preserves_record_bytes(self):
        with patch.object(ControlDatabase, "_upgrade_v5", return_value=None):
            with ControlDatabase(self.config, create=True) as db:
                db.put("fixture", "scope", "retained", {"text": "café"})
        before = self.raw().execute("SELECT * FROM records").fetchall()
        with self.assertRaisesRegex(DatabaseError, "explicit schema migration"):
            ControlDatabase(self.config)
        original = ControlDatabase._upgrade_v5
        def interrupted(c):
            original(c)
            raise RuntimeError("interrupted scoped index publication")
        with patch.object(ControlDatabase, "_upgrade_v5", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                ControlDatabase(self.config, migrate=True)
        raw = self.raw()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='records_scope_state'").fetchone())
        self.assertEqual(raw.execute("SELECT * FROM records").fetchall(), before)
        with ControlDatabase(self.config, migrate=True) as db:
            self.assertEqual(db.health()["schema_version"], SCHEMA_VERSION)
            with db.transaction() as c:
                self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM records")], before)
                self.assertEqual([r[2] for r in c.execute("PRAGMA index_info(records_scope_state)")],
                                 ["domain", "scope", "state", "updated_at", "record_key"])

    def test_health_refuses_missing_or_misdefined_scoped_activity_index(self):
        with ControlDatabase(self.config, create=True) as db:
            with db.transaction(write=True) as c:
                c.execute("DROP INDEX records_scope_state")
            with self.assertRaisesRegex(DatabaseError, "scoped activity index"):
                db.health()
            with db.transaction(write=True) as c:
                c.execute("CREATE INDEX records_scope_state ON records(domain,state)")
            with self.assertRaisesRegex(DatabaseError, "scoped activity index"):
                db.health()

    def test_health_refuses_partial_unique_or_collated_activity_indexes(self):
        with ControlDatabase(self.config, create=True) as db:
            definitions = (
                "CREATE INDEX records_scope_state ON records(domain,scope,state,updated_at,record_key) WHERE state='requested'",
                "CREATE UNIQUE INDEX records_scope_state ON records(domain,scope,state,updated_at,record_key)",
                "CREATE INDEX records_scope_state ON records(domain,scope,state COLLATE NOCASE,updated_at,record_key)",
            )
            for definition in definitions:
                with self.subTest(definition=definition):
                    with db.transaction(write=True) as c:
                        c.execute("DROP INDEX records_scope_state")
                        c.execute(definition)
                    with self.assertRaisesRegex(DatabaseError, "scoped activity index"):
                        db.health()

    def test_v3_event_index_upgrade_is_atomic_and_preserves_record_bytes(self):
        with patch.object(ControlDatabase, "_upgrade_v4", return_value=None), \
             patch.object(ControlDatabase, "_upgrade_v5", return_value=None):
            with ControlDatabase(self.config, create=True) as db:
                db.put("initiative.events", "retained-initiative", "000001-11111111-1111-4111-8111-111111111111.json", {"text": "retained é"})
        before = self.raw().execute("SELECT * FROM records").fetchall()
        original = ControlDatabase._upgrade_v4
        def interrupted(c):
            original(c)
            raise RuntimeError("interrupted event index publication")
        with patch.object(ControlDatabase, "_upgrade_v4", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                ControlDatabase(self.config, migrate=True)
        raw = self.raw()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='initiative_event_sequences'").fetchone())
        self.assertEqual(raw.execute("SELECT * FROM records").fetchall(), before)
        with ControlDatabase(self.config, migrate=True) as upgraded, upgraded.transaction() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in c.execute("SELECT * FROM records")], before)

    def test_v3_duplicate_event_sequences_refuse_upgrade_without_publication(self):
        with patch.object(ControlDatabase, "_upgrade_v4", return_value=None), \
             patch.object(ControlDatabase, "_upgrade_v5", return_value=None):
            with ControlDatabase(self.config, create=True) as db:
                for key in ("000001-11111111-1111-4111-8111-111111111111.json", "000001-22222222-2222-4222-8222-222222222222.json"):
                    db.put("initiative.events", "retained-initiative", key, {"sequence": 1})
        with self.assertRaises(DatabaseError):
            ControlDatabase(self.config, migrate=True)
        raw = self.raw()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(raw.execute("SELECT count(*) FROM records").fetchone()[0], 2)
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='initiative_event_sequences'").fetchone())

    def test_health_refuses_missing_event_sequence_index(self):
        with ControlDatabase(self.config, create=True) as db:
            with db.transaction(write=True) as c:
                c.execute("DROP INDEX initiative_event_sequences")
            with self.assertRaisesRegex(DatabaseError, "event sequence index"):
                db.health()

    def test_v2_sessions_upgrade_adds_native_permissions_and_retains_questions(self):
        with SessionStore(self.config, create=True) as store:
            sid = store.create(cwd=str(self.root), prompt="Review")["session_id"]
            generation = store.claim_owner(sid)["generation"]
            turn = store.claim_turn(sid, generation)["turn_id"]
            request = store.request(sid, turn, "Which chapter?", request_id=str(uuid.uuid4()))
            with store.db.transaction(write=True) as c:
                c.execute("DROP TABLE session_native_requests")
                c.execute("DROP INDEX records_scope_state")
                c.execute("PRAGMA user_version=2")
        with self.assertRaises(DatabaseError):
            SessionStore(self.config)
        with ControlDatabase(self.config, migrate=True) as db:
            with db.transaction() as c:
                self.assertIsNotNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='session_native_requests'").fetchone())
                self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        with SessionStore(self.config) as store:
            self.assertEqual(store.snapshot(sid)["requests"][0]["question"], "Which chapter?")

    def test_doctor_refuses_missing_native_permission_storage(self):
        with SessionStore(self.config, create=True) as store:
            with store.db.transaction(write=True) as c:
                c.execute("DROP TABLE session_native_requests")
            with self.assertRaisesRegex(DatabaseError, "health probe"):
                store.db.health()

    def test_create_establishes_private_layout_and_durable_settings(self) -> None:
        with ControlDatabase(self.config, create=True) as db:
            self.assertEqual(db.path, self.path)
            self.assertEqual(_mode(self.config.asha_home / "state"), 0o700)
            self.assertEqual(_mode(self.control), 0o700)
            self.assertEqual(_mode(self.path), 0o600)
            health = db.health()
        self.assertEqual(health["journal_mode"], "wal")
        self.assertEqual(health["synchronous"], 2)
        self.assertEqual(health["foreign_keys"], 1)
        self.assertEqual(health["integrity"], "ok")
        self.assertEqual(health["path"], str(self.path))
        stamped = self.raw().execute("PRAGMA application_id").fetchone()[0]
        self.assertEqual(stamped, APPLICATION_ID)
        # A second open sees the persisted WAL header without re-creating anything.
        with ControlDatabase(self.config) as again:
            self.assertEqual(again.health()["journal_mode"], "wal")

    def test_open_without_create_requires_an_existing_database(self) -> None:
        with self.assertRaises(DatabaseError) as missing:
            ControlDatabase(self.config)
        self.assertIn("does not exist", str(missing.exception))
        self.assertFalse(self.control.exists())
        ControlDatabase(self.config, create=True).close()
        os.truncate(self.path, 0)  # residue of an interrupted create
        with self.assertRaises(DatabaseError) as empty:
            ControlDatabase(self.config)
        self.assertIn("empty", str(empty.exception))
        self.assertEqual(self.path.stat().st_size, 0, "a refused open writes nothing")
        ControlDatabase(self.config, create=True).close()
        ControlDatabase(self.config).close()

    def test_refuses_a_foreign_sqlite_file_without_touching_it(self) -> None:
        ControlDatabase(self.config, create=True).close()
        self.path.unlink()
        foreign = sqlite3.connect(str(self.path))
        foreign.execute("CREATE TABLE stranger(x)")
        foreign.commit()
        foreign.close()
        os.chmod(self.path, 0o600)
        for create in (False, True):
            with self.assertRaises(DatabaseError) as refused:
                ControlDatabase(self.config, create=create)
            self.assertIn("not an Asha Control database", str(refused.exception))
        probe = self.raw()
        self.assertEqual(probe.execute("PRAGMA application_id").fetchone()[0], 0)
        self.assertEqual(probe.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_refuses_wrong_mode_and_symlinked_files(self) -> None:
        ControlDatabase(self.config, create=True).close()
        os.chmod(self.path, 0o644)
        with self.assertRaises(StoreError) as mode:
            ControlDatabase(self.config, create=True)
        self.assertIn("0600", str(mode.exception))
        os.chmod(self.path, 0o600)
        real = self.control / "real.sqlite3"
        self.path.rename(real)
        self.path.symlink_to(real)
        with self.assertRaises(StoreError) as linked:
            ControlDatabase(self.config, create=True)
        self.assertIn("symlink", str(linked.exception))
        self.path.unlink()
        real.rename(self.path)
        sidecar = self.control / (DATABASE_NAME + "-wal")
        sidecar.symlink_to(self.control / "elsewhere")
        with self.assertRaises(StoreError) as tampered:
            ControlDatabase(self.config)
        self.assertIn("symlink", str(tampered.exception))
        sidecar.unlink()
        ControlDatabase(self.config).close()

    def test_rejects_unbounded_busy_timeout(self) -> None:
        invalid: tuple[Any, ...] = (0, -1, 61, True, "5")
        for value in invalid:
            with self.assertRaises(DatabaseError):
                ControlDatabase(self.config, create=True, busy_timeout=value)

    # -- transactions -----------------------------------------------------

    def test_write_transaction_commits_and_failure_rolls_back(self) -> None:
        db = ControlDatabase(self.config, create=True)
        self.addCleanup(db.close)
        with db.transaction(write=True) as tx:
            tx.execute("CREATE TABLE items(name TEXT PRIMARY KEY, size INTEGER)")
            tx.execute("INSERT INTO items VALUES(?,?)", ("kept", 1))
        with self.assertRaises(RuntimeError):
            with db.transaction(write=True) as tx:
                tx.execute("INSERT INTO items VALUES(?,?)", ("lost", 2))
                raise RuntimeError("abandon")
        with db.transaction() as tx:
            rows = [dict(row) for row in tx.execute("SELECT * FROM items ORDER BY name")]
        self.assertEqual(rows, [{"name": "kept", "size": 1}])
        self.assertEqual(rows[0]["name"], "kept")
        seen = self.raw().execute("SELECT name FROM items").fetchall()
        self.assertEqual(seen, [("kept",)])

    def test_statement_errors_roll_back_and_surface_as_store_errors(self) -> None:
        db = ControlDatabase(self.config, create=True)
        self.addCleanup(db.close)
        with db.transaction(write=True) as tx:
            tx.execute("CREATE TABLE items(name TEXT PRIMARY KEY)")
        with self.assertRaises(DatabaseError) as duplicate:
            with db.transaction(write=True) as tx:
                tx.execute("INSERT INTO items VALUES('a')")
                tx.execute("INSERT INTO items VALUES('a')")
        self.assertIsInstance(duplicate.exception, StoreError)
        self.assertIn("UNIQUE", str(duplicate.exception))
        with db.transaction() as tx:
            self.assertEqual(tx.execute("SELECT count(*) FROM items").fetchone()[0], 0)

    def test_read_transaction_refuses_writes(self) -> None:
        db = ControlDatabase(self.config, create=True)
        self.addCleanup(db.close)
        with db.transaction(write=True) as tx:
            tx.execute("CREATE TABLE items(name TEXT)")
        with self.assertRaises(DatabaseError) as refused:
            with db.transaction() as tx:
                tx.execute("INSERT INTO items VALUES('sneaky')")
        self.assertIn("readonly", str(refused.exception))
        with db.transaction(write=True) as tx:
            tx.execute("INSERT INTO items VALUES('allowed')")
        with db.transaction() as tx:
            names = [row[0] for row in tx.execute("SELECT name FROM items")]
        self.assertEqual(names, ["allowed"])

    def test_nested_transactions_and_stale_handles_are_refused(self) -> None:
        db = ControlDatabase(self.config, create=True)
        self.addCleanup(db.close)
        with db.transaction(write=True) as outer:
            outer.execute("CREATE TABLE items(name TEXT)")
            with self.assertRaises(DatabaseError) as nested:
                with db.transaction():
                    pass
            self.assertIn("already active", str(nested.exception))
            outer.execute("INSERT INTO items VALUES('still committed')")
        with self.assertRaises(DatabaseError) as stale:
            outer.execute("SELECT 1")
        self.assertIn("ended", str(stale.exception))
        with db.transaction() as tx:
            self.assertEqual(tx.execute("SELECT count(*) FROM items").fetchone()[0], 1)

    def test_foreign_keys_are_enforced_on_every_connection(self) -> None:
        with ControlDatabase(self.config, create=True) as db:
            with db.transaction(write=True) as tx:
                tx.execute("CREATE TABLE parents(id TEXT PRIMARY KEY)")
                tx.execute("CREATE TABLE children(id TEXT PRIMARY KEY, parent TEXT NOT NULL REFERENCES parents(id))")
                tx.execute("INSERT INTO parents VALUES('p')")
        with ControlDatabase(self.config) as db:
            with self.assertRaises(DatabaseError) as orphan:
                with db.transaction(write=True) as tx:
                    tx.execute("INSERT INTO parents VALUES('q')")
                    tx.execute("INSERT INTO children VALUES('c', 'missing')")
            self.assertIn("FOREIGN KEY", str(orphan.exception))
            with db.transaction() as tx:
                self.assertEqual(tx.execute("SELECT count(*) FROM parents").fetchone()[0], 1)
                self.assertEqual(tx.execute("SELECT count(*) FROM children").fetchone()[0], 0)

    def test_lock_contention_is_bounded_explicit_and_read_tolerant(self) -> None:
        db = ControlDatabase(self.config, create=True, busy_timeout=0.2)
        self.addCleanup(db.close)
        with db.transaction(write=True) as tx:
            tx.execute("CREATE TABLE items(x INTEGER)")
        holder = self.raw()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO items VALUES(1)")
        started = time.monotonic()
        with self.assertRaises(DatabaseBusyError) as busy:
            with db.transaction(write=True):
                pass
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIsInstance(busy.exception, StoreError)
        with db.transaction() as tx:
            self.assertEqual(tx.execute("SELECT count(*) FROM items").fetchone()[0], 0)
        holder.execute("COMMIT")
        with db.transaction(write=True) as tx:
            tx.execute("INSERT INTO items VALUES(2)")
        with db.transaction() as tx:
            self.assertEqual(tx.execute("SELECT count(*) FROM items").fetchone()[0], 2)

    def test_wal_sidecars_inherit_the_private_mode(self) -> None:
        db = ControlDatabase(self.config, create=True)
        self.addCleanup(db.close)
        with db.transaction(write=True) as tx:
            tx.execute("CREATE TABLE items(x INTEGER)")
        for suffix in ("-wal", "-shm"):
            sidecar = self.control / (DATABASE_NAME + suffix)
            self.assertTrue(sidecar.exists(), suffix)
            self.assertEqual(_mode(sidecar), 0o600, suffix)

    def test_closed_database_refuses_use_and_close_is_idempotent(self) -> None:
        db = ControlDatabase(self.config, create=True)
        db.close()
        db.close()
        with self.assertRaises(DatabaseError) as closed:
            with db.transaction():
                pass
        self.assertIn("closed", str(closed.exception))
        with self.assertRaises(DatabaseError):
            db.health()

    # -- the session domain through the real database ----------------------

    def test_session_store_round_trip_persists_through_the_database(self) -> None:
        cwd = str(self.root)
        with SessionStore(self.config, create=True) as store:
            session = store.create(cwd=cwd, prompt="Investigate the flaky test.", max_turns=3)
            sid = session["session_id"]
            self.assertEqual(session["state"], "queued")
            owner = store.claim_owner(sid)
            generation = owner["generation"]
            self.assertEqual(generation, 1)
            opening = store.claim_turn(sid, generation)
            assert opening is not None, "the opening prompt is claimable"
            self.assertIsNone(store.claim_turn(sid, generation), "a running turn is not claimed twice")
            turn = opening["turn_id"]
            store.observe(sid, generation, turn, "initialized", {"native_id": "native-1"})
            with self.assertRaises(StoreError):
                store.observe(sid, generation, turn, "consumed", {"digest": digest("other")})
            store.observe(sid, generation, turn, "consumed", {"digest": opening["digest"]})
            with self.assertRaises(StoreError):
                store.observe(sid, generation + 1, turn, "text", {"text": "stale owner"})
            request_id = str(uuid.uuid4())
            store.request(sid, turn, "Which branch?", request_id=request_id)
            store.finish(sid, generation, turn, success=True)
            self.assertEqual(store.get(sid)["state"], "waiting-input")
            self.assertIsNone(store.claim_turn(sid, generation), "pending question blocks turns")
            with self.assertRaises(StoreError):
                store.answer(request_id, "master", expected_digest=digest("Which branch?!"))
            store.answer(request_id, "master", expected_digest=digest("Which branch?"))
            self.assertEqual(store.get(sid)["state"], "idle")
            follow_up = store.claim_turn(sid, generation)
            assert follow_up is not None, "the recorded answer is claimable"
            self.assertIn("master", follow_up["body"])
            store.finish(sid, generation, follow_up["turn_id"], success=True, reason="done")
            store.stop(sid)
            self.assertIsNone(store.claim_turn(sid, generation))
            store.stopped(sid, generation)
        with SessionStore(self.config) as store:
            self.assertEqual(store.get(sid)["state"], "stopped")
            self.assertEqual(store.get(sid)["native_id"], "native-1")
            snapshot = store.snapshot(sid)
            self.assertEqual(snapshot["counts"], {"stopped": 1})
            self.assertEqual(snapshot["requests"], [])
            kinds = [event["kind"] for event in snapshot["events"]]
            self.assertIn("session-created", kinds[:2])
            self.assertLess(kinds.index("session-created"), kinds.index("owner-claimed"))
            self.assertEqual(kinds[-1], "session-stopped")
            sequences = [event["sequence"] for event in snapshot["events"]]
            self.assertEqual(sequences, sorted(sequences))
            self.assertEqual(snapshot["next_event_cursor"], sequences[-1])
            with store.db.transaction() as tx:
                states = {
                    row["delivery_key"]: row["state"]
                    for row in tx.execute("SELECT delivery_key, state FROM session_messages")
                }
            # Completion is not consumption: only the opening message carried evidence.
            self.assertEqual(states, {"opening": "consumed", "answer:" + request_id: "submitted"})


class RecordDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_config({"ASHA_HOME": str(self.root / "asha"), "HOME": str(self.root)})
        self.db = ControlDatabase(self.config, create=True)
        self.addCleanup(self.db.close)

    def test_cas_replay_and_keyset_queries(self):
        first = self.db.put("test", "project", "one", {"state": "queued", "text": "Hello"})
        self.assertEqual(first, self.db.put("test", "project", "one", {"text": "Hello", "state": "queued"}))
        with ControlDatabase(self.config) as other:
            other.put("test", "project", "one", {"state": "running"}, expected_digest=first)
        with self.assertRaises(DatabaseError):
            self.db.put("test", "project", "one", {"state": "stale"}, expected_digest=first)
        self.db.put("test", "project", "two", {"state": "queued", "text": "100% literal"})
        self.assertEqual(self.db.list("test", state="queued"), [{"state": "queued", "text": "100% literal"}])
        self.assertEqual(self.db.list("test", after=("project", "one")), self.db.search("100%"))
        self.assertEqual(self.db.search("_"), [])

    def test_future_schema_refused_before_any_write(self):
        with self.db.transaction(write=True) as c:
            c.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(DatabaseError, "schema version"):
            ControlDatabase(self.config)

    def test_generic_database_does_not_wedge_session_observation(self):
        from lib.control.sessions import overview, ensure_owners, refuse_managed_operator
        self.assertFalse(overview(self.config)["initialized"])
        self.assertEqual(ensure_owners(self.config)["owners_started"], 0)
        refuse_managed_operator(self.config, {})
        with SessionStore(self.config, create=True) as store:
            self.assertEqual(store.snapshot()["sessions"], [])

    def test_doctor_reports_health_and_refuses_future_schema(self):
        from lib.control.doctor import _managed_sessions_probe
        self.assertEqual(_managed_sessions_probe(self.config).outcome, "match")
        with self.db.transaction(write=True) as c:
            c.execute("PRAGMA user_version=999")
        self.assertEqual(_managed_sessions_probe(self.config).outcome, "mismatch")

    def test_live_wal_backup_preserves_committed_data_and_refuses_overwrite(self):
        self.db.put("test", "p", "one", {"state": "ready"})
        destination = self.root / "backup.sqlite3"
        self.db.backup(destination)
        with sqlite3.connect(destination) as c:
            self.assertEqual(c.execute("SELECT state FROM records").fetchone()[0], "ready")
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(_mode(destination), 0o600)
        with self.assertRaises(DatabaseError):
            self.db.backup(destination)

    def test_full_text_search_updates_rebuilds_and_uses_index(self):
        first = self.db.put("notes", "p", "one", {"text": "café manuscript"})
        self.assertEqual(self.db.search("café"), [{"text": "café manuscript"}])
        self.db.put("notes", "p", "one", {"text": "revision complete"}, expected_digest=first)
        self.assertEqual(self.db.search("manuscript"), [])
        self.assertEqual(self.db.search('revision OR missing'), [])
        self.assertEqual(len(self.db.search("revision", domain="notes")), 1)
        self.assertEqual(self.db.search("revision", domain="other"), [])
        with self.db.transaction() as c:
            plan = c.execute("EXPLAIN QUERY PLAN SELECT rowid FROM records_search WHERE records_search MATCH ?", ('"revision"',)).fetchall()
        self.assertTrue(any("VIRTUAL TABLE INDEX" in row[3] for row in plan))
        self.db.rebuild_search()
        self.assertEqual(self.db.search("revision"), [{"text": "revision complete"}])
        self.db.put("notes", "p", "two", {"text": "surviving manuscript"})
        with self.db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE record_key='one'")
        with sqlite3.connect(self.db.path) as c:
            c.execute("VACUUM")
        self.assertEqual(self.db.search("surviving"), [{"text": "surviving manuscript"}])

    def test_upgrade_is_explicit_atomic_and_preserves_records(self):
        # Construct the exact previous schema; never downgrade a new database.
        self.db.close()
        self.db.path.unlink()
        self.db.path.touch(mode=0o600)
        with sqlite3.connect(self.db.path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE control_schema(identity TEXT PRIMARY KEY)")
            c.execute("INSERT INTO control_schema VALUES('asha.control.sqlite.v1')")
            c.execute("CREATE TABLE records(domain TEXT NOT NULL, scope TEXT NOT NULL, record_key TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(domain,scope,record_key))")
            c.execute("INSERT INTO records VALUES('notes','p','one','{\"text\":\"café\"}','original-digest',7,'ready','original-time')")
            c.execute(f"PRAGMA application_id={APPLICATION_ID}")
            c.execute("PRAGMA user_version=1")
        os.chmod(self.db.path, 0o600)
        backup = self.root / "v1-backup.sqlite3"
        backup.touch(mode=0o600)
        with sqlite3.connect(self.db.path) as original, sqlite3.connect(backup) as output:
            original.backup(output)
        os.chmod(backup, 0o600)
        recovered = load_config({"ASHA_HOME": str(self.root / "v1-recovery"), "HOME": str(self.root)})
        ControlDatabase.restore(recovered, backup)
        with ControlDatabase(recovered) as restored:
            self.assertEqual(restored.search("café"), [{"text": "café"}])
        with sqlite3.connect(backup) as original:
            self.assertEqual(original.execute("PRAGMA user_version").fetchone()[0], 1)
        with self.assertRaisesRegex(DatabaseError, "migration"):
            ControlDatabase(self.config)
        with patch.object(ControlDatabase, "_install_record_search", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                ControlDatabase(self.config, migrate=True)
        with sqlite3.connect(self.db.path) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("search_text", [r[1] for r in c.execute("PRAGMA table_info(records)")])
        with ControlDatabase(self.config, migrate=True) as upgraded:
            self.assertEqual(upgraded.search("café"), [{"text": "café"}])
            with upgraded.transaction() as c:
                row = c.execute("SELECT digest,revision,updated_at FROM records").fetchone()
                self.assertEqual(tuple(row), ("original-digest", 7, "original-time"))
        with ControlDatabase(self.config) as reopened:
            self.assertEqual(reopened.health()["schema_version"], SCHEMA_VERSION)

    def test_restore_committed_wal_state_pauses_dispatch_and_refuses_existing_state(self):
        from lib.control.sessions import ensure_owners
        with SessionStore(self.config, create=True) as store:
            original = store.create(cwd=str(self.root), prompt="Review this manuscript")
        self.db.put("notes", "p", "one", {"text": "Committed in WAL"})
        backup = self.root / "recovery.sqlite3"
        self.db.backup(backup)
        target = load_config({"ASHA_HOME": str(self.root / "recovered"), "HOME": str(self.root)})
        restored = ControlDatabase.restore(target, backup)
        self.assertEqual(_mode(restored), 0o600)
        with SessionStore(target) as store:
            self.assertEqual(store.get(original["session_id"]), original)
            self.assertEqual(store.db.search("Committed"), [{"text": "Committed in WAL"}])
            owner = store.claim_owner(original["session_id"])
            self.assertIsNone(store.claim_turn(owner["session_id"], owner["generation"]))
            with store.db.transaction() as c:
                mode, reason = c.execute("SELECT mode,reason FROM control_runtime").fetchone()
            self.assertEqual(mode, "paused")
            self.assertIn("reconciliation", reason)
        self.assertEqual(ensure_owners(target)["owners_started"], 0)
        with self.assertRaisesRegex(DatabaseError, "empty"):
            ControlDatabase.restore(target, backup)

    def test_restore_rejects_foreign_and_invalid_relationships_without_publication(self):
        backup = self.root / "bad.sqlite3"
        self.db.backup(backup)
        target = load_config({"ASHA_HOME": str(self.root / "recovered"), "HOME": str(self.root)})
        with sqlite3.connect(backup) as c:
            c.execute("CREATE TABLE parent(id TEXT PRIMARY KEY)")
            c.execute("CREATE TABLE child(id TEXT REFERENCES parent(id))")
            c.execute("INSERT INTO child VALUES('missing')")
        with self.assertRaisesRegex(DatabaseError, "relationships"):
            ControlDatabase.restore(target, backup)
        self.assertFalse((target.tasks_dir.parent / DATABASE_NAME).exists())
        self.assertEqual(list(target.tasks_dir.parent.iterdir()), [])
        with sqlite3.connect(backup) as c:
            c.execute("PRAGMA application_id=123")
        with self.assertRaisesRegex(DatabaseError, "not an Asha"):
            ControlDatabase.restore(target, backup)

    def test_restore_interruption_does_not_publish_partial_database(self):
        backup = self.root / "backup.sqlite3"
        self.db.backup(backup)
        target = load_config({"ASHA_HOME": str(self.root / "recovered"), "HOME": str(self.root)})
        with patch("lib.control.database.os.link", side_effect=OSError("interrupted publication")):
            with self.assertRaisesRegex(OSError, "interrupted publication"):
                ControlDatabase.restore(target, backup)
        self.assertFalse((target.tasks_dir.parent / DATABASE_NAME).exists())
        ControlDatabase.restore(target, backup)

    def test_message_search_is_scoped_paginated_and_rebuildable(self):
        with SessionStore(self.config, create=True) as store:
            first = store.create(cwd=str(self.root), prompt="café manuscript one")
            second = store.create(cwd=str(self.root), prompt="café manuscript two")
            store.enqueue(first["session_id"], "café manuscript three", key="follow-up")
            page = store.search("café", limit=1)
            self.assertFalse(page["complete"])
            following = store.search("café", after=page["next_cursor"])
            self.assertTrue(following["complete"])
            self.assertEqual(len(following["messages"]), 2)
            self.assertEqual(len(store.search("café", session_id=second["session_id"])["messages"]), 1)
            store.db.rebuild_search()
            self.assertEqual(len(store.search("café")["messages"]), 3)

    def test_migration_cli_refuses_live_owner_and_managed_actor(self):
        from lib.control.sessions import main
        env = {"ASHA_HOME": str(self.config.asha_home), "HOME": str(self.root)}
        with SessionStore(self.config, create=True) as store:
            session = store.create(cwd=str(self.root), prompt="Task")
            store.claim_owner(session["session_id"])
        self.assertEqual(main(["migrate", "--json"], env=env), 2)
        self.assertEqual(main(["migrate", "--json"], env={**env, "ASHA_MANAGED_SESSION_ID": session["session_id"]}), 2)


if __name__ == "__main__":
    unittest.main()
