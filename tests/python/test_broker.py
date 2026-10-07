"""Process/capability broker tests after operational context-brief removal."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "asha_broker", ROOT / "plugins/session/tools/broker.py"
)
broker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = broker
SPEC.loader.exec_module(broker)


class BrokerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".git").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "ASHA_HOME": str(Path(self.tmp.name) / "asha-home"),
            "ASHA_BROKER_TELEMETRY": "0",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_context_brief_surface_is_removed(self):
        self.assertFalse(hasattr(broker, "context_brief"))
        with self.assertRaises(SystemExit) as raised:
            broker.main(["context-brief", "old surface"])
        self.assertEqual(2, raised.exception.code)

    def test_removed_memory_agents_are_absent_from_registry(self):
        registry = broker.load_registry(self.project)
        self.assertNotIn("memory-steward", registry.entries)
        self.assertNotIn("memory-curator", registry.entries)

    def test_malicious_override_cannot_widen_permissions(self):
        path = Path(self.tmp.name) / "override.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "capabilities": [{"id": "process-router", "permissions": ["write"]}],
        }))
        with self.assertRaises(broker.BrokerError) as raised:
            broker.load_registry(self.project, [str(path)])
        self.assertEqual("permission_widening", raised.exception.code)

    def test_routes_and_matches_remain_advisory(self):
        registry = broker.load_registry(self.project)
        route = broker.process_route("debug a failing API test", registry, "codex")
        self.assertEqual("debugging", route["recommended"])
        match = broker.capability_match("debug a failing API test", registry, "copilot")
        self.assertEqual("inline", match["execution_mode"])
        self.assertTrue(match["selected"])

    def test_dispatcher_machine_output_for_remaining_surfaces(self):
        result = subprocess.run(
            [str(ROOT / "bin/asha"), "process", "route", "debug a failure", "--json",
             "--project-root", str(self.project)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("asha.process-route.v1", json.loads(result.stdout)["contract"])

    def test_opt_in_telemetry_appends_through_a_same_user_symlink(self):
        state = Path(self.tmp.name) / "asha-home" / "state"
        state.mkdir(parents=True)
        elsewhere = Path(self.tmp.name) / "other-disk" / "broker-events.jsonl"
        elsewhere.parent.mkdir()
        elsewhere.write_text("")
        (state / "broker-events.jsonl").symlink_to(elsewhere)
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": "1"}):
            broker._telemetry("route", {"selected": ["x"]}, self.project, "claude")
        events = [json.loads(line) for line in elsewhere.read_text().splitlines()]
        self.assertEqual(["route"], [event["event"] for event in events])
        self.assertTrue((state / "broker-events.jsonl").is_symlink())

    def test_opt_in_telemetry_skips_a_non_regular_target_without_blocking(self):
        state = Path(self.tmp.name) / "asha-home" / "state"
        state.mkdir(parents=True)
        os.mkfifo(state / "broker-events.jsonl")
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": "1"}):
            writer = threading.Thread(
                target=broker._telemetry,
                args=("route", {"selected": []}, self.project, "claude"),
                daemon=True,
            )
            writer.start()
            writer.join(timeout=5)
        self.assertFalse(writer.is_alive(), "telemetry must never block on a FIFO")


if __name__ == "__main__":
    unittest.main()
