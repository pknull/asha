"""The RP GM hands a profiled NPC to its agent only at voice-critical moments.

The continuity reviewer's `profiled_npc_no_agent` category is the rule: the GM
writes routine profiled-NPC lines itself and spawns the character agent for a
confrontation, a high-stakes interaction, a revelation or a first appearance in
a scene. The command and the GM prompt must not ask for an agent on every line.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO = Path(__file__).resolve().parents[2]
START = "plugins/rp/commands/start.md"
GM = "plugins/rp/agents/roleplay-gm.md"
REVIEWER = "plugins/write/agents/continuity-reviewer.md"

# Wordings of the retired rule: an agent for every profiled NPC line.
RETIRED = (
    r"(?i)\ball\s+profiled",
    r"(?i)never\s+writes\s+profiled",
    r"(?i)never\s+voices\s+profiled",
    r"(?i)mandatory:\s*for\s+each\s+profiled",
    r"(?i)any\s+npc\s+with\s+profile_file",
    r"(?i)pc\s+dialogue\s*→\s*spawn\s+character\s+agents",
)
MOMENTS = (r"(?i)confrontation", r"(?i)high-stakes", r"(?i)revelation", r"(?i)first appearance in (?:a |the )?scene")


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class NpcHandoffTests(unittest.TestCase):
    def test_the_all_profiled_wording_is_gone(self):
        for rel in (START, GM):
            text = read(rel)
            for pattern in RETIRED:
                with self.subTest(rel=rel, pattern=pattern):
                    self.assertNotRegex(text, pattern)

    def test_the_reviewer_rule_is_the_one_restated(self):
        category = read(REVIEWER).split("### 7. profiled_npc_no_agent", 1)[1].split("\n### 8.", 1)[0]
        self.assertIn("routine dialogue", category)
        for pattern in MOMENTS:
            with self.subTest(pattern=pattern):
                self.assertRegex(category, pattern)

    def test_start_orchestration_mode_states_the_voice_critical_rule(self):
        mode = read(START).split("### Orchestration Mode", 1)[1].split("\n## ", 1)[0]
        self.assertRegex(mode, r"(?i)routine")
        self.assertRegex(mode, r"(?i)voice-critical")
        for pattern in MOMENTS:
            with self.subTest(pattern=pattern):
                self.assertRegex(mode, pattern)

    def test_gm_description_and_panel_tree_state_the_voice_critical_rule(self):
        text = read(GM)
        match = re.search(r"(?m)^description: (.+)$", text)
        self.assertIsNotNone(match)
        description = match.group(1)
        self.assertRegex(description, r"(?i)voice-critical")
        self.assertRegex(description, r"(?i)routine")
        tree = text.split("# The Panel Architecture", 1)[1].split("## Your Authority", 1)[0]
        self.assertRegex(tree, r"(?i)voice-critical")
        self.assertRegex(tree, r"(?i)routine")


if __name__ == "__main__":
    unittest.main()
