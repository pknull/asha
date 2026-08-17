from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest import mock

from lib.control.cli import main
from lib.control.config import load_config
from lib.control.doctor import Probe, run_doctor
from lib.control.reconcile import Evidence, reconcile_task
from lib.control.store import TaskStore, task_digest
from tests.python.test_control_config_model import task_record, write_config


class FakeAdapters:
    def __init__(self, outcomes: dict[str, Evidence]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def tmux(self, task, run):
        self.calls.append("tmux")
        return self.outcomes["tmux"]

    def process(self, task, run):
        self.calls.append("process")
        return self.outcomes["process"]

    def jj(self, task):
        self.calls.append("jj")
        return self.outcomes["jj"]

    def event(self, task, run):
        self.calls.append("event")
        return self.outcomes["event"]


def evidence(**overrides: Evidence) -> dict[str, Evidence]:
    base = {
        "tmux": Evidence("tmux", "match", "owned pane matched"),
        "process": Evidence("process", "match", "pid and start identity matched"),
        "jj": Evidence("jj", "match", "workspace and change matched"),
        "event": Evidence("event", "match", "verified event", state="working"),
    }
    base.update(overrides)
    return base


class ControlReconciliationTests(unittest.TestCase):
    def test_prelaunch_task_status_is_derived_from_creation_journal(self) -> None:
        task = task_record()
        task["lifecycle"] = "creating"
        task["runs"] = []
        task["jj"]["change_id"] = "k" * 32
        task["jj"]["working_commit_id"] = "a" * 40
        ready = reconcile_task(task, creation={"phase": "ready-for-launch"})
        self.assertEqual(ready["state"], "creating")
        self.assertIsNone(ready["blocker"])

        interrupted = reconcile_task(task, creation={"phase": "workspace-add-intent"})
        self.assertEqual(interrupted["state"], "stale")
        self.assertIn("workspace-add-intent", interrupted["blocker"])

        missing = reconcile_task(task, creation=None)
        self.assertEqual(missing["state"], "stale")
        self.assertIn("journal", missing["blocker"])

        disappeared = reconcile_task(task, creation={
            "phase": "ready-for-launch", "workspace_present": False,
        })
        self.assertEqual(disappeared["state"], "stale")
        self.assertIn("missing", disappeared["blocker"])

        reappeared = reconcile_task(task, creation={
            "phase": "rolled-back", "workspace_present": True,
        })
        self.assertEqual(reappeared["state"], "stale")
        self.assertIn("reappeared", reappeared["blocker"])

    def test_verified_live_evidence_and_event_determine_run_state_without_mutation(self) -> None:
        task = task_record()
        before = json.dumps(task, sort_keys=True)
        adapters = FakeAdapters(evidence())
        result = reconcile_task(task, adapters)
        self.assertEqual(result["contract"], "asha.control-reconciliation.v1")
        self.assertEqual(result["state"], "working")
        self.assertEqual(adapters.calls, ["tmux", "process", "jj", "event"])
        self.assertEqual(json.dumps(task, sort_keys=True), before)

    def test_higher_precedence_identity_mismatch_short_circuits_to_stale(self) -> None:
        adapters = FakeAdapters(evidence(tmux=Evidence("tmux", "mismatch", "foreign owner")))
        result = reconcile_task(task_record(), adapters)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "tmux: foreign owner")
        self.assertEqual(adapters.calls, ["tmux"])

    def test_missing_live_pane_and_process_is_exited_not_old_working(self) -> None:
        task = task_record()
        task["runs"][0]["state"] = "working"
        adapters = FakeAdapters(evidence(
            tmux=Evidence("tmux", "missing", "pane absent"),
            process=Evidence("process", "missing", "process absent"),
            event=Evidence("event", "unavailable", "terminal event unavailable"),
        ))
        result = reconcile_task(task, adapters)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "process: missing without verified terminal event")
        self.assertEqual(adapters.calls, ["tmux", "process", "jj", "event"])

    def test_missing_process_still_checks_jj_before_deriving_terminal_state(self) -> None:
        adapters = FakeAdapters(evidence(
            process=Evidence("process", "missing", "process absent"),
            jj=Evidence("jj", "mismatch", "change identity differs"),
        ))
        result = reconcile_task(task_record(), adapters)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "jj: change identity differs")
        self.assertEqual(adapters.calls, ["tmux", "process", "jj"])

    def test_verified_terminal_event_decides_state_after_process_and_jj_are_missing_or_matched(self) -> None:
        for terminal in ("exited", "failed"):
            adapters = FakeAdapters(evidence(
                process=Evidence("process", "missing", "process absent"),
                event=Evidence("event", "match", "verified terminal event", state=terminal),
            ))
            with self.subTest(terminal=terminal):
                result = reconcile_task(task_record(), adapters)
                self.assertEqual(result["state"], terminal)

    def test_stored_terminal_state_is_last_fallback_without_terminal_event(self) -> None:
        task = task_record()
        task["lifecycle"] = "failed"
        task["runs"][0]["state"] = "failed"
        adapters = FakeAdapters(evidence(
            process=Evidence("process", "missing", "process absent"),
            event=Evidence("event", "unavailable", "event seam unavailable"),
        ))
        self.assertEqual(reconcile_task(task, adapters)["state"], "failed")

    def test_stored_terminal_state_rejects_live_active_or_different_terminal_evidence(self) -> None:
        for stored in ("exited", "failed"):
            for case, process, event_state, expected in (
                ("live", "match", None, "stale"),
                ("active-event", "missing", "working", "stale"),
                ("different-terminal", "missing", "failed" if stored == "exited" else "exited", "stale"),
                ("same-terminal", "missing", stored, stored),
                ("fallback", "missing", None, stored),
            ):
                task = task_record()
                task["lifecycle"] = "ended" if stored == "exited" else "failed"
                task["runs"][0]["state"] = stored
                overrides = {
                    "process": Evidence("process", process, "process evidence"),
                    "event": (
                        Evidence("event", "match", "event evidence", state=event_state)
                        if event_state else Evidence("event", "unavailable", "no event")
                    ),
                }
                with self.subTest(stored=stored, case=case):
                    result = reconcile_task(task, FakeAdapters(evidence(**overrides)))
                    self.assertEqual(result["runs"][0]["state"], expected)
                    self.assertEqual(
                        result["state"],
                        expected if expected == "stale" else (
                            "failed" if stored == "failed" else expected
                        ),
                    )
                    if expected == "stale":
                        self.assertIsNotNone(result["runs"][0]["blocker"])
                        self.assertEqual(result["blocker"], result["runs"][0]["blocker"])

    def test_missing_jj_identity_is_specific_stale_blocker(self) -> None:
        adapters = FakeAdapters(evidence(jj=Evidence("jj", "missing", "workspace absent")))
        result = reconcile_task(task_record(), adapters)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "jj: workspace absent")

    def test_unavailable_evidence_is_not_treated_as_mismatch(self) -> None:
        task = task_record()
        unavailable = {
            name: Evidence(name, "unavailable", "Increment 1 has no live adapter")
            for name in ("tmux", "process", "jj", "event")
        }
        result = reconcile_task(task, FakeAdapters(unavailable))
        self.assertEqual(result["state"], "starting")
        self.assertIsNone(result["blocker"])
        self.assertTrue(all(item["outcome"] == "unavailable" for item in result["evidence"]))

    def test_live_process_with_unavailable_semantic_adapter_is_unknown(self) -> None:
        adapters = FakeAdapters(evidence(event=Evidence("event", "unavailable", "unsupported harness")))
        self.assertEqual(reconcile_task(task_record(), adapters)["state"], "unknown")

    def test_active_event_is_not_trusted_when_live_process_evidence_is_unavailable(self) -> None:
        unavailable = FakeAdapters(evidence(
            tmux=Evidence("tmux", "unavailable", "tmux socket denied"),
            process=Evidence("process", "unavailable", "proc evidence denied"),
        ))
        result = reconcile_task(task_record(), unavailable)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(
            result["blocker"],
            "tmux, process: evidence unavailable; event state not trusted "
            "without live process evidence",
        )

        matched = reconcile_task(task_record(), FakeAdapters(evidence()))
        self.assertEqual(matched["state"], "working")
        self.assertIsNone(matched["blocker"])

    def test_missing_event_preserves_proven_active_state_when_jj_is_match_or_unavailable(self) -> None:
        for stored in ("starting", "working", "needs-input", "idle", "unknown"):
            for jj_outcome in ("match", "unavailable"):
                task = task_record()
                task["runs"][0]["state"] = stored
                adapters = FakeAdapters(evidence(
                    jj=Evidence("jj", jj_outcome, "jj identity evidence"),
                    event=Evidence("event", "missing", "no newer semantic event"),
                ))
                with self.subTest(stored=stored, jj_outcome=jj_outcome):
                    result = reconcile_task(task, adapters)
                    self.assertEqual(result["state"], stored)
                    self.assertIsNone(result["blocker"])

    def test_missing_event_resolves_or_blocks_a_stored_stale_state(self) -> None:
        task = task_record()
        task["runs"][0]["state"] = "stale"
        event_cases = (
            (Evidence("event", "match", "verified working event", state="working"), "working"),
            (Evidence("event", "missing", "no newer semantic event"), "starting"),
            (Evidence("event", "unavailable", "semantic adapter unavailable"), "unknown"),
        )
        for event_value, expected in event_cases:
            matched = FakeAdapters(evidence(event=event_value))
            with self.subTest(identities="matched", event=event_value.outcome):
                result = reconcile_task(task, matched)
                self.assertEqual(result["state"], expected)
                self.assertIsNone(result["blocker"])

        for source in ("tmux", "process", "jj"):
            for identity_outcome in ("missing", "mismatch", "unavailable"):
                for event_value, _ in event_cases:
                    identity = Evidence(
                        source, identity_outcome, f"{source} identity {identity_outcome}"
                    )
                    adapters = FakeAdapters(evidence(**{source: identity, "event": event_value}))
                    with self.subTest(
                        source=source,
                        identity_outcome=identity_outcome,
                        event=event_value.outcome,
                    ):
                        result = reconcile_task(task, adapters)
                        self.assertEqual(result["state"], "stale")
                        self.assertIsNotNone(result["blocker"])
                        if identity_outcome == "unavailable":
                            self.assertIn("unresolved", result["blocker"])
                        else:
                            self.assertEqual(
                                result["blocker"], f"{source}: {identity.detail}"
                            )
                        self.assertEqual(result["blocker"], result["runs"][0]["blocker"])
                        self.assertNotIn("event", adapters.calls)

    def test_failed_task_keeps_exact_derived_stale_ownership_blocker(self) -> None:
        task = task_record()
        task["lifecycle"] = "failed"
        task["runs"][0]["state"] = "working"
        result = reconcile_task(task, FakeAdapters(evidence(
            tmux=Evidence("tmux", "mismatch", "foreign pane owner"),
        )))
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "tmux: foreign pane owner")
        self.assertEqual(result["runs"][0]["blocker"], "tmux: foreign pane owner")

    def test_failed_task_aggregate_preserves_failure_while_live_run_is_reconciled(self) -> None:
        task = task_record()
        task["lifecycle"] = "failed"
        task["runs"][0]["state"] = "working"
        result = reconcile_task(task, FakeAdapters(evidence()))
        self.assertEqual(result["state"], "failed")
        self.assertIn("recovery", result["blocker"])
        self.assertEqual(result["runs"][0]["state"], "working")

        prelaunch = task_record()
        prelaunch["lifecycle"] = "failed"
        prelaunch["jj"]["change_id"] = None
        prelaunch["jj"]["working_commit_id"] = None
        prelaunch["runs"] = []
        result = reconcile_task(prelaunch, FakeAdapters(evidence()))
        self.assertEqual(result["state"], "failed")
        self.assertIn("recovery", result["blocker"])

    def test_event_state_cannot_contradict_a_matched_live_process(self) -> None:
        invalid = Evidence("event", "match", "verified terminal event", state="exited")
        adapters = FakeAdapters(evidence(event=invalid))
        result = reconcile_task(task_record(), adapters)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["blocker"], "event: terminal state contradicts matched live process")

    def test_hostile_adapter_mutation_cannot_change_input_or_later_adapter_snapshots(self) -> None:
        task = task_record()
        original = deepcopy(task)

        class MutatingAdapters(FakeAdapters):
            def tmux(self, task_arg, run_arg):
                self.calls.append("tmux")
                task_arg["lifecycle"] = "failed"
                run_arg["state"] = "failed"
                task_arg["jj"]["change_id"] = "forged"
                return self.outcomes["tmux"]

            def process(self, task_arg, run_arg):
                self.calls.append("process")
                if task_arg != original or run_arg != original["runs"][0]:
                    raise AssertionError("adapter received mutation from an earlier adapter")
                return self.outcomes["process"]

        adapters = MutatingAdapters(evidence())
        result = reconcile_task(task, adapters)
        self.assertEqual(result["state"], "working")
        self.assertEqual(task, original)

    def test_evidence_rejects_unbounded_or_control_character_fields_and_nonsemantic_event_states(self) -> None:
        invalid_calls = (
            lambda: Evidence("bad source", "match", "detail"),
            lambda: Evidence("tmux", "match", "x" * 501),
            lambda: Evidence("tmux", "match", "bad\ndetail"),
            lambda: Evidence("tmux", "match", "detail", state="working"),
            lambda: Evidence("event", "match", "detail", state="unknown"),
            lambda: Evidence("event", "match", "unsafe\u202eoutput", state="working"),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_adapter_cannot_mutate_returned_evidence_after_validation(self) -> None:
        tmux_result = Evidence("tmux", "match", "owned pane matched")

        class EvidenceMutator(FakeAdapters):
            def tmux(self, task_arg, run_arg):
                self.calls.append("tmux")
                return tmux_result

            def process(self, task_arg, run_arg):
                self.calls.append("process")
                object.__setattr__(tmux_result, "detail", "forged\nterminal output")
                return self.outcomes["process"]

        result = reconcile_task(task_record(), EvidenceMutator(evidence()))
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["evidence"][0]["detail"], "owned pane matched")


