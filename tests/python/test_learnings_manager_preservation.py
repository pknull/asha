#!/usr/bin/env python3
"""
Tests for learnings_manager round-trip preservation.

Bug class (Todoist 6gVq6vw4W5rHC5ww): write_learnings rebuilds the file
from scratch using only successfully-parsed Learning objects. Any content
that doesn't match the canonical regex (intro text under a category,
malformed entries, custom prose, trailing notes) is silently dropped on
the next parse → write round trip.

Round-trip contract under test:
- Read file → parse → write → file should be byte-identical (or as close
  as the canonical formatter can get) for inputs that contain extras.
- Specifically, unparseable content must NOT vanish.
"""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class LearningsRoundTripTests(unittest.TestCase):
    """Validate that round-tripping learnings.md preserves unparseable sections."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_lm_preserve_")
        self.fake_home = Path(self.tmp)
        (self.fake_home / ".asha").mkdir()
        self.learnings_file = self.fake_home / ".asha" / "learnings.md"

        for mod in ("learnings_manager",):
            sys.modules.pop(mod, None)
        # Patch Path.home() so the module picks up our tmp path on import
        self._home_patch = patch("pathlib.Path.home", return_value=self.fake_home)
        self._home_patch.start()
        import learnings_manager  # type: ignore[reportMissingImports]
        self.lm = learnings_manager
        # Defensive: rebind in case the module already cached LEARNINGS_PATH
        self.lm.LEARNINGS_PATH = self.learnings_file

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Bug repro: round-trip drops unparseable content
    # -------------------------------------------------------------------------

    def test_intro_text_under_category_survives_round_trip(self):
        """Prose between '## Category' and the first '### entry' must
        survive a parse → write cycle."""
        original = """# Learnings

Cross-project patterns with confidence tracking. Consulted at session start.

---

## Asha Rollout

This section tracks Asha rollout learnings. Confidence rises on confirm,
falls on contradict. Old entries decay to archive after 90 days.

### canonical-entry
- **Confidence**: 0.9
- **Trigger**: Adding entries to ~/.asha/learnings.md
- **Action**: Use ONLY the canonical schema in that exact order.
- **Evidence**:
  - 2026-04-29 | life | initial entry

"""
        self.learnings_file.write_text(original)

        learnings = self.lm.parse_learnings()
        self.lm.write_learnings(learnings)

        rewritten = self.learnings_file.read_text()
        self.assertIn(
            "This section tracks Asha rollout learnings.",
            rewritten,
            "Intro prose under '## Asha Rollout' was clobbered on round-trip",
        )
        self.assertIn(
            "Old entries decay to archive after 90 days.",
            rewritten,
        )

    def test_malformed_entry_under_category_preserved(self):
        """A '### entry' that doesn't match the canonical field order
        (e.g. has Shipped/Verdict instead of Trigger/Action) must NOT
        vanish on round-trip — it should be preserved verbatim alongside
        properly-parsed entries."""
        original = """# Learnings

Cross-project patterns.

---

## Acceptance Log

### a1-shipped
- **Shipped**: 2026-04-17
- **Verdict**: TENTATIVE PASS
- **Recheck**: 2026-05-04

### canonical-passed
- **Confidence**: 0.9
- **Trigger**: A2 acceptance verified
- **Action**: PASS. Manifest-driven bootstrap.
- **Evidence**:
  - 2026-04-28 | life | scan complete

