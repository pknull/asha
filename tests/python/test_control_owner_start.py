"""Structured session owners start where work is queued; no daemon schedules them (N1).

The supervisor daemon retired on 2026-10-07. An owner starts, detached, from
launch, send, resume, answer and admission resume; `session show` and
`session list` restart a missing owner after a crash or reboot. Terminal
Rooms and workers never start one.
"""
import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from tests.python import test_control_rooms as rooms_fixture


ROOT = Path(__file__).resolve().parents[2]


class FakeChild:
    def __init__(self, argv):
        self.argv = argv
        self.pid = 2 ** 22 + 1

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class OwnerStartFixture(unittest.TestCase):
    def setUp(self):
        rooms_fixture.RoomTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(self.env)
        from lib.control.session_hub import Hub
        self.hub = Hub(self.config, env=self.env, tmux=self.tmux)
        self.launches = []
        real_popen = __import__('subprocess').Popen
        lock = threading.Lock()

        def popen(argv, *args, **kwargs):
            # Record owner launches (and, on a tree that still has one, a
            # supervisor start); run nothing else of ours for real.
            words = [str(a) for a in argv]
            if 'owner' in words and any('control.sessions' in w for w in words) or 'supervisor' in words:
                with lock:
                    self.launches.append((words, kwargs))
                return FakeChild(words)
            return real_popen(argv, *args, **kwargs)
        self.enterContext(mock.patch('lib.control.sessions.subprocess.Popen', side_effect=popen))
        # CLI verbs read the fake terminal, never a real tmux server.
        self.enterContext(mock.patch('lib.control.session_hub.TmuxAdapter', return_value=self.tmux))
        self.enterContext(mock.patch('lib.control.hub_cli.terminal_inventory', return_value=(self.tmux, [])))

    def owner_launches(self):
        return [words[-1] for words, _ in self.launches if words[-2:-1] == ['owner']]

    def structured(self, **changes):
        return self.hub.launch(project=str(self.project), prompt='Summarize the inbox', name='Inbox',
                               harness='claude', transport='structured', **changes)

    def run_one_turn(self, sid):
        """Model an owner that claimed, ran one turn and exited."""
        from lib.control.session_store import SessionStore
        with SessionStore(self.config) as sessions:
            session = sessions.claim_owner(sid)
            message = sessions.claim_turn(sid, session['generation'])
            sessions.observe(sid, session['generation'], message['turn_id'], 'completed', {'summary': 'done'})
            sessions.finish(sid, session['generation'], message['turn_id'], success=True)
            with sessions.db.transaction(write=True) as c:
                c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (sid,))

    def kill_owner(self, sid, **columns):
        """A crashed or rebooted-away owner: its PID no longer names a live process."""
        from lib.control.session_store import SessionStore
        values = dict(owner_pid=2 ** 22 + 7, owner_identity='boot:gone:1', owner_launch_attempts=0,
                      owner_launch_after=0, **columns)
        with SessionStore(self.config) as sessions, sessions.db.transaction(write=True) as c:
            c.execute('UPDATE managed_sessions SET ' + ','.join(k + '=?' for k in values) + ' WHERE session_id=?',
                      (*values.values(), sid))

    def cli(self, *argv, env=None):
        from lib.control import sessions
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sessions.main(list(argv), env=self.env if env is None else env)
        return code, out.getvalue(), err.getvalue()


