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
        # learnings_dir() resolves at call time (honoring ASHA_HOME), so the
        # redirect patches the function rather than a frozen constant.
        self.patch = mock.patch.object(lm, "learnings_dir", lambda: self.bundle)
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

    def test_symlinked_learning_state_directories_are_followed(self):
        # The local user owns the bundle; links inside it are their layout
        # (threat model, 2026-10-05), not an escape to refuse.
        self.bundle.mkdir(parents=True)
        outside = Path(self.tmp.name) / "linked-state"
        (outside / "candidate").mkdir(parents=True)
        (outside / ".transactions").mkdir()
        (self.bundle / "candidate").symlink_to(outside / "candidate", target_is_directory=True)
        (self.bundle / ".transactions").symlink_to(outside / ".transactions", target_is_directory=True)
        learning = lm.propose("linked", "t", "a", project_dir=self.project,
                              session_id="s", reason="r")
        self.assertEqual("candidate", learning.state)
        self.assertTrue((outside / "candidate/linked.md").is_file())

        replay = lm.Learning("replay", "t", "a", state="candidate",
                             created=date.today().isoformat(), updated=date.today().isoformat())
        rendered = lm._render(replay)
        (outside / ".transactions/replay.json").write_text(json.dumps({
            "version": 2, "name": "replay.md", "state": "candidate", "content": rendered,
            "content_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        }))
        lm.expire_candidates(project_dir=self.project)
        self.assertEqual(rendered, (outside / "candidate/replay.md").read_text())
        self.assertFalse((outside / ".transactions/replay.json").exists())

    def test_the_v1_migration_verbs_are_retired(self):
        legacy = Path(self.tmp.name) / "learnings.md"
        legacy.write_text("# Legacy flat learning\n")
        for argv in (["migrate-plan", "--project-dir", str(self.project), str(legacy)],
                     ["migrate-amend", "--project-dir", str(self.project), "--review", "r",
                      "--output", "o", "--active-file", "a", "--decisions-file", "d"],
                     ["migrate-apply", "--project-dir", str(self.project), "--review", "r",
                      "--session-id", "s", "--active-file", "a", "--decisions-file", "d"]):
            with self.subTest(verb=argv[0]), mock.patch("sys.stderr"), \
                    self.assertRaises(SystemExit) as raised:
                lm._main(argv)
            self.assertEqual(raised.exception.code, 2)
        for name in ("migrate_plan", "amend_migration_plan", "migrate_apply", "MIGRATION_MARKER"):
            self.assertFalse(hasattr(lm, name), name)
        self.assertFalse((self.project / "Work/memory-migration").exists())

    def test_no_shipped_surface_routes_to_the_retired_migration(self):
        root = Path(__file__).resolve().parents[2]
        surfaces = [root / "lib/install.sh", root / "bin/asha-drift-check.sh",
                    *sorted(path for path in (root / "plugins/session").rglob("*.md")
                            if path.name != "README.md"),
                    *sorted((root / "plugins/session/tools").glob("*.py"))]
        for path in surfaces:
            text = path.read_text(encoding="utf-8")
            for retired in ("/session:consolidate", "migrate-plan", "migrate-apply", "migration-v2"):
                self.assertFalse(retired in text, f"{path.relative_to(root)} names {retired}")
        self.assertFalse((root / "plugins/session/commands/consolidate.md").exists())

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

if __name__ == "__main__":
    unittest.main()
