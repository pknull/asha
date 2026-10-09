"""Issue #124: read-only conditional dependency plans over the broker registry."""

import builtins
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "asha_broker_plan", ROOT / "plugins/session/tools/broker.py"
)
broker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = broker
SPEC.loader.exec_module(broker)

SHIPPED_REGISTRY = ROOT / "plugins/session/broker/capabilities.json"
GOLDEN = ROOT / "tests/fixtures/broker-route-match-562869a0.json"
HARNESSES = ("claude", "codex", "copilot", "opencode")


def entry(cap_id, *, dependencies=None, process=None, **fields):
    """A valid fixture capability whose surfaces are native skills everywhere."""
    value = {
        "id": cap_id,
        "kind": "skill",
        "description": f"Fixture capability {cap_id}.",
        "categories": ["fixture"],
        "task_patterns": [],
        "prerequisites": [],
        "required_config": [],
        "risk": "low",
        "approval": [],
        "output_contract": "fixture.v1",
        "permissions": ["read"],
        "harness_support": {
            harness: {
                "capability_ref": f"{harness}.capabilities.skills",
                "fallback": f"Run {cap_id} inline.",
            }
            for harness in HARNESSES
        },
        "fallback": f"Do {cap_id} by hand.",
        "ownership": {"owner": "asha/test", "version": "1.0.0"},
    }
    value.update(fields)
    if dependencies is not None:
        value["dependencies"] = dependencies
    if process is not None:
        value["process"] = process
    return value


def requires(target, **extra):
    return {"id": target, "relation": "requires", **extra}


def when_missing(command):
    return {"type": "command-missing", "command": command}


class PlanFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        (self.project / ".git").mkdir(parents=True)
        self.asha_home = self.base / "asha-home"
        self.env = mock.patch.dict(os.environ, {
            "ASHA_HOME": str(self.asha_home),
            "ASHA_BROKER_TELEMETRY": "0",
        }, clear=False)
        self.env.start()
        os.environ.pop("ASHA_BROKER_OVERRIDE", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def use_registry(self, *entries):
        data = json.loads(SHIPPED_REGISTRY.read_text(encoding="utf-8"))
        data["capabilities"] = list(entries)
        path = self.base / "registry.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        patcher = mock.patch.object(broker, "REGISTRY_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def load_error(self, *entries):
        self.use_registry(*entries)
        with self.assertRaises(broker.BrokerError) as raised:
            broker.load_registry(self.project)
        return raised.exception

    def plan(self, cap_id, harness="claude", probe=False, overrides=()):
        registry = broker.load_registry(self.project, overrides)
        return broker.capability_plan(cap_id, registry, harness, probe=probe)

    def path_with(self, *commands):
        bindir = self.base / ("bin-" + "-".join(commands or ("empty",)))
        bindir.mkdir()
        marker = self.base / "executed"
        for name in commands:
            tool = bindir / name
            tool.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
            tool.chmod(0o755)
        return str(bindir), marker


class PlanContractTests(PlanFixture):
    def test_plan_reports_edges_conditions_prerequisites_approvals_and_fallback(self):
        result = self.plan("github-cli", harness="codex")
        self.assertEqual("asha.capability-plan.v1", result["contract"])
        self.assertEqual("github-cli", result["selected"]["id"])
        self.assertEqual("inspection", result["execution_mode"])
        self.assertTrue(result["advisory_only"])
        self.assertIn(result["status"], {"blocked", "conditionally-blocked", "no-known-blockers"})
        edge = next(e for e in result["edges"] if e["to"] == "github-cli-setup")
        self.assertEqual("github-cli", edge["from"])
        self.assertEqual("requires", edge["relation"])
        self.assertEqual({"type": "command-missing", "command": "gh"}, edge["condition"])
        self.assertEqual("unevaluated", edge["condition_state"])
        names = {(p["capability"], p["name"]) for p in result["prerequisites"]}
        self.assertIn(("github-cli", "gh-authenticated-for-target-host"), names)
        self.assertTrue(all(p["state"] == "unverified" for p in result["prerequisites"]))
        approvals = {(a["capability"], a["approval"]) for a in result["approval_requirements"]}
        self.assertIn(("github-cli-setup", "explicit-user-approval-to-install"), approvals)
        self.assertTrue(result["fallback"])
        for key in ("nodes", "resolution_order", "required_config", "missing", "unverified",
                    "blockers", "probe", "registry", "prohibited_automatic_actions"):
            self.assertIn(key, result)
        self.assertEqual({"requested": False}, result["probe"])
        for action in ("install", "authenticate", "load-skills", "write-memory", "execute-workflow"):
            self.assertIn(action, result["prohibited_automatic_actions"])

    def test_dependency_edges_are_separate_from_human_prerequisites(self):
        self.use_registry(
            entry("root", prerequisites=["human-signoff"], dependencies=[requires("leaf")]),
            entry("leaf", prerequisites=["leaf-data"]),
        )
        result = self.plan("root")
        self.assertEqual([("root", "leaf")], [(e["from"], e["to"]) for e in result["edges"]])
        self.assertEqual(
            [("root", "human-signoff"), ("leaf", "leaf-data")],
            [(p["capability"], p["name"]) for p in result["prerequisites"]],
        )
        self.assertNotIn("leaf", [p["name"] for p in result["prerequisites"]])

    def test_process_capability_ids_become_unconditional_edges(self):
        result = self.plan("process.debugging")
        self.assertEqual(
            [("process.debugging", "debugger", "process"), ("process.debugging", "code-verify", "process")],
            [(e["from"], e["to"], e["source"]) for e in result["edges"]],
        )
        self.assertTrue(all(e["condition_state"] == "active" for e in result["edges"]))
        self.assertEqual(["debugger", "code-verify", "process.debugging"], result["resolution_order"])

    def test_unknown_selected_identifier_is_explicit(self):
        registry = broker.load_registry(self.project)
        with self.assertRaises(broker.BrokerError) as raised:
            broker.capability_plan("no-such-capability", registry, "claude")
        self.assertEqual("unknown_identifier", raised.exception.code)


class TransitiveResolutionTests(PlanFixture):
    def chain(self):
        return [
            entry("app", dependencies=[requires("lib"), {"id": "docs", "relation": "optional"}]),
            entry("lib", dependencies=[requires("base")]),
            entry("base"),
            entry("docs"),
        ]

    def test_transitive_order_is_dependencies_first_and_deterministic(self):
        self.use_registry(*self.chain())
        first = self.plan("app")
        self.assertEqual(["base", "lib", "docs", "app"], first["resolution_order"])
        nodes = {n["id"]: n for n in first["nodes"]}
        self.assertEqual("required", nodes["base"]["requirement"])
        self.assertEqual("optional", nodes["docs"]["requirement"])
        self.assertEqual(2, nodes["base"]["depth"])
        again = self.plan("app")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_registry_entry_order_does_not_change_the_plan(self):
        self.use_registry(*self.chain())
        forward = self.plan("app")
        self.use_registry(*reversed(self.chain()))
        backward = self.plan("app")
        forward.pop("registry")
        backward.pop("registry")
        self.assertEqual(forward, backward)

    def test_depth_is_the_shortest_path(self):
        self.use_registry(
            entry("top", dependencies=[requires("mid"), requires("low")]),
            entry("mid", dependencies=[requires("low")]),
            entry("low"),
        )
        depths = {n["id"]: n["depth"] for n in self.plan("top")["nodes"]}
        self.assertEqual({"top": 0, "mid": 1, "low": 1}, depths)

    def test_diamond_dependency_appears_once(self):
        self.use_registry(
            entry("top", dependencies=[requires("left"), requires("right")]),
            entry("left", dependencies=[requires("shared")]),
            entry("right", dependencies=[requires("shared")]),
            entry("shared"),
        )
        result = self.plan("top")
        self.assertEqual(["shared", "left", "right", "top"], result["resolution_order"])
        self.assertEqual(1, [n["id"] for n in result["nodes"]].count("shared"))


class GraphValidationTests(PlanFixture):
    def test_cycle_is_reported_with_its_path(self):
        error = self.load_error(
            entry("aa", dependencies=[requires("bb")]),
            entry("bb", dependencies=[requires("aa")]),
        )
        self.assertEqual("dependency_cycle", error.code)
        self.assertEqual({"cycle": ["aa", "bb", "aa"]}, error.details)

    def test_cycle_behind_an_inactive_condition_is_still_reported(self):
        error = self.load_error(
            entry("aa", dependencies=[requires("bb", when=when_missing("never-missing-tool"))]),
            entry("bb", dependencies=[{"id": "aa", "relation": "optional"}]),
        )
        self.assertEqual("dependency_cycle", error.code)

    def test_self_dependency_is_a_cycle(self):
        error = self.load_error(entry("aa", dependencies=[requires("aa")]))
        self.assertEqual("dependency_cycle", error.code)
        self.assertEqual({"cycle": ["aa", "aa"]}, error.details)

    def test_unknown_identifier_on_a_conditional_edge_is_reported(self):
        error = self.load_error(
            entry("aa", dependencies=[requires("ghost", when=when_missing("gh"))]),
        )
        self.assertEqual("unknown_identifier", error.code)
        self.assertEqual({"from": "aa", "to": "ghost"}, error.details)

    def test_cycle_through_process_capability_ids_is_reported(self):
        error = self.load_error(
            entry("flow", kind="process-template",
                  process={"priority": 1, "verification": [], "capability_ids": ["step"]}),
            entry("step", dependencies=[requires("flow")]),
        )
        self.assertEqual("dependency_cycle", error.code)

    def test_cli_error_json_carries_details(self):
        self.use_registry(
            entry("aa", dependencies=[requires("bb")]),
            entry("bb", dependencies=[requires("aa")]),
        )
        stdout = self.run_main(["capabilities-plan", "aa", "--json", "--project-root", str(self.project)])
        error = json.loads(stdout)
        self.assertEqual("dependency_cycle", error["error"]["code"])
        self.assertEqual(["aa", "bb", "aa"], error["error"]["details"]["cycle"])

    def test_existing_error_json_shape_is_unchanged(self):
        stdout = self.run_main(["capabilities-match", " ", "--json"], expect=2)
        self.assertNotIn("details", json.loads(stdout)["error"])

    def run_main(self, argv, expect=2):
        with mock.patch("sys.stdout") as out:
            lines = []
            out.write.side_effect = lines.append
            code = broker.main(argv)
        self.assertEqual(expect, code)
        return "".join(lines)


class ConflictingMetadataTests(PlanFixture):
    def assert_load_error(self, code, *entries):
        self.assertEqual(code, self.load_error(*entries).code)

    def test_duplicate_edges_to_one_target_conflict(self):
        self.assert_load_error(
            "conflicting_metadata",
            entry("aa", dependencies=[requires("bb"), {"id": "bb", "relation": "optional"}]),
            entry("bb"),
        )

    def test_dependency_repeating_a_process_capability_conflicts(self):
        self.assert_load_error(
            "conflicting_metadata",
            entry("flow", kind="process-template",
                  process={"priority": 1, "verification": [], "capability_ids": ["bb"]},
                  dependencies=[requires("bb", when=when_missing("gh"))]),
            entry("bb"),
        )

    def test_untyped_or_unknown_conditions_are_rejected(self):
        for condition in (
            {"type": "shell", "command": "test -x gh"},
            {"type": "command-missing"},
            {"type": "command-missing", "command": "gh", "extra": 1},
            {"type": "command-missing", "command": "gh; rm -rf /"},
            {"type": "command-missing", "command": "../bin/gh"},
            "command-missing gh",
        ):
            with self.subTest(condition=condition):
                self.assert_load_error(
                    "invalid_registry",
                    entry("aa", dependencies=[requires("bb", when=condition)]), entry("bb"),
                )

    def test_unknown_relation_and_edge_fields_are_rejected(self):
        for edge in ({"id": "bb", "relation": "loads"}, {"id": "bb", "relation": "requires", "run": "x"},
                     {"relation": "requires"}, "bb"):
            with self.subTest(edge=edge):
                self.assert_load_error("invalid_registry", entry("aa", dependencies=[edge]), entry("bb"))

    def test_override_cannot_rewrite_dependencies(self):
        path = self.base / "override.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "capabilities": [{"id": "github-cli", "dependencies": []}],
        }), encoding="utf-8")
        with self.assertRaises(broker.BrokerError) as raised:
            broker.load_registry(self.project, [str(path)])
        self.assertEqual("permission_widening", raised.exception.code)

    def test_override_disabling_a_dependency_blocks_instead_of_hiding_it(self):
        self.use_registry(entry("app", dependencies=[requires("lib")]), entry("lib"))
        path = self.base / "override.json"
        path.write_text(json.dumps({
            "schema_version": 1, "capabilities": [{"id": "lib", "enabled": False}],
        }), encoding="utf-8")
        result = self.plan("app", overrides=[str(path)])
        self.assertEqual("blocked", result["status"])
        self.assertIn({"capability": "lib", "reason": "disabled-by-override"},
                      [{k: b[k] for k in ("capability", "reason")} for b in result["blockers"]])
        self.assertIn("lib", result["resolution_order"])

    def test_override_added_approval_reaches_the_plan(self):
        self.use_registry(entry("app", dependencies=[requires("lib")]), entry("lib"))
        path = self.base / "override.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "capabilities": [{"id": "lib", "approval": ["site-approval"]}],
        }), encoding="utf-8")
        result = self.plan("app", overrides=[str(path)])
        self.assertIn({"capability": "lib", "approval": "site-approval", "applicability": "required"},
                      result["approval_requirements"])


