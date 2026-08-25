"""`asha migrate`: fail-closed preflight, atomic move, retirement, idempotence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control import migrate
from lib.control.config import ConfigError, load_config, migration_layout
from lib.control.store import TaskStore


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


HAVE_JJ = shutil.which("jj") is not None


class LegacyFixture(unittest.TestCase):
    """A complete pre-consolidation layout under one tmp root (one device)."""

    with_materialization = False

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        (self.root / "runtime").mkdir(mode=0o700)
        state = self.home / ".local/state/asha"
        self.control = state / "control"
        for depth in (".local", ".local/state", ".local/state/asha",
                      ".local/state/asha/control"):
            (self.home / depth).mkdir(mode=0o700)
        for kind in ("tasks", "transactions", "prunes", "repository-inits"):
            (self.control / kind).mkdir(mode=0o700)
        (self.control / "tasks/11111111-1111-4111-8111-111111111111.json").write_text(
            json.dumps({"lifecycle": "archived", "slug": "old-one"}))
        (self.control / "transactions/11111111-1111-4111-8111-111111111111.json").write_text(
            json.dumps({"phase": "run-recorded"}))
        (self.control / "transactions/11111111-1111-4111-8111-111111111111.ownership").write_text("o\n")
        (self.control / "prunes/11111111-1111-4111-8111-111111111111.json").write_text(
            json.dumps({"workspace_path": "/gone"}))
        initiative = self.control / "initiatives/22222222-2222-4222-8222-222222222222"
        initiative.mkdir(parents=True, mode=0o700)
        (initiative / "initiative.json").write_text(
            json.dumps({"state": "archived", "slug": "old-initiative"}))
        (self.control / "trust.jsonl").write_text("{}\n")
        (self.control / "trust.jsonl").chmod(0o664)
        (self.home / ".local/state/asha/proton-mail").mkdir(mode=0o700)
        (self.home / ".local/state/asha/proton-mail/replay-ledger.json").write_text("{}\n")

        self.workspaces = self.home / ".local/share/asha/workspaces"
        for depth in (".local/share", ".local/share/asha", ".local/share/asha/workspaces"):
            (self.home / depth).mkdir(mode=0o700, exist_ok=True)
        (self.workspaces / "empty-husk-1234").mkdir(mode=0o700)

        cache = self.home / ".cache/asha"
        cache.mkdir(parents=True, mode=0o700)
        (cache / "instructions.md").write_text("rendered\n")

        if self.with_materialization and HAVE_JJ:
            self.source = self.root / "project"
            self.source.mkdir(mode=0o700)
            git_env = {**os.environ, "HOME": str(self.home),
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q", str(self.source)], check=True, env=git_env)
            (self.source / "file.txt").write_text("x\n")
            subprocess.run(["git", "-C", str(self.source), "add", "."], check=True, env=git_env)
            subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "base"],
                           check=True, env=git_env)
            subprocess.run(["jj", "git", "init", "--colocate"], cwd=self.source,
                           check=True, capture_output=True, env=git_env)
            repo_key = self.workspaces / "project-abcd1234"
            mats = repo_key / "materializations"
            mats.mkdir(parents=True, mode=0o700)
            # Production registers a HASHED workspace name derived from the
            # repo key and the directory name — never the directory name
            # itself. The fixture mirrors that, so a forget by the wrong name
            # cannot pass (the original fixture equated the two and masked
            # exactly that bug).
            from lib.control.migrate import materialization_registration_name
            self.mat_dir = "verify-test-materialization"
            self.mat_name = materialization_registration_name(
                "project-abcd1234", self.mat_dir)
            subprocess.run(
                ["jj", "-R", str(self.source), "workspace", "add",
                 "--name", self.mat_name, str(mats / self.mat_dir)],
                check=True, capture_output=True, env=git_env)
            self.mat_path = mats / self.mat_dir
            # The live layout also carries a .journals DIRECTORY of
            # materialization journals — the residue class that crashed the
            # sweep in rehearsal.
            (mats / ".journals").mkdir(mode=0o700)
            (mats / ".journals" / f"{self.mat_name}.json").write_text("{}")
            (mats / ".asha-control-materializations.json").write_text("{}")

    def migrate(self, *args: str) -> int:
        return migrate.main(list(args), self.env)


class PreflightTests(LegacyFixture):
    def test_dry_run_mutates_nothing_and_prints_the_plan(self) -> None:
        before = _tree_digest(self.home)
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            code = self.migrate("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(_tree_digest(self.home), before, "dry run wrote something")

    def test_without_yes_it_refuses_after_printing_the_inventory(self) -> None:
        before = _tree_digest(self.home)
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate(), 2)
        self.assertEqual(_tree_digest(self.home), before)

    def test_live_control_sessions_refuse(self) -> None:
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions",
                        return_value=["asha-something-1234"]):
            self.assertEqual(self.migrate("--yes"), 2)
        self.assertFalse((self.home / ".asha/state").exists())

    def test_non_archived_records_refuse(self) -> None:
        (self.control / "tasks/33333333-3333-4333-8333-333333333333.json").write_text(
            json.dumps({"lifecycle": "ended", "slug": "still-here"}))
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate("--yes"), 2)
        self.assertFalse((self.home / ".asha/state").exists())

    def test_group_writable_asha_home_refuses_with_the_remediation(self) -> None:
        (self.home / ".asha").mkdir(mode=0o775)
        import io, contextlib
        stderr = io.StringIO()
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(self.migrate("--yes"), 2)
        self.assertIn("chmod g-w,o-w", stderr.getvalue())

    def test_foreign_new_state_refuses_to_merge(self) -> None:
        (self.home / ".asha/state/control").mkdir(parents=True, mode=0o700)
        import io, contextlib
        stderr = io.StringIO()
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(self.migrate("--yes"), 2)
        self.assertIn("no migration marker", stderr.getvalue())
        self.assertIn("cannot merge into it", stderr.getvalue())


class TenantRefusalTests(LegacyFixture):
    def test_a_tenant_in_new_state_refuses_by_name(self) -> None:
        state = self.home / ".asha/state"
        state.mkdir(parents=True, mode=0o700)
        (self.home / ".asha").chmod(0o700)
        (state / "broker-events.jsonl").write_text("{}\n")
        import io, contextlib
        stderr = io.StringIO()
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(self.migrate("--yes"), 2)
        self.assertIn("broker-events.jsonl", stderr.getvalue())
        self.assertIn("cannot merge", stderr.getvalue())

    def test_an_empty_new_state_is_replaced_atomically(self) -> None:
        state = self.home / ".asha/state"
        state.mkdir(parents=True, mode=0o700)
        (self.home / ".asha").chmod(0o700)
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate("--yes"), 0)
        self.assertTrue((state / "control/initiatives").is_dir())


class FullRunTests(LegacyFixture):
    with_materialization = True

    def test_full_migration_end_state(self) -> None:
        if not HAVE_JJ:
            self.skipTest("jj is not on PATH")
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate("--yes"), 0)
        layout = migration_layout(self.env)
        self.assertTrue(layout["marker"].is_file())
        marker = json.loads(layout["marker"].read_text())
        self.assertEqual(marker["status"], "complete")
        # Registry is empty and TRUTHFUL: zero tasks, zero skipped.
        config = load_config(self.env)
        store = TaskStore(config)
        self.assertEqual(store.list(), [])
        self.assertEqual(store.skipped, [])
        # Husks retired with a manifest.
        retired = sorted((layout["new_control"]).glob("retired-*/manifest.json"))
        self.assertEqual(len(retired), 1)
        manifest = json.loads(retired[0].read_text())
        self.assertEqual(manifest["counts"]["tasks"], 1)
        self.assertEqual(manifest["counts"]["transactions"], 2)  # json + sidecar
        self.assertEqual(manifest["counts"]["prunes"], 1)
        # Initiatives and repository-inits moved intact.
        self.assertTrue((layout["new_control"] / "initiatives").is_dir())
        self.assertTrue((layout["new_control"] / "repository-inits").is_dir())
        # trust.jsonl tightened; state 0700.
        self.assertEqual((layout["new_control"] / "trust.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual(layout["new_state"].stat().st_mode & 0o777, 0o700)
        # Proton satellite rode the same rename.
        self.assertTrue((layout["new_state"] / "proton-mail/replay-ledger.json").is_file())
        # Materialization forgotten and deleted; the .journals residue
        # directory cleared; workspace root created fresh.
        self.assertFalse(self.mat_path.exists())
        self.assertFalse((self.mat_path.parent / ".journals").exists())
        self.assertEqual(
            [item for item in marker["materializations"] if item.get("forget_error")],
            [], "every production-named registration must forget cleanly",
        )
        identities = subprocess.run(
            ["jj", "-R", str(self.source), "workspace", "list"],
            capture_output=True, text=True, env={**os.environ, "HOME": str(self.home)},
        ).stdout
        self.assertNotIn(self.mat_name, identities)
        self.assertTrue(layout["new_workspaces"].is_dir())
        # Cache moved.
        self.assertTrue((layout["new_cache"] / "instructions.md").is_file())
        # Banners at both legacy roots; gate reads them as migrated.
        self.assertTrue((layout["legacy_state"] / "ASHA-MOVED.md").is_file())
        load_config(self.env)  # no refusal
        # Doctor: match.
        from lib.control.doctor import _migration_probe
        probe = _migration_probe(config)
        self.assertEqual(probe.outcome, "match")

    def test_second_run_is_a_no_op(self) -> None:
        if not HAVE_JJ:
            self.skipTest("jj is not on PATH")
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate("--yes"), 0)
            digest = _tree_digest(self.home)
            self.assertEqual(self.migrate("--yes"), 0)
        self.assertEqual(_tree_digest(self.home), digest)


class GateAndProbeTests(LegacyFixture):
    def test_gate_refuses_before_and_probe_names_the_command(self) -> None:
        with self.assertRaisesRegex(ConfigError, "asha migrate"):
            load_config(self.env)
        config = load_config(self.env, check_legacy=False)
        from lib.control.doctor import _migration_probe
        probe = _migration_probe(config)
        self.assertEqual(probe.outcome, "mismatch")
        self.assertIn("run: asha migrate", probe.detail)

    def test_resurrected_legacy_data_past_the_marker_is_a_decoy_mismatch(self) -> None:
        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate("--yes"), 0)
        (self.control / "tasks").mkdir(parents=True)
        (self.control / "tasks/99999999-9999-4999-8999-999999999999.json").write_text("{}")
        config = load_config(self.env, check_legacy=False)
        from lib.control.doctor import _migration_probe
        probe = _migration_probe(config)
        self.assertEqual(probe.outcome, "mismatch")
        self.assertIn("reappeared", probe.detail)

    def test_the_migrator_never_calls_load_config(self) -> None:
        with mock.patch("lib.control.config.load_config",
                        side_effect=AssertionError("migrate must not call load_config")), \
                mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            self.assertEqual(self.migrate("--yes"), 0)


class ResumeTests(LegacyFixture):
    def test_crash_after_state_move_resumes_to_completion(self) -> None:
        calls = {"n": 0}
        real_write = migrate._journal_write

        def crash_after_state_moved(layout, phase, extra):
            real_write(layout, phase, extra)
            if phase == "state-moved":
                raise KeyboardInterrupt("simulated crash")

        with mock.patch("lib.control.tmux.TmuxAdapter.list_sessions", return_value=[]):
            with mock.patch.object(migrate, "_journal_write",
                                   side_effect=crash_after_state_moved):
                with self.assertRaises(KeyboardInterrupt):
                    self.migrate("--yes")
            # State is at the new root; legacy control gone; no marker yet.
            self.assertTrue((self.home / ".asha/state/control").is_dir())
            self.assertFalse(self.control.exists())
            layout = migration_layout(self.env)
            self.assertTrue(layout["journal"].is_file())
            # Resume completes.
            self.assertEqual(self.migrate("--yes"), 0)
        layout = migration_layout(self.env)
        self.assertTrue(layout["marker"].is_file())
        self.assertFalse(layout["journal"].exists())
        self.assertEqual(load_config(self.env).tasks_dir,
                         self.home / ".asha/state/control/tasks")


if __name__ == "__main__":
    unittest.main()
