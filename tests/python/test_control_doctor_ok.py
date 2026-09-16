from __future__ import annotations

import contextlib
import io
import json
import hashlib
import os
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
    def test_stale_workspace_probe_without_config_is_unavailable(self):
        adapter = mock.Mock()
        adapter.workspace_identities.return_value = {"asha-old": ("a", "b")}
        with mock.patch("lib.control.doctor.JjAdapter", return_value=adapter):
            result = run_doctor(None, probes={"stale-workspaces": DEFAULT_PROBES["stale-workspaces"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")
        adapter.discover_root.assert_not_called()

    def test_stale_workspace_probe_flags_unowned_control_names_without_mutating(self):
        adapter = mock.Mock()
        adapter.discover_root.return_value = self.root
        adapter.workspace_identities.return_value = {
            "default": ("a", "b"), "operator-work": ("c", "d"),
            "asha-materialization-old": ("e", "f"),
        }
        with mock.patch("lib.control.doctor.JjAdapter", return_value=adapter):
            result = run_doctor(self.config, probes={"stale-workspaces": DEFAULT_PROBES["stale-workspaces"]})
        self.assertTrue(result["ok"], "stale workspace warnings are advisory")
        self.assertEqual(result["probes"][0]["outcome"], "mismatch")
        self.assertIn("asha-materialization-old", result["probes"][0]["detail"])
        self.assertNotIn("operator-work", result["probes"][0]["detail"])
        adapter.forget_workspace.assert_not_called()

    def test_runtime_hooks_check_only_plan_harnesses_but_default_doctor_checks_all(self):
        claude = self.home / ".claude"
        codex = self.home / ".codex"
        claude.mkdir()
        codex.mkdir()
        handler = Path(__file__).resolve().parents[2] / "plugins/session/hooks/handlers/control-event.sh"
        events = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
        (claude / "settings.json").write_text(json.dumps({"hooks": {
            event: [{"hooks": [{"type": "command", "command": f"{handler} {event}"}]}] for event in events}}))
        (codex / "config.toml").write_text("broken = [")
        probes = {"hooks": DEFAULT_PROBES["hooks"]}
        self.assertTrue(run_doctor(self.config, probes=probes, required_harnesses=("claude",))["ok"])
        self.assertFalse(run_doctor(self.config, probes=probes)["ok"])
        self.assertFalse(run_doctor(self.config, probes=probes, required_harnesses=("codex",))["ok"])
        (claude / "settings.json").write_text('{}')
        self.assertFalse(run_doctor(self.config, probes=probes, required_harnesses=("claude",))["ok"])

    def test_required_harnesses_must_all_resolve_and_cannot_be_empty_or_unknown(self):
        probes = {"harness": DEFAULT_PROBES["harness"]}
        with mock.patch("lib.control.doctor.shutil.which", side_effect=lambda name: "/bin/claude" if name == "claude" else None):
            self.assertTrue(run_doctor(self.config, probes=probes, required_harnesses=("claude",))["ok"])
            self.assertFalse(run_doctor(self.config, probes=probes, required_harnesses=("claude", "codex"))["ok"])
        for value in ((), ("unknown",), "claude"):
            with self.assertRaises(ValueError):
                run_doctor(self.config, probes=probes, required_harnesses=value)

    def test_missing_required_hook_installation_refuses_without_affecting_generic_diagnostics(self):
        probes = {"hooks": DEFAULT_PROBES["hooks"]}
        self.assertTrue(run_doctor(self.config, probes=probes)["ok"])
        for name in ("claude", "codex"):
            result = run_doctor(self.config, probes=probes, required_harnesses=(name,))
            self.assertFalse(result["ok"])
            self.assertIn("installation is absent", result["probes"][0]["detail"])
        result = run_doctor(self.config, probes=probes, required_harnesses=("opencode",))
        self.assertIn("not inspected", result["probes"][0]["detail"])

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


class NativeOwnedCodexHookTests(DoctorOkFixture):
    def setUp(self) -> None:
        super().setUp()
        self.repo = Path(__file__).resolve().parents[2]
        self.native = self.home / ".codex"
        self.native.mkdir()
        self.cfg = self.native / "config.toml"
        self.hooks = self.native / "hooks.json"
        self.ledger = self.config.asha_home / "install-manifests/codex.json"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.fake_codex = self.tools / "codex"
        self.fake_codex.write_text('#!/bin/sh\n[ "$1" = --version ] || exit 91\nprintf "codex-cli 0.153.4\\n"\n')
        self.fake_codex.chmod(0o755)
        self.path = str(self.tools) + os.pathsep + os.environ["PATH"]

    def install(self, *, legacy: bool = False) -> None:
        script = ('source "$1/lib/install.sh"; DRY_RUN=0; FORCE=0; VERBOSE=0; '
                  'ONLY=""; WITH_CANARY=0; source "$1/harnesses/codex.sh"; ' +
                  ('_codex_build_hook_block' if legacy else 'codex_install_hooks'))
        result = subprocess.run(["bash", "-c", script, "doctor-fixture", str(self.repo)],
            cwd=self.repo, env=dict(self.env, PATH=self.path), capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        if legacy:
            self.cfg.write_bytes(b'features.hooks=true\n' + result.stdout)

    def probe(self):
        with mock.patch.dict(os.environ, {"PATH": self.path}):
            return DEFAULT_PROBES["hooks"](self.config)

    def test_json_only_is_inspected_and_default_evidence_is_version_specific(self):
        self.install()
        self.assertFalse(self.cfg.exists())
        probe = self.probe()
        self.assertEqual(probe.outcome, "match", probe.detail)
        self.assertIn("0.153.4 default-true evidence only", probe.detail)
        self.assertIn("trust and execution NOT verified", probe.detail)
        self.fake_codex.write_text('#!/bin/sh\nprintf "codex-cli 99.0.0\\n"\n')
        probe = self.probe()
        self.assertEqual(probe.outcome, "unavailable", probe.detail)
        self.assertIn("default unsupported", probe.detail)
        self.assertFalse(self.cfg.exists())

    def test_explicit_false_and_foreign_inline_keep_raw_config(self):
        raw = (b'# native\r\nfeatures.hooks=false\r\n[[hooks.Stop]]\r\n'
               b'[[hooks.Stop.hooks]]\r\ntype="command"\r\ncommand="/bin/true"\r\n'
               b'[hooks.state.native]\r\ntrusted_hash="unchanged"\r\n')
        self.cfg.write_bytes(raw)
        self.cfg.chmod(0o640)
        self.install()
        probe = self.probe()
        self.assertEqual(probe.outcome, "mismatch", probe.detail)
        self.assertIn("disabled", probe.detail)
        self.assertEqual(self.cfg.read_bytes(), raw)
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o640)
        self.cfg.write_bytes(raw.replace(b'false', b'true'))
        probe = self.probe()
        self.assertEqual(probe.outcome, "match", probe.detail)
        self.assertIn("mixed foreign inline/JSON", probe.detail)

    def test_missing_malformed_duplicate_and_unrecorded_are_not_green(self):
        self.cfg.write_text('features.hooks=true\n')
        self.assertEqual(self.probe().outcome, "missing")
        self.install()
        original = self.hooks.read_bytes()
        ledger = self.ledger.read_bytes()
        for data in (b'{', b'{"hooks":{},"hooks":{}}', original+b' '):
            self.hooks.write_bytes(data)
            self.assertEqual(self.probe().outcome, "unavailable")
        self.hooks.write_bytes(original)
        self.ledger.unlink()
        self.assertEqual(self.probe().outcome, "unavailable")
        self.ledger.write_bytes(ledger)
        self.cfg.write_text('bad=[')
        self.assertEqual(self.probe().outcome, "unavailable")
        self.cfg.unlink()
        self.cfg.symlink_to(self.hooks)
        self.assertEqual(self.probe().outcome, "unavailable")

    def test_expected_verification_and_style_seams_not_merely_some_commands(self):
        self.cfg.write_text('features.hooks=true\n')
        self.install()
        value = json.loads(self.hooks.read_bytes())
        for event, suffix in (("Stop", "verify-pass-complete.sh"),
                              ("PostToolUse", "post-tool-use.sh")):
            modified = json.loads(json.dumps(value))
            modified['hooks'][event] = [group for group in modified['hooks'][event]
                if not any(h['command'].endswith(suffix) for h in group['hooks'])]
            self.hooks.write_text(json.dumps(modified))
            rows = json.loads(self.ledger.read_bytes())
            rows['artifacts'][0]['sha256'] = hashlib.sha256(self.hooks.read_bytes()).hexdigest()
            self.ledger.write_text(json.dumps(rows))
            probe = self.probe()
            self.assertEqual(probe.outcome, "missing", probe.detail)
            self.assertIn(event, probe.detail)

    def test_exact_legacy_is_inspected_but_duplicate_native_json_refuses(self):
        self.install(legacy=True)
        probe = self.probe()
        self.assertEqual(probe.outcome, "match", probe.detail)
        raw = self.cfg.read_bytes()
        self.install()
        self.assertFalse(self.hooks.exists())
        self.assertEqual(self.cfg.read_bytes(), raw)
        self.hooks.write_text('{"hooks":{}}')
        self.assertEqual(self.probe().outcome, "unavailable")


if __name__ == "__main__":
    unittest.main()
