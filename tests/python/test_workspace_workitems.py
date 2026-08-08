#!/usr/bin/env python3
"""Tests for optional offline workspace work-item registry (issue #26)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import workspace_workitems as wi  # type: ignore[reportMissingImports]  # noqa: E402


class WorkItemFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_workitems_")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.old_home = os.environ.get("HOME")
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.ws = self.home / "Code" / "thallus"
        self.ws.mkdir(parents=True)
        (self.ws / ".asha").mkdir()
        self.manifest()
        for repo in ("frontend", "service"):
            (self.ws / repo).mkdir()

    def _restore_home(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def manifest(self, *, index=False):
        payload = {
            "version": 1,
            "workspace_name": "thallus",
            "memory": {
                "personal_root": "memory-local",
                "shared_root": "knowledge",
            },
            "repositories": [
                {"path": "frontend", "role": "web"},
                {"path": "service", "role": "api"},
            ],
            "work_items": {
                "private_root": "memory-local/work-items",
                "index_enabled": index,
                "stale_days": 30,
            },
        }
        (self.ws / ".asha" / "workspace.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    @property
    def registry(self):
        return self.ws / "memory-local" / "work-items"

    def create(self, item_id="feature-auth", **kwargs):
        values = {
            "start": self.ws,
            "item_id": item_id,
            "title": "Authorization boundary",
            "repositories": ["frontend", "service"],
            "today": "2026-08-08",
        }
        values.update(kwargs)
        return wi.create_item(**values)

    def candidate(self, data, name="candidate.json"):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class RegistryCases(WorkItemFixture):
    def test_create_list_show_and_unknown_fields_survive_link(self):
        report = self.create(custom={
            "team_field": {"squad": "infra", "weight": 3},
            "labels": ["security", "boundary"],
        })
        self.assertTrue(report["ok"], report)
        self.create("dependency", title="Dependency", repositories=["service"])

        linked = wi.link_item(
            self.ws, "feature-auth", "dependency", relation="depends-on",
            today="2026-08-09",
        )
        self.assertTrue(linked["ok"], linked)
        shown = wi.show_item(self.ws, "feature-auth")
        self.assertEqual(shown["item"]["team_field"], {"squad": "infra", "weight": 3})
        self.assertEqual(shown["item"]["labels"], ["security", "boundary"])
        self.assertEqual(shown["item"]["id"], "feature-auth")
        self.assertEqual(shown["item"]["relationships"], [
            {"relation": "depends-on", "target": "dependency"}
        ])
        listed = wi.list_items(self.ws)
        self.assertEqual([item["id"] for item in listed["items"]],
                         ["dependency", "feature-auth"])
        self.assertNotIn("objective", listed["items"][0],
                         "list is lightweight; show is explicit detail")

    def test_invalid_id_or_undeclared_repository_has_no_writes(self):
        for item_id, repos, code in (
            ("../escape", ["service"], "invalid_id"),
            ("valid-id", ["not-declared"], "undeclared_repository"),
        ):
            with self.subTest(item_id=item_id):
                report = self.create(item_id, repositories=repos)
                self.assertFalse(report["ok"])
                self.assertEqual(report["errors"][0]["code"], code)
                self.assertFalse(self.registry.exists())

    def test_link_requires_existing_valid_target(self):
        self.create()
        report = wi.link_item(self.ws, "feature-auth", "missing")
        self.assertEqual(report["errors"][0]["code"], "target_not_found")
        self.assertEqual(wi.show_item(self.ws, "feature-auth")["item"]["relationships"], [])

    def test_link_rejects_relation_text_that_fails_privacy_scrub(self):
        self.create()
        self.create("dependency", title="Dependency", repositories=["service"])
        before = (self.registry / "feature-auth.md").read_bytes()
        report = wi.link_item(
            self.ws, "feature-auth", "dependency",
            relation="token=ghp_abcdefghijklmnopqrstuvwxyz123456",
        )
        self.assertEqual(report["errors"][0]["code"], "privacy_violation")
        self.assertEqual((self.registry / "feature-auth.md").read_bytes(), before)

    def test_write_failure_leaves_no_partial_item(self):
        with mock.patch.object(wi, "_replace_file", side_effect=OSError("fixture")):
            report = self.create()
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "write_failed")
        self.assertFalse((self.registry / "feature-auth.md").exists())

    def test_create_rejects_raw_transcript_secret_and_invalid_custom_key(self):
        for custom, code in (
            ({"raw_transcript": [{"role": "user", "content": "private"}]},
             "privacy_violation"),
            ({"notes": "api_token=ghp_abcdefghijklmnopqrstuvwxyz123456"},
             "privacy_violation"),
            ({"bad\nfield": "value"}, "custom_field_invalid"),
        ):
            with self.subTest(custom=custom):
                report = self.create(custom=custom)
                self.assertEqual(report["errors"][0]["code"], code)
                self.assertFalse((self.registry / "feature-auth.md").exists())


class AdapterPrivacyCases(WorkItemFixture):
    def malicious_candidate(self):
        marker = self.tmp / "MUST_NOT_EXECUTE"
        return marker, {
            "external_id": "EXT-42",
            "title": "Auth for alice@example.com under /home/alice/work",
            "status": "Open",
            "repositories": ["service"],
            "objective": "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
            "acceptance_criteria": ["Boundary documented"],
            "verification_commands": [f"touch {marker}"],
            "provider": "file-fixture",
            "freshness": "2026-08-08T10:00:00Z",
            "source_url": "https://user:pass@example.test/ticket/42",
            "private_comments": ["secret internal comment"],
            "raw_transcript": [{"role": "user", "content": "private"}],
            "authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
            "evil_unknown": "ignored rather than imported",
        }

    def test_preview_scrubs_and_never_returns_raw_or_unknown_fields(self):
        marker, data = self.malicious_candidate()
        path = self.candidate(data)
        report = wi.preview_file_candidate(self.ws, path)
        self.assertTrue(report["ok"], report)
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("alice@example.com", serialized)
        self.assertNotIn("/home/alice", serialized)
        self.assertNotIn("ghp_", serialized)
        self.assertNotIn("secret internal comment", serialized)
        self.assertNotIn('"role"', serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("user:pass@", serialized)
        self.assertNotIn("evil_unknown", report["candidate"])
        self.assertIn("evil_unknown", report["ignored_fields"])
        self.assertFalse(marker.exists())
        self.assertRegex(report["preview_token"], r"^[0-9a-f]{64}$")

    def test_preview_scrubs_truncated_private_key_from_header_to_end(self):
        _, data = self.malicious_candidate()
        data["objective"] = (
            "prefix\n-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw"
        )
        report = wi.preview_file_candidate(self.ws, self.candidate(data))
        self.assertTrue(report["ok"], report)
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("BEGIN PRIVATE KEY", serialized)
        self.assertNotIn("MIIEvQIBADAN", serialized)
        self.assertIn("private_key", report["redactions"])

    def test_import_requires_matching_preview_and_never_executes_content(self):
        marker, data = self.malicious_candidate()
        path = self.candidate(data)
        refused = wi.import_file_candidate(self.ws, path, item_id="external-42")
        self.assertEqual(refused["errors"][0]["code"], "preview_required")
        self.assertFalse(self.registry.exists())

        preview = wi.preview_file_candidate(self.ws, path)
        with mock.patch("subprocess.run") as executed:
            imported = wi.import_file_candidate(
                self.ws, path, item_id="external-42",
                preview_token=preview["preview_token"], today="2026-08-08",
            )
        self.assertTrue(imported["ok"], imported)
        executed.assert_not_called()
        self.assertFalse(marker.exists())
        item = wi.show_item(self.ws, "external-42")["item"]
        self.assertEqual(item["adapter_provenance"]["provider"], "file-fixture")
        self.assertNotIn("private_comments", item)
        self.assertNotIn("raw_transcript", item)

    def test_changed_candidate_invalidates_preview_without_partial_write(self):
        path = self.candidate({
            "external_id": "EXT-1", "title": "One", "status": "Open",
            "repositories": ["service"], "provider": "fixture",
            "freshness": "2026-08-08T00:00:00Z",
            "source_url": "https://example.test/1",
        })
        preview = wi.preview_file_candidate(self.ws, path)
        changed = json.loads(path.read_text())
        changed["title"] = "Changed after preview"
        path.write_text(json.dumps(changed), encoding="utf-8")
        report = wi.import_file_candidate(
            self.ws, path, item_id="external-1",
            preview_token=preview["preview_token"],
        )
        self.assertEqual(report["errors"][0]["code"], "preview_mismatch")
        self.assertFalse(self.registry.exists())

    def test_unavailable_or_malformed_adapter_is_offline_and_non_mutating(self):
        missing = wi.preview_file_candidate(self.ws, self.tmp / "missing.json")
        self.assertEqual(missing["errors"][0]["code"], "adapter_unavailable")
        bad_path = self.tmp / "bad.json"
        bad_path.write_text("not-json", encoding="utf-8")
        malformed = wi.preview_file_candidate(self.ws, bad_path)
        self.assertEqual(malformed["errors"][0]["code"], "adapter_invalid")
        self.assertFalse(self.registry.exists())


class LintIndexCases(WorkItemFixture):
    def setUp(self):
        super().setUp()
        self.manifest(index=True)

    def test_lint_index_and_staleness_contract(self):
        self.create(custom={"adapter_provenance": {
            "adapter": "file", "provider": "fixture",
            "freshness": "2026-01-01T00:00:00Z",
            "source_url": "https://example.test/old",
            "candidate_sha256": "a" * 64,
        }})
        missing = wi.lint_registry(self.ws, today="2026-08-08")
        codes = {item["code"] for item in missing["findings"]}
        self.assertIn("index_missing", codes)
        self.assertIn("external_reference_stale", codes)

        built = wi.build_index(self.ws)
        self.assertTrue(built["ok"], built)
        clean_index = wi.lint_registry(self.ws, today="2026-08-08")
        self.assertNotIn("index_missing", {x["code"] for x in clean_index["findings"]})
        self.assertNotIn("index_inconsistent", {x["code"] for x in clean_index["findings"]})

        self.create("second", title="Second", repositories=["frontend"])
        drifted = wi.lint_registry(self.ws, today="2026-08-08")
        self.assertIn("index_inconsistent", {x["code"] for x in drifted["findings"]})

    def test_lint_detects_bad_frontmatter_id_repo_link_and_unresolved_external(self):
        self.registry.mkdir(parents=True)
        (self.registry / "bad.md").write_text(
            "---\nid: other\ntitle: Bad\nstatus: Open\n"
            "repositories: [\"unknown\"]\nrelationships: "
            "[{\"relation\":\"related\",\"target\":\"missing\"}]\n"
            "links: [\"../escape.md\",\"cross-cutting/missing.md\"]\n"
            "adapter_provenance: {\"adapter\":\"file\",\"provider\":\"x\"}\n"
            "---\n",
            encoding="utf-8",
        )
        (self.registry / "broken.md").write_text("not frontmatter\n", encoding="utf-8")
        report = wi.lint_registry(self.ws, today="2026-08-08")
        codes = {item["code"] for item in report["findings"]}
        self.assertTrue({
            "malformed_frontmatter", "id_filename_mismatch",
            "undeclared_repository", "unresolved_relationship",
            "external_reference_unresolved", "invalid_link", "unresolved_link",
        }.issubset(codes), report)


class PromotionPlanCases(WorkItemFixture):
    def setUp(self):
        super().setUp()
        self.create(custom={
            "objective": "Establish a stable authorization boundary",
            "verification_commands": ["pytest tests/auth"],
            "canonical_documents": ["cross-cutting/auth.md"],
        })
        self.evidence = self.ws / "service" / "contract.py"
        self.evidence.write_text("AUTH_VERSION = 2\n", encoding="utf-8")

    def test_promote_plan_emits_source_evidence_target_without_canonical_write(self):
        before = list((self.ws / "knowledge").rglob("*")) if (self.ws / "knowledge").exists() else []
        report = wi.promote_plan(
            self.ws, "feature-auth", target="cross-cutting/auth.md",
            evidence=[self.evidence],
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["source"]["kind"], "workspace-work-item")
        self.assertEqual(report["source"]["id"], "feature-auth")
        self.assertEqual(report["target"], "cross-cutting/auth.md")
        self.assertEqual(report["evidence"][0]["path"], "service/contract.py")
        self.assertTrue(report["requires_explicit_promote_apply"])
        after = list((self.ws / "knowledge").rglob("*")) if (self.ws / "knowledge").exists() else []
        self.assertEqual(before, after)

    def test_relative_evidence_is_resolved_from_workspace_not_process_cwd(self):
        report = wi.promote_plan(
            self.ws, "feature-auth", target="cross-cutting/auth.md",
            evidence=["service/contract.py"],
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidence"][0]["path"], "service/contract.py")

    def test_worktree_seed_is_data_only_and_requires_git_confirmation(self):
        refused = wi.promote_plan(
            self.ws, "feature-auth", target="cross-cutting/auth.md",
            evidence=[self.evidence], include_worktree_seed=True,
        )
        self.assertEqual(refused["errors"][0]["code"], "git_confirmation_required")
        with mock.patch("subprocess.run") as git:
            report = wi.promote_plan(
                self.ws, "feature-auth", target="cross-cutting/auth.md",
                evidence=[self.evidence], include_worktree_seed=True,
                git_confirmed=True,
            )
        self.assertTrue(report["ok"], report)
        git.assert_not_called()
        self.assertEqual(report["worktree_seed"]["repositories"], ["frontend", "service"])
        self.assertTrue(report["worktree_seed"]["data_only"])
        self.assertFalse((self.ws / ".asha" / "worktrees").exists())

    def test_promotion_rechecks_privacy_before_emitting_plan(self):
        path = self.registry / "feature-auth.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("---\n", '---\nprivate_notes: "person@example.com"\n', 1),
                        encoding="utf-8")
        report = wi.promote_plan(
            self.ws, "feature-auth", target="cross-cutting/auth.md",
            evidence=[self.evidence],
        )
        self.assertEqual(report["errors"][0]["code"], "promotion_scrub_required")
        self.assertFalse((self.ws / "knowledge").exists())


class CliCases(WorkItemFixture):
    def test_json_and_human_commands(self):
        rc, out = _run_cli([
            "workspace_workitems.py", "create", "cli-item", "--start", str(self.ws),
            "--title", "CLI item", "--repo", "service", "--json",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        rc, out = _run_cli([
            "workspace_workitems.py", "list", "--start", str(self.ws)
        ])
        self.assertEqual(rc, 0)
        self.assertIn("cli-item", out)
        rc, out = _run_cli([
            "workspace_workitems.py", "show", "cli-item", "--start", str(self.ws),
            "--json",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["item"]["title"], "CLI item")

    def test_adapter_link_lint_index_and_plan_cli_surfaces(self):
        self.manifest(index=True)
        self.create("dependency", title="Dependency", repositories=["service"])
        candidate = self.candidate({
            "external_id": "EXT-7", "title": "External seven", "status": "Open",
            "repositories": ["service"], "provider": "fixture",
            "freshness": "2026-08-08T00:00:00Z",
            "source_url": "https://example.test/7",
        })
        rc, out = _run_cli([
            "workspace_workitems.py", "preview", "--start", str(self.ws),
            "--file", str(candidate), "--json",
        ])
        self.assertEqual(rc, 0)
        token = json.loads(out)["preview_token"]
        rc, out = _run_cli([
            "workspace_workitems.py", "import", "external-7", "--start", str(self.ws),
            "--file", str(candidate), "--preview-token", token, "--json",
        ])
        self.assertEqual(rc, 0, out)
        rc, _ = _run_cli([
            "workspace_workitems.py", "link", "external-7", "dependency",
            "--start", str(self.ws), "--relation", "depends-on", "--json",
        ])
        self.assertEqual(rc, 0)
        rc, _ = _run_cli([
            "workspace_workitems.py", "index", "--start", str(self.ws), "--json",
        ])
        self.assertEqual(rc, 0)
        rc, out = _run_cli([
            "workspace_workitems.py", "lint", "--start", str(self.ws), "--json",
        ])
        self.assertEqual(rc, 0, out)
        evidence = self.ws / "service" / "proof.txt"
        evidence.write_text("proof\n", encoding="utf-8")
        rc, out = _run_cli([
            "workspace_workitems.py", "promote-plan", "external-7",
            "--start", str(self.ws), "--target", "cross-cutting/seven.md",
            "--evidence", str(evidence), "--json",
        ])
        self.assertEqual(rc, 0, out)
        self.assertFalse(json.loads(out)["canonical_write_performed"])


def _run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = wi.main(argv)
    return rc, out.getvalue()


if __name__ == "__main__":
    unittest.main()