class MissingAndUnavailableTests(PlanFixture):
    def test_missing_required_configuration_blocks_and_never_prints_values(self):
        self.use_registry(entry("app", dependencies=[requires("api")]),
                          entry("api", required_config=["FIXTURE_API_TOKEN"]))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FIXTURE_API_TOKEN", None)
            missing = self.plan("app")
        self.assertEqual("blocked", missing["status"])
        self.assertIn({"type": "config", "capability": "api", "name": "FIXTURE_API_TOKEN",
                       "applicability": "required"},
                      missing["missing"])
        secret = "sk-fixture-SECRET-value-0123456789"
        printed = []
        with mock.patch.dict(os.environ, {"FIXTURE_API_TOKEN": secret}):
            present = self.plan("app")
            for extra in (["--json"], []):
                with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                    out.write.side_effect = printed.append
                    err.write.side_effect = printed.append
                    self.assertEqual(0, broker.main(["capabilities-plan", "app", "--probe",
                                                     "--project-root", str(self.project), *extra]))
        self.assertNotIn(secret, json.dumps(present))
        self.assertTrue(printed)
        self.assertNotIn(secret, "".join(printed))
        config = next(c for c in present["required_config"] if c["name"] == "FIXTURE_API_TOKEN")
        self.assertTrue(config["present"])
        self.assertEqual("presence-only", config["verifies"])
        self.assertIn("no-known-blockers", present["status"])
        credential = [u for u in present["unverified"] if u["type"] == "credential"]
        self.assertTrue(credential)
        self.assertIn("authentication", credential[0]["detail"])
        self.assertIn("authorization", credential[0]["detail"])

    def test_unsupported_client_surface_blocks_and_uses_harness_fallback(self):
        unsupported = entry("plug")
        unsupported["harness_support"]["codex"] = {
            "capability_ref": "codex.capabilities.plugins",
            "fallback": "Use the inline procedure; Codex plugins are unsupported.",
        }
        self.use_registry(unsupported)
        result = self.plan("plug", harness="codex")
        node = result["nodes"][0]
        self.assertEqual("unsupported", node["declared_support"]["status"])
        self.assertEqual("blocked", result["status"])
        self.assertEqual("Use the inline procedure; Codex plugins are unsupported.", result["fallback"])
        self.assertIn("unsupported-on-harness", [b["reason"] for b in result["blockers"]])
        self.assertEqual("Use the inline procedure; Codex plugins are unsupported.", node["fallback"])
        supported = self.plan("plug", harness="claude")
        self.assertEqual("Do plug by hand.", supported["nodes"][0]["fallback"])
        self.assertEqual("native", supported["nodes"][0]["declared_support"]["status"])
        self.assertEqual({"state": "unverified", "basis": "declared-support-only"},
                         supported["nodes"][0]["availability"])

    def test_blockers_on_conditional_dependencies_do_not_claim_a_hard_block(self):
        self.use_registry(
            entry("app", dependencies=[requires("setup", when=when_missing("fixture-tool"))]),
            entry("setup", required_config=["FIXTURE_SETUP_ONLY"]),
        )
        os.environ.pop("FIXTURE_SETUP_ONLY", None)
        result = self.plan("app")
        self.assertEqual("conditionally-blocked", result["status"])
        self.assertEqual([], result["blockers"])
        self.assertEqual("setup", result["conditional_blockers"][0]["capability"])

    def test_requirement_only_through_an_inactive_edge_does_not_block(self):
        self.use_registry(
            entry("root", dependencies=[requires("gate", when=when_missing("present-tool")),
                                        {"id": "side", "relation": "optional"}]),
            entry("gate", dependencies=[requires("shared")]),
            entry("side", dependencies=[requires("shared")]),
            entry("shared", required_config=["FIXTURE_SHARED_ONLY"]),
        )
        os.environ.pop("FIXTURE_SHARED_ONLY", None)
        path, marker = self.path_with("present-tool")
        with mock.patch.dict(os.environ, {"PATH": path}):
            result = self.plan("root", probe=True)
        self.assertFalse(marker.exists())
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual("active", nodes["shared"]["activation"])
        self.assertEqual("required", nodes["shared"]["requirement"])
        self.assertEqual("no-known-blockers", result["status"])
        self.assertEqual(["shared"], [w["capability"] for w in result["warnings"]])

    def test_item_applicability_matches_the_blocker_classification(self):
        self.use_registry(
            entry("root", dependencies=[{"id": "side", "relation": "optional"},
                                        requires("gate", when=when_missing("fixture-tool")),
                                        {"id": "extra", "relation": "optional"}]),
            entry("side", dependencies=[requires("shared")]),
            entry("gate", dependencies=[requires("shared")]),
            entry("shared", approval=["shared-approval"], required_config=["FIXTURE_SHARED_CFG"]),
            entry("extra", approval=["extra-approval"]),
        )
        os.environ.pop("FIXTURE_SHARED_CFG", None)
        result = self.plan("root")
        approvals = {a["approval"]: a["applicability"] for a in result["approval_requirements"]}
        # Active through an optional path, required only behind an unevaluated
        # condition: conditional, exactly as the blocker lists say.
        self.assertEqual("conditional", approvals["shared-approval"])
        self.assertEqual("optional", approvals["extra-approval"])
        self.assertEqual(["shared"], [b["capability"] for b in result["conditional_blockers"]])
        self.assertEqual("conditional", next(m for m in result["missing"] if m["type"] == "config")["applicability"])
        self.assertEqual("conditionally-blocked", result["status"])

    def test_only_a_required_missing_command_needs_a_foundation(self):
        self.use_registry(
            entry("root", dependencies=[{"id": "setup", "relation": "optional", "when": when_missing("tool-a")},
                                        {"id": "mid", "relation": "optional"}]),
            entry("mid", dependencies=[requires("setup2", when=when_missing("tool-b"))]),
            entry("setup"), entry("setup2"),
        )
        path, _ = self.path_with()
        with mock.patch.dict(os.environ, {"PATH": path}):
            result = self.plan("root", probe=True)
        self.assertEqual({("tool-a", "optional"), ("tool-b", "optional")},
                         {(m["name"], m["applicability"]) for m in result["missing"]})
        self.assertEqual("no-known-blockers", result["status"])

    def test_a_known_missing_foundation_outranks_a_conditional_blocker(self):
        self.use_registry(
            entry("root", dependencies=[requires("setup", when=when_missing("tool-a")),
                                        requires("maybe", when=when_missing("tool-b"))]),
            entry("setup"),
            entry("maybe", required_config=["FIXTURE_MAYBE_CFG"]),
        )
        os.environ.pop("FIXTURE_MAYBE_CFG", None)
        path, _ = self.path_with("tool-b")
        with mock.patch.dict(os.environ, {"PATH": path}):
            plain = self.plan("root", probe=True)
        self.assertEqual("needs-foundation", plain["status"])
        with mock.patch.dict(os.environ, {"PATH": path}), \
                mock.patch.object(broker, "MAX_PROBE_COMMANDS", 1):
            bounded = self.plan("root", probe=True)
        self.assertEqual(["maybe"], [b["capability"] for b in bounded["conditional_blockers"]])
        self.assertEqual("needs-foundation", bounded["status"])

    def test_optional_dependency_problem_is_a_warning(self):
        self.use_registry(
            entry("app", dependencies=[{"id": "extra", "relation": "optional"}]),
            entry("extra", required_config=["FIXTURE_OPTIONAL_ONLY"]),
        )
        os.environ.pop("FIXTURE_OPTIONAL_ONLY", None)
        result = self.plan("app")
        self.assertEqual("no-known-blockers", result["status"])
        self.assertEqual("extra", result["warnings"][0]["capability"])


