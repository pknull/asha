#!/usr/bin/env python3
"""
Tests for workspace_manifest (workspace v1, delivery issue 1 — issue #31).

Pure lexical parse/validate of .asha/workspace.json per the ratified proposal
(docs/proposals/2026-08-06--workspace-memory.md): typed collected errors,
fail-closed (any error => no manifest), schema defaults, containment +
disjointness + the v1 operational_root pin, unknown keys preserved. No
filesystem access — git-worktree existence and symlink canonicalization
belong to detection/status (issues 2-3), not this layer.
"""

import json
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import workspace_manifest as wm  # type: ignore[reportMissingImports]  # noqa: E402


def _codes(errors):
    return sorted({e.code for e in errors})


def _fields(errors):
    return sorted({e.field for e in errors})


MINIMAL = {"version": 1, "workspace_name": "w"}


def _valid_full():
    # The ratified proposal's own schema example, verbatim in structure.
    return {
        "version": 1,
        "workspace_name": "example-workspace",
        "memory": {
            "operational_root": "Memory",
            "personal_root": "memory-local",
            "shared_root": "knowledge",
            "shared_git_root": ".",
            "promotion_mode": "pull-request",
        },
        "repositories": [
            {"path": "frontend", "role": "web", "docs": "knowledge/repos/frontend"},
            {"path": "service", "role": "api", "docs": "knowledge/repos/service"},
        ],
    }


class SuccessCases(unittest.TestCase):
    def test_minimal_manifest_applies_all_defaults(self):
        manifest, errors = wm.validate_manifest(MINIMAL)
        self.assertEqual(errors, [])
        self.assertIsNotNone(manifest)
        mem = manifest["memory"]
        self.assertEqual(mem["operational_root"], "Memory")
        self.assertEqual(mem["personal_root"], "memory-local")
        self.assertEqual(mem["shared_root"], "knowledge")
        self.assertEqual(mem["shared_git_root"], ".")
        self.assertEqual(mem["promotion_mode"], "pull-request")
        self.assertEqual(manifest["repositories"], [])

    def test_proposal_example_validates(self):
        manifest, errors = wm.validate_manifest(_valid_full())
        self.assertEqual(errors, [])
        self.assertEqual(len(manifest["repositories"]), 2)
        self.assertEqual(manifest["repositories"][0]["path"], "frontend")

    def test_parse_manifest_text_round_trip(self):
        manifest, errors = wm.parse_manifest_text(json.dumps(_valid_full()))
        self.assertEqual(errors, [])
        self.assertEqual(manifest["workspace_name"], "example-workspace")

    def test_paths_are_normalized(self):
        data = _valid_full()
        data["memory"]["operational_root"] = "./Memory/"
        data["repositories"][0]["path"] = "frontend/"
        data["repositories"][0]["docs"] = "./knowledge//repos/frontend"
        manifest, errors = wm.validate_manifest(data)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["memory"]["operational_root"], "Memory")
        self.assertEqual(manifest["repositories"][0]["path"], "frontend")
        self.assertEqual(
            manifest["repositories"][0]["docs"], "knowledge/repos/frontend"
        )

    def test_direct_commit_promotion_mode_allowed(self):
        data = _valid_full()
        data["memory"]["promotion_mode"] = "direct-commit"
        _, errors = wm.validate_manifest(data)
        self.assertEqual(errors, [])

    def test_unknown_keys_preserved_at_every_level(self):
        data = _valid_full()
        data["x_custom"] = {"nested": True}
        data["memory"]["x_note"] = "keep me"
        data["repositories"][0]["x_tag"] = "alpha"
        manifest, errors = wm.validate_manifest(data)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["x_custom"], {"nested": True})
        self.assertEqual(manifest["memory"]["x_note"], "keep me")
        self.assertEqual(manifest["repositories"][0]["x_tag"], "alpha")

    def test_input_dict_is_not_mutated(self):
        data = _valid_full()
        snapshot = json.dumps(data, sort_keys=True)
        wm.validate_manifest(data)
        self.assertEqual(json.dumps(data, sort_keys=True), snapshot)

    def test_path_with_spaces_is_legal(self):
        data = _valid_full()
        data["repositories"][0]["path"] = "my service"
        _, errors = wm.validate_manifest(data)
        self.assertEqual(errors, [])


# Table of error cases: (name, mutate(data) -> data or raw text, expected codes,
# optionally expected field substrings). Every case must fail CLOSED: no
# manifest object alongside errors.
def _mut(fn):
    data = _valid_full()
    fn(data)
    return data


