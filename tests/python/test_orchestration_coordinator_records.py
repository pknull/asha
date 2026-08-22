"""Coordinator generation records: validator, store lifecycle, and ordering."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lib.control.orchestration import model
from lib.control.orchestration.config import load_config
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.store import InitiativeStore, StoreError
from tests.python.test_orchestration_model import INITIATIVE_ID, TIMESTAMP, initiative


COORDINATOR_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SECOND_COORDINATOR_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
LATER = "2026-08-17T16:05:00Z"


def anchor(**overrides: object) -> dict:
    value = {
        "tmux_socket": None,
        "session": "keeper",
        "pane_id": "%7",
        "pane_pid": 4242,
        "process_start_identity": "boot:test:start:1234",
        "server_pid": 4000,
    }
    value.update(overrides)
    return value


def coordinator(**overrides: object) -> dict:
    value = {
        "contract": model.COORDINATOR_CONTRACT,
        "initiative_id": INITIATIVE_ID,
        "coordinator_id": COORDINATOR_ID,
        "generation": 1,
        "state": "active",
        "harness": "claude",
        "anchor": anchor(),
        "protocol_version": model.COORDINATOR_PROTOCOL_VERSION,
        "claimed_at": TIMESTAMP,
        "event_cursor": 0,
        "last_accepted_action_id": None,
        "predecessor_coordinator_id": None,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    }
    value.update(overrides)
    return value


class CoordinatorValidatorTests(unittest.TestCase):
    def test_valid_record_round_trips_and_digests(self) -> None:
        record = coordinator()
        self.assertEqual(model.validate_coordinator(record), record)
        self.assertRegex(record_digest(record), r"^[0-9a-f]{64}$")

    def test_closed_keys_and_future_contract_are_refused(self) -> None:
        extra = coordinator(unexpected=True)
        with self.assertRaisesRegex(model.ModelError, "unexpected field"):
            model.validate_coordinator(extra)
        future = coordinator(contract=model.COORDINATOR_CONTRACT.replace(".v1", ".v2"))
        with self.assertRaises(model.ModelError):
            model.validate_coordinator(future)
        with self.assertRaises(model.ModelError):
            model.validate_record(future)

    def test_absent_is_not_a_stored_state(self) -> None:
        with self.assertRaisesRegex(model.ModelError, "state is invalid"):
            model.validate_coordinator(coordinator(state="absent"))
        for state in model.COORDINATOR_STATES:
            if state == "absent":
                continue
            model.validate_coordinator(coordinator(state=state))

    def test_generation_protocol_and_cursor_bounds(self) -> None:
        for bad in ({"generation": 0}, {"generation": True}, {"protocol_version": 2},
                    {"event_cursor": -1}, {"event_cursor": model.MAX_EVENT_SEQUENCE + 1}):
            with self.subTest(bad=bad), self.assertRaises(model.ModelError):
                model.validate_coordinator(coordinator(**bad))

    def test_anchor_is_closed_and_typed(self) -> None:
        for bad in (
            {"pane_id": "7"}, {"pane_id": "%x"}, {"pane_pid": 0}, {"server_pid": -1},
            {"session": ""}, {"process_start_identity": "x" * 201},
        ):
            with self.subTest(bad=bad), self.assertRaises(model.ModelError):
                model.validate_coordinator(coordinator(anchor=anchor(**bad)))
        with self.assertRaisesRegex(model.ModelError, "unexpected field"):
            model.validate_coordinator(coordinator(anchor=anchor(extra=1)))
        model.validate_coordinator(coordinator(anchor=anchor(tmux_socket="/tmp/tmux-1000/asha")))

    def test_predecessor_must_differ_and_timestamps_are_ordered(self) -> None:
        with self.assertRaisesRegex(model.ModelError, "predecessor_coordinator_id must differ"):
            model.validate_coordinator(coordinator(predecessor_coordinator_id=COORDINATOR_ID))
        with self.assertRaisesRegex(model.ModelError, "updated_at must not precede"):
            model.validate_coordinator(coordinator(created_at=LATER))
        with self.assertRaisesRegex(model.ModelError, "claimed_at must not precede"):
            model.validate_coordinator(coordinator(created_at=LATER, updated_at=LATER))


class CoordinatorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        env = {
            "HOME": str(self.root / "home"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(env[key]).mkdir(mode=0o700)
        self.config = load_config(env)
        self.store = InitiativeStore(self.config)
        self.store.save_initiative(initiative())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unclaimed_initiative_has_no_current_coordinator(self) -> None:
        self.assertIsNone(self.store.current_coordinator(INITIATIVE_ID))
        self.assertEqual(self.store.list_coordinators_snapshot(INITIATIVE_ID), [])
        self.assertEqual(self.store.record_counts_snapshot(INITIATIVE_ID)["coordinators"], 0)

    def test_first_generation_persists_and_reads_back(self) -> None:
        path = self.store.save_coordinator(INITIATIVE_ID, coordinator())
        self.assertEqual(path.name, f"{COORDINATOR_ID}.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.read_coordinator(INITIATIVE_ID, COORDINATOR_ID), coordinator())
        self.assertEqual(self.store.current_coordinator(INITIATIVE_ID), coordinator())
        self.assertEqual(self.store.record_counts_snapshot(INITIATIVE_ID)["coordinators"], 1)

    def test_new_generation_must_be_next_and_name_its_predecessor(self) -> None:
        self.store.save_coordinator(INITIATIVE_ID, coordinator())
        with self.assertRaisesRegex(StoreError, "generation must be 2"):
            self.store.save_coordinator(
                INITIATIVE_ID, coordinator(coordinator_id=SECOND_COORDINATOR_ID, generation=3),
            )
        with self.assertRaisesRegex(StoreError, "must name its predecessor"):
            self.store.save_coordinator(
                INITIATIVE_ID, coordinator(coordinator_id=SECOND_COORDINATOR_ID, generation=2),
            )
        with self.assertRaisesRegex(StoreError, "predecessor_coordinator_id is unknown"):
            self.store.save_coordinator(INITIATIVE_ID, coordinator(
                coordinator_id=SECOND_COORDINATOR_ID, generation=2,
                predecessor_coordinator_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            ))
        with self.assertRaisesRegex(StoreError, "expected digest is required"):
            self.store.save_coordinator(INITIATIVE_ID, coordinator(generation=2))
        second = coordinator(
            coordinator_id=SECOND_COORDINATOR_ID, generation=2,
            predecessor_coordinator_id=COORDINATOR_ID,
        )
        self.store.save_coordinator(INITIATIVE_ID, second)
        listed = self.store.list_coordinators_snapshot(INITIATIVE_ID)
        self.assertEqual([item["generation"] for item in listed], [1, 2])
        self.assertEqual(self.store.current_coordinator(INITIATIVE_ID), second)

    def test_updates_require_cas_and_legal_transitions(self) -> None:
        first = coordinator()
        self.store.save_coordinator(INITIATIVE_ID, first)
        stale = copy.deepcopy(first)
        stale.update({"state": "stale", "updated_at": LATER})
        with self.assertRaisesRegex(StoreError, "expected digest is required"):
            self.store.save_coordinator(INITIATIVE_ID, stale)
        with self.assertRaises(StoreError):
            self.store.save_coordinator(INITIATIVE_ID, stale, expected_digest="0" * 64)
        self.store.save_coordinator(INITIATIVE_ID, stale, expected_digest=record_digest(first))
        revived = copy.deepcopy(stale)
        revived["state"] = "active"
        with self.assertRaisesRegex(StoreError, "illegal record transition: stale -> active"):
            self.store.save_coordinator(
                INITIATIVE_ID, revived, expected_digest=record_digest(stale),
            )
        fenced = copy.deepcopy(stale)
        fenced["state"] = "fenced"
        self.store.save_coordinator(INITIATIVE_ID, fenced, expected_digest=record_digest(stale))
        again = copy.deepcopy(fenced)
        again["event_cursor"] = 5
        with self.assertRaisesRegex(StoreError, "write-once terminal record"):
            self.store.save_coordinator(
                INITIATIVE_ID, again, expected_digest=record_digest(fenced),
            )

    def test_identity_generation_and_anchor_are_immutable(self) -> None:
        first = coordinator()
        self.store.save_coordinator(INITIATIVE_ID, first)
        for field, value in (
            ("generation", 2), ("harness", "codex"), ("claimed_at", LATER),
            ("anchor", anchor(pane_id="%9")), ("predecessor_coordinator_id", SECOND_COORDINATOR_ID),
        ):
            changed = copy.deepcopy(first)
            changed[field] = value
            changed["updated_at"] = LATER
            with self.subTest(field=field), self.assertRaisesRegex(StoreError, "immutable record field"):
                self.store.save_coordinator(
                    INITIATIVE_ID, changed, expected_digest=record_digest(first),
                )
        cursor = copy.deepcopy(first)
        cursor.update({"event_cursor": 3, "updated_at": LATER})
        self.store.save_coordinator(INITIATIVE_ID, cursor, expected_digest=record_digest(first))
        self.assertEqual(self.store.read_coordinator(INITIATIVE_ID, COORDINATOR_ID)["event_cursor"], 3)

    def test_foreign_filename_or_initiative_is_refused_on_read(self) -> None:
        self.store.save_coordinator(INITIATIVE_ID, coordinator())
        directory = self.config.initiatives_dir / INITIATIVE_ID / "coordinators"
        (directory / "not-a-record.json").write_text("{}")
        with self.assertRaisesRegex(StoreError, "invalid coordinators record filename"):
            self.store.list_coordinators_snapshot(INITIATIVE_ID)


if __name__ == "__main__":
    unittest.main()
