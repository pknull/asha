"""Claude Stop-hook wake responses for idle Control coordinators."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
import uuid
from unittest import mock

from lib.control.cli import _event_command
from lib.control.orchestration.coordinator import claim
from lib.control.orchestration.model import record_digest
from lib.control.store import TaskStore
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_control_config_model import task_record
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class ControlWakeChannelTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.pane_id = "%7"
        self.tmux = FakeTmux(pane_id=self.pane_id)
        self.pane_env = {**self.env, "TMUX_PANE": self.pane_id}
        self.coordinator = claim(
            self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
        )
        self.task_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        self.repo.chmod(0o700)
        task = task_record(
            task_id=self.task_id,
            repository_root=str(self.repo),
            workspace_path=str(self.config.control.workspace_root / self.task_id),
        )
        task["runs"][0].update({"run_id": self.run_id, "pane_id": self.pane_id})
        TaskStore(self.config.control).save(task)
        self.managed_env = {
            **self.pane_env,
            "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_TASK_ID": self.task_id,
            "ASHA_CONTROL_RUN_ID": self.run_id,
            "ASHA_CONTROL_STATE_DIR": str(self.config.control.tasks_dir),
        }

    def invoke(self, adapter=None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        selected = self.tmux if adapter is None else adapter
        with mock.patch("lib.control.cli.TmuxAdapter", return_value=selected), \
                mock.patch("lib.control.cli._publish_tmux_presentation"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = _event_command(
                ["--event", "turn-stopped", "--pane-id", self.pane_id],
                self.managed_env,
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_journal_tail_past_cursor_returns_single_line_block_json(self) -> None:
        tail = self.initiative()["last_event_sequence"]
        cursor = self.coordinator["event_cursor"]
        count = tail - cursor

        status, stdout, stderr = self.invoke()

        reason = (
            f"Control: {count} journal event(s) after cursor {cursor} for initiative "
            f"{self.initiative_id}; run 'asha initiative wait {self.initiative_id} "
            f"--after {cursor}' or read the snapshot before ending the turn."
        )
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            json.dumps(
                {"decision": "block", "reason": reason},
                sort_keys=True, separators=(",", ":"),
            ) + "\n",
        )

    def test_same_cursor_is_not_blocked_twice(self) -> None:
        self.assertEqual(json.loads(self.invoke()[1])["decision"], "block")
        self.assertEqual(self.invoke(), (0, "{}\n", ""))

    def test_worker_pane_without_coordinator_markers_returns_empty_object(self) -> None:
        self.tmux.options.clear()
        self.assertEqual(self.invoke(), (0, "{}\n", ""))

    def test_terminal_initiative_returns_empty_object(self) -> None:
        current = self.initiative()
        terminal = copy.deepcopy(current)
        terminal.update({
            "state": "cancelled",
            "state_revision": current["state_revision"] + 1,
            "updated_at": current["updated_at"],
        })
        self.store.save_initiative(terminal, expected_digest=record_digest(current))
        self.assertEqual(self.invoke(), (0, "{}\n", ""))

    def test_internal_wake_exception_returns_empty_object_and_exit_zero(self) -> None:
        class RaisingAdapter:
            def pane_option(self, pane_id: str, option: str):
                raise RuntimeError("injected wake failure")

        self.assertEqual(self.invoke(RaisingAdapter()), (0, "{}\n", ""))


if __name__ == "__main__":
    unittest.main()
