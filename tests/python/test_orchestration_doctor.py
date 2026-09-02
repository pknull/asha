from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.orchestration.config import load_config
from lib.control.orchestration.doctor import (
    Probe,
    _approval_decider,
    _approval_provenance_probe,
    _contracts_probe,
    _coordinator_cursor_probe,
    _suspect_approvals,
    run_orchestration_doctor,
)


def approval(request_id: str, state: str = "approved", **extra) -> dict:
    return {"request_id": request_id, "state": state, "actor_kind": "operator", **extra}


def decided_event(request_id: str, actor_kind: str, actor_id: str) -> dict:
    return {
        "type": "approval-decided", "subject_ids": [request_id],
        "actor_kind": actor_kind, "actor_id": actor_id,
    }


class OrchestrationApprovalProvenanceTests(unittest.TestCase):
    def test_decider_reads_the_record_first_and_the_journal_after(self) -> None:
        signed = approval("a", decided_by={"actor_id": "cli", "actor_kind": "operator"})
        self.assertEqual(
            _approval_decider(signed, [decided_event("a", "controller", "ignored")]),
            {"actor_id": "cli", "actor_kind": "operator"},
        )
        # Pre-split records carry no decider; the journal is their provenance.
        self.assertEqual(
            _approval_decider(approval("a"), [decided_event("a", "operator", "cli")]),
            {"actor_id": "cli", "actor_kind": "operator"},
        )
        self.assertIsNone(_approval_decider(approval("a"), []))
        self.assertIsNone(
            _approval_decider(approval("a"), [decided_event("b", "operator", "cli")]),
        )

    def test_operator_kind_decisions_need_an_operator_surface_or_named_authority(self) -> None:
        coordinator = "coordinator:74a1f315-642b-40e0-8eba-05dc1620e594"
        quiet = [
            (approval("a"), [decided_event("a", "operator", "cli")]),
            (approval("a"), [decided_event("a", "operator", "tui")]),
            (approval("a"), [decided_event("a", "operator", "standing-authority:74a1f315")]),
            # The standing authority journals its decision as a controller act.
            (approval("a"), [decided_event("a", "controller", "standing-authority")]),
            (approval("a", state="requested"), [decided_event("a", "operator", coordinator)]),
            (approval("a"), []),
        ]
        for record, events in quiet:
            with self.subTest(events=events, state=record["state"]):
                self.assertEqual(_suspect_approvals([record], events), [])

        flagged = _suspect_approvals(
            [approval("a")], [decided_event("a", "operator", coordinator)],
        )
        self.assertEqual(len(flagged), 1)
        self.assertIn(coordinator, flagged[0])
        self.assertIn("a", flagged[0])
        # A coordinator never signs; no live producer records that kind.
        self.assertEqual(
            len(_suspect_approvals(
                [approval("a")], [decided_event("a", "coordinator", coordinator)],
            )),
            1,
        )
        self.assertEqual(
            len(_suspect_approvals(
                [approval("a", decided_by={"actor_id": "cli", "actor_kind": "operator"})],
                [decided_event("a", "operator", coordinator)],
            )),
            0,
            "the record's own decider outranks the journal",
        )

    def test_probe_reports_per_initiative_and_stays_quiet_on_a_clean_plane(self) -> None:
        coordinator = "coordinator:74a1f315-642b-40e0-8eba-05dc1620e594"
        store = mock.Mock()
        store.list_initiatives.return_value = [
            {"initiative_id": "one"}, {"initiative_id": "two"},
        ]
        store.list_approvals_snapshot.side_effect = lambda name: {
            "one": [approval("a")], "two": [approval("b", state="requested")],
        }[name]
        store.list_events_snapshot.side_effect = lambda name: {
            "one": [decided_event("a", "operator", "cli")], "two": [],
        }[name]
        with mock.patch(
            "lib.control.orchestration.store.InitiativeStore", return_value=store,
        ):
            probe = _approval_provenance_probe(mock.Mock())
        self.assertEqual(probe.outcome, "match")
        # An undecided approval needs no journal read.
        self.assertEqual(store.list_events_snapshot.call_args_list, [mock.call("one")])

        store.list_events_snapshot.side_effect = lambda name: {
            "one": [decided_event("a", "operator", coordinator)], "two": [],
        }[name]
        with mock.patch(
            "lib.control.orchestration.store.InitiativeStore", return_value=store,
        ):
            probe = _approval_provenance_probe(mock.Mock())
        self.assertEqual(probe.outcome, "mismatch")
        self.assertIn(coordinator, probe.detail)
        self.assertLessEqual(len(probe.detail), 400)

    def test_probe_is_unavailable_rather_than_raising_on_an_unreadable_store(self) -> None:
        store = mock.Mock()
        store.list_initiatives.side_effect = OSError("registry gone")
        with mock.patch(
            "lib.control.orchestration.store.InitiativeStore", return_value=store,
        ):
            probe = _approval_provenance_probe(mock.Mock())
        self.assertEqual(probe.outcome, "unavailable")
        self.assertIn("registry gone", probe.detail)


