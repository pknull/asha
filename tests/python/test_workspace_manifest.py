#!/usr/bin/env python3
"""
Tests for workspace_manifest (workspace v1, delivery issue 1 — issue #31).

Pure lexical parse/validate of .asha/workspace.json per the ratified proposal
(docs/proposals/2026-08-06--workspace-memory.md): typed collected errors,
fail-closed (any error => no manifest), schema defaults, containment +
disjointness + the v1 operational_root pin, unknown keys preserved. No
filesystem access — git-worktree existence and symlink canonicalization
belong to detection/status (issues 2-3), not this layer.

Pass-2 hardening (PR #32 codex review): the validator must be TOTAL — hostile
input (cycles, depth, huge ints, NUL/surrogate paths, non-UTF-8 CLI files)
yields typed fail-closed errors, never an exception — and the error oracle
here asserts EXACT code sets, not inclusion, so extra or missing errors fail.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import workspace_manifest as wm  # type: ignore[reportMissingImports]  # noqa: E402


def _codes(errors):
    return sorted({e.code for e in errors})


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

    def test_dotdot_lookalike_segments_are_legal(self):
        # "..." and "..hidden" are ordinary names, not traversal.
        data = _valid_full()
        data["repositories"][0]["path"] = "...archive/..hidden"
        _, errors = wm.validate_manifest(data)
        self.assertEqual(errors, [])


def _mut(fn):
    data = _valid_full()
    fn(data)
    return data


class ErrorTable(unittest.TestCase):
    """Exact-set oracle: expected_codes is the COMPLETE set of distinct codes.

    Inclusion-only oracles let extra errors and suppressed errors pass — the
    pass-2 review proved it by hiding a collected-errors defect behind one.
    """

    def _assert_fails(self, data_or_text, expected_codes, expected_fields=()):
        if isinstance(data_or_text, str):
            manifest, errors = wm.parse_manifest_text(data_or_text)
        else:
            manifest, errors = wm.validate_manifest(data_or_text)
        self.assertIsNone(manifest, "fail-closed: errors must mean no manifest")
        self.assertTrue(errors, "expected at least one error")
        self.assertEqual(
            _codes(errors), sorted(set(expected_codes)),
            f"exact code-set mismatch: {errors}",
        )
        for field in expected_fields:
            self.assertTrue(
                any(field in e.field for e in errors),
                f"no error on field containing '{field}': {errors}",
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

    def test_both_required_fields_absent(self):
        _, errors = wm.validate_manifest({})
        self.assertEqual(_codes(errors), ["missing_field"])
        self.assertEqual(
            sorted(e.field for e in errors), ["version", "workspace_name"]
        )

    def test_workspace_name_missing(self):
        self._assert_fails({"version": 1}, ["missing_field"], ["workspace_name"])

    def test_workspace_name_blank(self):
        self._assert_fails({"version": 1, "workspace_name": "  "}, ["empty_value"])

    def test_workspace_name_wrong_type(self):
        self._assert_fails({"version": 1, "workspace_name": 42}, ["wrong_type"])

    def test_memory_wrong_type(self):
        self._assert_fails(
            {"version": 1, "workspace_name": "w", "memory": 5},
            ["wrong_type"],
            ["memory"],
        )

    def test_operational_root_reserved_pin(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("operational_root", "ops/memory")),
            ["operational_root_reserved"],
            ["memory.operational_root"],
        )

    def test_personal_root_nested_in_operational(self):
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
        data = _mut(lambda d: d["memory"].__setitem__("shared_git_root", "shared"))
        manifest, errors = wm.validate_manifest(data)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), ["containment_violation"])
        # The relation involves two fields; blaming the pinned-correct
        # operational_root would misdirect repair (pass-2 nit).
        self.assertEqual(errors[0].field, "memory")

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
        self._assert_fails(
            _mut(lambda d: d["repositories"][0].__setitem__("path", "a/../../b")),
            ["path_traversal"],
            ["repositories[0].path"],
        )

    def test_any_dotdot_segment_rejected_even_if_contained(self):
        # "a/../b" stays lexically inside the root; the validator still
        # refuses — normalization must never launder dot-dot (documented
        # strict-side choice).
        self._assert_fails(
            _mut(lambda d: d["repositories"][0].__setitem__("path", "a/../b")),
            ["path_traversal"],
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

    def test_windows_drive_slash_rejected(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", "C:/mem")),
            ["absolute_path"],
        )

    def test_windows_drive_backslash_rejected(self):
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", "C:\\mem")),
            ["absolute_path"],
        )

    def test_windows_drive_relative_rejected(self):
        # C:relative resolves against the drive's CWD on Windows — never
        # workspace-relative (pass-2 finding: it previously passed).
        self._assert_fails(
            _mut(lambda d: d["memory"].__setitem__("personal_root", "C:mem")),
            ["absolute_path"],
        )

    def test_unc_path_rejected(self):
        self._assert_fails(
            _mut(
                lambda d: d["memory"].__setitem__(
                    "personal_root", "\\\\server\\share"
                )
            ),
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

    def test_repository_missing_path_still_collects_other_errors(self):
        # Pass-2 BLOCKING: an early `continue` used to suppress these.
        self._assert_fails(
            _mut(
                lambda d: d["repositories"].__setitem__(
                    0, {"role": 3, "docs": "../x"}
                )
            ),
            ["missing_field", "wrong_type", "path_traversal"],
            ["repositories[0].path", "repositories[0].role", "repositories[0].docs"],
        )

    def test_repository_dot_rejected(self):
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
        self.assertEqual(
            _codes(errors),
            ["absolute_path", "invalid_promotion_mode", "path_traversal"],
        )

    def test_error_objects_carry_message(self):
        _, errors = wm.validate_manifest({"version": 2, "workspace_name": "w"})
        self.assertTrue(all(e.message for e in errors))


class HostilePathCases(unittest.TestCase):
    """Pass-2 BLOCKING: paths the runtime cannot even stat must fail HERE,
    inside the typed-error boundary, not later in canonicalization or git."""

    def _root_fails(self, value, code):
        data = _mut(lambda d: d["memory"].__setitem__("personal_root", value))
        manifest, errors = wm.validate_manifest(data)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), [code], f"for path {value!r}")

    def test_embedded_nul_rejected(self):
        self._root_fails("a\x00b", "invalid_path")

    def test_newline_rejected(self):
        self._root_fails("a\nb", "invalid_path")

    def test_tab_rejected(self):
        self._root_fails("a\tb", "invalid_path")

    def test_lone_surrogate_rejected(self):
        self._root_fails("\ud800", "invalid_path")

    def test_del_control_char_rejected(self):
        self._root_fails("a\x7fb", "invalid_path")


class TotalityCases(unittest.TestCase):
    """Pass-2 BLOCKING: the validator is total — hostile structure yields a
    typed fail-closed error, never an exception, never a bogus success."""

    def test_deeply_nested_unknown_value_fails_typed(self):
        deep = current = {}
        for _ in range(4000):
            nxt = {}
            current["k"] = nxt
            current = nxt
        data = {"version": 1, "workspace_name": "w", "x_deep": deep}
        manifest, errors = wm.validate_manifest(data)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), ["unprocessable"])

    def test_deeply_nested_json_text_fails_typed(self):
        # Whether the C json parser or the representability probe trips first
        # is a platform recursion-ceiling detail; the pinned contract is a
        # typed fail-closed verdict with no exception either way.
        text = (
            '{"version": 1, "workspace_name": "w", "x_deep": '
            + "[" * 4000 + "]" * 4000 + "}"
        )
        manifest, errors = wm.parse_manifest_text(text)
        self.assertIsNone(manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn(errors[0].code, ("invalid_json", "unprocessable"))

    def test_huge_integer_json_fails_typed(self):
        text = (
            '{"version": 1, "workspace_name": "w", "x_big": '
            + "9" * 5000 + "}"
        )
        manifest, errors = wm.parse_manifest_text(text)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), ["invalid_json"])

    def test_circular_input_fails_typed(self):
        data = {"version": 1, "workspace_name": "w"}
        data["x_cycle"] = data
        manifest, errors = wm.validate_manifest(data)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), ["unprocessable"])

    def test_non_json_value_fails_typed(self):
        data = {"version": 1, "workspace_name": "w", "x_set": {1, 2}}
        manifest, errors = wm.validate_manifest(data)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), ["unprocessable"])

    def test_nan_and_infinity_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            text = (
                '{"version": 1, "workspace_name": "w", "x_c": ' + constant + "}"
            )
            manifest, errors = wm.parse_manifest_text(text)
            self.assertIsNone(manifest, constant)
            self.assertEqual(_codes(errors), ["invalid_json"], constant)

    def test_none_input_fails_typed(self):
        manifest, errors = wm.validate_manifest(None)
        self.assertIsNone(manifest)
        self.assertEqual(_codes(errors), ["not_object"])


class CliCases(unittest.TestCase):
    """The shipped CLI surface: JSON verdict + exit code, never a traceback."""

    def _run(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wm.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _tmpfile(self, payload: bytes) -> str:
        fd, path = tempfile.mkstemp(prefix="asha_wm_", suffix=".json")
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        self.addCleanup(os.unlink, path)
        return path

    def test_valid_file_exits_zero_with_verdict(self):
        path = self._tmpfile(json.dumps(_valid_full()).encode("utf-8"))
        rc, out, _ = self._run(["workspace_manifest.py", path])
        self.assertEqual(rc, 0)
        verdict = json.loads(out)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["errors"], [])

    def test_invalid_manifest_exits_one_with_typed_errors(self):
        path = self._tmpfile(b'{"version": 2, "workspace_name": "w"}')
        rc, out, _ = self._run(["workspace_manifest.py", path])
        self.assertEqual(rc, 1)
        verdict = json.loads(out)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["errors"][0]["code"], "unsupported_version")

    def test_non_utf8_file_yields_typed_error_not_traceback(self):
        path = self._tmpfile(b"\xff\xfe\x00garbage")
        rc, out, _ = self._run(["workspace_manifest.py", path])
        self.assertEqual(rc, 1)
        verdict = json.loads(out)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["errors"][0]["code"], "unreadable")

    def test_missing_file_exits_one(self):
        rc, out, _ = self._run(["workspace_manifest.py", "/nonexistent/x.json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_bad_argv_exits_two(self):
        rc, _, err = self._run(["workspace_manifest.py"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)


if __name__ == "__main__":
    unittest.main()
