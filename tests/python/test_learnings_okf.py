"""The learnings store as an OKF v0.2 bundle: migration, marker-gated writer, gate seams."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "plugins/session/tools"
OKF_TOOLS = ROOT / "plugins/okf/tools"
GOLDEN = ROOT / "tests/python/golden/learnings-v2"
sys.path.insert(0, str(TOOLS))

import learnings_manager as lm  # noqa: E402
import memory_v2  # noqa: E402
import migrate_learnings_okf as migration  # noqa: E402


def tree_digest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def names(root: Path) -> dict:
    return {s: sorted(p.name for p in (root / s).glob("*.md") if p.name != "index.md")
            for s in lm.STATES if (root / s).is_dir()}


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home/.asha"
        self.bundle = self.home / "learnings"
        shutil.copytree(GOLDEN, self.bundle)
        self.patch = mock.patch.object(lm, "learnings_dir", lambda: self.bundle)
        self.patch.start()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        memory_v2.initialize(self.project)
        config = memory_v2.read_project_config(self.project)
        config["project_id"] = "p1"
        (self.project / ".asha/config.json").write_text(json.dumps(config))
        memory_v2.publish(self.project, memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def migrate(self, **kwargs):
        return migration.migrate(None, yes=True, **kwargs)

    def validate(self, root=None):
        return subprocess.run([sys.executable, str(OKF_TOOLS / "okf_validate.py"), str(root or self.bundle), "--strict"],
                              capture_output=True, text=True)


class MigrationTests(FixtureCase):
    def test_migration_keeps_every_filename_state_dir_and_text(self):
        before = names(self.bundle)
        texts = {l.id: (l.trigger, l.action, l.applicability, l.retirement_reason, [vars(e) for e in l.evidence])
                 for l in lm.list_state()}
        report = self.migrate()
        self.assertTrue(report["filenames_identical"])
        self.assertEqual(before, names(self.bundle))
        after = {l.id: (l.trigger, l.action, l.applicability, l.retirement_reason, [vars(e) for e in l.evidence])
                 for l in lm.list_state()}
        self.assertEqual(texts, after)
        self.assertEqual("a/b", lm.load("a/b").id)
        record = json.loads((self.bundle / "active/a-one.md").read_text().split("\n---\n")[0][4:])
        self.assertEqual({"by": "asha/" + lm.SESSION_PLUGIN_VERSION, "at": "2026-01-10"}, record["generated"])
        self.assertEqual("stable", record["status"])
        self.assertEqual("a-one", record["title"])
        self.assertEqual("Golden trigger one", record["description"])
        self.assertNotIn("verified", record)
        self.assertEqual("active", record["state"])
        self.assertEqual(3, len(record["evidence"]))
        candidate = json.loads((self.bundle / "candidate/c-old.md").read_text().split("\n---\n")[0][4:])
        self.assertEqual("draft", candidate["status"])
        self.assertEqual("2026-04-01T00:00:00+00:00", candidate["stale_after"])
        self.assertNotIn("stale_after", record)

    def test_render_active_is_byte_identical_after_migration(self):
        golden = (GOLDEN / "render-active-4500.txt").read_text().rstrip("\n")
        self.assertEqual(golden, lm.render_active(4500))
        small_before = lm.render_active(60)
        self.migrate()
        self.assertEqual(golden, lm.render_active(4500))
        self.assertEqual(small_before, lm.render_active(60))
        self.assertIn("Omitted", small_before)

    def test_control_selection_order_survives_the_migration(self):
        def order(rules):
            def key(rule):
                scope = rule.applicability
                sources = {e.session_id for e in rule.evidence if e.session_id not in {"", "unknown"}}
                return (not bool(scope.get("project_ids")), not bool(scope.get("harnesses")), -len(sources), rule.id)
            return [r.id for r in sorted(rules, key=key)]
        before = order(lm.list_state("active"))
        self.migrate()
        self.assertEqual(before, order(lm.list_state("active")))
        self.assertEqual(["b-two", "a-one"], before)

    def test_past_due_candidate_is_listed_and_stays_a_candidate(self):
        report = self.migrate()
        self.assertEqual(["c-old"], report["stale_candidates"])
        self.assertTrue((self.bundle / "candidate/c-old.md").is_file())
        stale = json.loads((self.bundle / lm.STALE_LIST).read_text())
        self.assertEqual(["c-old"], stale["stale"])
        self.assertEqual(["c-old"], lm.maintain_candidates(project_dir=self.project))
        self.assertTrue((self.bundle / "candidate/c-old.md").is_file())
        self.assertFalse((self.bundle / "retired/c-old.md").exists())

    def test_migrated_bundle_is_lint_clean_under_the_pinned_validator(self):
        self.migrate()
        result = self.validate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        files = 6 + 4 + 1  # six records, four indexes (root + three states), log.md
        self.assertIn(f"{files} file(s), 0 error(s), 0 lint(s)", result.stdout)
        self.assertFalse((self.bundle / "index.md").read_text().startswith("---"), "learnings declare no okf_version")
        self.assertIn("- [a-one](./a-one.md): Golden trigger one", (self.bundle / "active/index.md").read_text())
        self.assertIn("migrated 6 learning records", (self.bundle / "log.md").read_text())

    def test_migration_backs_up_beside_the_resolved_target_and_refuses_twice(self):
        before = tree_digest(self.bundle)
        report = self.migrate()
        backup = Path(report["backup"])
        self.assertEqual(self.bundle.parent, backup.parent)
        self.assertTrue(backup.name.startswith("learnings.pre-okf-v02-"))
        self.assertEqual(before, tree_digest(backup))
        after = tree_digest(self.bundle)
        with self.assertRaisesRegex(ValueError, "already migrated"):
            self.migrate()
        self.assertEqual(after, tree_digest(self.bundle))
        (self.bundle / lm.OKF_MARKER).unlink()
        with self.assertRaisesRegex(ValueError, "already in the OKF shape"):
            self.migrate(backup_dir=Path(self.tmp.name) / "second-backup")
        self.assertFalse((Path(self.tmp.name) / "second-backup").exists())
        self.assertEqual({k: v for k, v in after.items() if k != lm.OKF_MARKER}, tree_digest(self.bundle))

    def test_migration_checks_every_record_before_the_marker_or_backup(self):
        bad = self.bundle / "candidate/c-old.md"
        head, body = bad.read_text().split("\n---\n", 1)
        data = json.loads(head[4:])
        data["trigger"] = "   "
        bad.write_text("---\n" + json.dumps(data) + "\n---\n" + body)
        before = tree_digest(self.bundle)
        with self.assertRaisesRegex(ValueError, "description"):
            self.migrate()
        self.assertEqual(before, tree_digest(self.bundle))
        self.assertFalse((self.bundle / lm.OKF_MARKER).exists())
        self.assertFalse(any(self.bundle.parent.glob("learnings.pre-okf-*")))
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))

    def test_migration_refuses_symlinked_state_directories(self):
        real = Path(self.tmp.name) / "linked-active"
        shutil.move(str(self.bundle / "active"), str(real))
        (self.bundle / "active").symlink_to(real, target_is_directory=True)
        before = tree_digest(real)
        with self.assertRaisesRegex(ValueError, "is a symlink"):
            self.migrate()
        self.assertEqual(before, tree_digest(real))
        self.assertFalse((self.bundle / lm.OKF_MARKER).exists())
        self.assertFalse(any(self.bundle.parent.glob("learnings.pre-okf-*")))

    def test_migration_refuses_a_pending_journal_and_changes_nothing(self):
        (self.bundle / ".transactions").mkdir()
        (self.bundle / ".transactions/pending.json").write_text("{}")
        before = tree_digest(self.bundle)
        with self.assertRaisesRegex(ValueError, "pending learning transition"):
            self.migrate()
        self.assertEqual(before, tree_digest(self.bundle))
        self.assertFalse((self.bundle / lm.OKF_MARKER).exists())
        self.assertFalse(any(self.bundle.parent.glob("learnings.pre-okf-*")))

    def test_migration_writes_through_a_symlinked_root(self):
        target = Path(self.tmp.name) / "dotfiles-learnings"
        shutil.move(str(self.bundle), str(target))
        self.bundle.symlink_to(target, target_is_directory=True)
        report = self.migrate()
        self.assertTrue(self.bundle.is_symlink())
        self.assertEqual(target.resolve(), self.bundle.resolve())
        self.assertEqual(target.parent, Path(report["backup"]).parent)
        self.assertTrue((target / lm.OKF_MARKER).is_file())
        self.assertIn("stays a symlink", report["rollback"])
        self.assertEqual(0, self.validate().returncode)

    def test_migration_script_acts_on_the_given_root_only(self):
        other = Path(self.tmp.name) / "other-home/.asha"
        other.mkdir(parents=True)
        copy = Path(self.tmp.name) / "rehearsal"
        shutil.copytree(GOLDEN, copy)
        result = subprocess.run([sys.executable, str(TOOLS / "migrate_learnings_okf.py"), "--root", str(copy), "--yes"],
                                capture_output=True, text=True, env={**os.environ, "ASHA_HOME": str(other), "HOME": str(other.parent)})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((copy / lm.OKF_MARKER).is_file())
        self.assertFalse((other / "learnings").exists())
        self.assertEqual(tree_digest(GOLDEN), tree_digest(self.bundle), "the patched store was not touched")
        self.assertEqual(1, subprocess.run([sys.executable, str(TOOLS / "migrate_learnings_okf.py"), "--root", str(copy)],
                                           capture_output=True, text=True).returncode, "--yes is required")


class WriterTests(FixtureCase):
    def test_writer_shape_is_gated_on_the_migration_marker(self):
        lm.propose("legacy-shape", "t", "a", project_dir=self.project, session_id="s1", reason="r")
        legacy = json.loads((self.bundle / "candidate/legacy-shape.md").read_text().split("\n---\n")[0][4:])
        self.assertNotIn("status", legacy)
        self.assertNotIn("generated", legacy)
        self.assertEqual(["type", "id", "trigger", "action", "state", "created", "updated",
                          "retirement_reason", "evidence", "applicability"], list(legacy))
        self.assertFalse((self.bundle / "log.md").exists())
        self.assertTrue((self.bundle / "active/index.md").read_text().startswith("# Active learnings"))
        self.migrate()
        lm.propose("new-shape", "Trigger text", "a", project_dir=self.project, session_id="s1", reason="r")
        new = json.loads((self.bundle / "candidate/new-shape.md").read_text().split("\n---\n")[0][4:])
        self.assertEqual("draft", new["status"])
        self.assertEqual("Trigger text", new["description"])
        self.assertEqual(date.today().isoformat(), new["generated"]["at"])
        self.assertIn("- **Creation** `new-shape` propose: candidate (asha/", (self.bundle / "log.md").read_text())
        self.assertIn("- [new-shape](./new-shape.md): Trigger text", (self.bundle / "candidate/index.md").read_text())
        self.assertEqual(0, self.validate().returncode)

    def test_corroboration_bumps_stale_after_and_extend_is_for_candidates(self):
        self.migrate()
        lm.propose("bump", "t", "a", project_dir=self.project, session_id="s1", reason="r")
        learning = lm.load("bump")
        learning.updated = (date.today() - timedelta(days=100)).isoformat()
        with lm._global_lock():
            lm._save_unlocked(learning)
        self.assertIn("bump", lm.maintain_candidates(project_dir=self.project))
        lm.corroborate("bump", project_dir=self.project, session_id="s2", reason="again")
        self.assertNotIn("bump", lm.maintain_candidates(project_dir=self.project))
        record = json.loads((self.bundle / "candidate/bump.md").read_text().split("\n---\n")[0][4:])
        self.assertEqual(f"{(date.today() + timedelta(days=90)).isoformat()}T00:00:00+00:00", record["stale_after"])
        learning = lm.load("bump")
        learning.updated = (date.today() - timedelta(days=100)).isoformat()
        with lm._global_lock():
            lm._save_unlocked(learning)
        lm.extend("bump", project_dir=self.project)
        self.assertNotIn("bump", lm.maintain_candidates(project_dir=self.project))
        self.assertIn("`bump` extend: candidate", (self.bundle / "log.md").read_text())
        with self.assertRaisesRegex(ValueError, "only a candidate"):
            lm.extend("a-one", project_dir=self.project)

    def test_verify_records_people_only(self):
        self.migrate()
        with self.assertRaisesRegex(ValueError, "human:"):
            lm.verify("a-one", project_dir=self.project, by="asha/3.0.0")
        with self.assertRaisesRegex(ValueError, "human:"):
            lm.verify("a-one", project_dir=self.project, by="human:")
        lm.verify("a-one", project_dir=self.project)
        record = json.loads((self.bundle / "active/a-one.md").read_text().split("\n---\n")[0][4:])
        self.assertEqual([{"by": "human:keeper", "at": date.today().isoformat()}], record["verified"])
        self.assertEqual(record["verified"], [dict(v) for v in lm.load("a-one").verified])
        self.assertEqual(0, self.validate().returncode)

    def test_an_invalid_record_is_refused_before_any_write(self):
        self.migrate()
        class Sentinel(Exception):
            pass
        with mock.patch.object(lm, "_check_record", side_effect=Sentinel("seam")):
            with self.assertRaises(Sentinel):
                lm.propose("refused", "t", "a", project_dir=self.project, session_id="s1", reason="r")
        self.assertFalse((self.bundle / "candidate/refused.md").exists())
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))
        self.assertNotIn("refused", (self.bundle / "log.md").read_text())
        # The real check refuses an empty trigger: no title/description, nothing written.
        with self.assertRaisesRegex(ValueError, "description"):
            lm.propose("blank", "   ", "a", project_dir=self.project, session_id="s1", reason="r")
        self.assertFalse((self.bundle / "candidate/blank.md").exists())
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))

    def test_a_code_point_yaml_refuses_is_rejected_before_any_write(self):
        self.migrate()
        before = tree_digest(self.bundle)
        for text in ("step one\x7f", "c1 \x85 control", "noncharacter \ufffe", "lone \udcff surrogate"):
            with self.assertRaisesRegex(ValueError, "YAML or UTF-8 refuses"):
                lm.propose("rejected", "t", text, project_dir=self.project, session_id="s1", reason="r")
        self.assertEqual(before, tree_digest(self.bundle))
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))
        self.assertIn("a-one", lm.render_active())
        # The legacy writer is untouched: no marker, no refusal, bytes as before.
        legacy = Path(self.tmp.name) / "legacy-home/learnings"
        shutil.copytree(GOLDEN, legacy)
        with mock.patch.object(lm, "learnings_dir", lambda: legacy):
            learning = lm.propose("legacy-del", "t", "has \x7f del", project_dir=self.project, session_id="s1", reason="r")
            self.assertIn("\x7f", (legacy / "candidate/legacy-del.md").read_text())
            self.assertEqual("has \x7f del", lm.load("legacy-del").action)
        self.assertEqual(learning.state, "candidate")

    def test_an_id_the_link_grammar_cannot_address_is_refused(self):
        self.migrate()
        with self.assertRaisesRegex(ValueError, "would not parse as one link"):
            lm.propose("bad]id", "t", "a", project_dir=self.project, session_id="s1", reason="r")
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))
        self.assertFalse(list((self.bundle / "candidate").glob("bad-id*")))
        # A ')' inside the link text is fine under the grammar; only ']' breaks it.
        lm.propose("paren)id", "t", "a", project_dir=self.project, session_id="s1", reason="r")
        self.assertEqual(0, self.validate().returncode, self.validate().stdout)

    def test_a_failing_index_or_log_write_never_leaves_a_journal(self):
        self.migrate()
        okf_index, _, _ = lm._okf_tools()
        with mock.patch.object(okf_index, "write_indexes", side_effect=OSError("disk says no")):
            with mock.patch("sys.stderr") as stderr:
                learning = lm.propose("survives", "Trigger", "a", project_dir=self.project, session_id="s1", reason="r")
        self.assertEqual("candidate", learning.state)
        self.assertTrue((self.bundle / "candidate/survives.md").is_file())
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))
        self.assertTrue(stderr.write.called)
        self.assertIn("survives", lm.render_active(60) + "\n".join(l.id for l in lm.list_state("candidate")))
        self.assertIn("survives", lm.maintain_candidates(project_dir=self.project) + [l.id for l in lm.list_state("candidate")])
        # The derived index is stale and the gate says so; the next save repairs it.
        self.assertEqual(1, self.validate().returncode)
        lm.corroborate("survives", project_dir=self.project, session_id="s2", reason="again")
        self.assertEqual(0, self.validate().returncode, self.validate().stdout)
        self.assertIn("- [survives](./survives.md): Trigger", (self.bundle / "candidate/index.md").read_text())

    def test_an_unpaired_backtick_is_refused_so_it_cannot_hide_a_neighbour(self):
        self.migrate()
        with self.assertRaisesRegex(ValueError, "unpaired backtick"):
            lm.propose("prompt-shape", "the shell prompt shows ` and nothing else", "a",
                       project_dir=self.project, session_id="s1", reason="r")
        with self.assertRaisesRegex(ValueError, "unpaired backtick"):
            lm.propose("id`tick", "t", "a", project_dir=self.project, session_id="s1", reason="r")
        lm.propose("paired-ok", "run `make test` before pushing", "a", project_dir=self.project, session_id="s2", reason="r")
        lm.propose("zz-later", "run `make lint` too", "a", project_dir=self.project, session_id="s3", reason="r")
        self.assertEqual(0, self.validate().returncode, self.validate().stdout)
        self.assertEqual([], list((self.bundle / ".transactions").glob("*.json")))

    def test_a_trigger_containing_a_link_is_accepted(self):
        self.migrate()
        triggers = ("see <https://example.com/x> first", "read [the spec](https://example.com) first", 'href="x" appears')
        for i, trigger in enumerate(triggers):
            lm.propose(f"linky-{i}", trigger, "a", project_dir=self.project, session_id=f"link-s{i}", reason="r")
        self.assertEqual(3, len([l for l in lm.list_state("candidate") if l.id.startswith("linky-")]))
        self.assertEqual(0, self.validate().returncode, self.validate().stdout)

    def test_a_drained_state_directory_keeps_a_clean_index(self):
        self.migrate()
        for learning in lm.list_state("candidate"):
            lm.retire(learning.id, "ruled", project_dir=self.project)
        self.assertEqual([], lm.list_state("candidate"))
        index = (self.bundle / "candidate/index.md").read_text()
        self.assertNotIn("](./", index)
        self.assertIn("- [candidate/](./candidate/index.md): 0 concepts", (self.bundle / "index.md").read_text())
        self.assertEqual(0, self.validate().returncode, self.validate().stdout)
        okf_index, _, _ = lm._okf_tools()
        self.assertEqual([], okf_index.check_indexes(self.bundle, title="Learnings"))

    def test_verify_and_extend_need_the_migrated_store(self):
        with self.assertRaisesRegex(ValueError, "needs the OKF-migrated store"):
            lm.verify("a-one", project_dir=self.project)
        with self.assertRaisesRegex(ValueError, "needs the OKF-migrated store"):
            lm.extend("c-old", project_dir=self.project)
        self.assertEqual([], lm.load("a-one").verified)
        self.assertNotIn("verified", (self.bundle / "active/a-one.md").read_text())

    def test_journal_recovery_still_runs_under_the_marker_and_is_logged(self):
        self.migrate()
        learning = lm.load("c-old")
        learning.state = "active"
        rendered = lm._render(learning)
        journal = self.bundle / ".transactions/c-old.json"
        journal.parent.mkdir(exist_ok=True)
        journal.write_text(json.dumps({"version": 2, "name": "c-old.md", "state": "active", "content": rendered,
                                       "content_sha256": hashlib.sha256(rendered.encode()).hexdigest()}))
        with self.assertRaisesRegex(ValueError, "pending learning transition"):
            lm.render_active()
        lm.maintain_candidates(project_dir=self.project)
        self.assertTrue((self.bundle / "active/c-old.md").is_file())
        self.assertFalse(journal.exists())
        self.assertIn("**Fix** `c-old` recover: active", (self.bundle / "log.md").read_text())
        self.assertIn("c-old", lm.render_active())
        self.assertEqual(0, self.validate().returncode)

    def test_okf_tools_resolve_from_the_checkout_and_the_version_matches_the_readme(self):
        okf_index, okf_log, okf_common = lm._okf_tools()
        self.assertEqual(OKF_TOOLS / "okf_index.py", Path(okf_index.__file__).resolve())
        self.assertEqual(OKF_TOOLS / "okf_log.py", Path(okf_log.__file__).resolve())
        self.assertEqual(OKF_TOOLS / "okf_common.py", Path(okf_common.__file__).resolve())
        self.assertFalse((TOOLS / "validate.py").exists(), "the v0.1 copies under session/tools are retired")
        self.assertFalse((TOOLS / "okf_common.py").exists())
        self.assertFalse((TOOLS / "visualize.py").exists())
        readme = (ROOT / "plugins/session/README.md").read_text()
        self.assertIn(f"**Version**: {lm.SESSION_PLUGIN_VERSION}\n", readme)
        self.assertEqual(f"asha/{lm.SESSION_PLUGIN_VERSION}", lm.GENERATED_BY)

    def test_a_foreign_module_with_a_tool_name_is_a_collision_not_a_reuse(self):
        import types
        fake = types.ModuleType("okf_log")
        fake.__file__ = str(Path(self.tmp.name) / "elsewhere/okf_log.py")
        saved = sys.modules.pop("okf_log", None)
        sys.modules["okf_log"] = fake
        try:
            with self.assertRaisesRegex(ValueError, "different module named okf_log"):
                lm._okf_tools()
        finally:
            del sys.modules["okf_log"]
            if saved is not None:
                sys.modules["okf_log"] = saved

    def test_status_maps_back_to_state_when_state_is_absent(self):
        self.migrate()
        path = self.bundle / "active/a-one.md"
        head, body = path.read_text().split("\n---\n", 1)
        data = json.loads(head[4:])
        del data["state"]
        data["status"] = "draft"  # disagrees with the directory on purpose
        path.write_text("---\n" + json.dumps(data) + "\n---\n" + body)
        self.assertEqual("candidate", lm.load("a-one").state)
        del data["status"]
        path.write_text("---\n" + json.dumps(data) + "\n---\n" + body)
        self.assertEqual("active", lm.load("a-one").state)


if __name__ == "__main__":
    unittest.main()
