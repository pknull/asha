from __future__ import annotations

import unittest
from unittest import mock

from lib.control.harness import HarnessError
from lib.control.reconcile import Evidence, LiveAdapters, reconcile_task
from lib.control.tmux import PaneFacts, TmuxError
from tests.python.test_control_config_model import task_record


class FakeTmux:
    def __init__(
        self,
        task: dict,
        *,
        dead_status: int | None = None,
        dead_signal: int | None = None,
        missing: bool = False,
    ) -> None:
        self.task = task
        self.dead_status = dead_status
        self.dead_signal = dead_signal
        self.missing = missing

    def has_session(self, name):
        return True

    def session_option(self, name, option):
        return "1" if option == "@asha_managed" else self.task["task_id"]

    def pane_facts(self, pane_id):
        if self.missing:
            raise TmuxError("can't find pane")
        run = self.task["runs"][0]
        return PaneFacts(
            pane_id,
            run["pid"],
            True,
            self.dead_status,
            self.dead_signal,
            self.task["tmux"]["session"],
            self.task["tmux"]["window"],
            "fixture",
        )


class FakeAdapters:
    def __init__(self, task: dict, tmux: FakeTmux, *, event_state: str = "working") -> None:
        self.live = LiveAdapters(tmux=tmux)
        self.event_state = event_state

    def tmux(self, task, run):
        return self.live.tmux(task, run)

    def process(self, task, run):
        return self.live.process(task, run)

    def jj(self, task):
        return Evidence("jj", "match", "workspace and change matched")

    def event(self, task, run):
        return Evidence(
            "event", "match", "recent event snapshot", state=self.event_state,
        )


class ControlPaneDeathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = task_record()

    def reconcile(
        self,
        tmux: FakeTmux,
        *,
        process_matches: bool | None = None,
        event_state: str = "working",
    ):
        adapters = FakeAdapters(self.task, tmux, event_state=event_state)
        if process_matches is None:
            return reconcile_task(self.task, adapters)
        with mock.patch(
            "lib.control.reconcile.harness_api.verify_process",
            return_value=process_matches,
        ):
            return reconcile_task(self.task, adapters)

    def test_signal_death_is_failed_despite_recent_working_event(self) -> None:
        result = self.reconcile(FakeTmux(self.task, dead_signal=9))

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["runs"][0]["state"], "failed")
        self.assertEqual(
            result["runs"][0]["evidence"][1],
            {
                "source": "process",
                "outcome": "missing",
                "detail": "tmux pane process was killed by signal 9",
                "state": "failed",
                "stale": False,
            },
        )

    def test_status_zero_is_exited_despite_recent_working_event(self) -> None:
        result = self.reconcile(FakeTmux(self.task, dead_status=0))

        self.assertEqual(result["state"], "exited")
        self.assertEqual(result["runs"][0]["state"], "exited")

    def test_nonzero_status_is_failed_despite_recent_working_event(self) -> None:
        result = self.reconcile(FakeTmux(self.task, dead_status=7))

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["runs"][0]["state"], "failed")

    def test_absent_pane_with_live_process_is_stale_at_tmux_ownership(self) -> None:
        result = self.reconcile(
            FakeTmux(self.task, missing=True), process_matches=True,
        )

        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "tmux: recorded tmux pane is absent")
        self.assertEqual(
            result["runs"][0]["evidence"][1]["detail"],
            "recorded tmux pane is absent but the process identity is live",
        )

    def test_absent_pane_with_gone_process_is_failed_despite_working_event(self) -> None:
        result = self.reconcile(
            FakeTmux(self.task, missing=True), process_matches=False,
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["runs"][0]["state"], "failed")
        self.assertEqual(
            result["runs"][0]["evidence"][1]["detail"],
            "recorded tmux pane is absent and the process identity is gone",
        )

    def test_absent_pane_process_probe_failure_is_unavailable(self) -> None:
        adapters = FakeAdapters(self.task, FakeTmux(self.task, missing=True))
        run = self.task["runs"][0]

        with mock.patch(
            "lib.control.reconcile.harness_api.verify_process",
            side_effect=HarnessError("malformed stat"),
        ):
            process = adapters.process(self.task, run)

        self.assertEqual(process.outcome, "unavailable")
        self.assertIn("malformed stat", process.detail)

    def test_stored_exited_supersedes_older_working_snapshot(self) -> None:
        self.task["lifecycle"] = "ended"
        self.task["runs"][0]["state"] = "exited"

        result = self.reconcile(FakeTmux(self.task, dead_status=0))

        self.assertEqual(result["state"], "exited")
        self.assertEqual(result["runs"][0]["state"], "exited")

    def test_stored_failed_supersedes_older_working_snapshot(self) -> None:
        self.task["lifecycle"] = "failed"
        self.task["runs"][0]["state"] = "failed"

        result = self.reconcile(FakeTmux(self.task, dead_status=7))

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["runs"][0]["state"], "failed")

    def test_stored_exited_conflicts_with_failed_snapshot(self) -> None:
        self.task["lifecycle"] = "ended"
        self.task["runs"][0]["state"] = "exited"

        result = self.reconcile(
            FakeTmux(self.task, dead_status=0), event_state="failed",
        )

        self.assertEqual(result["state"], "stale")
        self.assertEqual(
            result["blocker"], "event: state contradicts stored terminal state",
        )


if __name__ == "__main__":
    unittest.main()
