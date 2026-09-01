from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "plugins/asha/skills/find-skills/tools/find_skills.py"
SPEC = importlib.util.spec_from_file_location("asha_find_skills", TOOL)
assert SPEC and SPEC.loader
find_skills = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(find_skills)

import find_skills_cli  # noqa: E402
import find_skills_common  # noqa: E402
import find_skills_inspect  # noqa: E402
import find_skills_store  # noqa: E402


class FakeClient:
    def __init__(self, json_values=None, byte_values=None):
        self.json_values = dict(json_values or {})
        self.byte_values = dict(byte_values or {})
        self.calls = []

    def get_json(self, url):
        self.calls.append(("json", url))
        if url not in self.json_values:
            raise AssertionError(f"unexpected JSON network request: {url}")
        return copy.deepcopy(self.json_values[url])

    def get_bytes(self, url, **_kwargs):
        self.calls.append(("bytes", url))
        if url not in self.byte_values:
            raise AssertionError(f"unexpected byte network request: {url}")
        return self.byte_values[url]


def inspection_fixture(
    *,
    source="acme/skills",
    skill_id="demo",
    frontmatter_extra="",
    support_files=None,
    skill_root="skills/demo",
    requested_path=None,
):
    revision = "a" * 40
    skill_md = (
        "---\n"
        "name: demo\n"
        "description: A small portable demonstration skill.\n"
        f"{frontmatter_extra}"
        "---\n"
        "\n# Demo\n\nFollow the reviewed procedure.\n"
    ).encode()
    support_files = support_files or {"reference.txt": (b"fixed reference\n", "100644")}
    prefix = "" if skill_root == "." else f"{skill_root}/"
    tree = [
        {
            "path": f"{prefix}SKILL.md",
            "type": "blob",
            "mode": "100644",
            "size": len(skill_md),
        }
    ]
    byte_values = {
        f"https://raw.githubusercontent.com/{source}/{revision}/{prefix}SKILL.md": skill_md
    }
    for relative, (data, mode) in support_files.items():
        tree.append(
            {
                "path": f"{prefix}{relative}",
                "type": "blob",
                "mode": mode,
                "size": len(data),
            }
        )
        byte_values[
            f"https://raw.githubusercontent.com/{source}/{revision}/{prefix}{relative}"
        ] = data
    license_bytes = b"Demo licence text\n"
    tree.append(
        {"path": "LICENSE", "type": "blob", "mode": "100644", "size": len(license_bytes)}
    )
    byte_values[
        f"https://raw.githubusercontent.com/{source}/{revision}/LICENSE"
    ] = license_bytes
    json_values = {
        f"https://api.github.com/repos/{source}/commits/HEAD": {"sha": revision},
        f"https://api.github.com/repos/{source}": {"license": {"spdx_id": "MIT"}},
        f"https://api.github.com/repos/{source}/git/trees/{revision}?recursive=1": {
            "truncated": False,
            "tree": tree,
        },
    }
    client = FakeClient(json_values, byte_values)
    inspection = find_skills.inspect_candidate(
        source, skill_id, skill_path=requested_path, client=client
    )
    return inspection, client, skill_md


class SearchTests(unittest.TestCase):
    def test_search_uses_public_json_route_and_parses_records(self):
        url = "https://www.skills.sh/api/search?q=postgres+review"
        client = FakeClient(
            {
                url: {
                    "skills": [
                        {
                            "id": "owner/repo/db-review",
                            "skillId": "db-review",
                            "name": "DB Review",
                            "installs": 42,
                            "source": "owner/repo",
                        }
                    ]
                }
            }
        )
        records = find_skills.search_skills("postgres review", client)
        self.assertEqual(records[0]["source"], "owner/repo")
        self.assertEqual(client.calls, [("json", url)])

    def test_search_rejects_short_query_before_network(self):
        client = FakeClient()
        with self.assertRaisesRegex(find_skills.ValidationError, "at least 2"):
            find_skills.search_skills("x", client)
        self.assertEqual(client.calls, [])

    def test_search_rejects_malformed_discovery_identity(self):
        with self.assertRaisesRegex(find_skills.ValidationError, "does not match"):
            find_skills.parse_search_payload(
                {
                    "skills": [
                        {
                            "id": "wrong/id/value",
                            "skillId": "demo",
                            "name": "Demo",
                            "installs": 1,
                            "source": "owner/repo",
                        }
                    ]
                }
            )

    def test_search_rejects_non_finite_install_counts(self):
        payload = {
            "skills": [
                {
                    "id": "owner/repo/demo",
                    "skillId": "demo",
                    "name": "Demo",
                    "installs": float("inf"),
                    "source": "owner/repo",
                }
            ]
        }
        with self.assertRaisesRegex(find_skills.ValidationError, "must be finite"):
            find_skills.parse_search_payload(payload)


