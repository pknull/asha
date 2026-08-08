#!/usr/bin/env python3
"""Tests for the workspace v3 canonical-knowledge core (epic #23)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import workspace_knowledge as wk  # type: ignore[reportMissingImports]  # noqa: E402


class KnowledgeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_wsk_")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, True))
        self.old_home = os.environ.get("HOME")
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.ws = self.home / "Code" / "thallus"
        self.ws.mkdir(parents=True)

    def _restore_home(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def manifest(self, *, mode="pull-request", shared="knowledge", shared_git="."):
        (self.ws / ".asha").mkdir(exist_ok=True)
        payload = {
            "version": 1,
            "workspace_name": "thallus",
            "memory": {
                "shared_root": shared, "shared_git_root": shared_git,
                "promotion_mode": mode,
            },
            "repositories": [
                {"path": "frontend", "role": "web"},
                {"path": "service", "role": "api"},
            ],
        }
        (self.ws / ".asha" / "workspace.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        for name in ("frontend", "service"):
            (self.ws / name).mkdir(exist_ok=True)

    def init(self, **kwargs):
        return wk.initialize_layout(self.ws, **kwargs)

    def evidence(self, name="contract.py", text="API_VERSION = 2\n"):
        path = self.ws / "service" / name
        path.write_text(text, encoding="utf-8")
        return path


class LayoutCases(KnowledgeFixture):
    def test_non_workspace_never_creates_implicitly(self):
        report = wk.initialize_layout(self.ws)
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "no_workspace")
        self.assertFalse((self.ws / "knowledge").exists())

    def test_predictable_layout_and_ownership_metadata(self):
        self.manifest()
        report = self.init()
        self.assertTrue(report["ok"], report)
        root = self.ws / "knowledge"
        self.assertTrue((root / "README.md").is_file())
        self.assertTrue((root / "cross-cutting").is_dir())
        self.assertTrue((root / "decisions").is_dir())
        self.assertFalse((root / "tickets").exists())
        for repo in ("frontend", "service"):
            for name in wk.REPOSITORY_DOCUMENTS:
                self.assertTrue((root / "repos" / repo / name).is_file())
        ownership = json.loads((root / wk.OWNERSHIP_FILE).read_text())
        index = json.loads((root / wk.INDEX_FILE).read_text())
        self.assertEqual(ownership["version"], 1)
        self.assertIn("repos/frontend/projectbrief.md", ownership["files"])
        self.assertIn("repos/service/activeContext.md", index["documents"])

    def test_optional_tickets_and_idempotent_rerun(self):
        self.manifest()
        first = self.init(include_tickets=True)
        before = (self.ws / "knowledge" / wk.OWNERSHIP_FILE).read_bytes()
        second = self.init(include_tickets=True)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue((self.ws / "knowledge" / "tickets").is_dir())
        self.assertEqual(second["created"], [])
        self.assertEqual(second["updated"], [])
        self.assertEqual(before, (self.ws / "knowledge" / wk.OWNERSHIP_FILE).read_bytes())

    def test_user_collision_and_owned_drift_are_preserved(self):
        self.manifest()
        root = self.ws / "knowledge"
        root.mkdir()
        (root / "README.md").write_text("user navigation\n", encoding="utf-8")
        report = self.init()
        self.assertTrue(report["ok"])
        self.assertIn("README.md", report["collisions"])
        self.assertEqual((root / "README.md").read_text(), "user navigation\n")

        stub = root / "repos" / "frontend" / "projectbrief.md"
        stub.write_text("user changed owned stub\n", encoding="utf-8")
        rerun = self.init()
        self.assertIn("repos/frontend/projectbrief.md", rerun["drifted"])
        self.assertEqual(stub.read_text(), "user changed owned stub\n")

    def test_shared_root_symlink_escape_fails_without_writes(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.manifest()
        (self.ws / "knowledge").symlink_to(outside, target_is_directory=True)
        report = self.init()
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "shared_root_escape")
        self.assertEqual(list(outside.iterdir()), [])


class LintCases(KnowledgeFixture):
    def setUp(self):
        super().setUp()
        self.manifest()
        self.init()

    def test_clean_generated_layout_passes(self):
        report = wk.lint_knowledge(self.ws)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["blocking"], [])

    def test_blocking_structure_privacy_and_registry_findings(self):
        root = self.ws / "knowledge"
        doc = root / "cross-cutting" / "bad.md"
        doc.write_text(
            "---\ntitle: Bad\ntype: knowledge\nupdated: 2026-08-08\n---\n"
            "[missing](nowhere.md)\n"
            "token=ghp_abcdefghijklmnopqrstuvwxyz123456\n"
            "owner: person@example.com\n"
            "path: /home/alice/private/file\n"
            "role: assistant\ncontent: raw transcript line\n",
            encoding="utf-8",
        )
        report = wk.lint_knowledge(self.ws)
        codes = {item["code"] for item in report["blocking"]}
        self.assertTrue({
            "broken_link", "secret_pattern", "personal_email",
            "personal_home_path", "transcript_content",
        }.issubset(codes), report)
        self.assertFalse(report["ok"])

    def test_malformed_frontmatter_and_index_inconsistency_block(self):
        root = self.ws / "knowledge"
        bad = root / "decisions" / "bad.md"
        bad.write_text("---\ntitle: never closes\n", encoding="utf-8")
        index = json.loads((root / wk.INDEX_FILE).read_text())
        index["documents"].append("decisions/missing.md")
        (root / wk.INDEX_FILE).write_text(json.dumps(index), encoding="utf-8")
        report = wk.lint_knowledge(self.ws)
        codes = {item["code"] for item in report["blocking"]}
        self.assertIn("malformed_frontmatter", codes)
        self.assertIn("index_missing_document", codes)

    def test_advisory_orphan_empty_coverage_and_stale(self):
        root = self.ws / "knowledge"
        orphan = root / "cross-cutting" / "orphan.md"
        orphan.write_text(
            "---\ntitle: Old\ntype: knowledge\nupdated: 2000-01-01\n---\nbody\n",
            encoding="utf-8",
        )
        empty = root / "decisions" / "empty.md"
        empty.touch()
        manifest = json.loads((self.ws / ".asha" / "workspace.json").read_text())
        manifest["repositories"].append({"path": "worker", "role": "jobs"})
        (self.ws / ".asha" / "workspace.json").write_text(json.dumps(manifest))
        (self.ws / "worker").mkdir()
        report = wk.lint_knowledge(self.ws, today="2026-08-08")
        codes = {item["code"] for item in report["advisory"]}
        self.assertTrue({"orphan_document", "empty_file", "missing_coverage", "stale_document"}.issubset(codes))
        self.assertTrue(report["ok"], "advisories must not make lint blocking")

    def test_symlinked_document_outside_root_blocks_without_reading_target(self):
        outside = self.tmp / "secret.md"
        outside.write_text("token=do-not-read", encoding="utf-8")
        link = self.ws / "knowledge" / "cross-cutting" / "linked.md"
        link.symlink_to(outside)
        report = wk.lint_knowledge(self.ws)
        codes = {item["code"] for item in report["blocking"]}
        self.assertIn("document_escape", codes)
        self.assertNotIn("secret_pattern", codes)


class PromotionCases(KnowledgeFixture):
    def setUp(self):
        super().setUp()
        self.manifest()
        self.init()
        self.source = self.ws / "Memory" / "candidate.md"
        self.source.parent.mkdir()
        self.source.write_text("API version is two.\n", encoding="utf-8")
        self.ev = self.evidence()
        (self.ws / ".gitignore").write_text("Work/\n.asha/state/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "master"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Asha Test"], cwd=self.ws, check=True)
        subprocess.run(["git", "config", "user.email", "asha@example.invalid"], cwd=self.ws, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/workspace.git"],
            cwd=self.ws, check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.ws, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=self.ws, check=True, capture_output=True)

    def plan(self, **kwargs):
        args = {
            "start": self.ws,
            "source": self.source,
            "target": "cross-cutting/api-version.md",
            "evidence": [self.ev],
        }
        args.update(kwargs)
        return wk.plan_promotion(**args)

    def test_github_remote_binding_rejects_credentials_ports_and_untrusted_schemes(self):
        accepted = [
            "https://github.com/example/workspace.git",
            "git@github.com:example/workspace.git",
            "ssh://git@github.com/example/workspace.git",
        ]
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(
                    wk._normalize_github_remote(value)["repository"], "example/workspace",
                )
        rejected = [
            "https://user:secret@github.com/example/workspace.git",
            "file://github.com/example/workspace.git",
            "ftp://github.com/example/workspace.git",
            "https://github.com:8443/example/workspace.git",
            "ssh://root@github.com/example/workspace.git",
        ]
        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(wk._normalize_github_remote(value))

    def artifact(self, plan=None, name="promotion.json"):
        plan = plan or self.plan()
        path = self.ws / "Work" / "promotion-plans" / name
        written = wk.write_promotion_plan(plan, path)
        self.assertTrue(written["ok"], written)
        return path, written["plan_digest"]

    def test_promotion_requires_evidence_and_classifies_source(self):
        no_ev = self.plan(evidence=[])
        self.assertFalse(no_ev["ok"])
        self.assertEqual(no_ev["errors"][0]["code"], "evidence_required")
        plan = self.plan()
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["classification"], "operational")
        self.assertEqual(plan["promotion_mode"], "pull-request")
        self.assertTrue(plan["review_required"])
        self.assertEqual(plan["evidence"][0]["path"], "service/contract.py")
        self.assertRegex(plan["evidence"][0]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(plan["publication"]["repository"], "example/workspace")
        self.assertEqual(plan["publication"]["base_branch"], "master")
        self.assertRegex(plan["publication"]["base_oid"], r"^[0-9a-f]{40}$")

    def test_external_source_requires_explicit_classification(self):
        external = self.tmp / "candidate.md"
        external.write_text("Fact.\n", encoding="utf-8")
        rejected = self.plan(source=external)
        self.assertEqual(rejected["errors"][0]["code"], "classification_required")
        accepted = self.plan(source=external, classification="personal")
        self.assertTrue(accepted["ok"], accepted)

    def test_scrubber_redacts_and_drops_transient_lines(self):
        self.source.write_text(
            "Owner person@example.com uses /home/alice/ws.\n"
            "api_token = secret-value-123\n"
            "Current branch: feature/in-flight\n"
            "Stable contract remains.\n",
            encoding="utf-8",
        )
        plan = self.plan()
        self.assertTrue(plan["ok"], plan)
        content = plan["content"]
        self.assertIn("[REDACTED_EMAIL]", content)
        self.assertIn("~/ws", content)
        self.assertIn("api_token = [REDACTED]", content)
        self.assertNotIn("Current branch", content)
        self.assertIn("transient_line_removed", {x["code"] for x in plan["scrubbed"]})

    def test_private_key_and_transcript_source_fail_scrubbing(self):
        self.source.write_text(
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        bad = self.plan()
        self.assertEqual(bad["errors"][0]["code"], "unscrubbable_secret")

        transcript = self.ws / "Memory" / "session-transcript.jsonl"
        transcript.write_text('{"role":"user","content":"raw"}\n', encoding="utf-8")
        bad = self.plan(source=transcript)
        self.assertEqual(bad["errors"][0]["code"], "transcript_source_forbidden")

    def test_direct_commit_requires_manifest_configuration(self):
        rejected = self.plan(requested_mode="direct-commit")
        self.assertEqual(rejected["errors"][0]["code"], "direct_commit_not_configured")

        self.manifest(mode="direct-commit")
        accepted = self.plan(requested_mode="direct-commit")
        self.assertTrue(accepted["ok"], accepted)
        self.assertFalse(accepted["review_required"])
        self.assertNotIn("open_pull_request", accepted["next_steps"])

    def test_target_symlink_escape_is_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        cross = self.ws / "knowledge" / "cross-cutting"
        shutil.rmtree(cross)
        cross.symlink_to(outside, target_is_directory=True)
        plan = self.plan()
        self.assertEqual(plan["errors"][0]["code"], "target_unsafe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_updates_document_and_index_only_after_explicit_confirm(self):
        plan = self.plan()
        artifact, digest = self.artifact(plan)
        refused = wk.apply_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=False
        )
        self.assertEqual(refused["errors"][0]["code"], "confirmation_required")
        self.assertFalse((self.ws / "knowledge" / "cross-cutting" / "api-version.md").exists())

        applied = wk.apply_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True
        )
        self.assertTrue(applied["ok"], applied)
        target = self.ws / "knowledge" / "cross-cutting" / "api-version.md"
        self.assertIn("API version is two", target.read_text())
        index = json.loads((self.ws / "knowledge" / wk.INDEX_FILE).read_text())
        self.assertIn("cross-cutting/api-version.md", index["documents"])

    def test_apply_revalidates_evidence_and_direct_commit_configuration(self):
        plan = self.plan()
        artifact, digest = self.artifact(plan)
        self.ev.write_text("API_VERSION = 3\n", encoding="utf-8")
        rejected = wk.apply_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True
        )
        self.assertEqual(rejected["errors"][0]["code"], "evidence_changed")

        self.ev.write_text("API_VERSION = 2\n", encoding="utf-8")
        self.manifest(mode="direct-commit")
        direct = self.plan(requested_mode="direct-commit")
        direct_artifact, direct_digest = self.artifact(direct, "direct.json")
        self.manifest(mode="pull-request")
        rejected = wk.apply_promotion_artifact(
            direct_artifact, confirmed_digest=direct_digest, confirmed=True
        )
        self.assertEqual(rejected["errors"][0]["code"], "direct_commit_not_configured")

    def test_artifact_digest_source_and_target_preimages_are_mandatory(self):
        plan = self.plan()
        self.assertEqual(plan["source_preimage"]["sha256"], wk._sha_bytes(self.source.read_bytes()))
        self.assertEqual(plan["target_preimage"], {"state": "absent", "sha256": None})
        artifact, digest = self.artifact(plan)
        wrong = wk.apply_promotion_artifact(
            artifact, confirmed_digest="0" * 64, confirmed=True
        )
        self.assertEqual(wrong["errors"][0]["code"], "digest_confirmation_mismatch")

        self.source.write_text("changed after review\n", encoding="utf-8")
        changed = wk.apply_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True
        )
        self.assertEqual(changed["errors"][0]["code"], "source_changed")

    def test_target_preimage_change_after_review_is_rejected(self):
        plan = self.plan()
        artifact, digest = self.artifact(plan)
        target = self.ws / "knowledge" / "cross-cutting" / "api-version.md"
        target.write_text("appeared after review\n", encoding="utf-8")
        rejected = wk.apply_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True
        )
        self.assertEqual(rejected["errors"][0]["code"], "target_changed")

    def test_apply_rolls_back_both_files_when_replace_fails(self):
        plan = self.plan()
        root = self.ws / "knowledge"
        target = root / "cross-cutting" / "api-version.md"
        old_target = b"---\ntitle: Old target\ntype: knowledge\nupdated: 2026-08-08\n---\n\n# Old target\n"
        target.write_bytes(old_target)
        plan = self.plan()
        artifact, digest = self.artifact(plan)
        old_index = (root / wk.INDEX_FILE).read_bytes()
        real_replace = wk._replace_contained
        calls = []

        def fail_second(root, dst, content, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("fixture failure")
            return real_replace(root, dst, content, **kwargs)

        with mock.patch.object(wk, "_replace_contained", side_effect=fail_second):
            report = wk.apply_promotion_artifact(
                artifact, confirmed_digest=digest, confirmed=True
            )
        self.assertFalse(report["ok"])
        self.assertEqual(target.read_bytes(), old_target)
        self.assertEqual((root / wk.INDEX_FILE).read_bytes(), old_index)
        self.assertEqual(list((self.ws / ".asha" / "state" / "knowledge-transactions").glob("*.json")), [])

    def test_internal_and_external_target_symlinks_fail_identically(self):
        cross = self.ws / "knowledge" / "cross-cutting"
        shutil.rmtree(cross)
        inside = self.ws / "knowledge" / "decisions"
        cross.symlink_to(inside, target_is_directory=True)
        internal = self.plan()
        cross.unlink()
        outside = self.tmp / "outside-two"
        outside.mkdir()
        cross.symlink_to(outside, target_is_directory=True)
        external = self.plan()
        self.assertEqual(internal["errors"], external["errors"])
        self.assertEqual(internal["errors"][0]["code"], "target_unsafe")

    def test_apply_parent_symlink_swap_cannot_write_outside_workspace(self):
        artifact, digest = self.artifact()
        root = self.ws / "knowledge"
        old_index = (root / wk.INDEX_FILE).read_bytes()
        outside = self.tmp / "swap-outside"
        outside.mkdir()
        cross = root / "cross-cutting"
        parked = root / "cross-cutting.parked"
        real_replace = wk._replace_contained
        swapped = False

        def swap_before_target(workspace, destination, content, **kwargs):
            nonlocal swapped
            if not swapped and destination.parent == cross:
                cross.rename(parked)
                cross.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_replace(workspace, destination, content, **kwargs)

        with mock.patch.object(wk, "_replace_contained", side_effect=swap_before_target):
            report = wk.apply_promotion_artifact(
                artifact, confirmed_digest=digest, confirmed=True,
            )
        self.assertFalse(report["ok"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((root / wk.INDEX_FILE).read_bytes(), old_index)

    def test_apply_does_not_overwrite_concurrent_edit_after_journaling(self):
        artifact, digest = self.artifact()
        index = self.ws / "knowledge" / wk.INDEX_FILE
        real_prepare = wk._prepare_promotion_transaction
        concurrent = b'{"concurrent":"user edit"}\n'

        def edit_after_journal(plan, write_set=None):
            prepared = real_prepare(plan, write_set)
            self.assertTrue(prepared["ok"], prepared)
            index.write_bytes(concurrent)
            return prepared

        with mock.patch.object(wk, "_prepare_promotion_transaction", side_effect=edit_after_journal):
            report = wk.apply_promotion_artifact(
                artifact, confirmed_digest=digest, confirmed=True,
            )
        self.assertFalse(report["ok"])
        self.assertEqual(index.read_bytes(), concurrent)
        self.assertFalse((self.ws / "knowledge" / "cross-cutting" / "api-version.md").exists())

    def test_reserved_index_and_ownership_symlink_aliases_are_rejected(self):
        for reserved in (wk.INDEX_FILE, wk.OWNERSHIP_FILE):
            with self.subTest(reserved=reserved):
                plan = self.plan()
                artifact, digest = self.artifact(plan, f"{reserved}.plan.json")
                root = self.ws / "knowledge"
                original = root / reserved
                alias = root / f"{reserved}.real"
                original.rename(alias)
                original.symlink_to(alias.name)
                report = wk.apply_promotion_artifact(
                    artifact, confirmed_digest=digest, confirmed=True
                )
                self.assertEqual(report["errors"][0]["code"], "reserved_path_unsafe")
                original.unlink()
                alias.rename(original)

    def test_recovery_journal_restores_a_crash_interrupted_write_set(self):
        plan = self.plan()
        artifact, digest = self.artifact(plan)
        prepared = wk._prepare_promotion_transaction(plan)
        self.assertTrue(prepared["ok"], prepared)
        journal = Path(prepared["journal_path"])
        target = self.ws / "knowledge" / "cross-cutting" / "api-version.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text(plan["content"], encoding="utf-8")
        recovered = wk.recover_promotion_journal(journal)
        self.assertTrue(recovered["ok"], recovered)
        self.assertFalse(target.exists())
        self.assertFalse(journal.exists())

    def test_publish_requires_confirmed_pull_request_plan_and_clean_git_root(self):
        artifact, digest = self.artifact()
        refused = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=False,
        )
        self.assertEqual(refused["errors"][0]["code"], "confirmation_required")

        direct = self.plan(requested_mode="pull-request")
        direct["promotion_mode"] = "direct-commit"
        direct["review_required"] = False
        direct["plan_digest"] = wk._plan_digest(direct)
        direct_artifact, direct_digest = self.artifact(direct, "not-pr.json")
        rejected = wk.publish_promotion_artifact(
            direct_artifact, confirmed_digest=direct_digest, confirmed=True,
            runner=lambda *args, **kwargs: self.fail("Git must not run"),
        )
        self.assertEqual(rejected["errors"][0]["code"], "pull_request_plan_required")

    def test_publish_creates_only_a_dedicated_branch_commit_and_draft_pr(self):
        artifact, digest = self.artifact()
        reviewed = json.loads(artifact.read_text(encoding="utf-8"))
        expected_paths = sorted([
            "knowledge/cross-cutting/api-version.md",
            f"knowledge/{wk.INDEX_FILE}", f"knowledge/{wk.OWNERSHIP_FILE}",
        ])
        calls = []

        def runner(args, *, cwd, input_text=None):
            calls.append((list(args), Path(cwd), input_text))
            stdout = ""
            rc = 0
            if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
                stdout = str(self.ws) + "\n"
            elif args[:3] == ["git", "hash-object", "--stdin"]:
                stdout = "a" * 40 + "\n"
            elif args[:2] == ["git", "rev-parse"] and (
                args[2].startswith(":") or args[2].startswith("HEAD:")
            ):
                stdout = "a" * 40 + "\n"
            elif args[:2] == ["git", "rev-parse"]:
                stdout = reviewed["publication"]["base_oid"] + "\n"
            elif args[:4] == ["git", "symbolic-ref", "--quiet", "--short"]:
                stdout = "master\n"
            elif args[:4] == ["git", "remote", "get-url", "origin"]:
                stdout = "https://github.com/example/workspace.git\n"
            elif args[:3] == ["git", "show-ref", "--verify"]:
                rc = 1
            elif args[:4] == ["git", "diff", "--cached", "--quiet"]:
                rc = 1
            elif args[:4] == ["git", "diff", "--cached", "--name-only"]:
                stdout = "\0".join(expected_paths) + "\0"
            elif args[:2] == ["git", "diff-tree"]:
                stdout = "\0".join(expected_paths) + "\0"
            elif args[:3] == ["gh", "pr", "create"]:
                stdout = "https://github.example/pr/17\n"
            return subprocess.CompletedProcess(args, rc, stdout, "")

        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True, runner=runner,
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["base"], "master")
        self.assertEqual(report["pr_url"], "https://github.example/pr/17")
        self.assertRegex(report["branch"], r"^asha/knowledge-[0-9a-f]{12}$")
        command_lists = [item[0] for item in calls]
        branch = report["branch"]
        self.assertIn(["git", "switch", "-c", branch], command_lists)
        push = next(command for command in command_lists if "push" in command)
        self.assertEqual(push[:4], ["git", "-c", "core.hooksPath=/dev/null", "push"])
        self.assertIn("--no-verify", push)
        self.assertEqual(push[-2], "https://github.com/example/workspace")
        self.assertTrue(push[-1].endswith(f":refs/heads/{branch}"))
        pr = next(command for command in command_lists if command[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", pr)
        self.assertEqual(pr[pr.index("--base") + 1], "master")
        self.assertEqual(pr[pr.index("--head") + 1], branch)
        self.assertEqual(pr[pr.index("--repo") + 1], "example/workspace")
        self.assertFalse(any("merge" in command for command in command_lists))
        commit = next(command for command in command_lists if "commit" in command)
        self.assertIn("core.hooksPath=/dev/null", commit)
        self.assertNotIn("--no-verify", commit)
        add = next(command for command in command_lists if command[:2] == ["git", "add"])
        self.assertEqual(add[:3], ["git", "add", "--"])
        self.assertEqual(
            set(add[3:]),
            set(expected_paths),
        )

    def test_publish_fails_closed_on_dirty_shared_git_root(self):
        artifact, digest = self.artifact()
        reviewed = json.loads(artifact.read_text(encoding="utf-8"))

        def runner(args, *, cwd, input_text=None):
            if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(args, 0, str(self.ws) + "\n", "")
            if args[:4] == ["git", "symbolic-ref", "--quiet", "--short"]:
                return subprocess.CompletedProcess(args, 0, "master\n", "")
            if args[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(
                    args, 0, reviewed["publication"]["base_oid"] + "\n", "",
                )
            if args[:3] == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(args, 0, " M tracked.txt\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True, runner=runner,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "shared_git_root_dirty")
        self.assertFalse((self.ws / "knowledge" / "cross-cutting" / "api-version.md").exists())

    def test_publish_refuses_remote_changed_after_review(self):
        artifact, digest = self.artifact()
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/other/repository.git"],
            cwd=self.ws, check=True,
        )
        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "review_remote_changed")
        self.assertFalse((self.ws / "knowledge" / "cross-cutting" / "api-version.md").exists())

    def test_publish_refuses_concurrently_staged_unreviewed_path_before_commit(self):
        artifact, digest = self.artifact()

        def runner(args, *, cwd, input_text=None):
            completed = subprocess.run(
                args, cwd=cwd, input=input_text, text=True,
                capture_output=True, check=False,
            )
            if args[:3] == ["git", "add", "--"]:
                (self.ws / "unreviewed-secret.txt").write_text("must not publish\n", encoding="utf-8")
                subprocess.run(
                    ["git", "add", "--", "unreviewed-secret.txt"],
                    cwd=self.ws, check=True, capture_output=True,
                )
            return completed

        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True, runner=runner,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "publication_index_mismatch")
        self.assertEqual(
            subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=self.ws,
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "fixture",
        )

    def test_publish_refuses_concurrent_exact_path_content_change_before_commit(self):
        artifact, digest = self.artifact()
        target = self.ws / "knowledge" / "cross-cutting" / "api-version.md"

        def runner(args, *, cwd, input_text=None):
            if args[:3] == ["git", "add", "--"]:
                target.write_text("MALICIOUS UNREVIEWED CONTENT\n", encoding="utf-8")
            return subprocess.run(
                args, cwd=cwd, input=input_text, text=True,
                capture_output=True, check=False,
            )

        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True, runner=runner,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "publication_content_mismatch")
        self.assertEqual(
            subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=self.ws,
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "fixture",
        )

    def test_publish_reports_failed_cleanup_after_apply_failure(self):
        artifact, digest = self.artifact()

        def runner(args, *, cwd, input_text=None):
            if args == ["git", "switch", "master"]:
                return subprocess.CompletedProcess(args, 1, "", "blocked")
            return subprocess.run(
                args, cwd=cwd, input=input_text, text=True,
                capture_output=True, check=False,
            )

        failed_apply = {"ok": False, "errors": [{"code": "fixture", "message": "failed"}]}
        with mock.patch.object(wk, "apply_promotion_artifact", return_value=failed_apply):
            report = wk.publish_promotion_artifact(
                artifact, confirmed_digest=digest, confirmed=True, runner=runner,
            )
        self.assertFalse(report["ok"])
        self.assertFalse(report["cleanup"]["base_switch_ok"])
        self.assertFalse(report["cleanup"]["branch_delete_ok"])
        self.assertEqual(report["cleanup"]["current_branch"], report["branch"])
        self.assertIn("recovery", report)

    def test_plan_rejects_write_set_outside_shared_git_root_before_apply(self):
        self.manifest(shared_git="Memory")
        report = self.plan()
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "shared_root_git_escape")
        self.assertFalse((self.ws / "knowledge" / "cross-cutting" / "api-version.md").exists())

    def test_publish_real_git_fixture_commits_only_reviewed_write_set(self):
        artifact, digest = self.artifact()

        def runner(args, *, cwd, input_text=None):
            if args[0] == "git" and "push" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(args, 0, "https://github.example/pr/18\n", "")
            return subprocess.run(
                args, cwd=cwd, input=input_text, text=True,
                capture_output=True, check=False,
            )

        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True, runner=runner,
        )
        self.assertTrue(report["ok"], report)
        changed = subprocess.run(
            ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
            cwd=self.ws, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertEqual(set(filter(None, changed)), set(report["staged"]))
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--show-current"], cwd=self.ws,
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            report["branch"],
        )

    def test_publish_runs_repository_hooks_only_with_separate_explicit_flag(self):
        artifact, digest = self.artifact()
        calls = []

        def runner(args, *, cwd, input_text=None):
            calls.append(list(args))
            if args[0] == "git" and "push" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(args, 0, "https://github.example/pr/19\n", "")
            return subprocess.run(
                args, cwd=cwd, input=input_text, text=True,
                capture_output=True, check=False,
            )

        report = wk.publish_promotion_artifact(
            artifact, confirmed_digest=digest, confirmed=True,
            run_git_hooks=True, runner=runner,
        )
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["git_hooks_executed"])
        commit = next(command for command in calls if "commit" in command)
        push = next(command for command in calls if "push" in command)
        self.assertEqual(commit[:2], ["git", "commit"])
        self.assertEqual(push[:2], ["git", "push"])
        self.assertNotIn("core.hooksPath=/dev/null", commit + push)
        self.assertNotIn("--no-verify", push)


class CliCases(KnowledgeFixture):
    def test_json_and_human_rendering(self):
        self.manifest()
        rc, out = _run_cli(["workspace_knowledge.py", "init", "--start", str(self.ws), "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        rc, out = _run_cli(["workspace_knowledge.py", "lint", "--start", str(self.ws)])
        self.assertEqual(rc, 0)
        self.assertIn("knowledge lint: PASS", out)

    def test_promotion_cli_plan_and_confirmed_apply(self):
        self.manifest()
        wk.initialize_layout(self.ws)
        source = self.ws / "Memory" / "candidate.md"
        source.parent.mkdir()
        source.write_text("Stable API contract.\n", encoding="utf-8")
        evidence = self.evidence()
        (self.ws / ".gitignore").write_text("Work/\n.asha/state/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "master"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Asha Test"], cwd=self.ws, check=True)
        subprocess.run(["git", "config", "user.email", "asha@example.invalid"], cwd=self.ws, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/workspace.git"],
            cwd=self.ws, check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.ws, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=self.ws, check=True, capture_output=True)
        planning = [
            "--start", str(self.ws), "--source", str(source),
            "--target", "cross-cutting/api.md", "--evidence", str(evidence),
        ]
        artifact = self.ws / "Work" / "promotion-plans" / "cli.json"
        rc, out = _run_cli([
            "workspace_knowledge.py", "promote", "plan", *planning,
            "--plan-out", str(artifact), "--json",
        ])
        self.assertEqual(rc, 0)
        planned = json.loads(out)
        self.assertTrue(artifact.is_file())
        self.assertTrue(planned["review_required"])
        applying = ["--plan", str(artifact), "--digest", planned["plan_digest"], "--json"]
        rc, out = _run_cli(["workspace_knowledge.py", "promote", "apply", *applying])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "confirmation_required")
        rc, out = _run_cli(["workspace_knowledge.py", "promote", "apply", *applying, "--confirm"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])


def _run_cli(argv):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = wk.main(argv)
    return rc, stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
