#!/usr/bin/env python3
"""
Tests for activeContext.md section-aware merge in pattern_analyzer.

Covers the parenthetical-heading bug observed in /session:save:
- Auto-synth generates "## What Was Accomplished" (generic file list).
- User has hand-curated "## What Was Accomplished (2026-05-06 — note)"
  with concrete content describing the actual session.
- Existing merge logic only matched headings exactly, so the user's
  parenthetical variant did not consume the auto's generic version.
  Result: generic auto block appeared at top, user's curated block
  duplicated lower in the file.

Expected behaviour after fix:
- "What Was Accomplished" is treated as a user-owned section with
  prefix tolerance ("What Was Accomplished (...)" matches the slot).
- When existing has any matching variant, all those variants are
  preserved and the auto's generic version is dropped.
- When existing has no matching variant (first synth), auto goes through.
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class MergePreservingCuratedTests(unittest.TestCase):
    """Validate that user-curated parenthetical sections survive synthesis."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_pa_merge_")
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)

        self._saved_env = {
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
        }
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)

        for mod in ("pattern_analyzer",):
            sys.modules.pop(mod, None)
        import pattern_analyzer  # type: ignore[reportMissingImports]  # noqa: E402
        self.pa = pattern_analyzer

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Bug repro: parenthetical heading
    # -------------------------------------------------------------------------

    def test_parenthetical_accomplished_heading_preserves_existing(self):
        """User's '## What Was Accomplished (date — note)' must NOT be
        clobbered by auto's generic '## What Was Accomplished' block."""
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished\n\n"
            "- Created 27 file(s): generic-file-list-here\n"
            "- Modified 30 file(s): another-generic-list\n\n"
            "## What Was Learned\n\n"
            "- No new patterns detected\n\n"
            "## Next Steps\n\n"
            "- Review and plan next session\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished (2026-05-06 — keeper Operating Signature)\n\n"
            "- **Added Operating Signature section to keeper.md** "
            "with concrete details about the work this session.\n"
            "- Skipped redundant aesthetic block from source profile.\n\n"
            "## What Was Learned\n\n"
            "- (stale, will be replaced)\n\n"
            "## Next Steps\n\n"
            "- [ ] Concrete pickup with file paths and tool names\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        # User's parenthetical heading is preserved verbatim
        self.assertIn(
            "## What Was Accomplished (2026-05-06 — keeper Operating Signature)",
            merged,
        )
        # Generic auto block content is absent
        self.assertNotIn("Created 27 file(s): generic-file-list-here", merged)
        self.assertNotIn("Modified 30 file(s): another-generic-list", merged)
        # User's curated content survives
        self.assertIn(
            "Added Operating Signature section to keeper.md",
            merged,
        )
        # No duplicate "What Was Accomplished" header (bare + parenthetical)
        bare_count = merged.count("\n## What Was Accomplished\n")
        self.assertEqual(
            bare_count, 0,
            "Generic '## What Was Accomplished' header should not appear "
            "alongside the user's parenthetical variant",
        )

    def test_first_synth_no_existing_accomplished_uses_auto(self):
        """If existing has NO 'What Was Accomplished' variant, auto's
        generic version should go through (first-synth case)."""
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished\n\n"
            "- Created some files\n\n"
            "## What Was Learned\n\n"
            "- nothing yet\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## Some Custom Section\n\n"
            "- user content\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        self.assertIn("## What Was Accomplished", merged)
        self.assertIn("Created some files", merged)
        # Custom section preserved as well
        self.assertIn("## Some Custom Section", merged)
        self.assertIn("user content", merged)

    def test_multiple_parenthetical_variants_all_preserved(self):
        """User stacks 'What Was Accomplished (date1)' and
        'What Was Accomplished (date2)' — both should survive."""
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished\n\n"
            "- generic auto content\n\n"
            "## What Was Learned\n\n"
            "- pattern X\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished (2026-05-06 evening)\n\n"
            "- session B work\n\n"
            "## What Was Accomplished (2026-05-06 morning)\n\n"
            "- session A work\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        self.assertIn("## What Was Accomplished (2026-05-06 evening)", merged)
        self.assertIn("## What Was Accomplished (2026-05-06 morning)", merged)
        self.assertIn("session A work", merged)
        self.assertIn("session B work", merged)
        self.assertNotIn("generic auto content", merged)

    def test_what_was_learned_remains_auto_managed(self):
        """'What Was Learned' is intentionally auto-managed: pattern
        detection from events is more useful than user curation."""
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Learned\n\n"
            "- fresh pattern from events\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Learned\n\n"
            "- stale hand-curated learning\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        self.assertIn("fresh pattern from events", merged)
        self.assertNotIn("stale hand-curated learning", merged)

    # -------------------------------------------------------------------------
    # Bug repro: stub-section recurrence (slot-isolation in user-owned merge)
    # -------------------------------------------------------------------------

    def test_blockers_and_next_steps_slot_isolated_during_merge(self):
        """When auto emits all four stub sections AND existing has all four
        user-curated, the merge must preserve each user's variant in its own
        slot — NOT consume them all when 'What Was Accomplished' is
        processed and then fall through to else-branch stubs for the rest.
        """
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished\n\n"
            "- Created 1 file(s): foo\n\n"
            "## What Was Learned\n\n"
            "- pattern X\n\n"
            "## Current Blockers\n\n"
            "- None detected\n\n"
            "## Next Steps\n\n"
            "- Review and plan next session\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished (2026-05-08 — real session)\n\n"
            "- Real curated work happened here.\n\n"
            "## Current Blockers\n\n"
            "- Real blocker that matters.\n\n"
            "## Next Steps\n\n"
            "- [ ] Real concrete pickup with file paths.\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        # User's curated content survives in EACH slot.
        self.assertIn("Real curated work happened here.", merged)
        self.assertIn("Real blocker that matters.", merged)
        self.assertIn("Real concrete pickup with file paths.", merged)
        # Auto's stub bodies are NOT in the merged output.
        self.assertNotIn("- None detected", merged)
        self.assertNotIn("- Review and plan next session", merged)
        self.assertNotIn("Created 1 file(s)", merged)
        # No bare stub headers floating without curated content above.
        # (The user's variants are the only Current Blockers / Next Steps headers.)
        bare_blockers = merged.count("\n## Current Blockers\n")
        bare_next = merged.count("\n## Next Steps\n")
        self.assertEqual(bare_blockers, 1, "exactly one Current Blockers section")
        self.assertEqual(bare_next, 1, "exactly one Next Steps section")

    def test_pure_stub_dropped_when_no_existing_variant(self):
        """First-synth case for a slot: if auto emits a pure-stub body and
        there's no existing variant for that slot, the stub is dropped
        entirely rather than seeded into the file (where future merges
        would preserve it indefinitely as 'existing user-owned content').
        """
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## What Was Accomplished\n\n"
            "- Real accomplishment from events\n\n"
            "## Current Blockers\n\n"
            "- None detected\n\n"
            "## Next Steps\n\n"
            "- Review and plan next session\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## Some Custom Section\n\n"
            "- existing custom content\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        self.assertIn("Real accomplishment from events", merged)
        # Stub-only sections are dropped when no existing variant exists.
        self.assertNotIn("## Current Blockers", merged)
        self.assertNotIn("## Next Steps", merged)
        self.assertNotIn("None detected", merged)
        self.assertNotIn("Review and plan next session", merged)
        # Custom section preserved.
        self.assertIn("## Some Custom Section", merged)

    def test_real_blockers_pass_through_when_no_existing(self):
        """If auto emits a NON-stub Current Blockers (real errors detected)
        and no existing variant exists, it should pass through unchanged."""
        auto = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## Current Blockers\n\n"
            "- Error: connection refused on localhost:8080\n"
            "- Blocked: pending review on PR #42\n\n"
        )
        existing = (
            "---\nversion: \"2.0\"\n---\n\n"
            "# Active Context\n\n"
            "## Some Custom\n\n"
            "- nope\n\n"
        )

        merged = self.pa._merge_preserving_curated(auto, existing)

        self.assertIn("## Current Blockers", merged)
        self.assertIn("Error: connection refused on localhost:8080", merged)
        self.assertIn("Blocked: pending review on PR #42", merged)


