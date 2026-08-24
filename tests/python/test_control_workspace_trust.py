"""Workspace trust is inherited from the source repository, never invented."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control import trust
from lib.control.config import ConfigError, load_config


COPILOT_HEADER = "// User settings belong in settings.json.\n// This file is managed automatically.\n"


class TrustStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".copilot").mkdir(parents=True)
        self.source = Path(self.temporary.name).resolve() / "repo"
        self.workspace = Path(self.temporary.name).resolve() / "ws"
        for path in (self.source, self.workspace):
            path.mkdir()
        self.claude_value = {
            "anonymousId": "keep-me",
            "projects": {
                str(self.source): {"hasTrustDialogAccepted": True, "lastCost": 3},
                "/somewhere/else": {"hasTrustDialogAccepted": True},
            },
        }
        self.write_claude()
        (self.home / ".codex/config.toml").write_text(
            f'model = "gpt"\n\n[projects."{self.source}"]\ntrust_level = "trusted"\n'
        )
        (self.home / ".copilot/config.json").write_text(
            COPILOT_HEADER + json.dumps({"firstLaunchAt": "x", "trustedFolders": [str(self.source)]}, indent=2) + "\n"
        )

    def write_claude(self) -> None:
        (self.home / ".claude.json").write_text(json.dumps(self.claude_value, indent=2) + "\n")

    def test_each_store_reports_its_own_exact_path_trust(self) -> None:
        for harness in trust.TRUST_HARNESSES:
            self.assertIs(trust.is_trusted(self.home, harness, self.source), True, harness)
            self.assertIs(trust.is_trusted(self.home, harness, self.workspace), False, harness)
        self.assertEqual(trust.trusting_harnesses(self.home, self.source), ["claude", "codex", "copilot"])
        self.assertEqual(trust.trusting_harnesses(self.home, self.workspace), [])
        self.assertIsNone(trust.is_trusted(self.home, "opencode", self.source))
        with self.assertRaisesRegex(trust.TrustError, "unknown harness"):
            trust.is_trusted(self.home, "emacs", self.source)

    def test_an_absent_store_is_unknown_not_untrusted(self) -> None:
        (self.home / ".claude.json").unlink()
        self.assertIsNone(trust.is_trusted(self.home, "claude", self.source))
        report = trust.grant(self.home, self.workspace)
        self.assertEqual(report["unavailable"], ["claude"])
        self.assertEqual(report["granted"], ["codex", "copilot"])

    def test_claude_grant_preserves_every_other_project_and_top_level_key(self) -> None:
        outcome = trust.grant(self.home, self.workspace, harnesses=["claude"])
        self.assertEqual(outcome["granted"], ["claude"])
        value = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(value["anonymousId"], "keep-me")
        self.assertEqual(value["projects"][str(self.source)], {"hasTrustDialogAccepted": True, "lastCost": 3})
        self.assertIn("/somewhere/else", value["projects"])
        entry = value["projects"][str(self.workspace)]
        self.assertIs(entry["hasTrustDialogAccepted"], True)
        self.assertIs(entry["hasCompletedProjectOnboarding"], True)
        self.assertEqual(trust.grant(self.home, self.workspace, harnesses=["claude"])["already"], ["claude"])

    def test_codex_and_copilot_grants_append_without_disturbing_existing_entries(self) -> None:
        trust.grant(self.home, self.workspace, harnesses=["codex", "copilot"])
        codex = (self.home / ".codex/config.toml").read_text()
        self.assertIn('model = "gpt"', codex)
        self.assertIn(f'[projects."{self.source}"]', codex)
        self.assertEqual(codex.count(f'[projects."{self.workspace}"]'), 1)
        self.assertIn('trust_level = "trusted"', codex.split(str(self.workspace))[1])
        raw = (self.home / ".copilot/config.json").read_text()
        self.assertTrue(raw.startswith(COPILOT_HEADER), "copilot comment header must survive")
        value = json.loads(raw[len(COPILOT_HEADER):])
        self.assertEqual(value["firstLaunchAt"], "x")
        self.assertEqual(value["trustedFolders"], [str(self.source), str(self.workspace)])
        trust.grant(self.home, self.workspace, harnesses=["codex", "copilot"])
        self.assertEqual((self.home / ".codex/config.toml").read_text().count(f'[projects."{self.workspace}"]'), 1)

    def test_grant_preserves_store_permissions_and_writes_atomically(self) -> None:
        store = self.home / ".claude.json"
        store.chmod(0o600)
        with mock.patch("lib.control.trust.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(trust.TrustError, "could not be updated|could not write"):
                trust.grant(self.home, self.workspace, harnesses=["claude"])
        self.assertEqual(json.loads(store.read_text()), self.claude_value, "failed write must not corrupt the store")
        self.assertEqual(len(list(self.home.glob(".claude.json.*"))), 0, "no temp file may be left behind")
        trust.grant(self.home, self.workspace, harnesses=["claude"])
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)

    def test_inheritance_grants_every_harness_when_the_source_is_trusted_anywhere(self) -> None:
        # Source trusted only in claude; the workspace is still levelled everywhere.
        (self.home / ".codex/config.toml").write_text('model = "gpt"\n')
        (self.home / ".copilot/config.json").write_text(COPILOT_HEADER + json.dumps({"trustedFolders": []}) + "\n")
        state = Path(self.temporary.name) / "state"
        report = trust.inherit_workspace_trust(
            self.home, source=self.source, workspace=self.workspace, state_dir=state,
        )
        self.assertTrue(report["applied"])
        self.assertEqual(report["inherited_from"], ["claude"])
        self.assertEqual(report["granted"], ["claude", "codex", "copilot"])
        self.assertIn("source is trusted in claude", report["reason"])
        for harness in trust.TRUST_HARNESSES:
            self.assertIs(trust.is_trusted(self.home, harness, self.workspace), True, harness)
        entry = json.loads((state / "trust.jsonl").read_text().splitlines()[0])
        self.assertEqual(entry["contract"], trust.TRUST_LEDGER_CONTRACT)
        self.assertEqual(entry["workspace"], str(self.workspace))
        self.assertEqual(entry["inherited_from"], ["claude"])

    def test_an_untrusted_source_grants_nothing_and_says_why(self) -> None:
        stranger = Path(self.temporary.name) / "stranger"
        stranger.mkdir()
        state = Path(self.temporary.name) / "state"
        report = trust.inherit_workspace_trust(
            self.home, source=stranger, workspace=self.workspace, state_dir=state,
        )
        self.assertFalse(report["applied"])
        self.assertEqual(report["granted"], [])
        self.assertIn("not trusted in any harness store", report["reason"])
        for harness in trust.TRUST_HARNESSES:
            self.assertIs(trust.is_trusted(self.home, harness, self.workspace), False)
        self.assertFalse((state / "trust.jsonl").exists())

    def test_never_mode_grants_nothing_and_an_unknown_mode_is_refused(self) -> None:
        report = trust.inherit_workspace_trust(
            self.home, source=self.source, workspace=self.workspace, mode="never",
        )
        self.assertFalse(report["applied"])
        self.assertEqual(report["reason"], "control.workspace_trust is never")
        self.assertIs(trust.is_trusted(self.home, "claude", self.workspace), False)
        with self.assertRaisesRegex(trust.TrustError, "workspace_trust must be one of"):
            trust.inherit_workspace_trust(self.home, source=self.source, workspace=self.workspace, mode="always")


class TrustConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = {
            "HOME": str(self.root / "home"), "ASHA_CONFIG": str(self.root / "config.json"),
            "XDG_STATE_HOME": str(self.root / "state"), "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)

    def write(self, control: dict) -> None:
        path = Path(self.env["ASHA_CONFIG"])
        path.write_text(json.dumps({"control": control}))
        path.chmod(0o600)

    def test_workspace_trust_defaults_to_inherit_and_rejects_unknown_modes(self) -> None:
        self.write({})
        self.assertEqual(load_config(self.env).workspace_trust, "inherit")
        self.write({"workspace_trust": "never"})
        self.assertEqual(load_config(self.env).workspace_trust, "never")
        self.write({"workspace_trust": "always"})
        with self.assertRaisesRegex(ConfigError, "workspace_trust must be one of"):
            load_config(self.env)


class LaunchTrustTests(unittest.TestCase):
    """A trust store problem delays a worker; it must never lose a launch."""

    def test_launch_helper_reports_but_never_raises(self) -> None:
        from lib.control.launch import _inherit_workspace_trust

        config = mock.Mock(home=Path("/nonexistent-home"), workspace_trust="inherit",
                           tasks_dir=Path("/nonexistent-home/state/tasks"))
        task = {"repository": {"root": "/nonexistent/repo"}, "jj": {"workspace_path": "/nonexistent/ws"}}
        report = _inherit_workspace_trust(config, task)
        self.assertFalse(report["applied"])
        with mock.patch("lib.control.trust.inherit_workspace_trust", side_effect=trust.TrustError("store moved")):
            self.assertIsNone(_inherit_workspace_trust(config, task))
        with mock.patch("lib.control.trust.inherit_workspace_trust", side_effect=OSError("permission denied")):
            self.assertIsNone(_inherit_workspace_trust(config, {"repository": {"root": "/a"}, "jj": {"workspace_path": "/b"}}))
        self.assertIsNone(_inherit_workspace_trust(config, {"repository": {}}))


if __name__ == "__main__":
    unittest.main()