class OrchestrationDoctorTests(unittest.TestCase):
    def test_contract_source_inspection_failure_is_unavailable(self) -> None:
        with mock.patch(
            "lib.control.orchestration.doctor.inspect.getsource",
            side_effect=OSError("source unavailable"),
        ):
            probe = _contracts_probe()
        self.assertEqual(probe.outcome, "unavailable")
        self.assertIn("source unavailable", probe.detail)

    def test_absent_initiatives_root_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}):
                payload = run_orchestration_doctor(config)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["contract"], "asha.orchestration-doctor.v1")
            self.assertEqual([probe["name"] for probe in payload["probes"]], [
                "orchestration-config", "initiatives-root", "control-contracts",
                "create-by-id", "control-doctor", "coordinator-seam",
                "approval-provenance", "coordinator-cursor",
            ])

    def test_coordinator_seam_probe_is_advisory_and_never_blocks_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (config.initiatives_dir.parent, config.initiatives_dir.parent.parent):
                path.chmod(0o700)
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}), \
                 mock.patch("lib.control.orchestration.doctor.shutil.which", return_value=None):
                payload = run_orchestration_doctor(config)
            seam = next(probe for probe in payload["probes"] if probe["name"] == "coordinator-seam")
            self.assertEqual(seam["outcome"], "unavailable")
            self.assertIn("tmux", seam["detail"])
            self.assertTrue(payload["ok"])
            self.assertIn(seam["detail"], payload["limitations"])
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}), \
                 mock.patch("lib.control.orchestration.doctor.shutil.which", return_value="/usr/bin/tmux"):
                payload = run_orchestration_doctor(config)
            seam = next(probe for probe in payload["probes"] if probe["name"] == "coordinator-seam")
            self.assertEqual(seam["outcome"], "match")

    def test_record_audit_is_opt_out_for_the_activation_handshake(self) -> None:
        """Activation asks whether this installation can run work, not whether
        history is clean, and must not walk every journal under the lock."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (config.initiatives_dir.parent, config.initiatives_dir.parent.parent):
                path.chmod(0o700)
            with mock.patch(
                "lib.control.orchestration.doctor.run_control_doctor",
                return_value={"ok": True},
            ), mock.patch(
                "lib.control.orchestration.doctor._approval_provenance_probe",
            ) as probe:
                payload = run_orchestration_doctor(config, audit_records=False)
            probe.assert_not_called()
            self.assertNotIn(
                "approval-provenance", [item["name"] for item in payload["probes"]],
            )
            self.assertTrue(payload["ok"])

    def test_approval_provenance_probe_is_advisory_and_never_blocks_ok(self) -> None:
        """One suspect historical approval must not brick activation plane-wide."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (config.initiatives_dir.parent, config.initiatives_dir.parent.parent):
                path.chmod(0o700)
            suspect = Probe("approval-provenance", "mismatch", "one suspect approval")
            with mock.patch(
                "lib.control.orchestration.doctor.run_control_doctor",
                return_value={"ok": True},
            ), mock.patch(
                "lib.control.orchestration.doctor.shutil.which", return_value="/usr/bin/tmux",
            ), mock.patch(
                "lib.control.orchestration.doctor._approval_provenance_probe",
                return_value=suspect,
            ):
                payload = run_orchestration_doctor(config)
            self.assertTrue(payload["ok"])
            self.assertIn("one suspect approval", payload["limitations"])

    def test_coordinator_cursor_probe_names_a_parked_coordinator_and_is_advisory(self) -> None:
        """A signed decision a parked coordinator never observed must be visible."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (config.initiatives_dir.parent, config.initiatives_dir.parent.parent):
                path.chmod(0o700)
            behind = Probe(
                "coordinator-cursor", "mismatch",
                "coordinators may not have observed decided events: "
                "6fc3419e coordinator cursor 21 is 1 event(s) behind tail 22",
            )
            with mock.patch(
                "lib.control.orchestration.doctor.run_control_doctor",
                return_value={"ok": True},
            ), mock.patch(
                "lib.control.orchestration.doctor.shutil.which", return_value="/usr/bin/tmux",
            ), mock.patch(
                "lib.control.orchestration.doctor._coordinator_cursor_probe",
                return_value=behind,
            ):
                payload = run_orchestration_doctor(config)
            self.assertTrue(payload["ok"])
            self.assertIn("1 event(s) behind tail 22", " ".join(payload["limitations"]))

    def test_coordinator_cursor_probe_is_quiet_without_initiatives(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (config.initiatives_dir.parent, config.initiatives_dir.parent.parent):
                path.chmod(0o700)

            probe = _coordinator_cursor_probe(config)

            self.assertEqual(probe.outcome, "match")
            self.assertIn("0 live coordinator", probe.detail)

    def test_coordinator_probe_distinguishes_parked_ready_work_from_an_armed_watch(self) -> None:
        store = mock.Mock()
        initiative = {
            "initiative_id": "11111111-1111-4111-8111-111111111111",
            "state": "running", "last_event_sequence": 8,
        }
        coordinator = {
            "coordinator_id": "22222222-2222-4222-8222-222222222222",
            "generation": 1, "state": "active", "event_cursor": 8,
            "updated_at": "2000-01-01T00:00:00Z",
        }
        store.list_initiatives.return_value = [initiative]
        store.current_coordinator.return_value = coordinator
        store.list_nodes_snapshot.return_value = [{
            "node_id": "implementation-a", "state": "ready",
        }]
        store.list_attempts_snapshot.return_value = []
        store.list_events_snapshot.return_value = []
        with mock.patch(
            "lib.control.orchestration.store.InitiativeStore", return_value=store,
        ):
            parked = _coordinator_cursor_probe(mock.Mock())
        self.assertEqual(parked.outcome, "mismatch")
        self.assertIn("implementation-a", parked.detail)
        self.assertIn("parked", parked.detail)

        coordinator["state"] = "waiting"
        coordinator["event_cursor"] = 7
        coordinator["updated_at"] = "2999-01-01T00:00:00Z"
        with mock.patch(
            "lib.control.orchestration.store.InitiativeStore", return_value=store,
        ):
            armed = _coordinator_cursor_probe(mock.Mock())
        self.assertEqual(armed.outcome, "match")

    def test_private_existing_root_and_control_ok_make_doctor_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            for path in (
                config.initiatives_dir.parent,
                config.initiatives_dir.parent.parent,
            ):
                path.chmod(0o700)
            with mock.patch("lib.control.orchestration.doctor.run_control_doctor", return_value={"ok": True}):
                payload = run_orchestration_doctor(config)
            self.assertTrue(payload["ok"])

    def test_namespace_safety_is_rechecked_at_probe_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(root / "missing"),
                   "ASHA_HOME": str(root / "asha"),
                   "XDG_RUNTIME_DIR": str(root / "runtime")}
            for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
                Path(env[key]).mkdir(mode=0o700)
            config = load_config(env)
            config.initiatives_dir.mkdir(parents=True, mode=0o700)
            unsafe = config.initiatives_dir.parent.parent
            unsafe.chmod(0o770)
            with mock.patch(
                "lib.control.orchestration.doctor.run_control_doctor",
                return_value={"ok": True},
            ):
                payload = run_orchestration_doctor(config)
            root_probe = next(
                probe for probe in payload["probes"]
                if probe["name"] == "initiatives-root"
            )
            self.assertEqual(root_probe["outcome"], "mismatch")


if __name__ == "__main__":
    unittest.main()