class CalibrationDedupTests(unittest.TestCase):
    """Validate that calibration signal writers dedup against existing content."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_pa_calib_")
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)

        self._saved_env = {
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
            "HOME": os.environ.get("HOME"),
        }
        # Redirect HOME so VOICE_FILE / KEEPER_FILE land under tmp.
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        os.environ["HOME"] = str(self.tmp)

        for mod in ("pattern_analyzer",):
            sys.modules.pop(mod, None)
        import pattern_analyzer  # type: ignore[reportMissingImports]  # noqa: E402
        self.pa = pattern_analyzer

        # Re-resolve VOICE_FILE / KEEPER_FILE to tmp HOME.
        self.pa.VOICE_FILE = Path(self.tmp) / ".asha" / "voice.md"
        self.pa.KEEPER_FILE = Path(self.tmp) / ".asha" / "keeper.md"
        self.pa.VOICE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_filter_new_calibration_signals_drops_duplicates(self):
        existing = (
            "## Calibration Log\n\n"
            "```\n"
            "2026-05-07T10:00:00 | life | \"already in keeper\"\n"
            "```\n"
        )
        signals = [
            {"text": "already in keeper", "category": "x", "timestamp": "2026-05-08"},
            {"text": "fresh new signal", "category": "y", "timestamp": "2026-05-08"},
        ]
        filtered = self.pa._filter_new_calibration_signals(signals, existing, truncate=60)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["text"], "fresh new signal")

    def test_append_to_keeper_skips_duplicates(self):
        """Live keeper.md write path: a signal whose text is already
        present must not be re-emitted with a fresh timestamp."""
        keeper_initial = (
            "---\ntype: human\n---\n# Keeper\n\n"
            "## Calibration Log\n\n"
            "```\n"
            "2026-05-07T10:00:00+10:00 | life | \"already-known signal\"\n"
            "```\n"
        )
        self.pa.KEEPER_FILE.write_text(keeper_initial)

        signals = [
            # Duplicate (already in keeper truncated to 60 chars)
            {"text": "already-known signal", "category": "x", "timestamp": "2026-05-08"},
            # New
            {"text": "this is a brand new calibration signal", "category": "y", "timestamp": "2026-05-08"},
        ]
        self.pa.append_to_keeper(signals)

        result = self.pa.KEEPER_FILE.read_text()
        # Original entry unchanged.
        self.assertIn("2026-05-07T10:00:00+10:00 | life | \"already-known signal\"", result)
        # No second occurrence with a 2026-05-08 timestamp.
        already_known_count = result.count("\"already-known signal\"")
        self.assertEqual(already_known_count, 1, "duplicate signal must not be re-emitted")
        # New signal landed.
        self.assertIn("brand new calibration signal", result)

    def test_append_to_keeper_no_signals_after_filter_is_noop(self):
        """If every signal is already present, the file should be unchanged."""
        keeper_initial = (
            "---\ntype: human\n---\n# Keeper\n\n"
            "## Calibration Log\n\n"
            "```\n"
            "2026-05-07T10:00:00+10:00 | life | \"signal one\"\n"
            "2026-05-07T10:00:00+10:00 | life | \"signal two\"\n"
            "```\n"
        )
        self.pa.KEEPER_FILE.write_text(keeper_initial)
        before = self.pa.KEEPER_FILE.read_text()

        signals = [
            {"text": "signal one", "category": "x", "timestamp": "2026-05-08"},
            {"text": "signal two", "category": "y", "timestamp": "2026-05-08"},
        ]
        self.pa.append_to_keeper(signals)

        after = self.pa.KEEPER_FILE.read_text()
        self.assertEqual(before, after, "all-duplicate batch must be a no-op")

    def test_append_to_voice_skips_duplicates(self):
        voice_initial = (
            "## Calibration Log\n\n"
            "- 2026-05-07: \"reduce whimsy in technical writing\" (whimsy)\n\n"
        )
        self.pa.VOICE_FILE.write_text(voice_initial)

        signals = [
            # Duplicate
            {"text": "reduce whimsy in technical writing", "category": "whimsy",
             "timestamp": "2026-05-08T10:00:00Z"},
            # New
            {"text": "watch for AI-tell phrasings in prose", "category": "ai_tell",
             "timestamp": "2026-05-08T10:00:00Z"},
        ]
        self.pa.append_to_voice(signals)

        result = self.pa.VOICE_FILE.read_text()
        # Duplicate count remains 1.
        self.assertEqual(result.count("reduce whimsy in technical writing"), 1)
        self.assertIn("AI-tell phrasings in prose", result)


if __name__ == "__main__":
    unittest.main()
