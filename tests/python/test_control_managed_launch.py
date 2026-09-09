"""Default intake is atomic, retryable and project-bound before model dispatch."""
import unittest
import uuid
from unittest import mock

from lib.control.session_store import SessionStore
from lib.control.store import StoreError
from lib.control.orchestration.store import InitiativeStore
from lib.control.registry_migration import stage_registries
from lib.control.registry_activation import activate_registries
from lib.control.runtime import set_admission
from tests.python.orchestration_execution_fixtures import ExecutionFixture


class FreshManagedLaunchTests(ExecutionFixture, unittest.TestCase):
    def test_migration_lock_refuses_launch_before_session_schema_creation(self):
        from lib.control.managed_launch import launch_managed
        from lib.control.database import ControlDatabase
        from lib.control.registry_guards import migration_lock
        set_admission(self.config.control, 'paused')
        stage_registries(self.config.control, self.root / 'stage')
        activate_registries(self.config.control, self.root / 'stage')
        with migration_lock(self.config.control, exclusive=True):
            with self.assertRaisesRegex(StoreError, 'migration is active'):
                launch_managed(self.config.control, project=str(self.repo), intent='First work',
                               env=self.env, jj=self.jj)
        with ControlDatabase(self.config.control) as db, db.transaction() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='session_schema'").fetchone())

    def test_first_launch_initializes_sessions_after_file_registry_activation(self):
        from lib.control.managed_launch import launch_managed
        set_admission(self.config.control, 'paused')
        stage_registries(self.config.control, self.root / 'stage')
        activate_registries(self.config.control, self.root / 'stage')
        with mock.patch('lib.control.managed_launch.harness_available', return_value=True):
            launched = launch_managed(self.config.control, project=str(self.repo), intent='First work',
                                      env=self.env, jj=self.jj)
        self.assertEqual(launched['state'], 'queued')
        with SessionStore(self.config.control) as sessions:
            self.assertEqual(len(sessions.snapshot(launched['session_id'])['messages']), 1)


