from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.cli import main as control_main
from lib.control.config import load_config
from lib.control.doctor import DEFAULT_PROBES, Probe, run_doctor
from lib.control.store import TaskStore
from tests.python.test_control_config_model import task_record


class DoctorOkFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)

    def invoke_doctor(self, payload: dict) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.run_doctor", return_value=payload), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main(["task", "doctor", "--json"], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()


class DoctorVerdictTests(DoctorOkFixture):
    def test_rooms_registry_probe_accepts_absent_store_and_refuses_corruption(self) -> None:
        clean = run_doctor(
            self.config, probes={"rooms-registry": DEFAULT_PROBES["rooms-registry"]},
        )
        self.assertTrue(clean["ok"])
        self.assertIn("0 durable Room", clean["probes"][0]["detail"])

        rooms = self.config.asha_home / "state/control/rooms"
        rooms.mkdir(parents=True)
        (rooms / "11111111-1111-4111-8111-111111111111.json").write_text("{}")
        broken = run_doctor(
            self.config, probes={"rooms-registry": DEFAULT_PROBES["rooms-registry"]},
        )
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["probes"][0]["outcome"], "mismatch")
        self.assertIn("could not be authenticated", broken["probes"][0]["detail"])

    def test_supervisor_service_probe_is_advisory_and_reports_all_states(self) -> None:
        values = dict(self.env, XDG_CONFIG_HOME=str(self.root / "config"))
        unit = self.root / "config/systemd/user/asha-supervisor.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n", encoding="utf-8")
        calls = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, b"", b"")

        result = run_doctor(
            self.config,
            probes={"supervisor-service": DEFAULT_PROBES["supervisor-service"]},
            env=values, runner=runner, which=lambda command: f"/usr/bin/{command}",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"], [{
            "name": "supervisor-service",
            "outcome": "mismatch",
            "detail": "supervisor service present=yes, enabled=no, active=no",
        }])
        self.assertEqual(calls, [
            ["/usr/bin/systemctl", "--user", "is-enabled", "asha-supervisor.service"],
            ["/usr/bin/systemctl", "--user", "is-active", "asha-supervisor.service"],
        ])

    def test_supervisor_service_probe_is_informational_without_systemctl(self) -> None:
        result = run_doctor(
            self.config,
            probes={"supervisor-service": DEFAULT_PROBES["supervisor-service"]},
            env=self.env, which=lambda _command: None,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")
        self.assertIn("systemctl is unavailable", result["probes"][0]["detail"])

    def test_missing_supervisor_unit_is_informational(self) -> None:
        values = dict(self.env, XDG_CONFIG_HOME=str(self.root / "config"))

        def runner(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"")

        result = run_doctor(
            self.config,
            probes={"supervisor-service": DEFAULT_PROBES["supervisor-service"]},
            env=values, runner=runner, which=lambda command: f"/usr/bin/{command}",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "missing")
        self.assertEqual(
            result["probes"][0]["detail"],
            "supervisor service present=no, enabled=no, active=no",
        )

    def test_repository_unavailable_outside_a_repo_is_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as outside, contextlib.chdir(outside):
            result = run_doctor(None, probes={
                "repository": DEFAULT_PROBES["repository"],
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")
        self.assertIn(result["probes"][0]["detail"], result["limitations"])

    def test_repository_mismatch_in_a_repo_remains_blocking(self) -> None:
        result = run_doctor(None, probes={
            "repository": lambda _config: Probe(
                "repository", "mismatch", "jj and Git heads disagree",
            ),
        })

        self.assertFalse(result["ok"])

    def test_doctor_cli_returns_one_when_required_checks_fail(self) -> None:
        payload = {
            "contract": "asha.control-doctor.v1",
            "ok": False,
            "probes": [{
                "name": "tmux", "outcome": "missing", "detail": "tmux is absent",
            }],
            "limitations": ["tmux is absent"],
        }

        status, stdout, stderr = self.invoke_doctor(payload)

        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout), payload)
        self.assertEqual(stderr, "")

    def test_doctor_cli_returns_zero_when_required_checks_pass(self) -> None:
        payload = {
            "contract": "asha.control-doctor.v1",
            "ok": True,
            "probes": [],
            "limitations": [],
        }

        status, stdout, stderr = self.invoke_doctor(payload)

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), payload)
        self.assertEqual(stderr, "")


class DoctorSupportedConfigurationTests(DoctorOkFixture):
    def _write_claude_hooks(self) -> None:
        handler = (
            Path(__file__).resolve().parents[2]
            / "plugins/session/hooks/handlers/control-event.sh"
        )
        hooks = {
            event: [{
                "hooks": [{"type": "command", "command": f"{handler} {event}"}],
            }]
            for event in (
                "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd",
            )
        }
        claude_home = self.home / ".claude"
        claude_home.mkdir()
        (claude_home / "settings.json").write_text(
            json.dumps({"hooks": hooks}), encoding="utf-8",
        )

    def test_hooks_probe_accepts_a_claude_only_install(self) -> None:
        self._write_claude_hooks()
        self.assertFalse((self.home / ".codex/config.toml").exists())

        result = run_doctor(
            self.config, probes={"hooks": DEFAULT_PROBES["hooks"]},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "match")
        self.assertIn("Claude", result["probes"][0]["detail"])
        self.assertNotIn("Codex", result["probes"][0]["detail"])

    def test_hooks_probe_does_not_ignore_a_malformed_present_config(self) -> None:
        codex_config = self.home / ".codex" / "config.toml"
        codex_config.mkdir(parents=True)

        result = run_doctor(
            self.config, probes={"hooks": DEFAULT_PROBES["hooks"]},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")
        self.assertIn("not a regular file", result["probes"][0]["detail"])

    def test_events_probe_skips_runs_without_a_claimed_event_seam(self) -> None:
        source = self.root / "source"
        source.mkdir()
        source.chmod(0o755)
        for harness in ("copilot", "opencode"):
            workspace = self.config.workspace_root / "repo-key" / f"{harness}-only"
            workspace.mkdir(parents=True)
            current = workspace
            while current != self.root:
                current.chmod(0o700)
                current = current.parent
            task = task_record(
                slug=f"{harness}-only",
                repository_root=str(source),
                workspace_path=str(workspace),
            )
            task["runs"][0]["harness"] = harness
            TaskStore(self.config).save(task)

        result = run_doctor(
            self.config,
            probes={"harness-events": DEFAULT_PROBES["harness-events"]},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "match")
        self.assertIn("no claimed semantic event seam", result["probes"][0]["detail"])
        self.assertIn("skipped 2", result["probes"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