class ErrorTable(unittest.TestCase):
    def _assert_fails(self, data_or_text, expected_codes, expected_fields=()):
        if isinstance(data_or_text, str):
            manifest, errors = wm.parse_manifest_text(data_or_text)
        else:
            manifest, errors = wm.validate_manifest(data_or_text)
        self.assertIsNone(manifest, "fail-closed: errors must mean no manifest")
        self.assertTrue(errors, "expected at least one error")
        for code in expected_codes:
            self.assertIn(code, _codes(errors), f"missing code {code}: {errors}")
        for field in expected_fields:
            self.assertTrue(
                any(field in e.field for e in errors),
                f"no error on field containing '{field}': {_fields(errors)}",
            )

    def test_invalid_json(self):
        self._assert_fails("{not json", ["invalid_json"])

    def test_root_not_object(self):
        self._assert_fails("[1, 2]", ["not_object"])

    def test_version_missing(self):
        self._assert_fails({"workspace_name": "w"}, ["missing_field"], ["version"])

    def test_version_wrong_type_string(self):
        self._assert_fails(
            {"version": "1", "workspace_name": "w"}, ["wrong_type"], ["version"]
        )

    def test_version_bool_rejected(self):
        # bool is an int subclass in Python; True must not read as version 1.
        self._assert_fails(
            {"version": True, "workspace_name": "w"}, ["wrong_type"], ["version"]
        )

    def test_version_unsupported(self):
        self._assert_fails(
            {"version": 2, "workspace_name": "w"},
            ["unsupported_version"],
            ["version"],
        )

    def test_workspace_name_missing(self):
        self._assert_fails({"version": 1}, ["missing_field"], ["workspace_name"])

    def test_workspace_name_empty(self):
        self._assert_fails(
            {"version": 1, "workspace_name": "  "}, ["empty_value"]
        )

    def test_workspace_name_wrong_type(self):
        self._assert_fails({"version": 1, "workspace_name": 42}, ["wrong_type"])

    def test_memory_wrong_type(self):
        self._assert_fails(
            {"version": 1, "workspace_name": "w", "memory": 5},
            ["wrong_type"],
            ["memory"],
        )

    def test_operational_root_reserved_pin(self):
        # The proposal's own example of a v1-reserved value.
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("operational_root", "ops/memory")),
            ["operational_root_reserved"],
            ["memory.operational_root"],
        )

    def test_personal_root_nested_in_operational(self):
        # Proposal disjointness example: would let `git add Memory/` stage
        # private files.
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", "Memory/private")),
            ["roots_not_disjoint"],
        )

    def test_shared_root_nested_in_personal(self):
        self._assert_fails(
            _mut(
                lambda d: d["memory"].__setitem__(
                    "shared_root", "memory-local/knowledge"
                )
            ),
            ["roots_not_disjoint"],
        )

    def test_operational_outside_shared_git_root(self):
        # Codex pass-1 scenario on PR #28: write root outside the commit repo.
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("shared_git_root", "shared")),
            ["containment_violation"],
        )

    def test_absolute_path_rejected(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", "/etc/x")),
            ["absolute_path"],
            ["memory.personal_root"],
        )

    def test_traversal_rejected(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("shared_root", "../outside")),
            ["path_traversal"],
        )

    def test_interior_traversal_rejected(self):
        # Normalization must not launder interior dot-dot segments.
        self._assert_fails(
            _mut(lambda d: d["repositories"][0].__setitem__("path", "a/../../b")),
            ["path_traversal"],
            ["repositories[0].path"],
        )

    def test_dot_rejected_for_memory_roots(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", ".")),
            ["invalid_path"],
        )

    def test_backslash_rejected(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("shared_root", "know\\ledge")),
            ["invalid_path"],
        )

    def test_windows_drive_path_rejected(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", "C:/mem")),
            ["absolute_path"],
        )

    def test_promotion_mode_invalid(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("promotion_mode", "auto")),
            ["invalid_promotion_mode"],
        )

    def test_repositories_not_list(self):
        self._assert_fails(
            _mut(lambda d: d.__setitem__("repositories", "frontend")),
            ["wrong_type"],
            ["repositories"],
        )

    def test_repository_entry_not_object(self):
        self._assert_fails(
            _mut(lambda d: d["repositories"].__setitem__(1, "service")),
            ["wrong_type"],
            ["repositories[1]"],
        )

    def test_repository_path_missing(self):
        self._assert_fails(
            _mut(lambda d: d["repositories"].__setitem__(0, {"role": "web"})),
            ["missing_field"],
            ["repositories[0].path"],
        )

    def test_repository_dot_rejected(self):
        # A child repository must be a proper subdirectory of the workspace.
        self._assert_fails(
            _mut(lambda d: d["repositories"][0].__setitem__("path", ".")),
            ["repo_path_not_child"],
        )

    def test_duplicate_repositories_after_normalization(self):
        self._assert_fails(
            _mut(lambda d: d["repositories"][1].__setitem__("path", "./frontend/")),
            ["duplicate_repository"],
        )

    def test_repository_docs_traversal(self):
        self._assert_fails(
            _mut(lambda d: d["repositories"][0].__setitem__("docs", "../x")),
            ["path_traversal"],
            ["repositories[0].docs"],
        )

    def test_repository_role_wrong_type(self):
        self._assert_fails(
            _mut(lambda d: d["repositories"][0].__setitem__("role", 3)),
            ["wrong_type"],
            ["repositories[0].role"],
        )

    def test_errors_are_collected_not_fail_fast(self):
        data = _mut(lambda d: d["memory"].__setitem__("personal_root", "/abs"))
        data["memory"]["promotion_mode"] = "auto"
        data["repositories"][0]["path"] = "../up"
        manifest, errors = wm.validate_manifest(data)
        self.assertIsNone(manifest)
        got = _codes(errors)
        for code in ("absolute_path", "invalid_promotion_mode", "path_traversal"):
            self.assertIn(code, got)

    def test_error_objects_carry_message(self):
        _, errors = wm.validate_manifest({"version": 2, "workspace_name": "w"})
        self.assertTrue(all(e.message for e in errors))


if __name__ == "__main__":
    unittest.main()
