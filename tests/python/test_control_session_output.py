import json
import unittest
from unittest import mock

from lib.control.record_registry import RecordRegistry
from lib.control.session_store import SessionStore
from lib.control.store import StoreError
from tests.python import test_control_managed_sessions as fixtures


class SessionOutputTests(unittest.TestCase):
    setUp = fixtures.SessionTests.setUp
    claim = fixtures.SessionTests.claim

    def emit(self, value):
        self.store.observe(self.sid, self.generation, self.turn["turn_id"], "text", {"text": value})

    def test_retention_keeps_audit_sequences_and_exposes_output_gaps(self):
        self.turn = self.claim()
        with mock.patch("lib.control.session_output.MAX_RECORDS", 2):
            for value in ("first", "second", "third"):
                self.emit(value)
        snapshot = self.store.snapshot(self.sid)
        events = [e for e in snapshot["events"] if e["kind"] == "text"]
        self.assertEqual(len(events), 3)
        self.assertFalse(events[0]["output"]["available"])
        self.assertEqual(events[0]["payload"], {"output_missing": True, "reason": "retention"})
        self.assertEqual([e["payload"]["text"] for e in events[1:]], ["second", "third"])
        self.assertEqual(snapshot["output_gaps"][0]["sequence"], events[0]["sequence"])
        with self.store.db.transaction() as c:
            raw = c.execute("SELECT payload FROM session_events WHERE sequence=?", (events[0]["sequence"],)).fetchone()[0]
            self.assertEqual(json.loads(raw)["output_ref"]["sha256"], events[0]["output"]["sha256"])
            self.assertEqual(c.execute("SELECT count(*) FROM records WHERE domain='session-output' AND scope=?", (self.sid,)).fetchone()[0], 2)
        after = self.store.snapshot(self.sid, after=events[0]["sequence"])
        self.assertEqual(after["output_gaps"], [])
        self.assertEqual(after["next_event_cursor"], events[-1]["sequence"])

    def test_byte_bound_and_other_session_isolation(self):
        self.turn = self.claim()
        other = self.store.create(cwd=self.tmp.name, prompt="other")
        generation = self.store.claim_owner(other["session_id"])["generation"]
        turn = self.store.claim_turn(other["session_id"], generation)
        self.store.observe(other["session_id"], generation, turn["turn_id"], "text", {"text": "other session"})
        with mock.patch("lib.control.session_output.MAX_BYTES", 180):
            for _ in range(10):
                self.emit("x" * 60)
        with self.store.db.transaction() as c:
            size = c.execute("SELECT COALESCE(sum(length(CAST(payload AS BLOB))),0) FROM records WHERE domain='session-output' AND scope=?", (self.sid,)).fetchone()[0]
            self.assertLessEqual(size, 180)
        events = self.store.snapshot(other["session_id"])["events"]
        self.assertEqual([e["payload"]["text"] for e in events if e["kind"] == "text"], ["other session"])

    def test_command_failure_facts_survive_diagnostic_retirement(self):
        self.turn = self.claim()
        with mock.patch('lib.control.session_output.MAX_RECORDS', 1):
            self.store.observe(self.sid, self.generation, self.turn['turn_id'], 'tool', {
                'name': 'commandExecution', 'tool_id': 'command-1', 'status': 'failed',
                'exit_code': -15, 'output': 'Interrupted command diagnostic'})
            self.emit('Later progress')
        tool = next(e for e in self.store.snapshot(self.sid)['events'] if e['kind'] == 'tool')
        self.assertFalse(tool['output']['available'])
        self.assertEqual(tool['payload']['status'], 'failed')
        self.assertEqual(tool['payload']['exit_code'], -15)
        self.assertNotIn('output', tool['payload'])

    def test_display_retirement_preserves_questions_and_recovery_revision(self):
        import uuid
        self.turn = self.claim()
        request_id = str(uuid.uuid4())
        self.store.request(self.sid, self.turn["turn_id"], "Which chapter?", request_id=request_id)
        with mock.patch("lib.control.session_output.MAX_RECORDS", 1):
            for text in ("a", "b", "c"):
                self.emit(text)
        current = self.store.get(self.sid)
        with SessionStore(self.config) as reopened:
            self.assertEqual(reopened.get_request(request_id)["question"], "Which chapter?")
            self.assertEqual(reopened.get(self.sid)["recovery_revision"], current["recovery_revision"])
            self.assertEqual(reopened.snapshot(self.sid)["next_event_cursor"], current["recovery_revision"])

    def test_output_and_envelope_publish_atomically(self):
        self.turn = self.claim()
        before = self.store.snapshot(self.sid)["next_event_cursor"]
        original = RecordRegistry.put
        def fail(registry, *args, **kwargs):
            if registry.domain == "session-output":
                raise StoreError("output storage interrupted")
            return original(registry, *args, **kwargs)
        with mock.patch.object(RecordRegistry, "put", fail), self.assertRaisesRegex(StoreError, "interrupted"):
            self.emit("not published")
        self.assertEqual(self.store.snapshot(self.sid)["next_event_cursor"], before)

    def test_unexplained_missing_output_is_corruption_not_a_retention_gap(self):
        self.turn = self.claim()
        self.emit("retained")
        with self.store.db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='session-output' AND scope=?", (self.sid,))
        with self.assertRaisesRegex(StoreError, "without a retention record"):
            self.store.snapshot(self.sid)

    def test_old_inline_output_and_surrogate_diagnostics_remain_readable(self):
        self.turn = self.claim()
        with self.store.db.transaction(write=True) as c:
            c.execute("INSERT INTO session_events(session_id,kind,payload,created_at) VALUES(?,'text',?,0)", (self.sid, json.dumps({"text": "legacy"})))
        self.emit("diagnostic \udcff")
        events = self.store.snapshot(self.sid)["events"]
        self.assertEqual([e["payload"]["text"] for e in events if e["kind"] == "text"], ["legacy", "diagnostic \udcff"])

    def test_durable_acknowledgement_is_monotonic_and_read_does_not_ack(self):
        self.turn = self.claim()
        self.emit("one")
        page = self.store.events(self.sid, consumer="control", limit=2)
        self.assertEqual(page["acknowledged_cursor"], 0)
        self.assertFalse(page["complete"])
        self.assertEqual(self.store.events(self.sid, consumer="control", limit=2), page)
        self.store.acknowledge_events(self.sid, "control", page["next_event_cursor"])
        with SessionStore(self.config) as reopened:
            following = reopened.events(self.sid, consumer="control")
            self.assertTrue(all(e["sequence"] > page["next_event_cursor"] for e in following["events"]))
            ack = reopened.acknowledge_events(self.sid, "control", following["next_event_cursor"])
            self.assertEqual(reopened.acknowledge_events(self.sid, "control", page["next_event_cursor"]), ack)
            self.assertEqual(reopened.events(self.sid, consumer="control")["events"], [])
            self.assertEqual(reopened.events(self.sid, consumer="chair")["acknowledged_cursor"], 0)
            self.assertEqual(reopened.get(self.sid)["turns"], 1)

    def test_ack_rejects_foreign_events_and_out_of_range_cursors(self):
        other = self.store.create(cwd=self.tmp.name, prompt="other")
        foreign = self.store.get(other["session_id"])["recovery_revision"]
        for cursor in (foreign, True, -1, 2**100):
            with self.subTest(cursor=cursor), self.assertRaises(StoreError):
                self.store.acknowledge_events(self.sid, "control", cursor)

    def test_slow_consumer_sees_gaps_after_restarting(self):
        self.turn = self.claim()
        prior = self.store.get(self.sid)["recovery_revision"]
        self.store.acknowledge_events(self.sid, "slow", prior)
        with mock.patch("lib.control.session_output.MAX_RECORDS", 1):
            for value in ("a", "b", "c"):
                self.emit(value)
        with SessionStore(self.config) as reopened:
            page = reopened.events(self.sid, consumer="slow")
            self.assertEqual(len(page["events"]), 3)
            self.assertEqual(len(page["output_gaps"]), 2)
            self.assertEqual(page["events"][-1]["payload"]["text"], "c")

    def test_custody_and_tool_facts_survive_display_expiry(self):
        self.turn = self.claim()
        with mock.patch("lib.control.session_output.MAX_RECORDS", 1):
            self.store.observe(self.sid, self.generation, self.turn["turn_id"], "progress", {
                "subtype": "native-input-acknowledged", "message_id": self.turn["message_id"]})
            self.store.observe(self.sid, self.generation, self.turn["turn_id"], "tool", {"tool_id": "call-1", "name": "Read"})
            self.emit("latest")
        events = self.store.events(self.sid)["events"]
        progress = next(e for e in events if e["kind"] == "progress")
        tool = next(e for e in events if e["kind"] == "tool")
        self.assertEqual(progress["payload"]["message_id"], self.turn["message_id"])
        self.assertEqual(progress["payload"]["subtype"], "native-input-acknowledged")
        self.assertEqual(tool["payload"]["name"], "Read")
        self.assertFalse(tool["output"]["available"])

    def test_backup_keeps_output_retention_and_acknowledgement_together(self):
        import sqlite3
        from pathlib import Path
        from lib.control.session_output import project
        self.turn = self.claim()
        with mock.patch("lib.control.session_output.MAX_RECORDS", 1):
            self.emit("expired")
            self.emit("kept")
        page = self.store.events(self.sid)
        self.store.acknowledge_events(self.sid, "control", page["next_event_cursor"])
        path = Path(self.tmp.name) / "output-backup.sqlite3"
        self.store.db.backup(path)
        with sqlite3.connect(path) as c:
            c.row_factory = sqlite3.Row
            events = [dict(r) for r in c.execute("SELECT * FROM session_events WHERE kind='text' ORDER BY sequence")]
            for event in events:
                project(c, event)
            self.assertFalse(events[0]["output"]["available"])
            self.assertEqual(events[1]["payload"]["text"], "kept")
            ack = RecordRegistry("session-event-consumers", scope=self.sid).read(c, "control")
            self.assertEqual(ack["value"]["through"], page["next_event_cursor"])

    def test_cli_event_delivery_does_not_advance_until_operator_acknowledges(self):
        import contextlib
        import io
        from lib.control.sessions import main
        self.turn = self.claim()
        self.emit("visible")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["events", self.sid, "--consumer", "control", "--json"], env=self.env), 0)
        page = json.loads(output.getvalue())
        args = ["ack-events", self.sid, "--consumer", "control", "--through", str(page["next_event_cursor"]), "--json"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args, env={**self.env, "ASHA_MANAGED_SESSION_ID": self.sid}), 2)
        # Release the fixture's owner identity so this call represents an
        # independent operator rather than the provider's own ancestry.
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?", (self.sid,))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args, env=self.env), 0)
        self.assertEqual(self.store.events(self.sid, consumer="control")["events"], [])

    def test_non_object_writes_are_refused_but_legacy_values_stay_readable(self):
        self.turn = self.claim()
        before = self.store.events(self.sid)["next_event_cursor"]
        for kind in ("text", "tool", "progress", "completed"):
            for payload in (None, [], "status", 4):
                with self.subTest(kind=kind, payload=payload), self.assertRaisesRegex(StoreError, "object"):
                    self.store.observe(self.sid, self.generation, self.turn["turn_id"], kind, payload)
        self.assertEqual(self.store.events(self.sid)["next_event_cursor"], before)
        with self.store.db.transaction(write=True) as c:
            c.execute("INSERT INTO session_events(session_id,kind,payload,created_at) VALUES(?,'progress',?,0)", (self.sid, json.dumps("legacy-status")))
        self.assertEqual(self.store.events(self.sid)["events"][-1]["payload"], "legacy-status")

    def test_malformed_retention_and_consumer_records_raise_store_errors(self):
        self.turn = self.claim()
        self.emit("retained")
        with self.store.db.transaction(write=True) as c:
            RecordRegistry("session-output-retention").put(c, self.sid, b'{"retired_through":"bad"}')
            RecordRegistry("session-event-consumers", scope=self.sid).put(c, "control", b'{"through":true}')
        with mock.patch("lib.control.session_output.MAX_RECORDS", 1), self.assertRaisesRegex(StoreError, "retention cursor"):
            self.emit("atomic failure")
        with self.assertRaisesRegex(StoreError, "consumer cursor"):
            self.store.events(self.sid, consumer="control")
        with self.assertRaisesRegex(StoreError, "consumer cursor"):
            self.store.acknowledge_events(self.sid, "control", 0)

    def test_payload_cannot_forge_authoritative_output_availability(self):
        self.turn = self.claim()
        self.store.observe(self.sid, self.generation, self.turn["turn_id"], "text", {"output_missing": True, "reason": "retention"})
        page = self.store.events(self.sid)
        self.assertTrue(page["events"][-1]["output"]["available"])
        self.assertEqual(page["output_gaps"], [])

    def test_consumer_limit_preserves_existing_checkpoints(self):
        with mock.patch("lib.control.session_output.MAX_CONSUMERS", 2):
            self.store.acknowledge_events(self.sid, "control", 0)
            self.store.acknowledge_events(self.sid, "chair", 0)
            with self.assertRaisesRegex(StoreError, "consumer limit"):
                self.store.acknowledge_events(self.sid, "extra", 0)
            cursor = self.store.get(self.sid)["recovery_revision"]
            self.store.acknowledge_events(self.sid, "control", cursor)
            self.assertEqual(self.store.events(self.sid, consumer="control")["acknowledged_cursor"], cursor)
