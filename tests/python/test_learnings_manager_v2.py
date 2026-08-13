"""Memory v2 explicit learning lifecycle."""

import json
import concurrent.futures
import hashlib
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import learnings_manager as lm  # noqa: E402
import memory_v2  # noqa: E402


class LearningLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name) / "learnings"
        self.patch = mock.patch.object(lm, "LEARNINGS_DIR", self.bundle)
        self.patch.start()
        self.projects = {}
        self.project = self.project_for("p1")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def project_for(self, pid):
        if pid not in self.projects:
            project = Path(self.tmp.name) / f"project-{pid}"
            project.mkdir()
            memory_v2.initialize(project)
            config = memory_v2.read_project_config(project)
            config["project_id"] = pid
            (project / ".asha/config.json").write_text(json.dumps(config))
            self.projects[pid] = project
        return self.projects[pid]

    def capability(self, sid="s1", pid="p1"):
        project = self.project_for(pid)
        memory_v2.publish(project, memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE)
        return project, sid

    def propose(self, sid="s1", pid="p1"):
        project, capability = self.capability(sid, pid)
        return lm.propose("disk-pressure", "Avoid broad scans", "Scope filesystem searches",
                          project_dir=project, session_id=capability,
                          reason="Observed I/O stall")

    def bind_review(self, review, active=memory_v2.ACTIVE_TEMPLATE,
                    decisions=memory_v2.DECISIONS_TEMPLATE):
        review["publication"] = {
            "active_context_sha256": hashlib.sha256(active.encode()).hexdigest(),
            "decisions_sha256": hashlib.sha256(decisions.encode()).hexdigest(),
        }
        return review

    def test_proposal_is_candidate_without_confidence_or_tier(self):
        learning = self.propose()
        self.assertEqual("candidate", learning.state)
        text = (self.bundle / "candidate/disk-pressure.md").read_text()
        self.assertNotIn("confidence", text.lower())
        self.assertNotIn("tier", text.lower())

    def test_status_listing_reports_malformed_state_instead_of_hiding_it(self):
        malformed = self.bundle / "candidate/broken.md"
        malformed.parent.mkdir(parents=True)
        malformed.write_text("not frontmatter\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid learning record"):
            lm.list_state("candidate")

    def test_evidence_dedupes_by_session_and_project(self):
        self.propose()
        project, capability = self.capability("s1", "p1")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability,
                       reason="duplicate")
        learning = lm.load("disk-pressure")
        self.assertEqual(1, len(learning.evidence))

    def test_activation_requires_three_sessions_across_two_projects(self):
        self.propose("s1", "p1")
        project, capability = self.capability("s2", "p1")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="again")
        self.assertFalse(lm.activate_if_eligible("disk-pressure", project_dir=project))
        project, capability = self.capability("s3", "p2")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="cross-project")
        self.assertTrue(lm.activate_if_eligible("disk-pressure", project_dir=project))
        self.assertTrue((self.bundle / "active/disk-pressure.md").is_file())
        self.assertFalse((self.bundle / "candidate/disk-pressure.md").exists())

    def test_same_session_or_same_project_cannot_manufacture_activation(self):
        self.propose("s1", "p1")
        for sid, reason in (("s1", "same pair"), ("s2", "second session"), ("s3", "third session one project")):
            project, capability = self.capability(sid, "p1")
            lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason=reason)
        self.assertFalse(lm.activate_if_eligible("disk-pressure", project_dir=self.project))

    def test_contradict_and_retire_preserve_the_record(self):
        self.propose()
        project, capability = self.capability("s2", "p2")
        lm.contradict("disk-pressure", project_dir=project, session_id=capability, reason="counterexample")
        learning = lm.load("disk-pressure")
        self.assertEqual("candidate", learning.state)
        self.assertEqual("contradict", learning.evidence[-1].kind)
        lm.retire("disk-pressure", "obsolete", project_dir=project)
        self.assertTrue((self.bundle / "retired/disk-pressure.md").is_file())

    def test_render_active_excludes_candidates_and_honors_byte_cap(self):
        self.propose()
        self.assertEqual("", lm.render_active(3000))
        project, capability = self.capability("s2", "p1")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="again")
        project, capability = self.capability("s3", "p2")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="cross-project")
        lm.activate_if_eligible("disk-pressure", project_dir=project)
        rendered = lm.render_active(80)
        self.assertLessEqual(len(rendered.encode()), 80)
        self.assertIn("disk-pressure", rendered)

    def test_candidate_expiry_moves_old_record_to_retired(self):
        learning = self.propose()
        learning.updated = (date.today() - timedelta(days=91)).isoformat()
        lm.save(learning, project_dir=self.project)
        expired = lm.expire_candidates(project_dir=self.project, days=90)
        self.assertEqual(["disk-pressure"], expired)
        self.assertTrue((self.bundle / "retired/disk-pressure.md").exists())

    def test_save_batch_limits_new_candidates_to_three(self):
        proposals = [
            {"id": f"item-{i}", "trigger": "t", "action": "a", "reason": "r"}
            for i in range(4)
        ]
        with self.assertRaisesRegex(ValueError, "3"):
            _, capability = self.capability()
            lm.propose_many(proposals, project_dir=self.project, session_id=capability)

    def test_single_proposal_cli_cannot_bypass_three_per_save_limit(self):
        _, capability = self.capability()
        for i in range(3):
            lm.propose(f"item-{i}", "t", "a", project_dir=self.project,
                       session_id=capability, reason="r")
        with self.assertRaisesRegex(ValueError, "3"):
            lm.propose("item-4", "t", "a", project_dir=self.project,
                       session_id=capability, reason="r")

    def test_ordinary_evidence_uses_session_heuristic_and_actual_project_id(self):
        project, capability = self.capability("real-session", "real-project")
        learning = lm.propose("bound", "t", "a", project_dir=project,
                              session_id=capability, reason="r")
        evidence = learning.evidence[0]
        self.assertEqual(("real-session", "real-project"),
                         (evidence.session_id, evidence.project_id))
        with self.assertRaisesRegex(ValueError, "session_id"):
            lm.propose("missing", "t", "a", project_dir=project,
                       session_id="unknown", reason="r")
        self.assertFalse(any(project.glob("Work/session-state/.learning-capability-*")))

    def test_contradiction_requires_three_new_positive_sessions_across_two_projects(self):
        self.propose("s1", "p1")
        p1, _ = self.capability("s2", "p1")
        lm.corroborate("disk-pressure", project_dir=p1, session_id="s2", reason="again")
        p2, _ = self.capability("s3", "p2")
        lm.corroborate("disk-pressure", project_dir=p2, session_id="s3", reason="cross")
        self.assertTrue(lm.activate_if_eligible("disk-pressure", project_dir=p2))
        lm.contradict("disk-pressure", project_dir=p2, session_id="s4", reason="counter")
        self.assertFalse(lm.activate_if_eligible("disk-pressure", project_dir=p2))
        lm.corroborate("disk-pressure", project_dir=p2, session_id="s5", reason="new one")
        lm.corroborate("disk-pressure", project_dir=p1, session_id="s6", reason="new two")
        self.assertFalse(lm.activate_if_eligible("disk-pressure", project_dir=p1))
        lm.corroborate("disk-pressure", project_dir=p2, session_id="s7", reason="new three")
        self.assertTrue(lm.activate_if_eligible("disk-pressure", project_dir=p2))

    def test_one_proposal_cannot_mutate_active_semantics(self):
        self.propose("s1", "p1")
        project, capability = self.capability("s2", "p1")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="again")
        project, capability = self.capability("s3", "p2")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="cross")
        self.assertTrue(lm.activate_if_eligible("disk-pressure", project_dir=project))
        project, capability = self.capability("s4", "p2")
        with self.assertRaisesRegex(ValueError, "active semantic"):
            lm.propose("disk-pressure", "malicious trigger", "malicious action",
                       project_dir=project, session_id=capability, reason="one assertion")
        current = lm.load("disk-pressure")
        self.assertEqual("Avoid broad scans", current.trigger)
        self.assertEqual("Scope filesystem searches", current.action)

    def test_candidate_semantic_change_resets_prior_positive_corroboration(self):
        self.propose("s1", "p1")
        project, capability = self.capability("s2", "p1")
        lm.corroborate("disk-pressure", project_dir=project, session_id=capability, reason="old semantics")
        project, capability = self.capability("s3", "p2")
        changed = lm.propose("disk-pressure", "changed trigger", "changed action",
                             project_dir=project, session_id=capability, reason="new semantics")
        positive = [item for item in changed.evidence if item.kind in ("propose", "corroborate")]
        self.assertEqual(1, len(positive))
        self.assertFalse(lm.activate_if_eligible("disk-pressure", project_dir=project))

    def test_concurrent_corroboration_serializes_without_lost_evidence(self):
        self.propose()
        projects = [Path(self.tmp.name) / f"p{i}" for i in range(40)]
        for i, project in enumerate(projects):
            (project / ".asha").mkdir(parents=True)
            (project / ".asha/config.json").write_text(json.dumps({"project_id": f"p{i % 2}"}))

        def add(i):
            # White-box contention probe: identity verification is covered
            # separately; this holds the exact global read/modify/write lock.
            with lm._global_lock():
                learning = lm._load_unlocked("disk-pressure")
                lm._add_evidence(learning, f"session-{i}", f"p{i % 2}", f"r{i}",
                                 "corroborate")
                lm._save_unlocked(learning)

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(add, range(40)))
        self.assertEqual(41, len(lm.load("disk-pressure").evidence))

    def test_interrupted_state_transition_is_completed_from_journal(self):
        learning = self.propose()
        learning.state = "active"
        real = lm._atomic
        failed = False

        def fail_destination_once(path, text):
            nonlocal failed
            if Path(path).parent.name == "active" and Path(path).name == "disk-pressure.md" and not failed:
                failed = True
                raise OSError("forced transition failure")
            return real(path, text)

        with mock.patch.object(lm, "_atomic", side_effect=fail_destination_once):
            with self.assertRaisesRegex(OSError, "forced"):
                lm.save(learning, project_dir=self.project)
        with self.assertRaisesRegex(ValueError, "pending learning transition"):
            lm.load("disk-pressure")
        lm.expire_candidates(project_dir=self.project)
        recovered = lm.load("disk-pressure")
        self.assertEqual("active", recovered.state)
        self.assertTrue((self.bundle / "active/disk-pressure.md").is_file())
        self.assertFalse((self.bundle / "candidate/disk-pressure.md").exists())

    def test_interrupted_same_state_update_replays_journal_content(self):
        learning = self.propose()
        learning.evidence.append(lm.Evidence(date.today().isoformat(), "s2", "p1", "new"))
        real = lm._atomic

        def fail_candidate(path, text):
            if Path(path).parent.name == "candidate":
                raise OSError("forced candidate failure")
            return real(path, text)

        with mock.patch.object(lm, "_atomic", side_effect=fail_candidate):
            with self.assertRaisesRegex(OSError, "forced"):
                lm.save(learning, project_dir=self.project)
        lm.expire_candidates(project_dir=self.project)
        recovered = lm.load("disk-pressure")
        self.assertEqual(["s1", "s2"], [item.session_id for item in recovered.evidence])

    def test_slug_collisions_remain_distinct_and_raw_ids_are_verified(self):
        _, capability = self.capability()
        first = lm.propose("a/b", "one", "first", project_dir=self.project,
                           session_id=capability, reason="r")
        second = lm.propose("a?b", "two", "second", project_dir=self.project,
                            session_id=capability, reason="r")
        self.assertEqual("a/b", first.id)
        self.assertEqual("a?b", second.id)
        records = [path for path in (self.bundle / "candidate").glob("a-b*.md")]
        self.assertEqual(2, len(records))
        self.assertEqual("first", lm.load("a/b").action)
        self.assertEqual("second", lm.load("a?b").action)

    def test_silence_marker_blocks_learning_mutations(self):
        _, capability = self.capability()
        (self.project / "Work/markers").mkdir(parents=True, exist_ok=True)
        (self.project / "Work/markers/silence").touch()
        with self.assertRaisesRegex(ValueError, "silence"):
            lm.propose("silent", "t", "a", project_dir=self.project,
                       session_id=capability, reason="r")
        self.assertFalse((self.bundle / "candidate").exists())

        legacy = Path(self.tmp.name) / "legacy-silent.md"
        legacy.write_text("legacy")
        with self.assertRaisesRegex(ValueError, "silence"):
            lm.migrate_plan([legacy], project_dir=self.project)

    def test_silence_prevents_pending_learning_journal_replay(self):
        learning = self.propose()
        learning.state = "active"
        journal = lm._transition_journal_path(learning)
        journal.parent.mkdir(parents=True, exist_ok=True)
        rendered = lm._render(learning)
        journal.write_text(json.dumps({
            "version": 2, "name": lm._path(learning).name, "state": "active",
            "content": rendered,
            "content_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        }))
        (self.project / "Work/markers").mkdir(parents=True, exist_ok=True)
        (self.project / "Work/markers/silence").touch()
        with self.assertRaisesRegex(ValueError, "silence"):
            lm.corroborate("disk-pressure", project_dir=self.project,
                           session_id="s2", reason="must not replay")
        self.assertTrue(journal.exists())
        self.assertFalse((self.bundle / "active/disk-pressure.md").exists())

    def test_reviewed_migration_is_idempotent_and_preserves_sources(self):
        legacy_dir = Path(self.tmp.name) / "legacy"
        legacy_dir.mkdir()
        legacy = legacy_dir / "one.md"
        legacy.write_text("old evidence\n", encoding="utf-8")
        (legacy_dir / "two.md").write_text("other evidence\n", encoding="utf-8")
        (legacy_dir / "event.jsonl").write_text('{"old":true}\n', encoding="utf-8")
        review = self.bind_review(lm.migrate_plan([legacy_dir], project_dir=self.project))
        self.assertEqual(3, len(review["items"]))
        legacy_item = next(item for item in review["items"] if item["source"] == str(legacy.resolve()))
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(),
                         legacy_item["source_sha256"])
        legacy_item.update({
            "decision": "accept", "item_type": "learning",
            "proposal": {"id": "migrated", "trigger": "t", "action": "a", "reason": "reviewed"},
        })
        review["items"].append({"source": str(legacy), "decision": "defer"})

        first = lm.migrate_apply(
            review, session_id="s1", project_dir=self.project,
            active_context=memory_v2.ACTIVE_TEMPLATE, decisions=memory_v2.DECISIONS_TEMPLATE)
        second = lm.migrate_apply(
            review, session_id="s1", project_dir=self.project,
            active_context=memory_v2.ACTIVE_TEMPLATE, decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertEqual(["migrated"], first["applied_learnings"])
        self.assertEqual(first, second)
        self.assertEqual(1, len(lm.load("migrated").evidence))
        self.assertEqual("old evidence\n", legacy.read_text(encoding="utf-8"))
        backups = list(Path(first["backup_dir"]).glob("*-one.md"))
        self.assertEqual([b"old evidence\n"], [path.read_bytes() for path in backups])

    def test_migration_rejects_stale_review_and_honors_reviewed_state(self):
        archive = Path(self.tmp.name) / "learnings-archive"
        archive.mkdir()
        legacy = archive / "old.md"
        legacy.write_text("old", encoding="utf-8")
        review = self.bind_review(lm.migrate_plan([archive], project_dir=self.project))
        item = review["items"][0]
        self.assertEqual("retired", item["proposed_state"])
        item.update({
            "decision": "accept", "item_type": "learning",
            "proposal": {"id": "old", "trigger": "t", "action": "a", "reason": "reviewed"},
        })
        legacy.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            lm.migrate_apply(review, session_id="migration-session",
                             project_dir=self.project,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)

    def test_migration_rejects_malformed_review_instead_of_silently_skipping(self):
        with self.assertRaisesRegex(ValueError, "review format"):
            lm.migrate_apply({"version": 2}, session_id="s",
                             project_dir=self.project,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)

    def test_migration_preflights_entire_batch_before_first_mutation(self):
        sources = []
        for i in range(2):
            path = Path(self.tmp.name) / f"legacy-{i}.md"
            path.write_text(f"legacy {i}")
            sources.append(path)
        review = self.bind_review(lm.migrate_plan(sources, project_dir=self.project))
        for i, item in enumerate(review["items"]):
            item.update({"decision": "accept", "item_type": "learning", "proposal": {
                "id": f"item-{i}", "trigger": "t", "action": "a", "reason": "reviewed"
            }})
        sources[1].write_text("stale")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            lm.migrate_apply(review, session_id="migration",
                             project_dir=self.project,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertFalse((self.bundle / "candidate/item-0.md").exists())

    def test_mixed_legacy_review_initializes_once_and_preserves_every_source(self):
        root = Path(self.tmp.name) / "legacy-project"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        active.write_text("# Current Work\nunique legacy project fact\n")
        flat = root / "legacy-learning.md"
        flat.write_text("unique legacy learning evidence\n")
        review = lm.migrate_plan([active, flat], project_dir=root)
        decisions_source = root / "Memory/decisions.md"
        decisions_source.write_text("# Legacy Decisions\nunique decision source\n")
        review = lm.migrate_plan([active, decisions_source, flat], project_dir=root)
        self.bind_review(review)
        for item in review["items"]:
            item["decision"] = "accept"
            if item["source"] == str(flat.resolve()):
                item.update({"item_type": "learning", "proposal": {
                    "id": "mixed", "trigger": "t", "action": "a", "reason": "reviewed"
                }})
        result = lm.migrate_apply(
            review, session_id="migration-session", project_dir=root,
            active_context=memory_v2.ACTIVE_TEMPLATE, decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertEqual(2, memory_v2.read_project_config(root)["memory_version"])
        self.assertEqual(["mixed"], result["applied_learnings"])
        backup_bytes = {p.read_bytes() for p in Path(result["backup_dir"]).iterdir()
                        if p.name != "manifest.json"}
        self.assertIn(b"# Current Work\nunique legacy project fact\n", backup_bytes)
        self.assertIn(b"unique legacy learning evidence\n", backup_bytes)
        self.assertEqual("unique legacy learning evidence\n", flat.read_text())

    def test_mixed_migration_rolls_back_config_publication_learning_ignore_and_backup(self):
        root = Path(self.tmp.name) / "rollback-project"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        original_active = b"# Current Work\nrollback specimen \xff\n"
        active.write_bytes(original_active)
        source = root / "legacy.md"
        source.write_text("learning specimen")
        legacy_decisions = root / "Memory/decisions.md"
        legacy_decisions.write_text("# Legacy Decisions\nold\n")
        review = lm.migrate_plan([active, legacy_decisions, source], project_dir=root)
        self.bind_review(review)
        for item in review["items"]:
            item["decision"] = "accept"
            if item["source"] == str(source.resolve()):
                item.update({"item_type": "learning", "proposal": {
                    "id": "rollback", "trigger": "t", "action": "a", "reason": "r"
                }})
        prior_ignore = (root / ".gitignore").read_bytes() if (root / ".gitignore").exists() else None
        with mock.patch.object(lm, "_save_unlocked", side_effect=OSError("forced learning write")):
            with self.assertRaisesRegex(OSError, "forced learning"):
                lm.migrate_apply(
                    review, session_id="migration", project_dir=root,
                    active_context=memory_v2.ACTIVE_TEMPLATE,
                    decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertEqual(original_active, active.read_bytes())
        self.assertEqual(b"# Legacy Decisions\nold\n",
                         (root / "Memory/decisions.md").read_bytes())
        self.assertFalse((root / ".asha/config.json").exists())
        current_ignore = (root / ".gitignore").read_bytes() if (root / ".gitignore").exists() else None
        self.assertEqual(prior_ignore, current_ignore)
        self.assertEqual("learning specimen", source.read_text())
        backups = root / "Work/memory-migration/backups"
        self.assertFalse(backups.exists() and any(backups.iterdir()))
        self.assertFalse((self.bundle / "candidate/rollback.md").exists())

    def test_next_apply_recovers_a_process_interrupted_whole_migration(self):
        root = Path(self.tmp.name) / "crash-project"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        original = b"## Current\ncrash rollback specimen\n"
        active.write_bytes(original)
        source = root / "legacy.md"
        source.write_text("learning before crash")
        legacy_decisions = root / "Memory/decisions.md"
        legacy_decisions.write_text("# Legacy Decisions\nold\n")
        review = lm.migrate_plan([active, legacy_decisions, source], project_dir=root)
        self.bind_review(review)
        for item in review["items"]:
            item["decision"] = "accept"
            if item["source"] == str(source.resolve()):
                item.update({"item_type": "learning", "proposal": {
                    "id": "crash-rule", "trigger": "t", "action": "a", "reason": "r"
                }})
        with mock.patch.object(lm, "_save_unlocked", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lm.migrate_apply(
                    review, session_id="migration", project_dir=root,
                    active_context=memory_v2.ACTIVE_TEMPLATE,
                    decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertTrue((root / ".asha/config.json").exists())
        self.assertTrue(any((self.bundle / ".transactions").glob("migration-*.json")))
        source.write_text("stale after crash")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            lm.migrate_apply(
                review, session_id="migration", project_dir=root,
                active_context=memory_v2.ACTIVE_TEMPLATE,
                decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertEqual(original, active.read_bytes())
        self.assertEqual(b"# Legacy Decisions\nold\n",
                         (root / "Memory/decisions.md").read_bytes())
        self.assertFalse((root / ".asha/config.json").exists())
        self.assertFalse(any((self.bundle / ".transactions").glob("migration-*.json")))
        backups = root / "Work/memory-migration/backups"
        self.assertFalse(backups.exists() and any(backups.iterdir()))

    def test_global_recovery_does_not_erase_a_later_project_learning(self):
        root = Path(self.tmp.name) / "crash-global-project"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        decisions = root / "Memory/decisions.md"
        active.write_text("## Current\nlegacy\n")
        decisions.write_text("# Legacy Decisions\nold\n")
        source = root / "legacy.md"
        source.write_text("migration source")
        review = self.bind_review(lm.migrate_plan([active, decisions, source], project_dir=root))
        for item in review["items"]:
            item["decision"] = "accept"
            if item["source"] == str(source.resolve()):
                item.update({"item_type": "learning", "proposal": {
                    "id": "crashed", "trigger": "t", "action": "a", "reason": "r"
                }})
        with mock.patch.object(lm, "_save_unlocked", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lm.migrate_apply(review, session_id="migration-a", project_dir=root,
                                 active_context=memory_v2.ACTIVE_TEMPLATE,
                                 decisions=memory_v2.DECISIONS_TEMPLATE)

        project_b, sid = self.capability("session-b", "project-b")
        lm.propose("later-b", "later", "preserve", project_dir=project_b,
                   session_id=sid, reason="after crash")
        self.assertEqual("preserve", lm.load("later-b").action)
        self.assertFalse(any((self.bundle / ".transactions").glob("migration-*.json")))

    def test_migration_binds_both_publication_roles_and_output_digests(self):
        root = Path(self.tmp.name) / "binding-project"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        decisions = root / "Memory/decisions.md"
        active.write_text("## Current\nlegacy\n")
        decisions.write_text("## Decisions\nlegacy\n")
        review = lm.migrate_plan([active, decisions], project_dir=root)
        for item in review["items"]:
            item["decision"] = "accept"
        review["publication"] = {
            "active_context_sha256": hashlib.sha256(memory_v2.ACTIVE_TEMPLATE.encode()).hexdigest(),
            "decisions_sha256": hashlib.sha256(memory_v2.DECISIONS_TEMPLATE.encode()).hexdigest(),
        }
        with self.assertRaisesRegex(ValueError, "digest"):
            lm.migrate_apply(review, session_id="s", project_dir=root,
                             active_context=memory_v2.ACTIVE_TEMPLATE.replace("Not yet", "Changed"),
                             decisions=memory_v2.DECISIONS_TEMPLATE)
        review["items"] = [item for item in review["items"]
                           if item.get("publication_role") != "decisions"]
        with self.assertRaisesRegex(ValueError, "both.*roles"):
            lm.migrate_apply(review, session_id="s", project_dir=root,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)

    def test_publication_cannot_overwrite_deferred_targets_via_unrelated_roles(self):
        root = Path(self.tmp.name) / "deferred-publication"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        decisions = root / "Memory/decisions.md"
        active.write_text("## Current\ndeferred active specimen\n")
        decisions.write_text("# Legacy Decisions\ndeferred decisions specimen\n")
        unrelated_a = root / "unrelated-a.md"
        unrelated_b = root / "unrelated-b.md"
        unrelated_a.write_text("unrelated a")
        unrelated_b.write_text("unrelated b")
        review = self.bind_review(lm.migrate_plan(
            [active, decisions, unrelated_a, unrelated_b], project_dir=root
        ))
        for item in review["items"]:
            if item["source"] == str(unrelated_a.resolve()):
                item.update({"decision": "accept", "item_type": "project-publication",
                             "publication_role": "activeContext"})
            elif item["source"] == str(unrelated_b.resolve()):
                item.update({"decision": "accept", "item_type": "project-publication",
                             "publication_role": "decisions"})
        with self.assertRaisesRegex(ValueError, "existing.*target.*accepted"):
            lm.migrate_apply(review, session_id="s", project_dir=root,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertIn("deferred active specimen", active.read_text())
        self.assertIn("deferred decisions specimen", decisions.read_text())

    def test_absent_publication_targets_require_and_accept_explicit_create_rows(self):
        root = Path(self.tmp.name) / "explicit-create-publication"
        root.mkdir()
        review = self.bind_review({"version": 2, "items": [
            {"decision": "accept", "item_type": "project-publication",
             "publication_role": "activeContext", "create": True,
             "target": str(root / "Memory/activeContext.md")},
            {"decision": "accept", "item_type": "project-publication",
             "publication_role": "decisions", "create": True,
             "target": str(root / "Memory/decisions.md")},
        ]})
        result = lm.migrate_apply(review, session_id="s", project_dir=root,
                                  active_context=memory_v2.ACTIVE_TEMPLATE,
                                  decisions=memory_v2.DECISIONS_TEMPLATE)
        self.assertEqual("applied", result["status"])
        self.assertEqual((memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE),
                         memory_v2.read_published(root))

    def _crashed_publication_migration(self, name="crash-conflict"):
        root = Path(self.tmp.name) / name
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        decisions = root / "Memory/decisions.md"
        active.write_text("## Current\nunique legacy active\n")
        decisions.write_text("# Legacy Decisions\nunique legacy decisions\n")
        source = root / "legacy.md"
        source.write_text("learning")
        review = self.bind_review(lm.migrate_plan([active, decisions, source], project_dir=root))
        for item in review["items"]:
            item["decision"] = "accept"
            if item["source"] == str(source.resolve()):
                item.update({"item_type": "learning", "proposal": {
                    "id": f"{name}-rule", "trigger": "t", "action": "a", "reason": "r"
                }})
        with mock.patch.object(lm, "_save_unlocked", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lm.migrate_apply(review, session_id="migration", project_dir=root,
                                 active_context=memory_v2.ACTIVE_TEMPLATE,
                                 decisions=memory_v2.DECISIONS_TEMPLATE)
        return root, review

    def test_recovery_conflict_preserves_entire_project_and_repair_evidence(self):
        root, _review = self._crashed_publication_migration()
        later_active = memory_v2.ACTIVE_TEMPLATE.replace("Not yet recorded.", "Later save.", 1)
        later_decisions = "# Decisions\n\n- Later save remains binding.\n"
        memory_v2.publish(root, later_active, later_decisions)
        config_before = (root / ".asha/config.json").read_bytes()
        journal = next((self.bundle / ".transactions").glob("migration-*.json"))
        backup = next((root / "Work/memory-migration/backups").iterdir())
        project_b = self.project_for("recovery-project-b")
        with self.assertRaisesRegex(ValueError, "recovery conflict.*preserved"):
            lm.propose("after-conflict", "t", "a", project_dir=project_b,
                       session_id="b-session", reason="r")
        self.assertEqual(config_before, (root / ".asha/config.json").read_bytes())
        self.assertEqual((later_active, later_decisions), memory_v2.read_published(root))
        self.assertTrue(journal.exists())
        self.assertTrue(backup.exists())

    def test_cross_project_recovery_respects_recorded_project_silence(self):
        root, _review = self._crashed_publication_migration("crash-silence")
        before = {path.relative_to(root): path.read_bytes()
                  for path in root.rglob("*") if path.is_file()}
        (root / "Work/markers").mkdir(parents=True)
        (root / "Work/markers/silence").touch()
        project_b = self.project_for("silence-project-b")
        with self.assertRaisesRegex(ValueError, "silenced.*preserved"):
            lm.propose("blocked-by-a", "t", "a", project_dir=project_b,
                       session_id="b-session", reason="r")
        after = {path.relative_to(root): path.read_bytes()
                 for path in root.rglob("*") if path.is_file()
                 and path != root / "Work/markers/silence"}
        self.assertEqual(before, after)
        self.assertTrue(any((self.bundle / ".transactions").glob("migration-*.json")))

    def test_preflight_bytes_back_backups_and_publication_target_is_rechecked(self):
        root = Path(self.tmp.name) / "source-race"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        decisions = root / "Memory/decisions.md"
        active.write_text("## Current\nreviewed active bytes\n")
        decisions.write_text("# Legacy Decisions\nreviewed decisions bytes\n")
        extra = root / "extra.md"
        extra.write_text("reviewed extra bytes")
        review = self.bind_review(lm.migrate_plan([active, decisions, extra], project_dir=root))
        for item in review["items"]:
            item["decision"] = "accept"
        real_ignore = memory_v2.ensure_private_ignores

        def race(_root):
            real_ignore(_root)
            active.write_text("raced active bytes")
            extra.write_text("raced extra bytes")

        with mock.patch.object(memory_v2, "ensure_private_ignores", side_effect=race):
            with self.assertRaisesRegex(ValueError, "publication preimage changed"):
                lm.migrate_apply(review, session_id="s", project_dir=root,
                                 active_context=memory_v2.ACTIVE_TEMPLATE,
                                 decisions=memory_v2.DECISIONS_TEMPLATE)
        backup = next((root / "Work/memory-migration/backups").iterdir())
        extra_backup = next(backup.glob("*-extra.md"))
        self.assertEqual(b"reviewed extra bytes", extra_backup.read_bytes())
        self.assertTrue(any((self.bundle / ".transactions").glob("migration-*.json")))

    def test_receipt_revalidation_rejects_missing_declared_backup(self):
        legacy = Path(self.tmp.name) / "receipt-source.md"
        legacy.write_text("receipt source")
        review = self.bind_review(lm.migrate_plan([legacy], project_dir=self.project))
        review["items"][0].update({"decision": "accept", "item_type": "learning",
                                   "proposal": {"id": "receipt-rule", "trigger": "t",
                                                "action": "a", "reason": "r"}})
        result = lm.migrate_apply(review, session_id="s", project_dir=self.project,
                                  active_context=memory_v2.ACTIVE_TEMPLATE,
                                  decisions=memory_v2.DECISIONS_TEMPLATE)
        Path(result["sources"][0]["backup"]).unlink()
        with self.assertRaisesRegex(ValueError, "receipt effects"):
            lm.migrate_apply(review, session_id="s", project_dir=self.project,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)

    def test_receipt_revalidation_rejects_drifted_publication_and_learning(self):
        root = Path(self.tmp.name) / "receipt-effects"
        (root / "Memory").mkdir(parents=True)
        active = root / "Memory/activeContext.md"
        decisions = root / "Memory/decisions.md"
        active.write_text("## Current\nlegacy active\n")
        decisions.write_text("# Legacy Decisions\nlegacy decision\n")
        learning_source = root / "learning.md"
        learning_source.write_text("learning source")
        review = self.bind_review(lm.migrate_plan(
            [active, decisions, learning_source], project_dir=root
        ))
        for item in review["items"]:
            item["decision"] = "accept"
            if item["source"] == str(learning_source.resolve()):
                item.update({"item_type": "learning", "proposal": {
                    "id": "receipt-effect-rule", "trigger": "t", "action": "a", "reason": "r"
                }})
        result = lm.migrate_apply(review, session_id="s", project_dir=root,
                                  active_context=memory_v2.ACTIVE_TEMPLATE,
                                  decisions=memory_v2.DECISIONS_TEMPLATE)
        memory_v2.publish(
            root, memory_v2.ACTIVE_TEMPLATE.replace("Not yet recorded.", "Later.", 1),
            memory_v2.DECISIONS_TEMPLATE,
        )
        with self.assertRaisesRegex(ValueError, "receipt effects"):
            lm.migrate_apply(review, session_id="s", project_dir=root,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)
        memory_v2.publish(root, memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE)
        learning_path = self.bundle / result["effects"]["learnings"][0]["relative_path"]
        learning_path.write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "receipt effects"):
            lm.migrate_apply(review, session_id="s", project_dir=root,
                             active_context=memory_v2.ACTIVE_TEMPLATE,
                             decisions=memory_v2.DECISIONS_TEMPLATE)

    def test_learning_state_symlink_rejected_before_save_or_replay(self):
        self.bundle.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            (self.bundle / "active").symlink_to(outside, target_is_directory=True)
            sentinel = outside / "victim.md"
            sentinel.write_text("keep")
            with self.assertRaisesRegex(ValueError, "symlinked learning"):
                lm.propose("victim", "t", "a", project_dir=self.project,
                           session_id="s", reason="r")
            self.assertEqual("keep", sentinel.read_text())

            tx = self.bundle / ".transactions"
            tx.mkdir()
            learning = lm.Learning("replay", "t", "a", state="active",
                                   created=date.today().isoformat(), updated=date.today().isoformat())
            rendered = lm._render(learning)
            (tx / "replay.json").write_text(json.dumps({
                "version": 2, "name": "replay.md", "state": "active",
                "content": rendered,
                "content_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            }))
            with self.assertRaisesRegex(ValueError, "symlinked learning"):
                lm.expire_candidates(project_dir=self.project)
            self.assertFalse((outside / "replay.md").exists())

    def test_owned_symlinked_bundle_root_is_supported(self):
        target = Path(self.tmp.name) / "dotfiles-learnings"
        target.mkdir()
        self.bundle.symlink_to(target, target_is_directory=True)

        learning = lm.propose(
            "dotfiles-root", "t", "a", project_dir=self.project,
            session_id="s", reason="r",
        )

        self.assertEqual("candidate", learning.state)
        self.assertTrue((target / "candidate/dotfiles-root.md").is_file())
        self.assertTrue((target / ".transactions").is_dir())

    def test_amended_plan_is_atomically_staged_with_bound_drafts(self):
        source = Path(self.tmp.name) / "amend-source.md"
        source.write_text("legacy")
        plan = lm.migrate_plan([source], project_dir=self.project)
        output = self.project / "Work/memory-migration/review.json"
        lm.write_migration_plan(plan, project_dir=self.project, output=output)
        plan["items"][0]["decision"] = "reject"
        staged = lm.amend_migration_plan(
            plan, project_dir=self.project, output=output,
            active_context=memory_v2.ACTIVE_TEMPLATE,
            decisions=memory_v2.DECISIONS_TEMPLATE,
        )
        persisted = json.loads(staged.read_text())
        self.assertEqual("reject", persisted["items"][0]["decision"])
        self.assertEqual(hashlib.sha256(memory_v2.ACTIVE_TEMPLATE.encode()).hexdigest(),
                         persisted["publication"]["active_context_sha256"])

    def test_nested_migration_symlinks_are_rejected_without_external_writes(self):
        for nested in ("backups", ".transactions", "applied"):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as outside:
                root = Path(self.tmp.name) / f"symlink-{nested.replace('.', 'dot')}"
                root.mkdir()
                memory_v2.initialize(root)
                migration = root / "Work/memory-migration"
                migration.mkdir(parents=True, exist_ok=True)
                (migration / nested).symlink_to(Path(outside), target_is_directory=True)
                source = root / "legacy.md"
                source.write_text("legacy")
                review = self.bind_review(lm.migrate_plan([source], project_dir=root))
                review["items"][0].update({"decision": "accept", "item_type": "legacy-evidence"})
                with self.assertRaisesRegex(ValueError, "symlink"):
                    lm.migrate_apply(review, session_id="s", project_dir=root,
                                     active_context=memory_v2.ACTIVE_TEMPLATE,
                                     decisions=memory_v2.DECISIONS_TEMPLATE)
                self.assertEqual([], list(Path(outside).iterdir()))

    def test_failure_before_transaction_readiness_preserves_global_learnings(self):
        self.propose()
        source = self.project / "legacy.md"
        source.write_text("legacy")
        review = self.bind_review(lm.migrate_plan([source], project_dir=self.project))
        before = {path.relative_to(self.bundle): path.read_bytes()
                  for path in self.bundle.rglob("*") if path.is_file()}
        with mock.patch.object(memory_v2, "ensure_private_ignores", side_effect=OSError("early")):
            with self.assertRaisesRegex(OSError, "early"):
                lm.migrate_apply(review, session_id="s", project_dir=self.project,
                                 active_context=memory_v2.ACTIVE_TEMPLATE,
                                 decisions=memory_v2.DECISIONS_TEMPLATE)
        after = {path.relative_to(self.bundle): path.read_bytes()
                 for path in self.bundle.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_private_plan_is_atomic_ignored_and_preserves_deferred_review(self):
        source = Path(self.tmp.name) / "legacy-plan.md"
        source.write_text("legacy")
        review = lm.migrate_plan([source], project_dir=self.project)
        output = self.project / "Work/memory-migration/review.json"
        self.assertEqual(output, lm.write_migration_plan(
            review, project_dir=self.project, output=output))
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "preserved"):
            lm.write_migration_plan({"version": 2, "items": []},
                                    project_dir=self.project, output=output)
        self.assertEqual(before, output.read_bytes())
        self.assertIn("/Work/memory-migration/", (self.project / ".gitignore").read_text())

    def test_flat_legacy_store_is_inventory_itemized_by_learning(self):
        flat = Path(self.tmp.name) / "learnings.md"
        flat.write_text(
            "# Learnings\n\n## Filesystem\n\n"
            "### first-rule\n- **Confidence**: 0.8\n- **Trigger**: first trigger\n"
            "- **Action**: first action\n- **Evidence**:\n  - 2026-01-01 | p | one\n\n"
            "### second-rule\n- **Confidence**: 0.5\n- **Trigger**: second trigger\n"
            "- **Action**: second action\n- **Evidence**:\n  - 2026-01-02 | p | two\n",
            encoding="utf-8",
        )
        plan = lm.migrate_plan([flat], project_dir=self.project)
        self.assertEqual(["first-rule", "second-rule"],
                         [item["source_fragment"] for item in plan["items"]])
        self.assertEqual("first trigger", plan["items"][0]["proposal"]["trigger"])
        self.assertTrue(all(item["decision"] == "defer" for item in plan["items"]))

    def test_root_okf_bundle_gets_a_reviewable_mapping_per_concept(self):
        bundle = Path(self.tmp.name) / "okf"
        bundle.mkdir()
        (bundle / "concept.md").write_text(
            "---\ntype: learning\nid: concept\n---\n\n# Concept\n\n"
            "**Trigger:** observed trigger\n\n**Action:** reviewed action\n",
            encoding="utf-8",
        )
        item = lm.migrate_plan([bundle], project_dir=self.project)["items"][0]
        self.assertEqual("concept", item["proposal"]["id"])
        self.assertEqual("observed trigger", item["proposal"]["trigger"])
        self.assertEqual("reviewed action", item["proposal"]["action"])


if __name__ == "__main__":
    unittest.main()
