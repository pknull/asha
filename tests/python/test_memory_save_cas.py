"""Two deliberate publishers must retain each other's contributions."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests.python.test_memory_v2 import memory_v2
import save_none


class SaveCAS(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        memory_v2.initialize(self.root)
        self.active = self.root / 'active.draft'; self.active.write_text(memory_v2.ACTIVE_TEMPLATE)
        self.decisions = self.root / 'decisions.draft'; self.decisions.write_text('# Decisions\n\n- First.\n')

    def cli(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return memory_v2.main(['publish', '--project-dir', str(self.root), '--active-file', str(self.active),
                                      '--decisions-file', str(self.decisions), *extra])
            except SystemExit as exc:
                return exc.code

    def test_user_publish_without_predraft_digests_refuses(self):
        self.assertNotEqual(self.cli(), 0)
        self.assertEqual(memory_v2.read_published(self.root)[1], memory_v2.DECISIONS_TEMPLATE)

    def test_two_publishers_same_baseline_then_deliberate_merge(self):
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(self.root))
        args = ['--expected-active', before['active'], '--expected-decisions', before['decisions']]
        self.assertEqual(self.cli(*args), 0)
        self.decisions.write_text('# Decisions\n\n- Second.\n')
        self.assertNotEqual(self.cli(*args), 0)
        self.assertIn('First.', memory_v2.read_published(self.root)[1])
        current = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(self.root))
        self.decisions.write_text('# Decisions\n\n- First.\n- Second.\n')
        self.assertEqual(self.cli('--expected-active', current['active'], '--expected-decisions', current['decisions']), 0)

    def test_scope_none_requires_baseline_and_reports_superseded_success(self):
        mapping = {'scope': 'none', 'commit_repo': None, 'plane_base': str(self.root)}
        with mock.patch.object(save_none.save_scope, 'resolve_effective_plane', return_value=(mapping, [])):
            with self.assertRaises(ValueError):
                save_none.publish_managed_none(self.root, self.active, self.decisions)
            before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(self.root))
            original = memory_v2.publish
            def publish_then_concurrent(*args, **kwargs):
                receipt = original(*args, **kwargs)
                original(self.root, memory_v2.ACTIVE_TEMPLATE, '# Decisions\n\n- First.\n- Later.\n')
                return receipt
            with mock.patch.object(memory_v2, 'publish', side_effect=publish_then_concurrent):
                result = save_none.publish_managed_none(self.root, self.active, self.decisions, expected_preimages=before)
            self.assertTrue(result['superseded'])
            self.assertEqual(result['publication']['status'], 'published')
            self.assertFalse(result['git_invoked'])
            self.assertNotEqual(result['after']['Memory/decisions.md'], result['current']['decisions'])