class ConditionalFoundationTests(PlanFixture):
    def test_without_probe_the_foundation_is_conditional_and_no_lookup_happens(self):
        with mock.patch.object(broker.shutil, "which", side_effect=AssertionError("probe without --probe")):
            result = self.plan("github-cli")
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual("conditional", nodes["github-cli-setup"]["activation"])
        self.assertIn("github-cli-setup", result["resolution_order"])
        condition = [u for u in result["unverified"] if u["type"] == "condition"]
        self.assertEqual("github-cli -> github-cli-setup", condition[0]["edge"])

    def test_probe_activates_the_setup_foundation_when_gh_is_missing(self):
        path, _ = self.path_with()
        with mock.patch.dict(os.environ, {"PATH": path}):
            result = self.plan("github-cli", probe=True)
        edge = next(e for e in result["edges"] if e["to"] == "github-cli-setup")
        self.assertEqual("active", edge["condition_state"])
        self.assertIn({"type": "command", "capability": "github-cli", "name": "gh",
                       "remedy": "github-cli-setup", "applicability": "required"}, result["missing"])
        # gh is known to be missing: the plan must not read as "proceed".
        self.assertEqual("needs-foundation", result["status"])
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual("active", nodes["github-cli-setup"]["activation"])
        self.assertEqual(["github-cli-setup", "github-cli"], result["resolution_order"])
        self.assertEqual({"requested": True, "kind": "path-lookup", "truncated": False,
                          "commands": [{"name": "gh", "found": False}]}, result["probe"])

    def test_probe_skips_the_foundation_when_gh_is_present_without_running_it(self):
        path, marker = self.path_with("gh")
        with mock.patch.dict(os.environ, {"PATH": path}):
            result = self.plan("github-cli", probe=True)
        self.assertFalse(marker.exists(), "a probe must never execute the command")
        edge = next(e for e in result["edges"] if e["to"] == "github-cli-setup")
        self.assertEqual("inactive", edge["condition_state"])
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual("inactive", nodes["github-cli-setup"]["activation"])
        # Requirement is the declared relation; activation says whether it applies here.
        self.assertEqual("required", nodes["github-cli-setup"]["requirement"])
        self.assertNotIn("github-cli-setup", result["resolution_order"])
        self.assertNotIn("github-cli-setup", [a["capability"] for a in result["approval_requirements"]])
        self.assertEqual([], result["missing"])

    def test_probe_is_bounded(self):
        leaves = [entry(f"leaf-{i:02d}") for i in range(broker.MAX_PROBE_COMMANDS + 3)]
        root = entry("root", dependencies=[
            requires(leaf["id"], when=when_missing(f"fixture-cmd-{i:02d}"))
            for i, leaf in enumerate(leaves)
        ])
        self.use_registry(root, *leaves)
        path, _ = self.path_with()
        with mock.patch.dict(os.environ, {"PATH": path}):
            result = self.plan("root", probe=True)
        self.assertEqual(broker.MAX_PROBE_COMMANDS, len(result["probe"]["commands"]))
        states = [e["condition_state"] for e in result["edges"]]
        self.assertEqual(["active"] * broker.MAX_PROBE_COMMANDS + ["unevaluated"] * 3, states)
        self.assertTrue(result["probe"]["truncated"])
        details = {u["detail"] for u in result["unverified"] if u["type"] == "condition"}
        self.assertEqual({"probe bound reached; not evaluated"}, details)

    def test_probe_budget_is_spent_only_on_edges_that_can_apply(self):
        gated = [entry(f"gated-{i:02d}") for i in range(broker.MAX_PROBE_COMMANDS)]
        self.use_registry(
            entry("root", dependencies=[requires("gate", when=when_missing("present-tool")),
                                        requires("needed", when=when_missing("needed-tool"))]),
            entry("gate", dependencies=[requires(g["id"], when=when_missing(f"gated-cmd-{i:02d}"))
                                        for i, g in enumerate(gated)]),
            entry("needed"),
            *gated,
        )
        path, _ = self.path_with("present-tool")
        with mock.patch.dict(os.environ, {"PATH": path}):
            result = self.plan("root", probe=True)
        self.assertEqual(["present-tool", "needed-tool"], [c["name"] for c in result["probe"]["commands"]])
        self.assertFalse(result["probe"]["truncated"])
        states = {(e["from"], e["to"]): e["condition_state"] for e in result["edges"]}
        self.assertEqual("inactive", states[("root", "gate")])
        self.assertEqual("active", states[("root", "needed")])
        self.assertEqual({"skipped"}, {v for (src, _), v in states.items() if src == "gate"})
        # Conditions below an inactive gate are irrelevant here, not unverified.
        self.assertEqual([], [u for u in result["unverified"] if u["type"] == "condition"])
        self.assertEqual([("root", "needed-tool")],
                         [(m["capability"], m["name"]) for m in result["missing"]])
        self.assertEqual("needs-foundation", result["status"])


