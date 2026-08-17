from __future__ import annotations

import unittest
from unittest import mock

from lib.control.reconcile import UnavailableAdapters
from lib.control.store import StoreError, task_digest
from lib.control.orchestration.reconcile import reconcile_nodes
from tests.python.test_control_config_model import task_record


class OrchestrationReconcileTests(unittest.TestCase):
    def test_unlinked_node_is_reported_without_control_reads(self) -> None:
        store = mock.Mock()
        store.list_links_snapshot.return_value = []
        store.list_attempts_snapshot.return_value = []
        control = mock.Mock()
        result = reconcile_nodes("11111111-1111-4111-8111-111111111111", [{"node_id": "work"}], store=store, control_store=control)
        self.assertEqual(result[0]["control_state"], "unlinked")
        control.peek.assert_not_called()

    def test_matching_mismatching_and_missing_control_records_are_observations_only(self) -> None:
        task = task_record(
            task_id="22222222-2222-4222-8222-222222222222",
            repository_root="/tmp/source", workspace_path="/tmp/workspace",
        )
        link = {
            "node_id": "work", "attempt_id": "33333333-3333-4333-8333-333333333333",
            "control_task_id": task["task_id"], "control_task_record_digest": task_digest(task),
        }
        store = mock.Mock()
        store.list_links_snapshot.return_value = [link]
        store.list_attempts_snapshot.return_value = []
        store.config.control = mock.Mock()
        control = mock.Mock()
        control.peek.return_value = task
        matched = reconcile_nodes(
            "11111111-1111-4111-8111-111111111111", [{"node_id": "work"}],
            store=store, control_store=control, adapters_factory=lambda _: UnavailableAdapters(),
        )[0]
        self.assertTrue(matched["digest_match"])
        changed = dict(link)
        changed["control_task_record_digest"] = "0" * 64
        store.list_links_snapshot.return_value = [changed]
        stale = reconcile_nodes(
            "11111111-1111-4111-8111-111111111111", [{"node_id": "work"}],
            store=store, control_store=control, adapters_factory=lambda _: UnavailableAdapters(),
        )[0]
        self.assertEqual(stale["control_state"], "stale")
        control.peek.side_effect = StoreError("task not found")
        missing = reconcile_nodes(
            "11111111-1111-4111-8111-111111111111", [{"node_id": "work"}],
            store=store, control_store=control, adapters_factory=lambda _: UnavailableAdapters(),
        )[0]
        self.assertEqual(missing["control_state"], "stale")


if __name__ == "__main__":
    unittest.main()
