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


if __name__ == "__main__":
    unittest.main()