class SideEffectTests(PlanFixture):
    def test_inspection_never_spawns_writes_or_reads_skill_files(self):
        before = sorted(str(p) for p in self.base.rglob("*"))
        reads = []
        real_read_text = Path.read_text
        real_open = builtins.open

        def recording_read_text(path, *args, **kwargs):
            reads.append(Path(path).resolve())
            return real_read_text(path, *args, **kwargs)

        def recording_open(file, *args, **kwargs):
            reads.append(Path(file).resolve())
            return real_open(file, *args, **kwargs)

        refuse = mock.Mock(side_effect=AssertionError("inspection must not spawn processes"))
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": "1"}), \
                mock.patch.object(Path, "read_text", recording_read_text), \
                mock.patch.object(builtins, "open", recording_open), \
                mock.patch.object(subprocess, "Popen", refuse), \
                mock.patch.object(os, "system", refuse), \
                mock.patch.object(os, "execv", refuse), \
                mock.patch.object(os, "execvp", refuse), \
                mock.patch("sys.stdout"):
            code = broker.main(["capabilities-plan", "github-cli", "--json",
                                "--project-root", str(self.project)])
        self.assertEqual(0, code)
        self.assertEqual(before, sorted(str(p) for p in self.base.rglob("*")),
                         "plan must not write telemetry, memory or any file")
        allowed = {broker.REGISTRY_PATH.resolve(), broker.HARNESS_REGISTRY_PATH.resolve()}
        self.assertTrue(reads)
        self.assertEqual(set(), set(reads) - allowed)

    def test_route_still_writes_opt_in_telemetry(self):
        # Proves the side-effect check above discriminates: the same opt-in
        # environment does write for route, the existing behaviour.
        with mock.patch.dict(os.environ, {"ASHA_BROKER_TELEMETRY": "1"}), mock.patch("sys.stdout"):
            self.assertEqual(0, broker.main(["process-route", "debug a failure", "--json",
                                             "--project-root", str(self.project)]))
        self.assertTrue((self.asha_home / "state" / "broker-events.jsonl").is_file())