class OwnerStartTests(OwnerStartFixture):
    def test_a_structured_launch_starts_its_owner_before_returning(self):
        caller = dict(self.env, TMUX='/tmp/t/default,1,0', TMUX_PANE='%3', ASHA_HUB_SESSION_ID=str(uuid.uuid4()),
                      ASHA_HUB_GENERATION='4', ASHA_ROOM_ID=str(uuid.uuid4()), ASHA_ROOM_INPUT_FENCE='fence',
                      ASHA_CONTROL_MANAGED_HINT='x', ASHA_ORCHESTRATION_ROLE='x', ASHA_COORDINATOR_LAUNCH='1',
                      PYTHONPATH=str(self.project), PYTHONHOME='/nowhere', PYTHONSTARTUP='/nowhere/x.py',
                      PYTHONSAFEPATH='', PYTHONUSERBASE='/nowhere', PYTHONWARNINGS='error')
        from lib.control.session_hub import Hub
        self.hub = Hub(self.config, env=caller, tmux=self.tmux)
        row = self.structured()
        self.assertEqual(self.owner_launches(), [row['session_id']])
        words, kwargs = self.launches[0]
        # Isolated (-I): no PYTHONPATH, user site or cwd on sys.path. The
        # bootstrap imports Control from this checkout's lib/ alone, as
        # lib/control.sh does for the router.
        self.assertEqual(words, [sys.executable, '-B', '-I', '-c',
                                 'import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); '
                                 'runpy.run_module("control.sessions", run_name="__main__")',
                                 str(ROOT / 'lib'), 'owner', row['session_id']])
        # Detached: its own session, no terminal input, cwd at the checkout root.
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdin'], __import__('subprocess').DEVNULL)
        self.assertEqual(Path(kwargs['cwd']), ROOT)
        child_env = kwargs['env']
        self.assertEqual(child_env['ASHA_HOME'], str(self.config.asha_home))
        for name in ('TMUX', 'TMUX_PANE', 'ASHA_HUB_SESSION_ID', 'ASHA_HUB_GENERATION', 'ASHA_ROOM_ID',
                     'ASHA_ROOM_INPUT_FENCE', 'ASHA_CONTROL_MANAGED_HINT', 'ASHA_ORCHESTRATION_ROLE',
                     'ASHA_COORDINATOR_LAUNCH'):
            self.assertNotIn(name, child_env)
        # Nor do the caller's Python startup variables reach it or its harness.
        self.assertEqual([k for k in child_env if k.startswith('PYTHON')], [])
        self.assertIsNone(self.hub.get(row['session_id'])['runtime_warning'])

    def test_a_send_to_an_idle_structured_session_starts_its_owner(self):
        sid = self.structured()['session_id']
        self.run_one_turn(sid)
        self.hub.send(sid, 'Now draft the replies', key='followup')
        self.assertEqual(self.owner_launches(), [sid, sid])

    def test_a_live_owner_gets_no_second_owner(self):
        from lib.control.session_store import SessionStore
        sid = self.structured()['session_id']
        with SessionStore(self.config) as sessions:
            sessions.claim_owner(sid)  # this test process is the live owner
        self.hub.send(sid, 'More context', key='more')
        self.assertEqual(self.owner_launches(), [sid])

    def test_two_concurrent_sends_start_one_owner(self):
        from lib.control.session_hub import Hub
        sid = self.structured()['session_id']
        self.run_one_turn(sid)
        start = threading.Barrier(2)
        errors = []

        def send(key):
            try:
                hub = Hub(self.config, env=self.env, tmux=self.tmux)
                start.wait(timeout=5)
                hub.send(sid, 'Context ' + key, key=key)
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)
        threads = [threading.Thread(target=send, args=(key,)) for key in ('one', 'two')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(self.owner_launches(), [sid, sid])

    def test_the_launch_reservation_alone_admits_one_of_two_racing_starts(self):
        # Without the hub's per-session action lock (a raw structured
        # session, two CLIs at once) the database reservation still decides.
        from lib.control.session_store import SessionStore
        from lib.control.sessions import ensure_owners
        with SessionStore(self.config, create=True) as sessions:
            sid = sessions.create(cwd=str(self.project), prompt='Raw utility')['session_id']
        start = threading.Barrier(2)
        results = []

        def race():
            start.wait(timeout=5)
            results.append(ensure_owners(self.config, env=self.env, session_id=sid)['owners_started'])
        threads = [threading.Thread(target=race) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(self.owner_launches(), [sid])

    def test_raw_session_cli_create_send_resume_and_answer_start_owners(self):
        from lib.control.session_store import SessionStore
        code, out, err = self.cli('create', '--cwd', str(self.project), '--prompt', 'Raw utility', '--json')
        self.assertEqual(code, 0, err)
        sid = __import__('json').loads(out)['session_id']
        self.assertEqual(self.owner_launches(), [sid])
        self.run_one_turn(sid)
        self.assertEqual(self.cli('send', sid, '--key', 'next', '--text', 'Follow-up', '--json')[0], 0)
        self.assertEqual(self.owner_launches(), [sid] * 2)
        # A question parks the session; the answer is queued work.
        with SessionStore(self.config) as sessions:
            session = sessions.claim_owner(sid)
            message = sessions.claim_turn(sid, session['generation'])
            request = sessions.request(sid, message['turn_id'], 'Which tone?', request_id=str(uuid.uuid4()),
                                       generation=session['generation'])
            sessions.finish(sid, session['generation'], message['turn_id'], success=True)
            self.assertEqual(sessions.get(sid)['state'], 'waiting-input')
            with sessions.db.transaction(write=True) as c:
                c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (sid,))
        code, _, err = self.cli('answer', request['request_id'], '--digest', request['digest'], '--text', 'Quiet', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.owner_launches(), [sid] * 3)
        with SessionStore(self.config) as sessions:
            sessions.stop(sid)
            digest = sessions.recovery_digest(sessions.get(sid))
        code, _, err = self.cli('resume', sid, '--digest', digest, '--text', 'Continue', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.owner_launches(), [sid] * 4)


class RecoveryTests(OwnerStartFixture):
    def test_a_crashed_owner_with_queued_work_restarts_on_session_show(self):
        sid = self.structured()['session_id']
        self.run_one_turn(sid)
        self.hub.send(sid, 'Follow-up', key='followup')
        self.kill_owner(sid)
        self.launches.clear()
        code, _, err = self.cli('show', sid, '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.owner_launches(), [sid])

    def test_owners_lost_to_a_reboot_restart_on_session_list(self):
        from lib.control.session_store import SessionStore
        queued = self.structured()['session_id']
        self.kill_owner(queued)
        with SessionStore(self.config) as sessions:
            # Interrupted mid-turn: a new owner reconciles it to uncertain.
            running = sessions.create(cwd=str(self.project), prompt='Long job')['session_id']
            owner = sessions.claim_owner(running)
            sessions.claim_turn(running, owner['generation'])
            # Idle with nothing queued, and parked on an open question: no owner can act.
            idle = sessions.create(cwd=str(self.project), prompt='Done job')['session_id']
            parked = sessions.create(cwd=str(self.project), prompt='Asking job')['session_id']
            owner = sessions.claim_owner(parked)
            turn = sessions.claim_turn(parked, owner['generation'])
            sessions.request(parked, turn['turn_id'], 'Which?', request_id=str(uuid.uuid4()),
                             generation=owner['generation'])
            sessions.finish(parked, owner['generation'], turn['turn_id'], success=True)
            sessions.enqueue(parked, 'Unrelated context', key='context')
        self.run_one_turn(idle)
        for sid in (running, parked):
            self.kill_owner(sid)
        self.launches.clear()
        code, _, err = self.cli('list', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(self.owner_launches()), sorted([queued, running]))

    def test_reads_by_a_worker_or_managed_actor_never_start_owners(self):
        sid = self.structured()['session_id']
        self.kill_owner(sid)
        self.launches.clear()
        for extra in ({'ASHA_SESSION_PROFILE': 'worker'}, {'ASHA_MANAGED_SESSION_ID': sid}):
            env = dict(self.env, **extra)
            self.assertEqual(self.cli('list', '--json', env=env)[0], 0)
            self.assertEqual(self.cli('show', sid, '--json', env=env)[0], 0)
        self.assertEqual(self.owner_launches(), [])

    def assert_terminal_session_starts_no_owner(self, profile):
        row = self.hub.launch(project=str(self.project), prompt='Trim the games', name='Games', harness='claude',
                              profile=profile)
        self.hub.send(row['session_id'], 'Context', key='context')
        self.assertEqual(self.cli('show', row['session_id'], '--json')[0], 0)
        self.assertEqual(self.cli('list', '--json')[0], 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.tmux.created), 1)
        self.assertEqual(self.hub.show(row['session_id'])['transport'], 'terminal')

    def test_terminal_workers_never_start_owners(self):
        self.assert_terminal_session_starts_no_owner('worker')

    def test_rooms_never_start_owners(self):
        self.assert_terminal_session_starts_no_owner('room')

    def test_admission_resume_starts_owners_for_work_queued_while_paused(self):
        from lib.control.runtime import admission, set_admission
        set_admission(self.config, 'paused')
        row = self.structured()
        self.assertEqual(self.owner_launches(), [])
        self.assertEqual(row['reason'], 'Queued; runtime admission is paused')
        code, out, err = self.cli('admission', 'resume', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(admission(self.config)['mode'], 'running')
        self.assertEqual(self.owner_launches(), [row['session_id']])


class BulkOwnerStartTests(OwnerStartFixture):
    """One bulk call considers every eligible session, not only a first page (parity P2)."""

    def many(self, count, **columns):
        """``count`` raw structured sessions in a fixed created_at order."""
        from lib.control.session_store import SessionStore
        base = time.time() - 10_000
        with SessionStore(self.config, create=True) as sessions:
            sids = [sessions.create(cwd=str(self.project), prompt=f'Job {i}')['session_id'] for i in range(count)]
            with sessions.db.transaction(write=True) as c:
                for index, sid in enumerate(sids):
                    values = dict(created_at=base + index, **columns)
                    c.execute('UPDATE managed_sessions SET ' + ','.join(k + '=?' for k in values)
                              + ' WHERE session_id=?', (*values.values(), sid))
        return sids

    def set_columns(self, sids, **columns):
        from lib.control.session_store import SessionStore
        with SessionStore(self.config) as sessions, sessions.db.transaction(write=True) as c:
            for sid in sids:
                c.execute('UPDATE managed_sessions SET ' + ','.join(k + '=?' for k in columns)
                          + ' WHERE session_id=?', (*columns.values(), sid))

    def live_owner(self, sids):
        from lib.control.harness import process_identity
        self.set_columns(sids, owner_pid=os.getpid(), owner_identity=process_identity(os.getpid()))

    def test_admission_resume_starts_every_queued_session_past_the_first_hundred(self):
        from lib.control.runtime import set_admission
        set_admission(self.config, 'paused')
        sids = self.many(105)
        # Ineligible rows lead the order: five live owners, five launches
        # still in their reservation backoff.
        self.live_owner(sids[:5])
        self.set_columns(sids[5:10], owner_launch_attempts=1, owner_launch_after=time.time() + 300)
        code, out, err = self.cli('admission', 'resume', '--json')
        self.assertEqual(code, 0, err)
        self.assertIsNone(__import__('json').loads(out)['owner_warning'])
        self.assertEqual(sorted(self.owner_launches()), sorted(sids[10:]))
        # A second bulk call has nothing left to start.
        self.launches.clear()
        from lib.control.sessions import ensure_owners
        result = ensure_owners(self.config, env=self.env)
        self.assertEqual((result['owners_started'], self.launches), (0, []))

    def test_stop_intent_completes_for_every_ownerless_session_past_the_first_hundred(self):
        from lib.control.session_store import SessionStore
        from lib.control.sessions import ensure_owners
        sids = self.many(105, stop_requested=1)
        self.live_owner(sids[:5])  # a live owner completes its own stop
        ensure_owners(self.config, env=self.env)
        with SessionStore(self.config) as sessions:
            states = [sessions.get(sid)['state'] for sid in sids]
        self.assertEqual(states, ['queued'] * 5 + ['stopped'] * 100)
        self.assertEqual(self.launches, [])


class OwnerIsolationTests(unittest.TestCase):
    """A real owner process ignores the caller's Python startup environment (parity P1).

    The owner runs with the operator's authority outside any harness sandbox.
    A caller's PYTHONPATH may point into a project a sandboxed agent can
    write; the owner must import only the checkout's own Control package.
    """

    def setUp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.marker = self.root / 'project-code-ran'
        self.project = self.root / 'project'
        self.project.mkdir()
        plant = ('import pathlib\n'
                 'with pathlib.Path({marker!r}).open("a") as handle:\n'
                 '    handle.write({name!r} + " from the project ran in the owner\\n")\n')
        (self.project / 'sitecustomize.py').write_text(plant.format(marker=str(self.marker), name='sitecustomize'))
        (self.project / 'json.py').write_text(plant.format(marker=str(self.marker), name='json')
                                              + 'import os\nos._exit(73)\n')
        # No harness executable is reachable, and no turn will be claimed.
        self.env = {'HOME': str(self.root), 'ASHA_HOME': str(self.root / 'asha'), 'PATH': '/usr/bin:/bin'}
        self.config = load_config(self.env)
        from lib.control.session_store import SessionStore
        self.store = SessionStore(self.config, create=True)
        self.addCleanup(self.store.close)

    def lost_running_turn(self):
        """A session whose owner died mid-turn: a new owner only reconciles it."""
        sid = self.store.create(cwd=str(self.project), prompt='Long job')['session_id']
        owner = self.store.claim_owner(sid)
        self.store.claim_turn(sid, owner['generation'])
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET owner_pid=?,owner_identity='boot:gone:1' WHERE session_id=?",
                      (2 ** 22 + 7, sid))
        return sid, owner['generation']

    def test_a_poisoned_pythonpath_never_runs_project_code_in_the_owner(self):
        from lib.control import sessions
        sid, generation = self.lost_running_turn()
        poisoned = dict(self.env, PYTHONPATH=str(self.project), PYTHONSTARTUP=str(self.project / 'json.py'),
                        PYTHONUSERBASE=str(self.project))
        self.assertEqual(sessions.ensure_owners(self.config, env=poisoned, session_id=sid)['owners_started'], 1)
        child = sessions._OWNER_CHILDREN.pop(sid)
        code = child.wait(timeout=60)
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else '')
        self.assertEqual(code, 0, (self.config.tasks_dir.parent / 'session-logs' / (sid + '.log'))
                         .read_text(errors='replace')[-2000:])
        # The legitimate owner entry ran in that very process: it claimed the
        # session, reconciled the lost turn and handed custody back.
        session = self.store.get(sid)
        self.assertEqual((session['state'], session['generation'], session['owner_pid']),
                         ('uncertain', generation + 1, None))
        claims = [e['payload'] for e in self.store.snapshot(sid)['events'] if e['kind'] == 'owner-claimed']
        self.assertEqual(json.loads(claims[-1]) if isinstance(claims[-1], str) else claims[-1],
                         {'pid': child.pid, 'generation': generation + 1})

    def test_the_owner_interpreter_is_isolated_even_from_an_unscrubbed_environment(self):
        # Each layer holds alone: here the environment scrub is bypassed.
        from lib.control import sessions
        sid, generation = self.lost_running_turn()
        poisoned = dict(self.env, PYTHONPATH=str(self.project), ASHA_CONFIG=str(self.config.config_path))
        sessions._launch_owner(self.config, sid, poisoned)
        code = sessions._OWNER_CHILDREN.pop(sid).wait(timeout=60)
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else '')
        self.assertEqual(code, 0)
        self.assertEqual(self.store.get(sid)['generation'], generation + 1)


