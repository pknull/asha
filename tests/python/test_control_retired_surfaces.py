"""The legacy initiative engine, `asha task` and L3 staging are retired (L-b).

Their entry points are refused rather than silently reinterpreted, and the
surviving Control surfaces (the doctor, the supervisor, the Codex actor tool,
the dashboard) no longer reach them.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control import cli


class RetiredSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args), env=self.env)
        return code, out.getvalue(), err.getvalue()

    def test_asha_task_is_refused_as_retired(self):
        for args in (["task"], ["task", "list", "--json"], ["task", "start", "--goal", "x"],
                     ["task", "doctor"], ["task", "report", "--file", "r.json"]):
            code, out, err = self._run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertIn("retired", err, args)

    def test_asha_migrate_is_refused_as_retired(self):
        legacy = self.home / ".local/state/asha/control/tasks"
        legacy.mkdir(parents=True)
        for args in (["migrate"], ["migrate", "--dry-run"], ["migrate", "--yes"], ["migrate", "--json"]):
            code, out, err = self._run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertIn("retired", err, args)
        self.assertTrue(legacy.is_dir())
        self.assertFalse((self.root / "asha").exists())

    def test_legacy_control_routes_are_refused(self):
        for args in (["control", "event", "--event", "turn-stopped"], ["control", "--initiatives"],
                     ["control", "registry", "status"]):
            code, out, err = self._run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertIn("asha control: project harness launcher", err, args)
        code, out, _err = self._run("control", "--help")
        self.assertEqual(code, 0)
        for retired in ("--initiatives", "registry", "initiative progression"):
            self.assertNotIn(retired, out)

    def test_asha_initiative_reaches_only_the_read_only_evidence_reader(self):
        code, out, err = self._run("initiative", "list", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"contract": "asha.initiative-evidence-list.v1", "initiatives": []})
        for verb in ("approve", "create", "coordinator", "authority", "projects", "doctor"):
            code, out, err = self._run("initiative", verb, "x")
            self.assertEqual((code, out), (2, ""), verb)
            self.assertIn("retired", err, verb)

    def test_control_doctor_runs_the_surviving_probes_only(self):
        from lib.control.doctor import DEFAULT_PROBES
        self.assertEqual(set(DEFAULT_PROBES), {
            "python", "configuration", "supervisor-service",
            "tmux", "harness", "gh", "rooms-registry", "managed-sessions", "hooks", "tui",
        })
        payload = {"contract": "asha.control-doctor.v1", "ok": True, "limitations": [],
                   "probes": [{"name": "python", "outcome": "match", "detail": "ok"}]}
        with mock.patch("lib.control.doctor.run_doctor", return_value=payload) as doctor:
            code, out, err = self._run("control", "doctor", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), payload)
        doctor.assert_called_once()
        with mock.patch("lib.control.doctor.run_doctor", return_value={**payload, "ok": False}):
            code, out, _err = self._run("control", "doctor")
        self.assertEqual(code, 1)
        self.assertIn("python: ok", out)
        code, _out, err = self._run("control", "doctor", "--bogus")
        self.assertEqual(code, 2)

    def test_codex_actor_exposes_only_ask(self):
        from lib.control.codex_actor import TOOL
        self.assertEqual(TOOL["inputSchema"]["properties"]["operation"]["enum"], ["ask"])
        self.assertNotIn("initiative", TOOL["description"])

    def test_dashboard_has_no_advanced_initiatives_key(self):
        from lib.control import session_keys, session_tui
        sheet = "\n".join(session_keys.key_sheet())
        self.assertNotIn("G workflows", sheet)
        self.assertNotIn("initiative", sheet.lower())
        self.assertNotIn("G workflows", session_keys.row_keys(None))
        self.assertFalse(hasattr(session_tui.Dashboard, "suspend_for_workflows"))

    def test_an_owner_for_an_initiative_bound_session_stops_it_without_a_turn(self):
        from lib.control.config import load_config
        from lib.control.session_store import SessionStore
        from lib.control.sessions import ensure_owners, run_owner
        config = load_config(self.env)
        with mock.patch.dict("lib.control.session_harness.CAPABILITIES", {"claude": {"managed": True}}), \
                SessionStore(config, create=True) as store:
            sid = store.create(cwd=str(self.root), prompt="Coordinate", harness="claude",
                               initiative_id=str(uuid.uuid4()))["session_id"]
        with mock.patch("lib.control.sessions.subprocess.Popen") as popen:
            self.assertEqual(ensure_owners(config, env=self.env)["owners_started"], 0)
        popen.assert_not_called()
        transport = mock.Mock(side_effect=AssertionError("an initiative-bound session must not run"))
        self.assertEqual(run_owner(config, sid, env=self.env, once=True, transport_factory=transport), 0)
        transport.assert_not_called()
        with SessionStore(config) as store:
            session = store.get(sid)
        self.assertEqual((session["state"], session["stop_requested"]), ("stopped", 1))

    def test_supervisor_run_never_loads_the_engine(self):
        from lib.control import supervisor_service
        self.assertFalse(hasattr(supervisor_service, "tick"))
        self.assertFalse(hasattr(supervisor_service, "_initiative_sweep"))


if __name__ == "__main__":
    unittest.main()
