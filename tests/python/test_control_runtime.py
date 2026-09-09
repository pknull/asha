from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lib.control.config import load_config
from lib.control.runtime import admission, require_admission, set_admission
from lib.control.session_store import SessionStore
from lib.control.sessions import ensure_owners, run_owner
from lib.control.store import StoreError
from lib.control.orchestration.supervisor_daemon import supervisor_main


class RuntimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha")}
        self.config = load_config(self.env)

    def test_pause_is_durable_idempotent_and_fences_new_claims(self):
        with SessionStore(self.config, create=True) as store:
            session = store.create(cwd=str(self.root), prompt="Task")
            owner = store.claim_owner(session["session_id"])
            original = admission(self.config)
            paused = set_admission(self.config, "paused", expected_revision=original["revision"])
            self.assertEqual(set_admission(self.config, "paused")["revision"], paused["revision"])
            self.assertIsNone(store.claim_turn(owner["session_id"], owner["generation"]))
            self.assertEqual(ensure_owners(self.config)["owners_started"], 0)
            with self.assertRaisesRegex(StoreError, "changed"):
                set_admission(self.config, "running", expected_revision=original["revision"])
            set_admission(self.config, "running", expected_revision=paused["revision"])
            self.assertIsNotNone(store.claim_turn(owner["session_id"], owner["generation"]))

    def test_drain_finishes_admitted_turn_and_retains_follow_up(self):
        with SessionStore(self.config, create=True) as store:
            session = store.create(cwd=str(self.root), prompt="Task")
            sid = session["session_id"]
            owner = store.claim_owner(sid)
            turn = store.claim_turn(sid, owner["generation"])
            store.enqueue(sid, "Follow-up", key="next")
            set_admission(self.config, "draining")
            store.finish(sid, owner["generation"], turn["turn_id"], success=True)
            self.assertEqual(store.get(sid)["state"], "idle")
            self.assertIsNone(store.claim_turn(sid, owner["generation"]))
        self.assertEqual(run_owner(self.config, sid, env=self.env, once=True, transport_factory=Mock(side_effect=AssertionError("must not launch"))), 0)
        with SessionStore(self.config) as store:
            self.assertEqual(store.get(sid)["state"], "idle")
            self.assertEqual(store.snapshot(sid)["messages"][0]["state"], "queued")

    def test_stop_owner_cancels_pending_work_without_dispatch(self):
        with SessionStore(self.config, create=True) as store:
            sid = store.create(cwd=str(self.root), prompt="Task")["session_id"]
        set_admission(self.config, "stopped")
        self.assertEqual(run_owner(self.config, sid, env=self.env, once=True, transport_factory=Mock(side_effect=AssertionError("must not launch"))), 0)
        with SessionStore(self.config) as store:
            self.assertEqual(store.get(sid)["state"], "stopped")
            self.assertEqual(store.snapshot(sid)["messages"][0]["state"], "cancelled")

    def test_paused_runtime_refuses_worker_and_scheduler_launch_seams(self):
        from lib.control.launch import launch_task
        from lib.control.orchestration.scheduler import dispatch
        from lib.control.orchestration.config import load_config as orchestration_config
        set_admission(self.config, "paused")
        with self.assertRaisesRegex(StoreError, "admission is paused"):
            launch_task(self.config, {}, harness="claude")
        with self.assertRaisesRegex(StoreError, "admission is paused"):
            dispatch(Mock(), orchestration_config(self.env), "id", "node", action={})

    def test_cli_mutations_refuse_managed_actor_and_status_is_read_only(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(supervisor_main(["pause", "--json"], env={**self.env, "ASHA_MANAGED_SESSION_ID": "label"}), 2)
            self.assertEqual(supervisor_main(["pause", "--json"], env=self.env), 0)
            self.assertEqual(supervisor_main(["drain", "--json"], env=self.env), 0)
            self.assertEqual(supervisor_main(["resume", "--json"], env=self.env), 0)
            before = admission(self.config)
            supervisor_main(["status", "--json"], env=self.env)
            self.assertEqual(admission(self.config), before)

    def test_corrupt_state_stops_dispatch_instead_of_assuming_running(self):
        set_admission(self.config, "paused")
        with SessionStore(self.config, create=True) as store:
            with store.db.transaction(write=True) as c:
                c.execute("DELETE FROM control_runtime")
        with self.assertRaisesRegex(StoreError, "missing"):
            require_admission(self.config)

    def test_quick_stop_then_resume_does_not_erase_cancellation(self):
        with SessionStore(self.config, create=True) as store:
            sid = store.create(cwd=str(self.root), prompt="Task")["session_id"]
            owner = store.claim_owner(sid)
            set_admission(self.config, "stopped")
            set_admission(self.config, "running")
            self.assertEqual(store.get(sid)["stop_requested"], 1)
            self.assertIsNone(store.claim_turn(sid, owner["generation"]))

    def test_stopped_session_recovery_keeps_old_input_cancelled(self):
        with SessionStore(self.config, create=True) as store:
            sid = store.create(cwd=str(self.root), prompt="Original assignment")["session_id"]
            set_admission(self.config, "stopped")
            stopped = store.get(sid)
            self.assertEqual(stopped["state"], "stopped")
            store.resume(sid, prompt="Fresh inspected recovery", expected_digest=store.recovery_digest(stopped))
            self.assertEqual(store.get(sid)["stop_requested"], 0)
            messages = store.snapshot(sid)["messages"]
            self.assertEqual([r["state"] for r in messages], ["queued", "cancelled"])
            set_admission(self.config, "running")
            owner = store.claim_owner(sid)
            self.assertEqual(store.claim_turn(sid, owner["generation"])["body"], "Fresh inspected recovery")

    def test_missing_policy_is_reported_by_owner_reconciliation(self):
        with SessionStore(self.config, create=True) as store:
            with store.db.transaction(write=True) as c:
                c.execute("DELETE FROM control_runtime")
        with self.assertRaisesRegex(StoreError, "missing"):
            ensure_owners(self.config)

    def test_quiesce_can_stop_a_proven_owner_before_schema_upgrade(self):
        from lib.control.harness import process_identity
        from lib.control.sessions import quiesce
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with SessionStore(self.config, create=True) as store:
                sid = store.create(cwd=str(self.root), prompt="Task")["session_id"]
                with store.db.transaction(write=True) as c:
                    c.execute("UPDATE managed_sessions SET owner_pid=?,owner_identity=? WHERE session_id=?", (child.pid, process_identity(child.pid), sid))
                    # Exercise the old-version inspection gate with a process
                    # owned by the test; no old-schema writes are permitted.
                    c.execute("PRAGMA user_version=1")
            with patch("lib.control.orchestration.supervisor_daemon.stop_supervisor", return_value=({"message": "not running"}, 1)):
                result = quiesce(self.config, self.env)
            self.assertEqual(result["signalled_sessions"], [sid])
            self.assertEqual(child.wait(timeout=3), -15)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