class OwnerCustodyTests(unittest.TestCase):
    """An owner hands custody back in the transaction that finds no runnable input."""

    def setUp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {'HOME': str(self.root), 'ASHA_HOME': str(self.root / 'asha')}
        self.config = load_config(self.env)
        from lib.control.session_store import SessionStore
        self.store = SessionStore(self.config, create=True)
        self.addCleanup(self.store.close)
        self.sid = self.store.create(cwd=str(self.root), prompt='First')['session_id']

    def transport(self, completed):
        class Transport:
            input_not_submitted = False

            def __init__(self, _argv, **_kwargs):
                pass

            def events(self, prompt, cancelled):
                completed.append(prompt.split('\n', 1)[0])
                yield 'completed', {'summary': 'ok'}
        return Transport

    def test_input_queued_as_the_owner_retires_is_run_by_that_owner(self):
        from lib.control.session_store import SessionStore
        from lib.control.sessions import run_owner
        real = SessionStore.claim_turn
        landed = []

        def claim_turn(store, sid, generation):
            message = real(store, sid, generation)
            if message is None and not landed:
                # A send lands after the owner's last claim, before it exits.
                landed.append(store.enqueue(sid, 'Late', key='late'))
            return message
        completed = []
        with mock.patch.object(SessionStore, 'claim_turn', claim_turn):
            self.assertEqual(run_owner(self.config, self.sid, env=self.env,
                                       transport_factory=self.transport(completed)), 0)
        self.assertEqual(completed, ['First', 'Late'])
        session = self.store.get(self.sid)
        self.assertEqual((session['state'], session['turns']), ('idle', 2))
        self.assertIsNone(session['owner_pid'])
        self.assertTrue(self.store.reserve_owner_launch(self.sid))

    def test_an_owner_waits_for_turn_capacity_instead_of_exiting(self):
        from lib.control.session_store import MAX_RUNNING_TURNS
        from lib.control.sessions import run_owner
        others = [self.store.create(cwd=str(self.root), prompt='Busy')['session_id'] for _ in range(MAX_RUNNING_TURNS)]
        for other in others:
            owner = self.store.claim_owner(other)
            self.store.claim_turn(other, owner['generation'])
        completed, seen_while_full = [], []

        def free_capacity():
            from lib.control.session_store import SessionStore
            time.sleep(1.5)
            seen_while_full.extend(completed)
            with SessionStore(self.config) as store, store.db.transaction(write=True) as c:
                c.execute("UPDATE session_turns SET state='completed' WHERE session_id IN (?,?)", tuple(others))
        helper = threading.Thread(target=free_capacity)
        started = time.monotonic()
        helper.start()
        try:
            # run_owner installs signal handlers, so it runs on this thread.
            self.assertEqual(run_owner(self.config, self.sid, env=self.env,
                                       transport_factory=self.transport(completed)), 0)
        finally:
            helper.join(timeout=10)
        self.assertGreaterEqual(time.monotonic() - started, 1.5, 'owner exited while its input waited for capacity')
        self.assertEqual(seen_while_full, [])
        self.assertEqual(completed, ['First'])

    def test_a_busy_release_keeps_custody_and_retries(self):
        from lib.control.database import DatabaseBusyError
        from lib.control.session_store import SessionStore
        from lib.control.sessions import run_owner
        real = SessionStore.release_owner
        attempts = []

        def release_owner(store, sid, generation):
            attempts.append(sid)
            if len(attempts) == 1:
                raise DatabaseBusyError('Control database is busy: begin: database is locked')
            return real(store, sid, generation)
        completed = []
        with mock.patch.object(SessionStore, 'release_owner', release_owner):
            self.assertEqual(run_owner(self.config, self.sid, env=self.env,
                                       transport_factory=self.transport(completed)), 0)
        self.assertEqual((completed, len(attempts)), (['First'], 2))
        self.assertIsNone(self.store.get(self.sid)['owner_pid'])

    def test_release_keeps_custody_only_while_input_is_runnable(self):
        from lib.control.store import StoreError
        owner = self.store.claim_owner(self.sid)
        self.assertFalse(self.store.release_owner(self.sid, owner['generation']))
        self.assertEqual(self.store.get(self.sid)['owner_pid'], os.getpid())
        with self.assertRaisesRegex(StoreError, 'stale or foreign'):
            self.store.release_owner(self.sid, owner['generation'] + 1)
        turn = self.store.claim_turn(self.sid, owner['generation'])
        self.store.finish(self.sid, owner['generation'], turn['turn_id'], success=True)
        self.assertTrue(self.store.release_owner(self.sid, owner['generation']))
        session = self.store.get(self.sid)
        self.assertIsNone(session['owner_pid'])
        self.assertIn('owner-released', [e['kind'] for e in self.store.snapshot(self.sid)['events']])

    def test_a_new_owner_reconciles_a_turn_its_crashed_predecessor_left_running(self):
        from lib.control.sessions import run_owner
        owner = self.store.claim_owner(self.sid)
        self.store.claim_turn(self.sid, owner['generation'])
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET owner_pid=?,owner_identity='boot:gone:1' WHERE session_id=?",
                      (2 ** 22 + 7, self.sid))
        self.store.enqueue(self.sid, 'Queued behind the lost turn', key='next')
        completed = []
        self.assertEqual(run_owner(self.config, self.sid, env=self.env,
                                   transport_factory=self.transport(completed)), 0)
        session = self.store.get(self.sid)
        self.assertEqual(session['state'], 'uncertain')
        self.assertEqual(completed, [])  # never a blind replay
        self.assertIsNone(session['owner_pid'])


if __name__ == '__main__':
    unittest.main()
