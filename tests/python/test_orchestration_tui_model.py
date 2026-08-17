from __future__ import annotations

import unittest

from lib.control.orchestration.tui_model import TuiModel


class OrchestrationTuiModelTests(unittest.TestCase):
    def test_tree_rows_filter_sort_detail_and_bounded_events(self) -> None:
        initiative = {"initiative_id": "i", "slug": "demo", "label": "Demo", "state": "approved"}
        nodes = [
            {"node_id": "b", "state": "approved", "type": "review", "goal": "Review"},
            {"node_id": "a", "state": "proposed", "type": "work", "goal": "Build"},
            {"node_id": "old", "state": "superseded", "type": "work", "goal": "Old"},
        ]
        attempts = [{"attempt_id": "x", "node_id": "a", "state": "allocated", "ordinal": 1}]
        events = [{"sequence": number, "type": "plan-proposed"} for number in range(1, 8)]
        model = TuiModel(initiative, nodes, attempts, events, event_limit=3)
        self.assertEqual([row["id"] for row in model.rows()], ["i", "a", "x", "b"])
        self.assertEqual([row["id"] for row in model.superseded_rows()], ["old"])
        self.assertEqual(model.detail("old")["state"], "superseded")
        self.assertEqual([item["sequence"] for item in model.event_tail()], [5, 6, 7])
        self.assertEqual(model.detail("a")["goal"], "Build")
        self.assertEqual([row["id"] for row in model.rows(query="review")], ["i", "b"])


if __name__ == "__main__":
    unittest.main()
