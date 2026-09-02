"""Codex permission events with visible-pane needs-input fallback."""

from __future__ import annotations

import os
import subprocess
import time
import unittest
from unittest import mock

from lib.control.harness import INPUT_PROMPT_MARKERS
from lib.control.reconcile import (
    Evidence,
    LiveAdapters,
    reconcile_task,
    reconcile_task_with_observation,
)
from lib.control.socket_reaper import TmuxSocketReaper
from lib.control.tmux import PaneFacts, TmuxAdapter, TmuxError
from tests.python.test_control_config_model import task_record

CODEX_PROMPT = [
    "  Would you like to run the following command?",
    "  $ git clone https://example.invalid/repo",
    "› 1. Yes, proceed (y)",
    "  2. No, and tell Codex what to do differently (esc)",
    "  Press enter to confirm or esc to cancel",
]


class FakeTmux:
    def __init__(self, task: dict, *, tail: list[str], dead: bool = False,
                 tail_error: bool = False) -> None:
        self.task = task
        self.tail = tail
        self.dead = dead
        self.tail_error = tail_error
        self.tail_calls = 0

    def has_session(self, name):
        return True

    def session_option(self, name, option):
        return "1" if option == "@asha_managed" else self.task["task_id"]

    def pane_facts(self, pane_id):
        run = self.task["runs"][0]
        return PaneFacts(
            pane_id, run["pid"], self.dead, 0 if self.dead else None, None,
            self.task["tmux"]["session"], self.task["tmux"]["window"], "fixture",
        )

    def pane_tail(self, pane_id, *, lines=12):
        self.tail_calls += 1
        if self.tail_error:
            raise TmuxError("capture failed")
        return list(self.tail)[-lines:]


class Adapters:
    def __init__(self, tmux: FakeTmux, *, event: Evidence) -> None:
        self.live = LiveAdapters(tmux=tmux)
        self._event = event

    def tmux(self, task, run):
        return self.live.tmux(task, run)

    def process(self, task, run):
        return Evidence("process", "match", "pid and start identity matched")

    def jj(self, task):
        return Evidence("jj", "match", "workspace and change matched")

    def event(self, task, run):
        return self._event


def _working_event(stale: bool = False) -> Evidence:
    return Evidence(
        "event", "match", "tool-completed snapshot", state="working", stale=stale,
    )


class VisiblePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = task_record()
        self.task["runs"][0]["harness"] = "codex"

    def reconcile(self, tmux, event=None):
        return reconcile_task(self.task, Adapters(tmux, event=event or _working_event()))

    def test_visible_codex_prompt_outranks_a_working_event(self) -> None:
        tmux = FakeTmux(self.task, tail=CODEX_PROMPT)
        result = self.reconcile(tmux)
        self.assertEqual(result["state"], "needs-input")
        self.assertIsNone(result["blocker"])
        detail = next(e for e in result["evidence"] if e["source"] == "tmux")
        self.assertEqual(detail["state"], "needs-input")
        self.assertIn("pane shows the codex input prompt", detail["detail"])
        self.assertIn("Would you like to run", detail["detail"])

    def test_visible_prompt_outranks_a_stale_working_event(self) -> None:
        result = self.reconcile(FakeTmux(self.task, tail=CODEX_PROMPT), _working_event(stale=True))
        self.assertEqual(result["state"], "needs-input")

    def test_fresh_permission_event_is_primary_and_pane_detection_is_fallback(self) -> None:
        permission = Evidence(
            "event", "match", "permission-requested snapshot",
            state="needs-input", observed_at="2026-08-18T23:54:20Z",
        )

        result, observation = reconcile_task_with_observation(
            self.task,
            Adapters(FakeTmux(self.task, tail=CODEX_PROMPT), event=permission),
        )

        self.assertEqual(result["state"], "needs-input")
        self.assertEqual(observation.state, "needs-input")
        self.assertEqual(observation.source, "event")
        self.assertEqual(observation.observed_at, "2026-08-18T23:54:20Z")

    def test_verified_turn_stopped_idle_outranks_a_lingering_prompt(self) -> None:
        stopped = Evidence(
            "event", "match", "verified turn-stopped snapshot", state="idle",
        )

        result = self.reconcile(FakeTmux(self.task, tail=CODEX_PROMPT), stopped)

        self.assertEqual(result["state"], "idle")
        self.assertIsNone(result["blocker"])

    def test_no_prompt_keeps_the_event_state(self) -> None:
        result = self.reconcile(FakeTmux(self.task, tail=["  Working (12s • esc to interrupt)"]))
        self.assertEqual(result["state"], "working")

    def test_prompt_below_the_visible_tail_does_not_count(self) -> None:
        # The prompt scrolled up: twenty later lines mean it was answered.
        tail = CODEX_PROMPT + [f"  output line {index}" for index in range(20)]
        result = self.reconcile(FakeTmux(self.task, tail=tail))
        self.assertEqual(result["state"], "working")

    def test_dead_pane_never_reads_the_screen(self) -> None:
        tmux = FakeTmux(self.task, tail=CODEX_PROMPT, dead=True)
        with mock.patch("lib.control.reconcile.harness_api.verify_process", return_value=False):
            adapters = Adapters(tmux, event=_working_event())
            adapters.process = lambda task, run: LiveAdapters(tmux=tmux).process(task, run)
            result = reconcile_task(self.task, adapters)
        self.assertEqual(tmux.tail_calls, 0)
        self.assertNotEqual(result["state"], "needs-input")

    def test_terminal_event_is_not_overridden_by_a_prompt(self) -> None:
        ended = Evidence("event", "match", "session-ended", state="exited")
        result = self.reconcile(FakeTmux(self.task, tail=CODEX_PROMPT), ended)
        self.assertNotEqual(result["state"], "needs-input")

    def test_capture_failure_is_silent(self) -> None:
        result = self.reconcile(FakeTmux(self.task, tail=CODEX_PROMPT, tail_error=True))
        self.assertEqual(result["state"], "working")

    def test_harness_without_markers_never_captures(self) -> None:
        self.task["runs"][0]["harness"] = "copilot"
        tmux = FakeTmux(self.task, tail=CODEX_PROMPT)
        result = self.reconcile(tmux)
        self.assertEqual(tmux.tail_calls, 0)
        self.assertEqual(result["state"], "working")
        self.assertEqual(INPUT_PROMPT_MARKERS["copilot"], ())

    def test_claude_trust_dialog_in_a_fresh_workspace_is_needs_input(self) -> None:
        # Verbatim from a real Control-launched Claude worker (2026-08-23).
        trust = [
            "Quick safety check: Is this a project you created or one you trust?",
            "Claude Code'll be able to read, edit, and execute files here.",
            "\u276f 1. Yes, I trust this folder",
            "  2. No, exit",
            "Enter to confirm \u00b7 Esc to cancel",
        ]
        self.task["runs"][0]["harness"] = "claude"
        result = self.reconcile(FakeTmux(self.task, tail=trust))
        self.assertEqual(result["state"], "needs-input")
        detail = next(e for e in result["evidence"] if e["source"] == "tmux")
        self.assertIn("pane shows the claude input prompt", detail["detail"])
        self.assertIn("Is this a project you created", detail["detail"])

    def test_claude_permission_prompt_is_needs_input_but_ordinary_output_is_not(self) -> None:
        self.task["runs"][0]["harness"] = "claude"
        permission = ["Bash(cargo test)", "Do you want to proceed?", "\u276f 1. Yes"]
        self.assertEqual(self.reconcile(FakeTmux(self.task, tail=permission))["state"], "needs-input")
        prose = [
            "Updated src/monitor/layout.rs with the sparkline renderer.",
            "Running cargo test to confirm; will report when it finishes.",
            "The plan is to proceed with the headroom colors next.",
        ]
        self.assertEqual(self.reconcile(FakeTmux(self.task, tail=prose))["state"], "working")


class RealTmuxPaneTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.socket = f"asha-tail-test-{os.getpid()}"
        self.enterContext(TmuxSocketReaper(self.socket))
        capability = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "list-commands", "capture-pane"],
            capture_output=True, text=True, check=False,
        )
        if capability.returncode != 0:
            self.skipTest("isolated tmux sockets are unavailable in this execution sandbox")
        self.adapter = TmuxAdapter(socket=self.socket)

    def test_pane_tail_returns_the_last_visible_lines(self) -> None:
        pane = self.adapter.create_task_session(
            session="asha-tail-12345678", window="work", start_directory="/",
            environment={},
            holder_argv=["sh", "-c", "printf 'first\\n\\nWould you like to run the following command?\\n"
                                    "Press enter to confirm or esc to cancel\\n'; sleep 30"],
            session_options={"@asha_managed": "1", "@asha_task_id": "x"},
            pane_options={}, pane_title="asha:codex:implementer",
        )
        for _ in range(100):
            tail = self.adapter.pane_tail(pane, lines=3)
            if any("Press enter" in line for line in tail):
                break
            time.sleep(0.05)
        self.assertEqual(len(tail), 3)
        self.assertEqual(tail[-1], "Press enter to confirm or esc to cancel")
        self.assertEqual(tail[0], "first")
        with self.assertRaises(ValueError):
            self.adapter.pane_tail(pane, lines=0)


if __name__ == "__main__":
    unittest.main()
