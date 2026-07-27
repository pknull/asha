#!/usr/bin/env python3
"""
Tests for the OKF concept-bundle learnings store.

Replaces test_learnings_manager_preservation.py: the old flat-file round-trip
"preservation" machinery is gone (one-concept-per-file removes the co-mingled
blob it existed to protect). These tests cover the directory backend, the migrator,
the guardrail's per-file strip, and — critically — that the public API's return
shapes are frozen (pattern_analyzer swallows exceptions, so a regression there
would be invisible in production).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import importlib.util
HAVE_YAML = importlib.util.find_spec("yaml") is not None


class OKFLearningsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_okf_")
        self.asha = Path(self.tmp) / ".asha"
        self.asha.mkdir(parents=True)

        # A project with a Memory/ dir so silence detection is deterministic
        # (resolves to this project; no marker => not silenced).
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory").mkdir(parents=True)
        (self.project / "Work" / "markers").mkdir(parents=True)

        self._saved_env = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_PROJECT_DIR")}
        os.environ["HOME"] = str(self.tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)

        for mod in ("learnings_manager", "migrate_learnings_to_okf", "save_guardrail"):
            sys.modules.pop(mod, None)
        import learnings_manager  # type: ignore[reportMissingImports]
        import migrate_learnings_to_okf  # type: ignore[reportMissingImports]
        import save_guardrail  # type: ignore[reportMissingImports]
        self.lm = learnings_manager
        self.mig = migrate_learnings_to_okf
        self.sg = save_guardrail

        # Rebind all path globals to the sandbox (defensive — independent of
        # Path.home() resolution timing).
        self.lm.ASHA_DIR = self.asha
        self.lm.LEARNINGS_DIR = self.asha / "learnings"
        self.mig.LEGACY_HOT = self.asha / "learnings.md"
        self.mig.LEGACY_COLD = self.asha / "learnings-archive.md"

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def _confidences_in(self, rendered):
        return [float(line.split(":", 1)[1])
                for line in rendered.splitlines()
                if line.startswith("- **Confidence**:")]

    def _block_count(self, rendered):
        return sum(1 for line in rendered.splitlines() if line.startswith("### "))


class UpsertAndConfidenceTests(OKFLearningsTestBase):
    def test_upsert_by_slug_no_duplicates(self):
        r1 = self.lm.add_learning("Cat", "ollama-http", "t", "a", "p1", "first")
        self.assertEqual(r1["status"], "created")
        self.assertAlmostEqual(r1["confidence"], 0.3)

        r2 = self.lm.add_learning("Cat", "ollama-http", "t", "a", "p2", "second")
        self.assertEqual(r2["status"], "updated")
        self.assertGreater(r2["confidence"], 0.3)

        files = [p for p in self.lm.LEARNINGS_DIR.glob("*.md") if p.name != "index.md"]
        self.assertEqual(len(files), 1, "upsert must not create a duplicate file")
        body = files[0].read_text()
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_confirm_and_contradict_math_and_removal(self):
        self.lm.add_learning("Cat", "x", "t", "a", "p", "init")  # 0.3
        c = self.lm.confirm_learning("x", "p", "ok")
        # 0.3 -> min(0.9, 0.3 + 0.1*0.6) = 0.36
        self.assertAlmostEqual(c["confidence"], 0.36)

        # Drive confidence down until removed (<0.2 deletes the file).
        last: dict = {}
        for _ in range(10):
            last = self.lm.contradict_learning("x", "p", "nope")
            if last["status"] == "removed":
                break
        self.assertEqual(last["status"], "removed")
        self.assertFalse(self.lm._learning_path("x").exists())

    def test_not_found_status(self):
        self.assertEqual(self.lm.confirm_learning("nope", "p")["status"], "not_found")
        self.assertEqual(self.lm.contradict_learning("nope", "p", "r")["status"], "not_found")


class AtomicWriteTests(OKFLearningsTestBase):
    def test_atomic_write_failure_leaves_original_and_no_orphan(self):
        self.lm.add_learning("Cat", "keep", "t", "a", "p", "r")
        path = self.lm._learning_path("keep")
        before = path.read_text()

        with mock.patch.object(self.lm.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.lm._write_learning(self.lm._parse_file(path))

        self.assertEqual(path.read_text(), before, "original must be untouched on failure")
        orphans = [p for p in self.lm.LEARNINGS_DIR.iterdir() if p.name.startswith(".")]
        self.assertEqual(orphans, [], "no temp/orphan files may remain")


class HotTierTests(OKFLearningsTestBase):
    def _seed(self, n):
        # confidences 0.60..0.60+0.02*(n-1); i>=5 cross 0.70
        for i in range(n):
            conf = round(0.60 + 0.02 * i, 2)
            learning = self.lm.Learning(
                id=f"l{i:02d}", category="C", confidence=conf,
                trigger="t", action="a",
                evidence=[self.lm.Evidence("2026-01-01", "p", "n", "initial")],
                created="2026-01-01", updated=f"2026-01-{(i % 28) + 1:02d}",
            )
            self.lm._atomic_write_file(self.lm._learning_path(learning.id),
                                       self.lm._render_learning(learning))

    def test_cap_and_ordering(self):
        self._seed(15)  # 10 entries >= 0.70
        rendered = self.lm.render_hot_tier(max_entries=10, max_bytes=1_000_000)
        confs = self._confidences_in(rendered)
        self.assertEqual(len(confs), 10, "hot tier capped at 10 entries")
        self.assertTrue(all(c >= 0.70 for c in confs), "all hot entries >= 0.70")
        self.assertEqual(confs, sorted(confs, reverse=True), "confidence-descending order")

    def test_injection_byte_budget_truncates_at_boundary(self):
        self._seed(15)
        full = self.lm.render_hot_tier(max_entries=10, max_bytes=1_000_000)
        budget = len(full.encode("utf-8")) // 2
        truncated = self.lm.render_hot_tier(max_entries=10, max_bytes=budget)
        self.assertLessEqual(len(truncated.encode("utf-8")), budget)
        self.assertGreaterEqual(self._block_count(truncated), 1, "at least one entry emitted")
        self.assertLess(self._block_count(truncated), self._block_count(full), "truncation occurred")

    def test_empty_hot_tier_is_preamble_only(self):
        self.lm.add_learning("C", "low", "t", "a", "p", "r")  # 0.3, below 0.7
        rendered = self.lm.render_hot_tier(max_bytes=3000)
        self.assertEqual(self._block_count(rendered), 0)
        self.assertIn("Cross-project patterns", rendered)


class IndexTierTests(OKFLearningsTestBase):
    """render_index_tier: one-line-per-concept index-first injection."""

    def _seed(self, n, trigger="t", action="a"):
        for i in range(n):
            conf = round(0.30 + 0.03 * i, 2)
            learning = self.lm.Learning(
                id=f"idx{i:02d}", category="C", confidence=conf,
                trigger=trigger, action=action,
                evidence=[self.lm.Evidence("2026-01-01", "p", "n", "initial")],
                created="2026-01-01", updated="2026-01-01",
            )
            self.lm._atomic_write_file(self.lm._learning_path(learning.id),
                                       self.lm._render_learning(learning))

    def _index_lines(self, rendered):
        return [l for l in rendered.splitlines() if l.startswith("- ")]

    def test_every_concept_gets_one_line_hot_first(self):
        self._seed(6)
        rendered = self.lm.render_index_tier(max_bytes=1_000_000)
        lines = self._index_lines(rendered)
        self.assertEqual(len(lines), 6, "whole bundle indexed, not just the hot tier")
        confs = [float(l.split("[C ", 1)[1].split("]", 1)[0]) for l in lines]
        self.assertEqual(confs, sorted(confs, reverse=True), "confidence-descending order")
        self.assertIn("idx05", lines[0], "highest confidence first")

    def test_hook_is_compressed_and_capped(self):
        self._seed(1, trigger="word " * 60, action="line\none\ntwo   three")
        rendered = self.lm.render_index_tier(max_bytes=1_000_000)
        line = self._index_lines(rendered)[0]
        self.assertNotIn("\t", line)
        self.assertLess(len(line), 200, "hook capped near _INDEX_HOOK_CHARS")
        self.assertIn("…", line, "over-cap hook carries an ellipsis")

    def test_multiline_hook_never_breaks_one_line_contract(self):
        self._seed(1, trigger="a\nb\nc", action="d\ne")
        rendered = self.lm.render_index_tier(max_bytes=1_000_000)
        line = self._index_lines(rendered)[0]
        self.assertIn("a b c -> d e", line, "newlines collapse to spaces")

    def test_budget_truncates_with_honest_tail(self):
        self._seed(12)
        full = self.lm.render_index_tier(max_bytes=1_000_000)
        budget = len(full.encode("utf-8")) // 2
        truncated = self.lm.render_index_tier(max_bytes=budget)
        self.assertLessEqual(len(truncated.encode("utf-8")), budget + 120,
                             "tail reservation keeps output near budget")
        shown = len(self._index_lines(truncated))
        self.assertGreaterEqual(shown, 1)
        self.assertLess(shown, 12)
        self.assertIn(f"{12 - shown} more concept(s) omitted", truncated)
        self.assertIn("full index: ~/.asha/learnings/index.md", truncated)

    def test_empty_bundle_is_preamble_only(self):
        self.lm.LEARNINGS_DIR.mkdir(exist_ok=True)
        rendered = self.lm.render_index_tier(max_bytes=3000)
        self.assertEqual(self._index_lines(rendered), [])
        self.assertIn("Learnings index", rendered)


class RetireTests(OKFLearningsTestBase):
    """retire: concluded records leave the live bundle for the archive."""

    def test_retire_moves_file_and_leaves_every_live_surface(self):
        self.lm.add_learning("Rollout", "b9-acceptance", "phase b9 verification",
                             "PASS", "asha", "closed record")
        self.lm.add_learning("Tooling", "keeper", "still live", "act", "p", "r")
        result = self.lm.retire_learning("b9-acceptance", "phase shipped; can never recur")
        self.assertEqual(result["status"], "retired")

        live = [p.name for p in self.lm.LEARNINGS_DIR.glob("*.md")]
        self.assertNotIn("b9-acceptance.md", live)
        archived = self.asha / self.lm.ARCHIVE_DIR_NAME / "b9-acceptance.md"
        self.assertTrue(archived.is_file())
        text = archived.read_text()
        self.assertIn("retired:", text)
        self.assertIn("phase shipped; can never recur", text)
        self.assertIn("phase b9 verification", text, "full record survives archival")

        index = (self.lm.LEARNINGS_DIR / "index.md").read_text()
        self.assertNotIn("b9-acceptance", index)
        self.assertIn("keeper", index)
        self.assertNotIn("b9-acceptance", self.lm.render_index_tier(max_bytes=100_000))

    def test_retire_unknown_id_errors_without_side_effects(self):
        result = self.lm.retire_learning("no-such-concept", "n/a")
        self.assertIn("error", result)
        self.assertFalse((self.asha / self.lm.ARCHIVE_DIR_NAME).exists())

    def test_retired_concept_is_not_retrievable(self):
        self.lm.add_learning("Cat", "xanthic-probe", "xanthic probe calibration",
                             "act", "p", "r")
        self.lm.retire_learning("xanthic-probe", "concluded")
        sys.path.insert(0, str(TOOLS_DIR))
        import memory_retrieval  # type: ignore[reportMissingImports]
        entries = memory_retrieval.learning_entries(self.lm.LEARNINGS_DIR)
        self.assertEqual([e for e in entries if e.id == "xanthic-probe"], [])


class MigrationTests(OKFLearningsTestBase):
    HOT = """# Learnings