class CompatibilityTests(PlanFixture):
    def normalized(self, value):
        value = copy.deepcopy(value)
        if "registry" in value and isinstance(value["registry"], dict):
            value["registry"] = {k: v for k, v in value["registry"].items()
                                 if k not in ("path", "harness_registry_path")}
        for row in value.get("selected", []) + value.get("unavailable", []):
            row.pop("registry_source", None)
        return value

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def test_route_and_match_match_the_562869a0_baseline(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
        registry = broker.load_registry(self.project)
        with mock.patch.object(broker, "capability_plan", side_effect=AssertionError("resolver invoked")):
            for key, expected in golden.items():
                harness, task = key.split("::", 1)
                with self.subTest(case=key):
                    route = broker.process_route(task, registry, harness)
                    match = self.normalized(broker.capability_match(task, registry, harness))
                    self.assertEqual(expected["route_sha256"], self.digest(route))
                    self.assertEqual(expected["route_recommended"], route["recommended"])
                    selected = [row["id"] for row in match["selected"]]
                    if "github" in task:
                        # The one intended, additive change: the new generic
                        # GitHub CLI foundation (#125) matches tasks naming GitHub.
                        self.assertEqual(expected["match_selected"] + ["github-cli"], selected)
                        self.assertEqual(expected["match_unavailable"], [r["id"] for r in match["unavailable"]])
                        continue
                    self.assertEqual(expected["match_selected"], selected)
                    self.assertEqual(expected["match_unavailable"], [r["id"] for r in match["unavailable"]])
                    self.assertEqual(expected["match_sha256"], self.digest(match))


class ShippedRegistryTests(PlanFixture):
    def test_every_shipped_capability_plans_on_every_harness(self):
        registry = broker.load_registry(self.project)
        for cap_id in registry.entries:
            for harness in HARNESSES:
                with self.subTest(capability=cap_id, harness=harness):
                    result = broker.capability_plan(cap_id, registry, harness)
                    self.assertEqual(cap_id, result["resolution_order"][-1])

    def test_schema_documents_the_validator_vocabulary(self):
        schema = json.loads((ROOT / "plugins/session/broker/capabilities.schema.json").read_text())
        edge = schema["$defs"]["dependency"]
        self.assertEqual(sorted(broker.DEPENDENCY_RELATIONS), sorted(edge["properties"]["relation"]["enum"]))
        documented = sorted(variant["properties"]["type"]["const"]
                            for variant in schema["$defs"]["condition"]["oneOf"])
        self.assertEqual(sorted(broker.CONDITION_TYPES), documented)
        self.assertIn("dependencies", schema["$defs"]["capability"]["properties"])

    def test_dispatcher_exposes_plan_and_lists_it_on_error(self):
        result = subprocess.run(
            [str(ROOT / "bin/asha"), "capabilities", "plan", "github-cli", "--json",
             "--project-root", str(self.project)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("asha.capability-plan.v1", json.loads(result.stdout)["contract"])
        wrong = subprocess.run([str(ROOT / "bin/asha"), "capabilities", "resolve"],
                               text=True, capture_output=True, check=False)
        self.assertEqual(2, wrong.returncode)
        self.assertIn("capabilities plan", wrong.stderr)

    def test_human_plan_output_names_conditions_and_approvals(self):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "plugins/session/tools/broker.py"),
             "capabilities-plan", "github-cli", "--harness", "copilot",
             "--project-root", str(self.project)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("github-cli-setup", result.stdout)
        self.assertIn("command-missing gh", result.stdout)
        self.assertIn("explicit-user-approval-to-install", result.stdout)
        self.assertIn("inspection only", result.stdout)


if __name__ == "__main__":
    unittest.main()