class InspectionTests(unittest.TestCase):
    def test_inspect_pins_revision_hashes_all_files_and_reports_license(self):
        inspection, client, _skill_md = inspection_fixture()
        report = find_skills.inspection_report(inspection)
        self.assertTrue(report["importable"])
        self.assertEqual(report["revision"], "a" * 40)
        self.assertEqual(set(report["files"]), {"SKILL.md", "reference.txt"})
        self.assertIn("Follow the reviewed procedure.", report["skill_markdown"])
        self.assertRegex(report["tree_digest"], r"^[0-9a-f]{64}$")
        self.assertIsNone(report["license"]["spdx_id"])
        self.assertRegex(report["license"]["repository_file"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(any(key.startswith("_") for key in report))
        self.assertTrue(all("a" * 40 in url for kind, url in client.calls if kind == "bytes"))
        client.json_values["https://api.github.com/repos/acme/skills"] = {
            "license": {"spdx_id": "Apache-2.0"}
        }
        repeated = find_skills.inspect_candidate("acme/skills", "demo", client=client)
        self.assertEqual(inspection["license"], repeated["license"])
        self.assertNotIn(("json", "https://api.github.com/repos/acme/skills"), client.calls)

    def test_inspect_reports_dependencies_permissions_and_unsafe_shapes(self):
        inspection, _client, _skill_md = inspection_fixture(
            frontmatter_extra=(
                "allowed-tools: Bash Read\n"
                "metadata:\n"
                "  dependencies: python3\n"
                "  permissions: credential-store\n"
            ),
            support_files={
                "scripts/run.py": (
                    b'import os\n# https://example.invalid/api\nos.system("pip install bad")\nprint(os.environ["TOKEN"])\n',
                    "100755",
                )
            },
        )
        report = find_skills.inspection_report(inspection)
        self.assertFalse(report["importable"])
        self.assertIn("allowed-tools", report["unsupported_keys"])
        self.assertIn("python3", report["dependencies"]["declared"])
        self.assertIn("Bash", report["tools"])
        categories = {item["category"] for item in report["safety_findings"]}
        self.assertTrue(
            {"network_calls", "shell_out", "package_installation", "credential_access", "executable_support_file"}
            <= categories
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(find_skills.ValidationError, "unsupported-key:allowed-tools"):
                find_skills.build_import_proposal(
                    inspection, Path(tmp) / "home", Path(tmp) / "repo"
                )

    def test_symlink_and_path_escape_are_visible_and_block_import(self):
        inspection, _client, _skill_md = inspection_fixture(
            support_files={"outside": (b"../../secrets\n", "120000")}
        )
        self.assertIn("unsupported-shape:symlink:outside", inspection["import_blockers"])
        self.assertTrue(
            any(item["category"] == "path_escape" for item in inspection["safety_findings"])
        )

    def test_unknown_harness_semantics_fail_loudly_by_key(self):
        inspection, _client, _skill_md = inspection_fixture(
            frontmatter_extra="hooks: ./hooks.json\n"
        )
        self.assertIn("hooks", inspection["unsupported_keys"])
        self.assertIn("unsupported-key:hooks", inspection["import_blockers"])

    def test_malformed_yaml_scalars_are_rejected(self):
        for malformed in (
            '"unterminated', "'unterminated", "[unterminated", "plain: mapping",
        ):
            skill_md = (
                "---\n"
                "name: demo\n"
                f"description: {malformed}\n"
                "---\n"
                "# Demo\n"
            ).encode()
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(find_skills.ValidationError, "YAML"):
                    find_skills.parse_frontmatter(skill_md)

    def test_frontmatter_uses_standard_yaml_quoting_comments_and_character_rules(self):
        invalid_values = {
            "invalid escape": '"bad\\qescape"',
            "NUL": '"bad\x00value"',
            "ESC": '"bad\x1bvalue"',
            "VT": '"bad\x0bvalue"',
            "FF": '"bad\x0cvalue"',
            "FS": '"bad\x1cvalue"',
            "GS": '"bad\x1dvalue"',
            "RS": '"bad\x1evalue"',
        }
        for label, value in invalid_values.items():
            skill_md = (
                "---\nname: demo\n"
                f"description: {value}\n"
                "---\n# Demo\n"
            ).encode()
            with self.subTest(control=label, value=repr(value)):
                with self.assertRaisesRegex(find_skills.ValidationError, "valid YAML"):
                    find_skills.parse_frontmatter(skill_md)

        parsed, _body, errors = find_skills.parse_frontmatter(
            b"---\nname: demo\ndescription: 'it''s portable' # reviewed\n"
            b"license: MIT # SPDX identifier\n---\n# Demo\n"
        )
        self.assertEqual(parsed["description"], "it's portable")
        self.assertEqual(parsed["license"], "MIT")
        self.assertEqual(errors, [])

    def test_metadata_keys_are_blockers_and_json_sorting_remains_safe(self):
        inspection, _client, _skill_md = inspection_fixture(
            frontmatter_extra=(
                "metadata:\n"
                "  tools: python3\n"
                "  2020: historical\n"
            )
        )
        report = find_skills.inspection_report(inspection)
        self.assertIn(
            "frontmatter:metadata key 2020 must be a string",
            report["import_blockers"],
        )
        rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
        self.assertIn("<non-string int: 2020>", rendered)

    def test_yaml_native_date_scalar_cannot_crash_json_evidence(self):
        inspection, _client, _skill_md = inspection_fixture(
            frontmatter_extra="license: 2020-01-01\n"
        )
        report = find_skills.inspection_report(inspection)
        self.assertIn("frontmatter:license must be a string", report["import_blockers"])
        self.assertEqual(report["frontmatter"]["license"], "2020-01-01")
        self.assertEqual(report["license"]["declared"], "2020-01-01")
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False)

    def test_recursive_yaml_alias_cannot_crash_json_evidence(self):
        inspection, _client, _skill_md = inspection_fixture(
            frontmatter_extra="metadata: &loop {nested: *loop}\n"
        )
        report = find_skills.inspection_report(inspection)
        self.assertIn("metadata values must be strings", report["frontmatter_errors"])
        rendered = json.dumps(report, sort_keys=True, allow_nan=False)
        self.assertIn("recursive", rendered)

    def test_deep_and_exponentially_aliased_yaml_is_bounded(self):
        deeply_nested = b"---\nname: demo\ndescription: Portable.\nmetadata: " \
            + b"[" * 500 + b"leaf" + b"]" * 500 + b"\n---\n# Demo\n"
        with self.assertRaisesRegex(find_skills.ValidationError, "YAML|depth"):
            find_skills.parse_frontmatter(deeply_nested)
        aliases = ["metadata:", "  level0: &level0 [leaf, leaf]"]
        for depth in range(1, 15):
            aliases.append(
                f"  level{depth}: &level{depth} [*level{depth - 1}, *level{depth - 1}]"
            )
        inspection, _, _ = inspection_fixture(frontmatter_extra="\n".join(aliases) + "\n")
        rendered = json.dumps(find_skills.inspection_report(inspection), sort_keys=True)
        self.assertLess(len(rendered), 20_000)
        self.assertIn("repeated reference", rendered)

    def test_frontmatter_fails_loudly_when_pyyaml_is_unavailable(self):
        skill_md = b"---\nname: demo\ndescription: Portable.\n---\n# Demo\n"
        with patch.object(find_skills_common, "yaml", None):
            with self.assertRaisesRegex(find_skills.FindSkillsError, "PyYAML"):
                find_skills.parse_frontmatter(skill_md)

    def test_repository_root_skill_is_inspectable_and_importable(self):
        for requested_path in (None, "."):
            with self.subTest(requested_path=requested_path):
                inspection, _client, skill_md = inspection_fixture(
                    skill_root=".", requested_path=requested_path
                )
                report = find_skills.inspection_report(inspection)
                self.assertTrue(report["importable"])
                self.assertEqual(report["upstream_path"], ".")
                self.assertIn("SKILL.md", report["files"])
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = root / "repo"
                    (repo / "plugins").mkdir(parents=True)
                    proposal = find_skills.build_import_proposal(
                        inspection, root / "asha-home", repo
                    )
                    find_skills._write_import(
                        proposal, approve=True, replace=False
                    )
                    self.assertEqual(
                        (root / "asha-home/skills/demo/SKILL.md").read_bytes(),
                        skill_md,
                    )

    def test_optional_frontmatter_types_and_limits_follow_agent_skills(self):
        cases = {
            "license: [MIT]\n": "license must be a string",
            "compatibility: [python]\n": "compatibility must be a string",
            "compatibility:\n": "compatibility must be non-empty when provided",
            f"compatibility: {'x' * 501}\n": "compatibility exceeds 500 characters",
            "metadata:\n  tags: [one, two]\n": "metadata values must be strings",
            "metadata:\n  version: 1.0\n": "metadata values must be strings",
            "metadata:\n  - invalid\n": "metadata must be a mapping",
        }
        for extra, expected in cases.items():
            skill_md = (
                "---\nname: demo\ndescription: Valid description.\n"
                f"{extra}---\n# Demo\n"
            ).encode()
            with self.subTest(extra=extra):
                _parsed, _body, errors = find_skills.parse_frontmatter(skill_md)
                self.assertIn(expected, errors)

    def test_repository_paths_reject_unicode_format_controls(self):
        with self.assertRaisesRegex(find_skills.ValidationError, "unsafe repository path"):
            find_skills_common.validate_relative_path(
                "scripts/safe\u202eforged.py", label="repository path"
            )

    def test_safety_assessment_detects_direct_socket_and_popen_calls(self):
        inspection, _client, _skill_md = inspection_fixture(
            support_files={
                "helper.py": (
                    b'import os, socket\nos.popen("id")\nsocket.create_connection(("host", 443))\n',
                    "100644",
                )
            }
        )
        categories = {item["category"] for item in inspection["safety_findings"]}
        self.assertIn("shell_out", categories)
        self.assertIn("network_calls", categories)

    def test_safety_assessment_detects_direct_and_renamed_import_calls(self):
        cases = {
            "direct.py": (
                b"from subprocess import run as execute\n"
                b"from socket import create_connection as connect\n"
                b"execute(['id'])\nconnect(('example.invalid', 443))\n"
            ),
            "renamed.py": (
                b"import subprocess as process\nimport socket as network\n"
                b"process.run(['id'])\n"
                b"network.create_connection(('example.invalid', 443))\n"
            ),
        }
        for path, data in cases.items():
            with self.subTest(path=path):
                inspection, _client, _skill_md = inspection_fixture(
                    support_files={path: (data, "100644")}
                )
                categories = {
                    item["category"] for item in inspection["safety_findings"]
                }
                self.assertIn("shell_out", categories)
                self.assertIn("network_calls", categories)

    def test_extensionless_python_shebang_gets_ast_alias_analysis(self):
        inspection, _client, _skill_md = inspection_fixture(
            support_files={
                "runner": (
                    b"#!/usr/bin/env python3\n"
                    b"import subprocess as process\n"
                    b"import socket as network\n"
                    b"process.run(['id'])\n"
                    b"network.create_connection(('example.invalid', 443))\n",
                    "100644",
                )
            }
        )
        findings = [
            item for item in inspection["safety_findings"] if item["path"] == "runner"
        ]
        self.assertEqual(
            {"shell_out", "network_calls"},
            {item["category"] for item in findings},
        )

    def test_non_mapping_github_tree_entries_fail_on_every_consumer(self):
        tree = ["not-an-object"]
        consumers = (
            lambda: find_skills_inspect._choose_skill_root(tree, "demo", None),
            lambda: find_skills_inspect._selected_entries(tree, ".", "SKILL.md"),
            lambda: find_skills_inspect._license_report(
                tree, {}, "acme/skills", "a" * 40, FakeClient()
            ),
        )
        for consumer in consumers:
            with self.subTest(consumer=consumer.__code__.co_firstlineno):
                with self.assertRaisesRegex(find_skills.ValidationError, r"tree\[0\]"):
                    consumer()

class HttpClientTests(unittest.TestCase):
    def test_json_responses_have_bounded_structure_and_integer_conversion(self):
        client = find_skills.HttpClient()
        cases = (
            (b"[" * 10_000 + b"]" * 10_000, "nesting depth"),
            (b"1" * 5_000, "not valid"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                with patch.object(client, "get_bytes", return_value=payload):
                    with self.assertRaisesRegex(find_skills.ValidationError, message):
                        client.get_json("https://example.invalid/data")


class CliOutputSafetyTests(unittest.TestCase):
    def test_human_evidence_escapes_terminal_control_characters(self):
        report = {
            "candidate": "owner/repo/demo",
            "revision": "a" * 40,
            "upstream_path": "skills/demo",
            "name": "demo",
            "files": {},
            "tree_digest": "b" * 64,
            "license": {"declared": "MIT\x1b[2J"},
            "dependencies": {},
            "tools": ["tool\rforged"],
            "permissions": [],
            "safety_findings": [
                {
                    "category": "shell_out",
                    "path": "SKILL.md",
                    "evidence": "erase\x1b[2J\rforged",
                }
            ],
            "import_blockers": [],
            "skill_markdown": "---\ndescription: safe\x1b[2J\rforged\n---\n",
        }
        records = [
            {
                "id": "owner/repo/demo",
                "name": "erase\x1b[2J\rforged\u202ename",
                "installs": 1,
            }
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            find_skills_cli._print_search(records)
            find_skills_cli._print_inspection(report)
        rendered = output.getvalue()

        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn(r"\x1b", rendered)
        self.assertIn(r"\r", rendered)
        self.assertIn(r"\u202e", rendered)

    def test_proposal_and_status_paths_escape_terminal_format_controls(self):
        proposal = {
            "action": "create",
            "destination": "/tmp/safe\u202eforged",
            "local_state": {
                "state": "drifted",
                "issues": [{"kind": "hash-drift", "path": "file\u202eforged"}],
            },
            "writes": [{
                "path": "/tmp/write\u202eforged", "sha256": "a" * 64,
                "bytes": 1, "executable": False,
            }],
        }
        status = {
            "state": "drifted", "store": "/tmp/store\u202eforged",
            "skills": [{
                "name": "demo", "state": "drifted",
                "issues": [{"kind": "extra-file", "path": "extra\u202eforged"}],
            }],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            find_skills_cli._print_proposal(proposal)
            with patch.object(find_skills_cli, "status_store", return_value=status):
                find_skills_cli._run_status(
                    SimpleNamespace(asha_home=Path("/tmp"), name=None, json=False)
                )
        rendered = output.getvalue()
        self.assertNotIn("\u202e", rendered)
        self.assertEqual(rendered.count(r"\u202e"), 5)


class ImportAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.asha_home = root / "asha-home"
        self.repo = root / "repo"
        (self.repo / "plugins").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_import_rejects_names_that_cannot_mount_before_writing(self):
        inspection, _client, _skill_md = inspection_fixture()
        cases = (
            (55, None),
            (56, "Agent Skills 64-character limit"),
            (64, "Agent Skills 64-character limit"),
            (65, "valid import name"),
        )
        for length, expected_error in cases:
            candidate = copy.deepcopy(inspection)
            candidate["name"] = "a" * length
            with self.subTest(length=length):
                if expected_error is None:
                    proposal = find_skills.build_import_proposal(
                        candidate, self.asha_home, self.repo
                    )
                    self.assertEqual(proposal["name"], candidate["name"])
                else:
                    with self.assertRaisesRegex(
                        find_skills.ValidationError, expected_error
                    ):
                        find_skills.build_import_proposal(
                            candidate, self.asha_home, self.repo
                        )
                self.assertFalse(self.asha_home.exists())

    def test_dry_run_is_exact_and_approval_gate_writes_nothing(self):
        inspection, _client, skill_md = inspection_fixture()
        proposal = find_skills.build_import_proposal(
            inspection, self.asha_home, self.repo
        )
        report = find_skills.proposal_report(proposal)
        self.assertEqual(report["action"], "create")
        self.assertEqual(len(report["writes"]), 3)  # two upstream files plus lock
        self.assertFalse(self.asha_home.exists())
        with self.assertRaisesRegex(find_skills.ApprovalError, "--approve"):
            find_skills._write_import(proposal, approve=False, replace=False)
        self.assertFalse(self.asha_home.exists())

        result = find_skills._write_import(proposal, approve=True, replace=False)
        self.assertTrue(result["written"])
        imported = self.asha_home / "skills/demo/SKILL.md"
        self.assertEqual(imported.read_bytes(), skill_md)
        self.assertNotIn(b"acme/skills", imported.read_bytes())
        lock = json.loads((self.asha_home / "skills/imported.lock.json").read_text())
        entry = lock["skills"]["demo"]
        self.assertEqual(entry["source"], "acme/skills")
        self.assertEqual(entry["revision"], "a" * 40)
        self.assertEqual(entry["state"], "clean")
        self.assertEqual(find_skills.status_store(self.asha_home)["state"], "clean")

    def test_repeated_pinned_proposals_have_identical_lockfile_bytes(self):
        inspection, _client, _skill_md = inspection_fixture()
        with patch.object(
            find_skills_store, "utc_now", return_value="2026-01-01T00:00:00Z"
        ):
            first = find_skills.build_import_proposal(
                inspection, self.asha_home, self.repo
            )
        with patch.object(
            find_skills_store, "utc_now", return_value="2026-01-01T00:00:01Z"
        ):
            second = find_skills.build_import_proposal(
                inspection, self.asha_home, self.repo
            )
        self.assertEqual(first["_lock_document"], second["_lock_document"])
        self.assertEqual(first["writes"], second["writes"])

    def test_status_recomputes_hashes_and_reimport_reports_drift_without_mutation(self):
        inspection, _client, _skill_md = inspection_fixture()
        proposal = find_skills.build_import_proposal(
            inspection, self.asha_home, self.repo
        )
        find_skills._write_import(proposal, approve=True, replace=False)
        changed = self.asha_home / "skills/demo/reference.txt"
        changed.write_text("local change\n")
        before = changed.read_bytes()

        status = find_skills.status_store(self.asha_home)
        self.assertEqual(status["state"], "drifted")
        self.assertEqual(status["skills"][0]["issues"][0]["kind"], "hash-drift")
        retry = find_skills.build_import_proposal(
            inspection, self.asha_home, self.repo
        )
        self.assertEqual(retry["action"], "replace-drifted")
        self.assertTrue(retry["requires_replace_approval"])
        with self.assertRaisesRegex(find_skills.ApprovalError, "--replace"):
            find_skills._write_import(retry, approve=True, replace=False)
        self.assertEqual(changed.read_bytes(), before)

    def test_replace_needs_separate_approval_and_preserves_backup_and_history(self):
        inspection, _client, _skill_md = inspection_fixture()
        first = find_skills.build_import_proposal(inspection, self.asha_home, self.repo)
        find_skills._write_import(first, approve=True, replace=False)
        changed = self.asha_home / "skills/demo/reference.txt"
        changed.write_bytes(b"Keeper local bytes\n")
        retry = find_skills.build_import_proposal(inspection, self.asha_home, self.repo)
        result = find_skills._write_import(retry, approve=True, replace=True)
        self.assertTrue(result["written"])
        self.assertEqual(Path(result["backup"]).joinpath("reference.txt").read_bytes(), b"Keeper local bytes\n")
        lock = json.loads((self.asha_home / "skills/imported.lock.json").read_text())
        self.assertEqual(len(lock["history"]["demo"]), 1)

    def test_replace_refuses_symlinked_backup_root_before_moving_destination(self):
        inspection, _client, _skill_md = inspection_fixture()
        first = find_skills.build_import_proposal(inspection, self.asha_home, self.repo)
        find_skills._write_import(first, approve=True, replace=False)
        destination = self.asha_home / "skills/demo"
        changed = destination / "reference.txt"
        changed.write_bytes(b"Keeper local bytes\n")
        retry = find_skills.build_import_proposal(inspection, self.asha_home, self.repo)
        lock_path = self.asha_home / "skills/imported.lock.json"
        lock_before = lock_path.read_bytes()
        outside = Path(self.temp.name) / "outside-backups"
        outside.mkdir()
        backup_root = self.asha_home / "skills/.find-skills-backups"
        backup_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(find_skills.ValidationError, "must not be a symlink"):
            find_skills._write_import(retry, approve=True, replace=True)

        self.assertTrue(destination.is_dir())
        self.assertEqual(changed.read_bytes(), b"Keeper local bytes\n")
        self.assertEqual(lock_path.read_bytes(), lock_before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_plugin_and_already_imported_name_collisions_are_refused(self):
        inspection, _client, _skill_md = inspection_fixture()
        bundled = self.repo / "plugins/example/skills/demo/SKILL.md"
        bundled.parent.mkdir(parents=True)
        bundled.write_text(
            "---\nname: demo\ndescription: Bundled owner.\n---\n# Demo\n"
        )
        with self.assertRaisesRegex(find_skills.CollisionError, "bundled Asha skill"):
            find_skills.build_import_proposal(inspection, self.asha_home, self.repo)

        bundled.unlink()
        first = find_skills.build_import_proposal(inspection, self.asha_home, self.repo)
        find_skills._write_import(first, approve=True, replace=False)
        other = copy.deepcopy(inspection)
        other["source"] = "other/repository"
        with self.assertRaisesRegex(find_skills.CollisionError, "already owned"):
            find_skills.build_import_proposal(other, self.asha_home, self.repo)

        for field, value in (
            ("skill_id", "different-skill"),
            ("upstream_path", "skills/different-skill"),
        ):
            same_repository = copy.deepcopy(inspection)
            same_repository[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(find_skills.CollisionError, "already owned"):
                    find_skills.build_import_proposal(
                        same_repository, self.asha_home, self.repo
                    )

    def test_untracked_directory_is_reported_not_silently_adopted(self):
        skill = self.asha_home / "skills/untracked/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: untracked\ndescription: Local.\n---\n")
        status = find_skills.status_store(self.asha_home)
        self.assertEqual(status["state"], "drifted")
        self.assertEqual(status["skills"][0]["state"], "untracked")

    def test_lock_entries_and_required_fields_fail_validation_cleanly(self):
        inspection, _client, _skill_md = inspection_fixture()
        proposal = find_skills.build_import_proposal(
            inspection, self.asha_home, self.repo
        )
        find_skills._write_import(proposal, approve=True, replace=False)
        lock_path = self.asha_home / "skills/imported.lock.json"
        pristine = json.loads(lock_path.read_text())

        no_skill_md = copy.deepcopy(pristine["skills"]["demo"])
        no_skill_md.update(files={}, tree_digest=find_skills_common.tree_digest({}))
        malformed_entries = (
            (42, "lock entry 'demo'"),
            ({key: value for key, value in pristine["skills"]["demo"].items() if key != "source"}, "lock entry 'demo'"),
            (no_skill_md, "SKILL.md"),
        )
        for entry, expected in malformed_entries:
            document = copy.deepcopy(pristine)
            document["skills"]["demo"] = entry
            lock_path.write_text(json.dumps(document))
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(find_skills.ValidationError, expected):
                    find_skills.status_store(self.asha_home)

        document = copy.deepcopy(pristine)
        document["skills"]["demo"] = 42
        lock_path.write_text(json.dumps(document))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = find_skills.main(
                ["status", "--asha-home", str(self.asha_home), "--json"]
            )
        self.assertEqual(rc, 2)
        self.assertIn("lock entry 'demo' must be an object", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_deep_lock_document_fails_with_validation_status_not_traceback(self):
        lock_path = self.asha_home / "skills/imported.lock.json"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("[" * 200000 + "]" * 200000)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = find_skills.main(
                ["status", "--asha-home", str(self.asha_home), "--json"]
            )
        rendered = stderr.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("ERROR: invalid imported skill lockfile", rendered)
        self.assertNotIn("Traceback", rendered)
        self.assertNotIn("RecursionError", rendered)

    def test_concurrent_proposal_lock_document_and_guard_share_one_snapshot(self):
        inspection, _client, _skill_md = inspection_fixture()
        alpha = copy.deepcopy(inspection)
        alpha.update(
            {"name": "alpha", "skill_id": "alpha", "upstream_path": "skills/alpha"}
        )
        beta = copy.deepcopy(inspection)
        beta.update(
            {"name": "beta", "skill_id": "beta", "upstream_path": "skills/beta"}
        )
        loaded = threading.Event()
        committed = threading.Event()
        original_load = find_skills_store.load_lock

        def paused_load(path):
            document = original_load(path)
            if threading.current_thread().name.startswith("stale-builder"):
                loaded.set()
                self.assertTrue(committed.wait(timeout=5))
            return document

        with patch.object(find_skills_store, "load_lock", paused_load):
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="stale-builder"
            ) as executor:
                stale_future = executor.submit(
                    find_skills.build_import_proposal,
                    alpha,
                    self.asha_home,
                    self.repo,
                )
                self.assertTrue(loaded.wait(timeout=5))
                current = find_skills.build_import_proposal(
                    beta, self.asha_home, self.repo
                )
                find_skills._write_import(current, approve=True, replace=False)
                committed.set()
                stale = stale_future.result(timeout=5)

        with self.assertRaisesRegex(find_skills.CollisionError, "changed"):
            find_skills._write_import(stale, approve=True, replace=False)
        lock = json.loads((self.asha_home / "skills/imported.lock.json").read_text())
        self.assertEqual(set(lock["skills"]), {"beta"})


class RepairStructureTests(unittest.TestCase):
    def test_imported_adapter_is_extracted_without_line_count_gaming(self):
        install = REPO_ROOT / "lib/install.sh"
        imported = REPO_ROOT / "lib/imported-skills.sh"
        self.assertTrue(imported.is_file())
        self.assertLessEqual(len(install.read_bytes().splitlines()), 800)
        self.assertLessEqual(len(imported.read_bytes().splitlines()), 800)
        install_text = install.read_text()
        imported_text = imported.read_text()
        self.assertIn('source "$MARKET_ROOT/lib/imported-skills.sh"', install_text)
        for function in (
            "asha_imported_skills_root()",
            "prepare_imported_skill_adapter()",
            "mklink_imported_skill()",
        ):
            self.assertNotIn(function, install_text)
            self.assertIn(function, imported_text)
        self.assertIn("# shellcheck disable=SC2034", imported_text)

    def test_docs_scope_stdlib_transport_and_name_pyyaml(self):
        documents = (
            REPO_ROOT / "plugins/asha/skills/find-skills/SKILL.md",
            REPO_ROOT / "docs/find-skills.md",
            REPO_ROOT / "plugins/asha/README.md",
        )
        for document in documents:
            text = document.read_text()
            with self.subTest(document=document.relative_to(REPO_ROOT)):
                self.assertIn("PyYAML", text)
                self.assertIn("standard library", text)
                self.assertIn("inspection", text.lower())
                self.assertIn("mount", text.lower())
                self.assertIn("unavailable", text.lower())
        self.assertNotIn(
            "tool uses Python 3's standard library",
            documents[0].read_text(),
        )


if __name__ == "__main__":
    unittest.main()