"""
        self.learnings_file.write_text(original)

        learnings = self.lm.parse_learnings()
        self.lm.write_learnings(learnings)

        rewritten = self.learnings_file.read_text()
        self.assertIn(
            "### a1-shipped",
            rewritten,
            "Malformed (non-canonical) entry was dropped on round-trip",
        )
        self.assertIn("**Shipped**: 2026-04-17", rewritten)
        self.assertIn("**Verdict**: TENTATIVE PASS", rewritten)
        # Canonical entry still present
        self.assertIn("### canonical-passed", rewritten)

    def test_trailing_custom_block_after_last_entry_preserved(self):
        """Free-form text after the last structured entry within a
        category (before the next ## heading or EOF) must survive."""
        original = """# Learnings

---

## Tool Usage

### structured-one
- **Confidence**: 0.7
- **Trigger**: x
- **Action**: y
- **Evidence**:
  - 2026-04-01 | proj | note

> Quoted aside: structured entries above; the bullet list below is
> intentional, captures things that don't fit the schema yet.

- not-yet-formalized item one
- not-yet-formalized item two

"""
        self.learnings_file.write_text(original)

        learnings = self.lm.parse_learnings()
        self.lm.write_learnings(learnings)

        rewritten = self.learnings_file.read_text()
        self.assertIn(
            "Quoted aside",
            rewritten,
            "Trailing custom block dropped on round-trip",
        )
        self.assertIn("not-yet-formalized item one", rewritten)
        self.assertIn("not-yet-formalized item two", rewritten)

    def test_multi_evidence_entry_all_lines_parsed(self):
        """An entry with 2+ evidence bullets must round-trip with all
        evidence lines preserved. Pre-fix bug: EVIDENCE_PATTERN missing
        re.MULTILINE meant `.+?` swallowed whole blocks, dropping all
        but the final evidence line on each round-trip."""
        original = """# Learnings

---

## Tool Usage

### prefer-edit
- **Confidence**: 0.85
- **Trigger**: File operations in this codebase
- **Action**: Use Edit for file modifications
- **Evidence**:
  - 2026-05-06 | AAS | Used 158x successfully in AAS
  - 2026-05-06 | AAS | Used 35x successfully in AAS
  - 2026-05-05 | life | confirmed pattern across sessions

"""
        self.learnings_file.write_text(original)
        learnings = self.lm.parse_learnings()

        matches = [l for l in learnings["Tool Usage"] if l.id == "prefer-edit"]
        self.assertEqual(len(matches), 1, "prefer-edit not parsed")
        prefer_edit = matches[0]
        self.assertEqual(
            len(prefer_edit.evidence), 3,
            f"Expected 3 evidence entries, got {len(prefer_edit.evidence)}: "
            f"{[ev.note for ev in prefer_edit.evidence]}",
        )

        self.lm.write_learnings(learnings)
        rewritten = self.learnings_file.read_text()
        self.assertIn("Used 158x successfully", rewritten)
        self.assertIn("Used 35x successfully", rewritten)
        self.assertIn("confirmed pattern across sessions", rewritten)

    def test_round_trip_is_idempotent(self):
        """Two consecutive parse → write cycles must produce byte-identical
        output. Pre-fix bug: leading whitespace from cached section text
        bled through, so each round-trip added a blank line at the start
        of every section, growing the file unboundedly."""
        original = """# Learnings

---

## Cat A

Intro prose.

### a1
- **Confidence**: 0.7
- **Trigger**: t
- **Action**: a
- **Evidence**:
  - 2026-04-01 | proj | note

## Cat B

### b1
- **Confidence**: 0.8
- **Trigger**: t
- **Action**: a
- **Evidence**:
  - 2026-04-02 | proj | note

"""
        self.learnings_file.write_text(original)
        self.lm.write_learnings(self.lm.parse_learnings())
        pass1 = self.learnings_file.read_text()

        self.lm.write_learnings(self.lm.parse_learnings())
        pass2 = self.learnings_file.read_text()

        self.assertEqual(
            pass1, pass2,
            "Round-trip not idempotent — file would drift on every save",
        )

    def test_canonical_only_round_trip_remains_clean(self):
        """When the file has zero extras (purely canonical), round-trip
        should not introduce noise. This guards against the preserve
        path leaking content into otherwise-clean files."""
        original = """# Learnings

Cross-project patterns with confidence tracking. Consulted at session start.

---

## Asha Rollout

### only-entry
- **Confidence**: 0.9
- **Trigger**: t
- **Action**: a
- **Evidence**:
  - 2026-04-01 | proj | note

"""
        self.learnings_file.write_text(original)

        learnings = self.lm.parse_learnings()
        self.lm.write_learnings(learnings)

        rewritten = self.learnings_file.read_text()
        # Single entry preserved
        self.assertEqual(rewritten.count("### only-entry"), 1)
        # No phantom content from preserve path
        self.assertNotIn("This section tracks", rewritten)
        self.assertNotIn("Quoted aside", rewritten)


if __name__ == "__main__":
    unittest.main()
