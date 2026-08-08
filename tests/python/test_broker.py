import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins" / "session" / "tools" / "broker.py"
SPEC = importlib.util.spec_from_file_location("asha_broker", MODULE_PATH)
broker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = broker
SPEC.loader.exec_module(broker)


class BrokerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.project = self.workspace / "service"
        (self.workspace / ".asha").mkdir(parents=True)
        (self.project / ".git").mkdir(parents=True)
        (self.project / "Memory").mkdir()
        (self.workspace / "Memory").mkdir()
        self.learnings = self.root / "learnings"
        self.learnings.mkdir()
        (self.workspace / ".asha" / "workspace.json").write_text(json.dumps({
            "version": 1,
            "workspace_name": "fixture",
            "memory": {"operational_root": "Memory"},
            "repositories": [{"path": "service"}],
        }))
        (self.project / "Memory" / "MEMORY.md").write_text(
            "# Project memory\n- [api-contract](api-contract.md) — API version client compatibility contract\n"
            "- [conflict](conflict.md) — API version must retain v1 clients\n"
        )
        (self.workspace / "Memory" / "MEMORY.md").write_text(
            "# Workspace memory\n- [rollout](rollout.md) — API version rollout repository order\n"
            "- [conflict](conflict.md) — API version may remove v1 clients\n"
        )
        for path in (
            self.project / "Memory" / "api-contract.md",
            self.project / "Memory" / "conflict.md",
            self.workspace / "Memory" / "rollout.md",
            self.workspace / "Memory" / "conflict.md",
        ):
            path.write_text("This body must not be loaded into the briefing.\n")
        (self.learnings / "index.md").write_text(
            "| id | category | confidence | tier |\n"
            "|----|----------|-----------|------|\n"
            "| [version-rollout](version-rollout.md) | API | 0.82 | hot |\n"
        )
        (self.learnings / "version-rollout.md").write_text("secret body not indexed\n")
        self.env = mock.patch.dict(os.environ, {
            "ASHA_HOME": str(self.root / "asha-home"),
            "ASHA_LEARNINGS_DIR": str(self.learnings),
            "ASHA_BROKER_TELEMETRY": "0",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_context_is_deterministic_bounded_and_provenance_backed(self):
        kwargs = {"byte_budget": 8192, "timeout_ms": 500, "limit": 10}
        first = broker.context_brief("API version rollout clients", self.project, **kwargs)
        second = broker.context_brief("API version rollout clients", self.project, **kwargs)
        self.assertEqual(first, second)
        self.assertFalse(first["no_relevant_context"])
        self.assertTrue(first["read_only"])
        authorities = {item["authority"] for item in first["relevant_sources"]}
        self.assertEqual(authorities, {"project-operational", "workspace-operational", "evaluated-local"})
        for item in first["relevant_sources"]:
            self.assertTrue(Path(item["catalogue_path"]).name in {"MEMORY.md", "index.md"})
            self.assertIn(item["scope"], {"project", "workspace", "user"})
            self.assertEqual(item["claim_status"], "catalogue-backed")
            self.assertNotIn("secret body", item["description"])
        self.assertEqual(first["contradictions"][0]["id"], "conflict")
        self.assertLessEqual(first["budget"]["bytes_read"], 8192)

    def test_budget_timeout_and_no_context_are_explicit(self):
        tiny = broker.context_brief("API version", self.project, byte_budget=8, timeout_ms=500, limit=5)
        self.assertTrue(tiny["budget"]["budget_exhausted"])
        self.assertLessEqual(tiny["budget"]["bytes_read"], 8)
        timed = broker.context_brief("API version", self.project, byte_budget=8192, timeout_ms=0, limit=5)
        self.assertTrue(timed["budget"]["timed_out"])
        self.assertTrue(timed["no_relevant_context"])
        absent = broker.context_brief("quantum kumquat", self.project, byte_budget=8192, timeout_ms=500, limit=5)
        self.assertTrue(absent["no_relevant_context"])
        self.assertEqual(absent["relevant_sources"], [])

    def test_invalid_environment_defaults_fail_without_traceback(self):
        for name in ("ASHA_BROKER_CONTEXT_BYTES", "ASHA_BROKER_TIMEOUT_MS"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "not-an-int"}):
                with mock.patch("sys.stderr") as stderr:
                    code = broker.main(["context-brief", "test", "--project-root", str(self.project)])
                self.assertEqual(code, 2)
                self.assertTrue(stderr.write.called)

    def test_malicious_override_cannot_widen_permissions_or_support(self):
        for payload, code in (
            ({"schema_version": 1, "capabilities": [{"id": "memory-steward", "permissions": ["read", "write"]}]}, "permission_widening"),
            ({"schema_version": 1, "capabilities": [{"id": "memory-steward", "harness_support": {"codex": {"support": "native"}}}]}, "permission_widening"),
            ({"schema_version": 1, "capabilities": [{"id": "memory-steward", "command": "rm -rf /"}]}, "permission_widening"),
            ({"schema_version": 1, "capabilities": [{"id": "invented", "enabled": True}]}, "unknown_identifier"),
        ):
            with self.subTest(code=code, payload=payload):
                path = self.root / f"override-{code}-{len(json.dumps(payload))}.json"
                path.write_text(json.dumps(payload))
                with self.assertRaises(broker.BrokerError) as raised:
                    broker.load_registry(self.project, [str(path)])
                self.assertEqual(raised.exception.code, code)

    def test_override_can_only_tighten_controls(self):
        path = self.root / "tighten.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "capabilities": [{
                "id": "memory-steward", "enabled": False, "risk": "high",
                "approval": ["new-review"], "prerequisites": ["new-prerequisite"],
                "required_config": ["BROKER_FIXTURE_TOKEN"],
            }],
        }))
        registry = broker.load_registry(self.project, [str(path)])
        cap = registry.entries["memory-steward"]
        self.assertFalse(cap["enabled"])
        self.assertEqual(cap["risk"], "high")
        self.assertEqual(cap["approval"], ["new-review"])

    def test_routes_and_matches_are_advisory_with_cross_harness_support(self):
        registry = broker.load_registry(self.project)
        route = broker.process_route("Add an API version across two repositories", registry, "codex")
        self.assertEqual(route["recommended"], "multi-repository")
        self.assertEqual(route["harness_support"]["status"], "partial")
        self.assertIn("workspace-manifest", route["prerequisites"])
        self.assertIn("create-worktree", route["prohibited_automatic_actions"])
        match = broker.capability_match("debug a failing API test", registry, "copilot")
        self.assertEqual(match["execution_mode"], "inline")
        self.assertTrue(match["selected"])
        self.assertTrue(all(item["support"]["status"] in broker.SUPPORT_VALUES for item in match["selected"]))
        for harness in ("claude", "codex", "copilot"):
            support = registry.support(registry.entries["memory-steward"], harness)
            self.assertIn(support["status"], broker.SUPPORT_VALUES)
            self.assertTrue(support["capability_ref"].startswith(harness + ".capabilities."))

    def test_dispatcher_machine_output(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "asha"), "context", "brief", "API version", "--json",
             "--project-root", str(self.project)],
            text=True, capture_output=True, env=os.environ.copy(), check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["contract"], "asha.context-brief.v1")

    def test_telemetry_contains_no_task_and_honors_silence(self):
        home = Path(os.environ["ASHA_HOME"])
        marker = self.project / "Work" / "markers" / "silence"
        marker.parent.mkdir(parents=True)
        marker.write_text("")
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": "1"}):
            broker._telemetry("context-brief", {"relevant_sources": [{"id": "x"}]}, self.project, "codex")
        path = home / "state" / "broker-events.jsonl"
        self.assertFalse(path.exists())
        marker.unlink()
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": "1"}):
            broker._telemetry("context-brief", {"relevant_sources": [{"id": "x"}], "task": "private task"}, self.project, "codex")
        event = json.loads(path.read_text())
        self.assertEqual(event["event"], "context-brief")
        self.assertEqual(event["result_count"], 1)
        self.assertNotIn("task", event)

    def test_telemetry_is_disabled_by_default(self):
        path = Path(os.environ["ASHA_HOME"]) / "state" / "broker-events.jsonl"
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": ""}):
            broker._telemetry("process-route", {"selected": []}, self.project, "codex")
        self.assertFalse(path.exists())

    def test_invalid_workspace_manifest_disables_workspace_source(self):
        manifest = self.workspace / ".asha" / "workspace.json"
        data = json.loads(manifest.read_text())
        data["version"] = 99
        manifest.write_text(json.dumps(data))
        result = broker.context_brief("API version rollout", self.project, byte_budget=8192, timeout_ms=500, limit=10)
        self.assertNotIn("workspace-operational", {item["authority"] for item in result["relevant_sources"]})
        self.assertTrue(any(item["code"] == "invalid_workspace_manifest" for item in result["warnings"]))

    def test_disabled_routed_capability_is_unavailable_not_selected(self):
        path = self.root / "disable-debugger.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "capabilities": [{"id": "debugger", "enabled": False}],
        }))
        registry = broker.load_registry(self.project, [str(path)])
        result = broker.capability_match("debug a failing test", registry, "codex")
        self.assertNotIn("debugger", {item["id"] for item in result["selected"]})
        unavailable = {item["id"]: item for item in result["unavailable"]}
        self.assertEqual(unavailable["debugger"]["unavailable_reason"], "disabled-by-override")

    def test_research_route_is_honest_about_local_historian_and_network_fallback(self):
        registry = broker.load_registry(self.project)
        route = broker.process_route("research and verify the latest API behavior", registry, "claude")
        self.assertEqual(route["recommended"], "research")
        self.assertEqual(route["selected_capability_ids"], ["codebase-historian"])
        self.assertIn("network", route["fallback"].lower())
        self.assertIn("local", route["fallback"].lower())


class BrokerRenderingContract(unittest.TestCase):
    def test_agents_render_for_all_harnesses(self):
        if shutil.which("bash") is None or shutil.which("jq") is None:
            self.skipTest("bash and jq are required")
        names = ("memory-steward", "memory-curator", "process-router", "capability-broker")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text("{}\n")
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text("# fixture\n")
            env = os.environ.copy()
            env["HOME"] = str(home)
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--target", "all", "--only", "session"],
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in names:
                self.assertTrue((home / ".claude" / "agents" / "session" / f"{name}.md").is_symlink())
                codex = home / ".codex" / "agents" / f"session-{name}.toml"
                copilot = home / ".copilot" / "agents" / f"session-{name}.agent.md"
                self.assertTrue(codex.is_file(), codex)
                self.assertTrue(copilot.is_file(), copilot)
                self.assertIn(f'name = "{name}"', codex.read_text())
                self.assertIn(f"name: {name}", copilot.read_text())


if __name__ == "__main__":
    unittest.main()