Cross-project patterns with confidence tracking. Consulted at session start.

---

## Cat A

### alpha
- **Confidence**: 0.8
- **Trigger**: ta
- **Action**: aa
- **Evidence**:
  - 2026-01-01 | proj | first [initial]
  - 2026-02-02 | proj | second

### malformed-entry
- **Confidence**: oops
not canonical at all
"""

    COLD = """## Cat B

### beta
- **Confidence**: 0.5
- **Trigger**: tb
- **Action**: ab
- **Evidence**:
  - 2026-01-03 | proj | conly

### alpha
- **Confidence**: 0.6
- **Trigger**: ta
- **Action**: aa
- **Evidence**:
  - 2026-03-03 | proj | fromcold
"""

    def _seed_legacy(self):
        (self.asha / "learnings.md").write_text(self.HOT)
        (self.asha / "learnings-archive.md").write_text(self.COLD)

    def test_migration_correctness(self):
        self._seed_legacy()
        rc = self.mig.run(dry_run=False)
        self.assertEqual(rc, 0)

        # malformed block is NOT migrated (and the file is retained).
        self.assertFalse(self.lm._learning_path("malformed-entry").exists())
        self.assertTrue((self.asha / "learnings.md").exists(), "legacy file retained")

        learnings = self.lm.parse_learnings()
        flat = {l.id: l for entries in learnings.values() for l in entries}
        self.assertIn("alpha", flat)
        self.assertIn("beta", flat)

        # alpha merged across tiers: max confidence (0.8), evidence unioned (3).
        self.assertAlmostEqual(flat["alpha"].confidence, 0.8)
        self.assertEqual(len(flat["alpha"].evidence), 3)
        notes = {e.note for e in flat["alpha"].evidence}
        self.assertEqual(notes, {"first", "second", "fromcold"})

    def test_migration_reports_unparsed(self):
        self._seed_legacy()
        # capture stdout JSON report via dry-run
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mig.run(dry_run=True)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["unparsed_blocks"], 1)
        self.assertEqual(report["new_files"], 2)

    def test_migration_idempotent(self):
        self._seed_legacy()
        self.mig.run(dry_run=False)
        first = sorted(p.name for p in self.lm.LEARNINGS_DIR.glob("*.md"))
        self.mig.run(dry_run=False)
        second = sorted(p.name for p in self.lm.LEARNINGS_DIR.glob("*.md"))
        self.assertEqual(first, second, "re-running must not add/duplicate files")
        # alpha evidence not duplicated
        alpha = self.lm._parse_file(self.lm._learning_path("alpha"))
        self.assertEqual(len(alpha.evidence), 3)

    def test_migration_after_premature_save_strands_nothing(self):
        # A save created a concept file before migration ran.
        self.lm.add_learning("Cat C", "gamma", "tg", "ag", "p", "r")
        self._seed_legacy()
        self.mig.run(dry_run=False)
        for slug in ("gamma", "alpha", "beta"):
            self.assertTrue(self.lm._learning_path(slug).exists(),
                            f"{slug} must survive migration")

    def test_migration_stamps_superseded_banner_content_preserved(self):
        self._seed_legacy()
        self.mig.run(dry_run=False)
        for name in ("learnings.md", "learnings-archive.md"):
            content = (self.asha / name).read_text()
            self.assertTrue(content.startswith(self.lm.SUPERSEDED_SENTINEL),
                            f"{name} must lead with the supersession sentinel")
        # Original entries preserved verbatim below the banner.
        stamped_hot = (self.asha / "learnings.md").read_text()
        self.assertIn(self.HOT, stamped_hot)
        # Re-run: banner not duplicated, parse unaffected, no new files.
        self.mig.run(dry_run=False)
        self.assertEqual(
            (self.asha / "learnings.md").read_text().count(self.lm.SUPERSEDED_SENTINEL), 1)
        alpha = self.lm._parse_file(self.lm._learning_path("alpha"))
        self.assertEqual(len(alpha.evidence), 3)

    def test_migration_stamp_report_lists_first_run_only(self):
        self._seed_legacy()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mig.run(dry_run=False)
        self.assertEqual(json.loads(buf.getvalue())["legacy_stamped"],
                         ["learnings.md", "learnings-archive.md"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mig.run(dry_run=False)
        self.assertEqual(json.loads(buf.getvalue())["legacy_stamped"], [])

    def test_migration_dry_run_writes_nothing(self):
        self._seed_legacy()
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            self.mig.run(dry_run=True)
        self.assertFalse(self.lm.LEARNINGS_DIR.exists(), "dry run must not create the bundle")
        self.assertEqual((self.asha / "learnings.md").read_text(), self.HOT,
                         "dry run must not stamp the legacy file")

    def test_migration_preserves_symlink_and_stamps_external_target(self):
        # The issue-#12 scenario: learnings.md is a dotfiles symlink; the flat
        # file is backed up, the bundle is not.
        dotfiles = Path(self.tmp) / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "learnings.md"
        target.write_text(self.HOT)
        (self.asha / "learnings.md").symlink_to(target)

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.mig.run(dry_run=False)
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())

        # Coverage warning surfaced in the report.
        self.assertTrue(report["backup_coverage_warnings"],
                        "symlinked legacy file must produce a coverage warning")
        self.assertIn("learnings.md", report["backup_coverage_warnings"][0])

        # The symlink survives; the banner landed in the external target, so the
        # backed-up copy is self-describing as stale.
        link = self.asha / "learnings.md"
        self.assertTrue(link.is_symlink(), "stamping must not replace the symlink")
        stamped = target.read_text()
        self.assertTrue(stamped.startswith(self.lm.SUPERSEDED_SENTINEL))
        self.assertIn(self.HOT, stamped)

    def test_migration_no_coverage_warning_for_plain_files(self):
        self._seed_legacy()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mig.run(dry_run=False)
        self.assertEqual(json.loads(buf.getvalue())["backup_coverage_warnings"], [])


class LegacyStatusTests(OKFLearningsTestBase):
    def test_clean_when_no_legacy_files(self):
        self.lm.add_learning("C", "solo", "t", "a", "p", "r")
        status = self.lm.legacy_flat_status()
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["legacy"], [])

    def test_flags_unstamped_decoy_next_to_live_bundle(self):
        self.lm.add_learning("C", "solo", "t", "a", "p", "r")
        (self.asha / "learnings.md").write_text(MigrationTests.HOT)
        status = self.lm.legacy_flat_status()
        self.assertEqual(status["status"], "warnings")
        self.assertTrue(status["legacy"][0]["stale_decoy"])
        self.assertIn("resurrect", status["warnings"][0])

    def test_no_decoy_flag_without_bundle(self):
        # Pre-migration state: flat file only. Nothing has diverged yet.
        (self.asha / "learnings.md").write_text(MigrationTests.HOT)
        status = self.lm.legacy_flat_status()
        self.assertEqual(status["status"], "ok")
        self.assertFalse(status["legacy"][0]["stale_decoy"])

    def test_clean_after_migration_stamps(self):
        (self.asha / "learnings.md").write_text(MigrationTests.HOT)
        (self.asha / "learnings-archive.md").write_text(MigrationTests.COLD)
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            self.mig.run(dry_run=False)
        status = self.lm.legacy_flat_status()
        self.assertEqual(status["status"], "ok")
        for entry in status["legacy"]:
            self.assertTrue(entry["superseded_banner"])
            self.assertFalse(entry["stale_decoy"])

    def test_unstamped_symlink_gets_both_warnings(self):
        self.lm.add_learning("C", "solo", "t", "a", "p", "r")
        dotfiles = Path(self.tmp) / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "learnings.md"
        target.write_text(MigrationTests.HOT)
        (self.asha / "learnings.md").symlink_to(target)
        status = self.lm.legacy_flat_status()
        self.assertEqual(status["status"], "warnings")
        entry = status["legacy"][0]
        self.assertTrue(entry["symlink"])
        self.assertTrue(entry["resolves_outside_asha"])
        self.assertEqual(len(status["warnings"]), 2,
                         "decoy + backup-coverage warnings expected")


class GuardrailTests(OKFLearningsTestBase):
    def test_per_file_strip_removes_only_noise(self):
        self.lm.add_learning("C", "sequence-foo", "t", "a", "p", "r")
        self.lm.add_learning("C", "prefer-bar", "t", "a", "p", "r")
        self.lm.add_learning("C", "real-baz", "t", "a", "p", "r")

        n1 = self.sg.strip_sequence_noise(self.lm.LEARNINGS_DIR)
        n2 = self.sg.strip_prefer_noise(self.lm.LEARNINGS_DIR)
        self.assertEqual((n1, n2), (1, 1))

        self.assertFalse(self.lm._learning_path("sequence-foo").exists())
        self.assertFalse(self.lm._learning_path("prefer-bar").exists())
        self.assertTrue(self.lm._learning_path("real-baz").exists())

        # index rebuilt without the removed entries
        index = (self.lm.LEARNINGS_DIR / "index.md").read_text()
        self.assertNotIn("sequence-foo", index)
        self.assertNotIn("prefer-bar", index)
        self.assertIn("real-baz", index)

        # idempotent
        self.assertEqual(self.sg.strip_sequence_noise(self.lm.LEARNINGS_DIR), 0)


class GoldenOutputTests(OKFLearningsTestBase):
    """Freeze the public return shapes pattern_analyzer depends on."""

    def setUp(self):
        super().setUp()
        self.lm.add_learning("Cat", "k1", "trig1", "act1", "p", "r")
        self.lm.add_learning("Cat", "k2", "trig2", "act2", "p", "r")

    def test_query_shape(self):
        out = self.lm.query_learnings()
        self.assertEqual(set(out.keys()), {"count", "learnings"})
        self.assertEqual(out["count"], 2)
        self.assertEqual(
            set(out["learnings"][0].keys()),
            {"id", "category", "confidence", "trigger", "action", "evidence_count"},
        )

    def test_list_shape(self):
        out = self.lm.list_categories()
        self.assertEqual(set(out.keys()), {"categories"})
        self.assertEqual(set(out["categories"][0].keys()), {"category", "count", "avg_confidence"})

    def test_export_shape(self):
        out = self.lm.export_learnings()
        self.assertIn("Cat", out)
        entry = out["Cat"][0]
        self.assertEqual(set(entry.keys()), {"id", "confidence", "trigger", "action", "evidence"})
        self.assertEqual(set(entry["evidence"][0].keys()), {"date", "project", "note", "effect"})


class BodyPreservationTests(OKFLearningsTestBase):
    def test_unknown_body_section_survives_confirm(self):
        path = self.lm._learning_path("noted")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: learning\n"
            "id: noted\n"
            "category: Cat\n"
            "confidence: 0.5\n"
            "tier: cold\n"
            "trigger: t\n"
            "action: a\n"
            "created: '2026-01-01'\n"
            "updated: '2026-01-01'\n"
            "---\n\n"
            "# noted\n\n"
            "**Trigger:** t\n**Action:** a\n\n"
            "## Evidence\n- 2026-01-01 | p | n [initial]\n\n"
            "## Notes\nhand-written keeper note\n"
        )
        self.lm.confirm_learning("noted", "p", "ok")
        after = path.read_text()
        self.assertIn("## Notes", after)
        self.assertIn("hand-written keeper note", after)


@unittest.skipUnless(HAVE_YAML, "PyYAML required for validate.py")
class ValidateSmokeTests(OKFLearningsTestBase):
    def test_generated_bundle_passes_validate_strict(self):
        self.lm.add_learning("Cat", "one", "t1", "a1", "p", "r")
        self.lm.add_learning("Cat", "two", "t2", "a2", "p", "r")
        validate = TOOLS_DIR / "validate.py"
        proc = subprocess.run(
            [sys.executable, str(validate), str(self.lm.LEARNINGS_DIR), "--strict"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"validate failed:\n{proc.stdout}\n{proc.stderr}")


class LinkTests(OKFLearningsTestBase):
    def setUp(self):
        super().setUp()
        for lid in ("alpha", "beta", "gamma"):
            self.lm.add_learning("Cat", lid, f"trig {lid}", f"act {lid}", "p", "r")

    def _related_of(self, lid):
        return self.lm._parse_related(self.lm._parse_file(self.lm._learning_path(lid)))

    def test_link_sets_related_and_is_idempotent(self):
        r1 = self.lm.link_learnings("alpha", ["beta"], reason="pairs with")
        self.assertEqual(r1["changed"], ["alpha"])
        self.assertIn("beta", self._related_of("alpha"))
        r2 = self.lm.link_learnings("alpha", ["beta"], reason="pairs with")
        self.assertEqual(r2["changed"], [], "re-link must be idempotent")

    def test_link_bidirectional_adds_reciprocal(self):
        self.lm.link_learnings("alpha", ["beta"], reason="r", bidirectional=True)
        self.assertIn("beta", self._related_of("alpha"))
        self.assertIn("alpha", self._related_of("beta"))

    def test_link_skips_dangling_target(self):
        r = self.lm.link_learnings("alpha", ["ghost"], reason="r")
        self.assertEqual(r["changed"], [])
        self.assertNotIn("ghost", self._related_of("alpha"))

    def test_link_skips_self(self):
        r = self.lm.link_learnings("alpha", ["alpha"], reason="r")
        self.assertEqual(r["changed"], [])
        self.assertEqual(self._related_of("alpha"), {})

    def test_prune_removes_dangling_after_delete(self):
        self.lm.link_learnings("alpha", ["beta", "gamma"], reason="r")
        self.assertEqual(set(self._related_of("alpha")), {"beta", "gamma"})
        self.lm._learning_path("gamma").unlink()
        res = self.lm.prune_dangling_links()
        self.assertIn("alpha", res["files"])
        self.assertEqual(set(self._related_of("alpha")), {"beta"})

    def test_link_merges_and_preserves_reasons(self):
        self.lm.link_learnings("alpha", ["beta"], reason="first reason")
        self.lm.link_learnings("alpha", ["gamma"], reason="second reason")
        rel = self._related_of("alpha")
        self.assertEqual(rel.get("beta"), "first reason")
        self.assertEqual(rel.get("gamma"), "second reason")

    def test_related_survives_confirm(self):
        self.lm.link_learnings("alpha", ["beta"], reason="r")
        self.lm.confirm_learning("alpha", "p", "ok")
        self.assertIn("beta", self._related_of("alpha"))

    def test_link_candidates_window(self):
        import re as _re
        p = self.lm._learning_path("alpha")
        p.write_text(_re.sub(r"updated: '[^']*'", "updated: '2000-01-01'", p.read_text()))
        out = self.lm.link_candidates(days=7)
        ids = {c["id"] for c in out["candidates"]}
        self.assertNotIn("alpha", ids, "stale learning excluded from window")
        self.assertIn("beta", ids, "recent learning included")
        self.assertEqual(set(out.keys()), {"window_days", "since", "candidates", "bundle"})
        self.assertEqual({b["id"] for b in out["bundle"]}, {"alpha", "beta", "gamma"})


if __name__ == "__main__":
    unittest.main()