class ControlDoctorTests(unittest.TestCase):
    def test_doctor_uses_only_injected_probes_and_reports_limitations(self) -> None:
        calls: list[str] = []

        def fake_tmux(config):
            calls.append("tmux")
            return Probe("tmux", "unavailable", "fake probe intentionally unavailable")

        result = run_doctor(None, probes={"tmux": fake_tmux})
        self.assertEqual(result["contract"], "asha.control-doctor.v1")
        self.assertEqual(calls, ["tmux"])
        self.assertFalse(result["ok"])
        self.assertIn("fake probe intentionally unavailable", result["limitations"])

    def test_default_doctor_live_probes_tmux_jj_and_repository(self) -> None:
        """jj and repository are real probes now, not Increment 1 stubs.

        They previously reported "not probed in Increment 1" forever, so doctor
        under-reported the two things an operator most needs before starting a
        task: whether jj exposes the command surface Control depends on, and
        whether the current directory can host a task at all.
        """
        result = run_doctor(None)
        by_name = {probe["name"]: probe for probe in result["probes"]}
        self.assertIn(by_name["tmux"]["outcome"], {"match", "unavailable"})
        self.assertIn("harness", by_name)
        for name in ("jj", "repository"):
            self.assertIn(
                by_name[name]["outcome"],
                {"match", "missing", "mismatch", "unavailable"},
            )
            self.assertNotIn("not probed in Increment 1", by_name[name]["detail"])

    def test_probe_rejects_unbounded_or_control_character_fields(self) -> None:
        for call in (
            lambda: Probe("bad name", "match", "detail"),
            lambda: Probe("tmux", "match", "x" * 501),
            lambda: Probe("tmux", "match", "bad\ndetail"),
            lambda: Probe("tmux", "match", "bad\u202edetail"),
        ):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_doctor_revalidates_and_copies_a_probe_result(self) -> None:
        result = Probe("tmux", "match", "bounded")

        def hostile(config):
            object.__setattr__(result, "detail", "forged\noutput")
            return result

        with self.assertRaises(ValueError):
            run_doctor(None, probes={"tmux": hostile})


class ControlCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        home = root / "home"
        home.mkdir()
        self.env = {
            "HOME": str(home),
            "ASHA_CONFIG": str(root / "missing.json"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        # Isolate tmux completely: the live adapters shell out to the default
        # tmux socket, which would otherwise reach the developer's real server
        # and make evidence depend on ambient panes.  An empty TMUX_TMPDIR with
        # no inherited client guarantees "no server", so every reconciliation
        # here is deterministic regardless of the host environment.
        socket_dir = root / "tmux-socket"
        socket_dir.mkdir(mode=0o700)
        tmux_env = {"TMUX_TMPDIR": str(socket_dir)}
        patcher = mock.patch.dict(os.environ, tmux_env)
        patcher.start()
        os.environ.pop("TMUX", None)
        self.addCleanup(patcher.stop)
        self.config = load_config(self.env)
        self.record = task_record(
            slug="cli-test",
            repository_root=str(root / "repositories/source"),
            workspace_path=str(self.config.workspace_root / "repo-key/cli-test"),
        )
        TaskStore(self.config).save(self.record)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, args: list[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = main(args, env=self.env)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_list_json_is_versioned_stdout_only_and_deterministic(self) -> None:
        rc, stdout, stderr = self.invoke(["task", "list", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")
        data = json.loads(stdout)
        self.assertEqual(data["contract"], "asha.control-task-list.v1")
        self.assertEqual(data["tasks"][0]["task_id"], self.record["task_id"])
        # This never-launched fixture has no tmux session, no live process, and
        # no jj workspace.  Reconciliation follows the documented precedence
        # tmux -> process -> jj: with tmux isolated to an empty socket dir the
        # recorded session resolves as `missing`, and a missing tmux session
        # with a non-missing process is the first and reported blocker, before
        # jj is consulted (see the isolated tmux socket in setUp). Verified
        # empirically against a machine with tmux installed; codex's sandbox,
        # which has no tmux binary, degrades tmux to `unavailable` and would
        # otherwise mislead this assertion toward the jj blocker.
        self.assertEqual(data["tasks"][0]["status"], "stale")
        self.assertEqual(
            data["tasks"][0]["blocker"], "tmux: recorded tmux session is absent",
        )

    def test_show_json_has_narrow_versioned_contract(self) -> None:
        rc, stdout, stderr = self.invoke(["task", "show", "cli-test", "--json"])
        self.assertEqual((rc, stderr), (0, ""))
        data = json.loads(stdout)
        self.assertEqual(set(data), {"contract", "task", "reconciliation"})
        self.assertEqual(data["contract"], "asha.control-task-show.v1")
        self.assertEqual(data["task"], self.record)

    def test_show_text_includes_every_run_jj_tmux_blocker_and_evidence(self) -> None:
        expected_digest = task_digest(self.record)
        self.record["runs"][0]["state"] = "exited"
        second = deepcopy(self.record["runs"][0])
        second["run_id"] = str(uuid.uuid4())
        second["role"] = "reviewer"
        second["pane_id"] = "%24"
        second["state"] = "starting"
        self.record["runs"].append(second)
        TaskStore(self.config).save(self.record, expected_digest=expected_digest)

        rc, stdout, stderr = self.invoke(["task", "show", "cli-test"])

        self.assertEqual((rc, stderr), (0, ""))
        self.assertIn(self.record["jj"]["workspace_name"], stdout)
        self.assertIn(self.record["jj"]["change_id"], stdout)
        self.assertIn(self.record["tmux"]["session"], stdout)
        self.assertIn("Blocker:", stdout)
        self.assertIn("Evidence:", stdout)
        self.assertIn(self.record["runs"][0]["evidence"], stdout)
        self.assertIn(self.record["runs"][0]["evidence_at"], stdout)
        self.assertIn("Harness session:", stdout)
        self.assertIn("Derived blocker:", stdout)
        for run in self.record["runs"]:
            self.assertIn(run["run_id"], stdout)

    def test_hostile_registry_diagnostic_is_fixed_safe_text(self) -> None:
        path = self.config.tasks_dir / f"{self.record['task_id']}.json"
        hostile = deepcopy(self.record)
        hostile["raw-secret\u202e"] = True
        path.write_text(json.dumps(hostile))
        path.chmod(0o600)

        rc, stdout, stderr = self.invoke(["task", "list", "--json"])

        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["tasks"], [])
        self.assertEqual(payload["skipped"][0]["name"], path.name)
        self.assertNotIn("raw-secret", stderr)
        self.assertNotIn("\u202e", stderr)
        self.assertNotIn("Traceback", stderr)
        stderr.encode("utf-8")

    def test_recursive_state_json_returns_fixed_cli_error(self) -> None:
        path = self.config.tasks_dir / f"{self.record['task_id']}.json"
        path.write_text("[" * 10000 + "0" + "]" * 10000)
        path.chmod(0o600)
        rc, stdout, stderr = self.invoke(["task", "list", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["tasks"], [])
        self.assertEqual(payload["skipped"][0]["name"], path.name)
        self.assertIn("nesting", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("[[[", stderr)

    def test_recursive_config_json_returns_fixed_cli_error(self) -> None:
        write_config(
            Path(self.env["ASHA_CONFIG"]), "[" * 10000 + "0" + "]" * 10000
        )
        rc, stdout, stderr = self.invoke(["task", "doctor", "--json"])
        self.assertEqual((rc, stdout), (2, ""))
        self.assertIn("nesting", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("[[[", stderr)

    def test_doctor_json_has_versioned_contract(self) -> None:
        rc, stdout, stderr = self.invoke(["task", "doctor", "--json"])
        self.assertEqual((rc, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["contract"], "asha.control-doctor.v1")

    def test_control_reports_non_tty_fallback_without_launch_fallthrough(self) -> None:
        rc, stdout, stderr = self.invoke(["control"])
        self.assertEqual(rc, 2)
        self.assertEqual(stdout, "")
        self.assertIn("asha task list --json", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_start_requires_a_goal(self) -> None:
        rc, stdout, stderr = self.invoke(["task", "start"])
        self.assertEqual(rc, 2)
        self.assertEqual(stdout, "")
        self.assertIn("requires --goal", stderr)


if __name__ == "__main__":
    unittest.main()