class ManagedLaunchTests(ExecutionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with SessionStore(self.config.control, create=True):
            pass
        set_admission(self.config.control, 'paused')
        stage_registries(self.config.control, self.root / 'stage')
        activate_registries(self.config.control, self.root / 'stage')
        self.store = InitiativeStore(self.config)
        self.before = len(self.store.list_initiatives())
        self.launch_id = str(uuid.uuid4())
        available = mock.patch('lib.control.managed_launch.harness_available', return_value=True)
        available.start()
        self.addCleanup(available.stop)

    def launch(self, **kwargs):
        from lib.control.managed_launch import launch_managed
        return launch_managed(self.config.control, project=str(self.repo), intent='Review the chapter',
            env=self.env, launch_id=self.launch_id, jj=self.jj, **kwargs)

    def test_retry_retains_one_initiative_session_and_opening_message(self):
        first = self.launch()
        second = self.launch()
        self.assertEqual(first['initiative_id'], second['initiative_id'])
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(first['harness'], 'claude')
        self.assertEqual(first['admission']['mode'], 'paused')
        self.assertEqual(len(self.store.list_initiatives()), self.before + 1)
        with SessionStore(self.config.control) as sessions:
            state = sessions.snapshot(first['session_id'])
        self.assertEqual(len(state['messages']), 1)
        self.assertEqual(state['sessions'][0]['initiative_id'], first['initiative_id'])
        self.assertEqual(state['sessions'][0]['state'], 'queued')
        events = self.store.list_events_snapshot(first['initiative_id'])
        self.assertEqual([e['type'] for e in events], ['initiative-created'])

    def test_failure_before_session_commit_leaves_no_partial_initiative(self):
        with mock.patch.object(SessionStore, '_create_in_transaction', side_effect=StoreError('injected write failure')):
            with self.assertRaisesRegex(StoreError, 'injected'):
                self.launch()
        self.assertEqual(len(self.store.list_initiatives()), self.before)
        self.assertEqual(self.launch()['state'], 'queued')

    def test_changed_retry_is_refused(self):
        self.launch()
        from lib.control.managed_launch import launch_managed
        with self.assertRaisesRegex(StoreError, 'changed'):
            launch_managed(self.config.control, project=str(self.repo), intent='Different work',
                           env=self.env, launch_id=self.launch_id, jj=self.jj)

    def test_committed_retry_does_not_probe_vcs_again(self):
        first = self.launch()
        with mock.patch('lib.control.orchestration.cli._prepare_initiative', side_effect=AssertionError('unexpected VCS probe')):
            self.assertEqual(self.launch()['session_id'], first['session_id'])

    def test_codex_launch_preserves_stopped_admission(self):
        set_admission(self.config.control, 'stopped')
        with mock.patch('lib.control.orchestration.supervisor_daemon.start_supervisor') as start:
            launched = self.launch(harness='codex')
        self.assertEqual(launched['harness'], 'codex')
        self.assertEqual(launched['admission']['mode'], 'stopped')
        self.assertEqual(launched['state'], 'queued')
        start.assert_not_called()

    def test_unverified_harness_and_managed_actor_are_refused_before_creation(self):
        with self.assertRaisesRegex(StoreError, 'managed adapter'):
            self.launch(harness='copilot')
        from lib.control.managed_launch import launch_managed
        with self.assertRaisesRegex(StoreError, 'managed actors'):
            launch_managed(self.config.control, project=str(self.repo), intent='Work',
                           env={**self.env, 'ASHA_MANAGED_SESSION_ID': str(uuid.uuid4())}, jj=self.jj)
        self.assertEqual(len(self.store.list_initiatives()), self.before)

    def test_supervisor_failure_reports_committed_custody(self):
        set_admission(self.config.control, 'running')
        with mock.patch('lib.control.orchestration.supervisor_daemon.start_supervisor', side_effect=OSError('launch unavailable')):
            launched = self.launch()
        self.assertEqual(launched['state'], 'queued')
        self.assertIn('launch unavailable', launched['supervisor']['message'])
        self.assertEqual(len(self.store.list_initiatives()), self.before + 1)

    def test_broad_project_directory_does_not_guess_a_repository(self):
        from lib.control.managed_launch import launch_managed
        with self.assertRaises(ValueError):
            launch_managed(self.config.control, project=str(self.root), intent='Work', env=self.env, jj=self.jj)
        self.assertEqual(len(self.store.list_initiatives()), self.before)

    def test_cli_defaults_to_atomic_managed_launch_and_inspects_before_claim(self):
        from lib.control.orchestration.cli import _coordinator_command
        tmux = mock.Mock()
        with mock.patch('lib.control.managed_launch.JjAdapter', return_value=self.jj):
            launched, as_json = _coordinator_command([
                'launch', '--project', str(self.repo), '--intent', 'Review the chapter',
                '--launch-id', self.launch_id, '--json',
            ], self.store, self.env, tmux, config=self.config)
        self.assertTrue(as_json)
        self.assertEqual(launched['transport'], 'managed')
        inspected, _ = _coordinator_command(['attach', launched['initiative_id'], '--json'],
                                            self.store, self.env, tmux, config=self.config)
        self.assertEqual(inspected['session_id'], launched['session_id'])
        self.assertEqual(inspected['snapshot']['sessions'][0]['state'], 'queued')
        tmux.has_session.assert_not_called()
        tmux.create_session.assert_not_called()

    def test_session_inspection_is_read_only_and_supports_wide_keys(self):
        from lib.control.tui import _managed_session_view
        from tests.python.test_control_tui_focus import FakeScreen, FakeCurses
        launched = self.launch()
        with SessionStore(self.config.control) as sessions:
            before = sessions.snapshot(launched['session_id'])
        screen = FakeScreen([-1, 'r', 'n', 'p', 'q'])
        result = _managed_session_view(screen, FakeCurses(), self.config.control, launched['session_id'])
        self.assertIn('inspection closed', result)
        self.assertIn('queued', screen.text)
        with SessionStore(self.config.control) as sessions:
            self.assertEqual(sessions.snapshot(launched['session_id']), before)

    def test_form_retries_lost_receipt_with_same_launch_id_and_retained_fields(self):
        from lib.control.managed_launch import launch_managed
        from lib.control.tui import _launch_coordinator_session, TuiModel
        from tests.python.test_control_tui_focus import FakeScreen, FakeCurses
        receipts = []

        def deliver(config, **kwargs):
            receipt = launch_managed(config, jj=self.jj, **kwargs)
            receipts.append(receipt)
            if len(receipts) == 1:
                raise OSError('injected lost receipt')
            return receipt

        projects = {'projects': [{'root': str(self.repo), 'name': 'Project',
                                 'asha_project': True, 'project_id': 'one'}]}
        screen = FakeScreen([10, 10, *'Review the chapter', 10, FakeCurses.KEY_RESIZE, 10], width=42)
        with mock.patch('lib.control.managed_launch.launch_managed', side_effect=deliver), \
             mock.patch('lib.control.orchestration.projects.list_projects_across', return_value=projects), \
             mock.patch('lib.control.tui._refresh_initiatives', side_effect=StoreError('refresh failed')):
            result = _launch_coordinator_session(screen, FakeCurses(), TuiModel([]), self.config.control, self.env)
        self.assertIn('queued', result)
        self.assertIn('display refresh unavailable: refresh failed', result)
        self.assertIn('injected lost receipt', screen.text.replace('\n', ''))
        self.assertEqual(receipts[0]['launch_id'], receipts[1]['launch_id'])
        self.assertEqual(receipts[0]['session_id'], receipts[1]['session_id'])
        self.assertEqual(len(self.store.list_initiatives()), self.before + 1)

    def test_unavailable_harness_refuses_new_work_but_preserves_committed_retry(self):
        with mock.patch('lib.control.managed_launch.harness_available', return_value=False):
            with self.assertRaisesRegex(StoreError, 'executable is unavailable'):
                self.launch()
        self.assertEqual(len(self.store.list_initiatives()), self.before)
        first = self.launch()
        with mock.patch('lib.control.managed_launch.harness_available', return_value=False):
            self.assertEqual(self.launch()['session_id'], first['session_id'])

    def test_admission_read_failure_still_returns_custody(self):
        with mock.patch('lib.control.managed_launch.admission', side_effect=StoreError('policy unreadable')):
            launched = self.launch()
        self.assertEqual(launched['admission']['mode'], 'unavailable')
        self.assertIn('policy unreadable', launched['supervisor']['message'])
        self.assertEqual(self.launch()['session_id'], launched['session_id'])

    def test_coordinator_sessions_includes_queued_intake_without_tmux(self):
        from lib.control.orchestration.coordinator import list_coordinator_sessions
        from lib.control.tmux import TmuxError
        launched = self.launch()
        tmux = mock.Mock()
        tmux.list_sessions.side_effect = TmuxError('tmux unavailable')
        result = list_coordinator_sessions(self.config.control, store=self.store, tmux=tmux)
        self.assertEqual(result['sessions'][0]['session_id'], launched['session_id'])
        self.assertEqual(result['sessions'][0]['state'], 'queued')
        self.assertTrue(result['managed_page']['complete'])
        self.assertIn('tmux unavailable', result['legacy_error'])

    def test_listing_managed_failure_preserves_legacy_rows(self):
        from lib.control.orchestration.coordinator import list_coordinator_sessions
        tmux = mock.Mock()
        tmux.list_sessions.return_value = [self.config.control.session_prefix + 'coord-test']
        with mock.patch('lib.control.session_activity.page', side_effect=StoreError('managed read failed')):
            result = list_coordinator_sessions(self.config.control, store=self.store, tmux=tmux)
        self.assertEqual(result['managed_error'], 'managed read failed')
        self.assertEqual(result['sessions'][0]['transport'], 'tmux')
        self.assertIsNone(result['sessions'][0]['session_id'])

    def test_listing_cursor_covers_filtered_sessions_and_excludes_stopped(self):
        from lib.control.orchestration.coordinator import list_coordinator_sessions
        with SessionStore(self.config.control) as sessions:
            unbound = sessions.create(cwd=str(self.repo), prompt='Other conversation')
        launched = self.launch()
        tmux = mock.Mock()
        tmux.list_sessions.return_value = []
        first = list_coordinator_sessions(self.config.control, store=self.store, tmux=tmux, limit=1)
        self.assertEqual(first['sessions'], [])
        self.assertFalse(first['managed_page']['complete'])
        self.assertIn('all current managed sessions', first['managed_page']['scope'])
        second = list_coordinator_sessions(self.config.control, store=self.store, tmux=tmux, limit=1,
                                           after=first['managed_page']['next_cursor'])
        self.assertEqual(second['sessions'][0]['session_id'], launched['session_id'])
        with SessionStore(self.config.control) as sessions:
            sessions.stop(launched['session_id'])
            sessions.stop(unbound['session_id'])
        self.assertEqual(list_coordinator_sessions(self.config.control, store=self.store, tmux=tmux)['sessions'], [])
