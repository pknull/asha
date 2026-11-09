"""#130: a session publishes a nested project's Memory (Keeper ruling, revised 2026-11-03).

`asha control session handoff --component PATH` (and `--read --component PATH`)
publishes to a Memory v2 project nested inside the acting session's own
project. The folder structure decides; no manifest is read. The Keeper's
rules, each pinned below: PATH's real path lies strictly inside the session's
own root (never the root, an ancestor, a sibling or a link out); no folder of
that real path below the root, PATH's own included, is hidden (the spelling is
never judged); PATH is its own Memory v2 project with a different `project_id`;
any depth is allowed; the row is recorded under the nested project's
`project_id` and never makes the session read as saved; either plane silenced
refuses; nothing invokes Git. Review r2's findings are pinned too: the
publication stays inside the directory first authorized when its name is
renamed or replaced (ComponentReplacementTests), keeps the identity first
authorized, and `--read --component` refuses the options it would ignore.

The fixture still writes the first ruling's `.asha/workspace.json` so tests
whose behaviour did not change run identically on the pre-revision commit
(7c30f169); the code no longer reads it, and the changed-behaviour tests
either leave their projects undeclared or remove the manifest.

Controller fixtures driving the real Hub handoff path; not native delivery proof.
"""
import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tests.python.test_control_session_closure import ACTIVE, DECISIONS, sha
from tests.python.test_session_publication_rows import PublicationRowFixture
from lib.control import session_closure as closure
from lib.control.session_closure import memory_v2
from lib.control.session_publication import latest_saved_at, saved_for_assignment
from lib.control.store import StoreError

# Distinct from the root's starting pair, so a root digest can never pass for a component's.
START_ACTIVE = "# Objective\nComponent start\n\n# State\nNew\n\n# Next\n- Begin\n\n# Blockers\n- None\n"
START_DECISIONS = "# Decisions\n\n- No component decisions yet.\n"
CHILD_ACTIVE = "# Objective\nKeep the feed indexed\n\n# State\nIndexed\n\n# Next\n- Review\n\n# Blockers\n- None\n"
CHILD_DECISIONS = "# Decisions\n\n- Component Memory is published from the workspace Room.\n"
OTHER_ACTIVE = "# Objective\nThe component's own session\n\n# State\nSaved first\n\n# Next\n- Keep\n\n# Blockers\n- None\n"
OTHER_DECISIONS = "# Decisions\n\n- The component's own save landed first.\n"


class ComponentFixture(PublicationRowFixture):
    """A thallus-like tree: the fixture project is the root, with nested v2 projects (and the old manifest)."""

    def setUp(self):
        super().setUp()
        self.egregore = self.component('egregore', 'egregore-project')
        self.fle = self.component('research/fle-ale', 'fle-ale-project')
        self.scry = self.component('scry', 'scry-project')
        self.declare('egregore', 'research/fle-ale', 'scry')

    def component(self, rel, project_id, *, config=True, base=None):
        path = (base or self.project) / rel
        (path / 'Memory').mkdir(parents=True)
        if config:
            (path / '.asha').mkdir()
            (path / '.asha/config.json').write_text(json.dumps({
                'initialized': True, 'memory_version': 2, 'project_id': project_id, 'name': rel}))
        (path / 'Memory/activeContext.md').write_text(START_ACTIVE)
        (path / 'Memory/decisions.md').write_text(START_DECISIONS)
        return path

    def declare(self, *paths):
        (self.project / '.asha/workspace.json').write_text(json.dumps({
            'version': 1, 'workspace_name': 'thallus', 'repositories': [{'path': p} for p in paths]}))

    def child_digests(self, child):
        return {name: sha((child / 'Memory' / name).read_text()) for name in closure.MEMORY_FILES}

    def contents(self, child):
        return tuple((child / 'Memory' / name).read_text() for name in closure.MEMORY_FILES)

    def inodes(self, child):
        """An atomic replacement gives a file a new inode, so unchanged inodes mean it was never rewritten."""
        return tuple(os.stat(child / 'Memory' / name).st_ino for name in closure.MEMORY_FILES)

    def child_drafts(self, active=CHILD_ACTIVE, decisions=CHILD_DECISIONS):
        active_file, decisions_file = self.root / 'component-active.draft', self.root / 'component-decisions.draft'
        active_file.write_text(active)
        decisions_file.write_text(decisions)
        return str(active_file), str(decisions_file)

    def room(self):
        return self.launch(profile='room')['session_id']

    def publish_component(self, sid, path, *, child=None, expected=None, request=None, detail=None):
        active, decisions = self.child_drafts()
        if expected is None:
            expected = self.child_digests(child or self.egregore)
        with self.acting_as(sid):
            return self.hub.handoff(request, component=path, active_file=active, decisions_file=decisions,
                                    expected=expected, detail=detail)

    def read_component(self, sid, path):
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            return self.hub.handoff_read(component=path)

    def silence(self, plane):
        marker = plane / 'Work/markers/silence'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        self.addCleanup(lambda: marker.unlink(missing_ok=True))

    @contextmanager
    def child_lock_held(self, child):
        """Hold the component's publication lock as its own in-flight save would; yield once the Room waits on it.

        The signal is the publisher's own exclusive ``flock`` on that lock file's inode, however it opened
        the file (by path or through a directory descriptor), so the Room cannot pass the lock before
        ``release``: a change made before ``release`` always precedes the locked recheck, with no sleep.
        """
        lock = closure.secure_path(child, 'Work/session-state/.memory-publication.lock', create_parents=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        real_flock = fcntl.flock
        real_flock(fd, fcntl.LOCK_EX)
        held = os.fstat(fd)
        waiting = threading.Event()

        def watched(other, operation):
            if other != fd and operation & fcntl.LOCK_EX:
                metadata = os.fstat(other)
                if (metadata.st_dev, metadata.st_ino) == (held.st_dev, held.st_ino):
                    waiting.set()
            return real_flock(other, operation)
        try:
            with mock.patch.object(fcntl, 'flock', watched):
                yield waiting, lambda: real_flock(fd, fcntl.LOCK_UN)
        finally:
            real_flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def before_open(self, prefix, change):
        """Run ``change`` once, just before the publisher opens a file whose name starts with ``prefix``.

        A scheduling hook, not a sleep. Either publisher opens its lock, journal and Memory temporaries
        through ``os.open`` (the path-based one by way of ``tempfile.mkstemp``), with the same names, so
        the hook lands at the same step of the transaction in both.
        """
        real_open = os.open
        fired = []

        def scheduled(path, flags, mode=0o777, *args, **kwargs):
            if not fired and os.path.basename(os.fsdecode(path)).startswith(prefix):
                fired.append(os.fsdecode(path))
                change()
            return real_open(path, flags, mode, *args, **kwargs)
        with mock.patch.object(os, 'open', scheduled):
            yield fired

    def destination(self, kind, n):
        """A directory a replaced component could be redirected into, with Memory distinct from START."""
        if kind == 'acting-root':
            target = self.project
        else:
            rel, base = {'outside': (f'outside-{n}', self.root), 'hidden': (f'.trash/hidden-{n}', None),
                         'nested': (f'nested-{n}', None)}[kind]
            target = self.component(rel, f'{kind}-{n}-project', base=base)
            (target / 'Memory/activeContext.md').write_text(OTHER_ACTIVE)
            (target / 'Memory/decisions.md').write_text(OTHER_DECISIONS)
        (target / 'Work/session-state').mkdir(parents=True, exist_ok=True)
        return target

    def in_thread(self, call):
        outcome = []

        def run():
            try:
                outcome.append(call())
            except BaseException as exc:   # surfaced by the test's assertions
                outcome.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        return worker, outcome


class ComponentPublicationTests(ComponentFixture):
    def test_nested_project_publishes_under_its_own_project_id(self):
        """O1: the child's bytes land, the row names the child, the Room stays unsaved."""
        sid = self.room()
        root_before = self.digests()
        result = self.publish_component(sid, 'egregore')
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertEqual(self.digests(), root_before)
        self.assertEqual(result['outcome'], 'published')
        self.assertIs(result['git_invoked'], False)
        self.assertEqual(result['hub_publication_status'], 'recorded')
        self.assertEqual(result['component']['path'], 'egregore')
        self.assertEqual(result['component']['project_id'], 'egregore-project')
        self.assertEqual(result['component']['destination'], str(self.egregore / 'Memory'))
        self.assertIs(result['counts_as_session_save'], False)
        row, = self.rows(sid)
        self.assertEqual(row['generation'], 1)
        self.assertEqual(row['project_id'], 'egregore-project')
        receipt = row['receipt']
        self.assertEqual(receipt['project_id'], 'egregore-project')
        self.assertEqual(receipt['destination_project_id'], 'egregore-project')
        self.assertEqual(receipt['acting_project_id'], 'novel-project')
        self.assertEqual(receipt['component'], 'egregore')
        self.assertEqual(receipt['hub_session_id'], sid)
        self.assertEqual(receipt['hub_generation'], 1)
        self.assertEqual(receipt['destination'], str(self.egregore / 'Memory'))
        current = self.hub.get(sid)
        self.assertIsNone(self.hub.show(sid)['memory_saved_at'])
        self.assertIsNone(latest_saved_at(self.hub, current))
        self.assertFalse(saved_for_assignment(self.hub, current))
        self.assertNotEqual(self.hub.get(sid)['lifecycle'], 'closed')   # the Room stays open

    def test_absolute_path_names_the_same_nested_project(self):
        """A nested path two levels down (life's research/fle-ale) named by its absolute path."""
        sid = self.room()
        result = self.publish_component(sid, str(self.fle), child=self.fle)
        self.assertEqual(self.contents(self.fle), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertEqual(result['component']['path'], 'research/fle-ale')
        row, = self.rows(sid)
        self.assertEqual(row['project_id'], 'fle-ale-project')

    def test_read_component_reports_the_components_digests(self):
        sid = self.room()
        facts = self.read_component(sid, 'egregore')
        self.assertEqual(facts['session_id'], sid)
        self.assertTrue(facts['memory']['available'])
        self.assertEqual(facts['memory']['destination'], str(self.egregore / 'Memory'))
        self.assertEqual(facts['memory']['baseline'], self.child_digests(self.egregore))
        self.assertNotEqual(facts['memory']['baseline'], self.digests())
        self.assertEqual(facts['paths']['decisions.md'], str(self.egregore / 'Memory/decisions.md'))
        self.assertEqual(facts['component'], {'path': 'egregore', 'project_id': 'egregore-project',
                                              'acting_root': str(self.project)})
        self.assertIsNone(facts['request_id'])

    def test_single_project_handoff_is_unchanged(self):
        sid = self.room()
        active, decisions = self.drafts()
        with self.acting_as(sid):
            result = self.hub.handoff(None, active_file=active, decisions_file=decisions, expected=self.digests())
        self.assertEqual(result['hub_publication_status'], 'recorded')
        self.assertNotIn('component', result)
        row, = self.rows(sid)
        self.assertEqual(row['project_id'], 'novel-project')
        self.assertNotIn('destination_project_id', row['receipt'])
        self.assertIsNotNone(self.hub.show(sid)['memory_saved_at'])
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))

    def test_component_flag_publishes_only(self):
        """No attestation or blocker for a component: those speak for the session's own plane."""
        sid = self.room()
        active, decisions = self.child_drafts()
        for outcome in ('no-durable-update', 'blocked', 'failed'):
            for files in ({}, dict(active_file=active, decisions_file=decisions, expected=self.child_digests(self.egregore))):
                with self.subTest(outcome=outcome, files=bool(files)), self.acting_as(sid), \
                        self.assertRaisesRegex(StoreError, 'needs both draft files and no other outcome'):
                    self.hub.handoff(None, component='egregore', outcome=outcome, detail='nothing', **files)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])


class ComponentOrderingTests(ComponentFixture):
    def test_root_save_between_read_and_publish_keeps_rows_apart(self):
        """O2: a root save changes no component digest; each row keeps its own project_id."""
        sid = self.room()
        expected = self.read_component(sid, 'egregore')['memory']['baseline']
        active, decisions = self.drafts()
        with self.acting_as(sid):
            self.hub.handoff(None, active_file=active, decisions_file=decisions, expected=self.digests())
        self.publish_component(sid, 'egregore', expected=expected)
        self.assertEqual(sorted(row['project_id'] for row in self.rows(sid)), ['egregore-project', 'novel-project'])
        self.assertEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))
        root_row, = [row for row in self.rows(sid) if row['project_id'] == 'novel-project']
        self.assertEqual(self.hub.show(sid)['memory_saved_at'], root_row['published_at'])

    def test_component_save_between_read_and_publish_refuses_the_stale_draft(self):
        """O3: the component's own session saved first; the Room's stale draft never overwrites it."""
        sid = self.room()
        expected = self.read_component(sid, 'egregore')['memory']['baseline']
        with mock.patch.dict(os.environ, self.env, clear=True):
            memory_v2.publish(self.egregore, OTHER_ACTIVE, OTHER_DECISIONS)
        with self.assertRaisesRegex(StoreError, 'preimage changed.*--read --component PATH --json.*then reread both of the '
                                                'nested project.s Memory files, copy them to a new preimage '
                                                'directory.*never only refresh'):
            self.publish_component(sid, 'egregore', expected=expected)
        self.assertEqual(self.contents(self.egregore), (OTHER_ACTIVE, OTHER_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_component_save_between_childs_read_and_its_own_save_refuses_the_childs_stale_draft(self):
        """O3 reversed (#136): the parent's --component save lands first; the child's own stale save, plain or
        answering its close, never overwrites it, and the close stays pending (visible, not lost)."""
        parent, child = self.room(), self.launch(project=str(self.egregore), profile='room')['session_id']
        rid = self.hub.request_close(child, wait=60)['closure']['request_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(child)):
            expected = self.hub.handoff_read()['memory']['baseline']
        self.publish_component(parent, 'egregore')
        active, decisions = self.drafts(OTHER_ACTIVE, OTHER_DECISIONS)
        for request in (None, rid):
            with self.subTest(request=request), self.acting_as(child), self.assertRaisesRegex(
                    StoreError, 'preimage changed.*handoff --read --json.*then reread both Memory files.*never only refresh'):
                self.hub.handoff(request, active_file=active, decisions_file=decisions, expected=expected)
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertEqual(self.rows(child), [])
        row = self.hub.get(child)
        self.assertTrue(closure.pending(row['closure'], row))
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(child)):
            fresh = self.hub.handoff_read()['memory']['baseline']   # the retry: digests again, then reread and merge
        with self.acting_as(child):
            self.hub.handoff(rid, active_file=active, decisions_file=decisions, expected=fresh)
        self.assertEqual([row['receipt']['source'] for row in self.rows(child)], ['close'])

    def test_component_save_landing_while_the_room_waits_on_the_lock_wins(self):
        """O4: the compare runs under the component's lock, after the in-flight save."""
        sid = self.room()
        expected = self.child_digests(self.egregore)
        with self.child_lock_held(self.egregore) as (waiting, release):
            worker, outcome = self.in_thread(lambda: self.publish_component(sid, 'egregore', expected=expected))
            self.assertTrue(waiting.wait(10))
            time.sleep(0.1)
            memory_v2.atomic_write(self.egregore / 'Memory/activeContext.md', OTHER_ACTIVE)
            memory_v2.atomic_write(self.egregore / 'Memory/decisions.md', OTHER_DECISIONS)
            release()
            worker.join(20)
        self.assertIsInstance(outcome[0], StoreError)
        self.assertIn('preimage changed', str(outcome[0]))
        self.assertEqual(self.contents(self.egregore), (OTHER_ACTIVE, OTHER_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def refused_before_publication(self, plane):
        sid = self.room()
        expected = self.read_component(sid, 'egregore')['memory']['baseline']
        self.silence(plane)
        with self.assertRaisesRegex(StoreError, 'silence'):
            self.publish_component(sid, 'egregore', expected=expected)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_root_silenced_before_publication_refuses(self):
        """O5, workspace-root plane."""
        self.refused_before_publication(self.project)

    def test_component_silenced_before_publication_refuses(self):
        """O5, component plane."""
        self.refused_before_publication(self.egregore)

    def refused_while_waiting_on_the_lock(self, plane):
        sid = self.room()
        expected = self.child_digests(self.egregore)
        with self.child_lock_held(self.egregore) as (waiting, release):
            worker, outcome = self.in_thread(lambda: self.publish_component(sid, 'egregore', expected=expected))
            self.assertTrue(waiting.wait(10))
            time.sleep(0.1)
            self.silence(plane)
            release()
            worker.join(20)
        self.assertIsInstance(outcome[0], StoreError)
        self.assertIn('silence', str(outcome[0]))
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_root_silenced_while_waiting_on_the_lock_refuses(self):
        """O6, workspace-root plane: rechecked under the component's lock, just before the write."""
        self.refused_while_waiting_on_the_lock(self.project)

    def test_component_silenced_while_waiting_on_the_lock_refuses(self):
        """O6, component plane."""
        self.refused_while_waiting_on_the_lock(self.egregore)

    def test_read_component_reports_silence_of_either_plane(self):
        sid = self.room()
        for plane in (self.project, self.egregore):
            with self.subTest(plane=str(plane)):
                marker = plane / 'Work/markers/silence'
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
                try:
                    memory = self.read_component(sid, 'egregore')['memory']
                finally:
                    marker.unlink()
                self.assertFalse(memory['available'])
                self.assertIn('silence', memory['reason'])

    def test_close_request_is_never_answered_by_a_component_save(self):
        """O8: --component cannot name a request, and a component row never ends the close."""
        sid = self.room()
        rid = self.hub.request_close(sid, wait=60)['closure']['request_id']
        with self.assertRaisesRegex(StoreError, 'close request'):
            self.publish_component(sid, 'egregore', request=rid)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.publish_component(sid, 'egregore')
        row = self.hub.get(sid)
        self.assertEqual(row['lifecycle'], 'closing')
        self.assertIsNone(latest_saved_at(self.hub, row, since=row['closure']['requested_at']))
        with mock.patch.object(self.hub, '_live', return_value=False):
            closed = self.hub.await_close(sid, rid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertIsNone(self.hub.get(sid)['closure']['saved_at'])

    def refused_after_change_while_waiting(self, change, pattern, child):
        """The guard's authority recheck: ``change`` lands while the Room waits on the component's lock."""
        sid = self.room()
        expected = self.child_digests(self.egregore)
        inodes = self.inodes(self.egregore)
        with self.child_lock_held(self.egregore) as (waiting, release):
            worker, outcome = self.in_thread(lambda: self.publish_component(sid, 'egregore', expected=expected))
            self.assertTrue(waiting.wait(10))
            time.sleep(0.1)
            change()
            release()
            worker.join(20)
        self.assertIsInstance(outcome[0], StoreError)
        self.assertRegex(str(outcome[0]), pattern)
        self.assertEqual(self.contents(child), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.inodes(child), inodes)   # refused under the lock, before anything was written
        self.assertEqual(self.rows(sid), [])

    def test_component_deinitialized_while_waiting_on_the_lock_refuses(self):
        self.refused_after_change_while_waiting(lambda: (self.egregore / '.asha/config.json').unlink(),
                                                'Memory v2', self.egregore)

    def test_component_moved_under_a_hidden_folder_while_waiting_on_the_lock_refuses(self):
        hidden = self.project / '.trash' / 'egregore'

        def move():
            hidden.parent.mkdir()
            os.rename(self.egregore, hidden)
            os.symlink(hidden, self.egregore)
        self.refused_after_change_while_waiting(move, 'hidden', hidden)

    def test_component_swapped_for_an_outside_link_while_waiting_on_the_lock_refuses(self):
        moved = self.root / 'moved-egregore'

        def swap():
            os.rename(self.egregore, moved)
            os.symlink(moved, self.egregore)
        self.refused_after_change_while_waiting(swap, 'outside', moved)

    def test_guard_binds_the_directory_the_publisher_holds(self):
        """The check refuses a binding that is not the authorized component, before anything is created there."""
        outside = self.component('outside-repo', 'outside-project', base=self.root)
        sid = self.room()
        bind = memory_v2.BoundProject

        def bound_elsewhere(_anchor, _parts):
            return bind(self.root, ('outside-repo',))
        with mock.patch.object(memory_v2, 'BoundProject', bound_elsewhere), \
                self.assertRaisesRegex(StoreError, 'changed during publication'):
            self.publish_component(sid, 'egregore')
        self.assertEqual(self.contents(outside), (START_ACTIVE, START_DECISIONS))
        self.assertFalse((outside / 'Work').exists())   # refused before the lock file or journal directory
        self.assertEqual(self.rows(sid), [])

    def test_invalid_detail_is_refused_before_writing(self):
        sid = self.room()
        for detail in (' ', 'x' * 4001, 'nul\x00byte'):
            with self.subTest(detail=detail[:8]), self.assertRaisesRegex(StoreError, 'invalid handoff detail'):
                self.publish_component(sid, 'egregore', detail=detail)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_component_deinitialized_after_read_refuses(self):
        """O10: authority is resolved again at publication, not reused from the read."""
        sid = self.room()
        expected = self.read_component(sid, 'egregore')['memory']['baseline']
        (self.egregore / '.asha/config.json').unlink()
        with self.assertRaisesRegex(StoreError, 'Memory v2'):
            self.publish_component(sid, 'egregore', expected=expected)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))


class ComponentReplacementTests(ComponentFixture):
    """Review r2 finding 1: the child directory renamed or replaced after the locked guard (O12-O18, O21).

    The publisher holds the directory the guard authorized and writes only inside it. If the name no
    longer leads there at the commit check, it restores that directory from its own journal and refuses:
    no write lands anywhere else, nothing is recorded, and the receipt never names a moved project.
    """
    KINDS = ('outside', 'acting-root', 'hidden', 'nested')
    COMMIT_REFUSAL = 'restored from its journal'

    def replaced(self, sid, prefix, kind, n, *, journal=False, pattern=COMMIT_REFUSAL):
        """Replace a fresh component with a link to ``kind`` just before the publisher opens ``prefix``."""
        child = self.component(f'child-{n}', f'child-{n}-project')
        destination = self.destination(kind, n)
        if journal:
            # A crashed earlier save: the journal holds START, the active file is half-written.
            memory_v2.prepare_publication_journal(child)
            (child / 'Memory/activeContext.md').write_text(OTHER_ACTIVE)
        expected = {'activeContext.md': sha(START_ACTIVE), 'decisions.md': sha(START_DECISIONS)}
        before, root_before = self.contents(destination), self.digests()
        moved = self.project / f'moved-{n}'

        def replace():
            os.rename(child, moved)
            os.symlink(destination, child, target_is_directory=True)
        with self.before_open(prefix, replace) as fired:
            refusal = self.refusal(lambda: self.publish_component(sid, f'child-{n}', expected=expected))
        self.assertTrue(fired, 'the scheduled replacement never ran')
        self.assertIsNotNone(refusal, 'published although its directory was replaced')
        self.assertRegex(str(refusal), pattern)
        self.assertEqual(self.contents(destination), before)
        if kind != 'acting-root':
            self.assertEqual(self.digests(), root_before)
        self.assertEqual(self.contents(moved), (START_ACTIVE, START_DECISIONS))
        self.assertFalse((moved / 'Work/session-state/.memory-publication-transaction.json').exists())
        self.assertFalse((destination / 'Work/session-state/.memory-publication-transaction.json').exists())
        self.assertEqual(self.rows(sid), [])

    @staticmethod
    def refusal(call):
        try:
            call()
        except StoreError as exc:
            return exc
        return None

    def each_kind(self, prefix, **options):
        sid = self.room()   # one Room per test: the fixture's tmux owns one session
        for n, kind in enumerate(self.KINDS):
            with self.subTest(destination=kind):
                self.replaced(sid, prefix, kind, n, **options)

    def test_replaced_before_the_lock_is_opened_refuses(self):
        """O12: refused by the locked check (already so at dd051e7b); nothing is written anywhere."""
        self.each_kind('.memory-publication.lock', pattern=r'\S')   # any refusal

    def test_replaced_before_the_journal_is_prepared_writes_nothing_elsewhere(self):
        """O13."""
        self.each_kind('..memory-publication-transaction.json.tmp.')   # "." + the journal's own dotted name

    def test_replaced_before_the_active_write_writes_nothing_elsewhere(self):
        """O14, the reviewer's case, for another project, the acting root, a hidden project and a nested one."""
        self.each_kind('.activeContext.md.tmp.')

    def test_replaced_between_the_two_writes_writes_nothing_elsewhere(self):
        """O15: the active file already landed in the held directory; both are restored."""
        self.each_kind('.decisions.md.tmp.')

    def test_replaced_while_recovering_a_prepared_journal_writes_nothing_elsewhere(self):
        """O16: recovery restores the held directory, never the link's target."""
        self.each_kind('.activeContext.md.tmp.', journal=True)

    def test_renamed_away_with_a_stand_in_directory_writes_nothing_into_it(self):
        """O17: the project moved under .trash/ and a plain directory with an empty Memory/ took its name."""
        sid = self.room()
        hidden = self.project / '.trash/egregore'

        def move():
            hidden.parent.mkdir()
            os.rename(self.egregore, hidden)
            (self.egregore / 'Memory').mkdir(parents=True)
        with self.before_open('.activeContext.md.tmp.', move) as fired:
            refusal = self.refusal(lambda: self.publish_component(sid, 'egregore'))
        self.assertTrue(fired)
        self.assertIsNotNone(refusal, 'published into the stand-in')
        self.assertRegex(str(refusal), self.COMMIT_REFUSAL)
        self.assertEqual(sorted(os.listdir(self.egregore / 'Memory')), [])
        self.assertEqual(self.contents(hidden), (START_ACTIVE, START_DECISIONS))
        self.assertFalse((hidden / 'Work/session-state/.memory-publication-transaction.json').exists())
        self.assertEqual(self.rows(sid), [])

    def test_renamed_away_mid_publication_restores_the_project_and_refuses(self):
        """O17, no stand-in: refused at dd051e7b too, but the moved project kept a half-written pair and its journal."""
        sid = self.room()
        hidden = self.project / '.trash/egregore'

        def move():
            hidden.parent.mkdir()
            os.rename(self.egregore, hidden)
        with self.before_open('.decisions.md.tmp.', move) as fired:
            refusal = self.refusal(lambda: self.publish_component(sid, 'egregore'))
        self.assertTrue(fired)
        self.assertIsNotNone(refusal)
        self.assertFalse(self.egregore.exists())   # nothing was created at the old name
        self.assertEqual(self.contents(hidden), (START_ACTIVE, START_DECISIONS))
        self.assertFalse((hidden / 'Work/session-state/.memory-publication-transaction.json').exists())
        self.assertEqual(self.rows(sid), [])

    def test_silenced_during_the_writes_restores_and_refuses(self):
        """O21: the commit check is the guard again, so silence set before it refuses and restores."""
        sid = self.room()
        for plane in ('root', 'component'):
            with self.subTest(plane=plane):
                child = self.component(f'quiet-{plane}', f'quiet-{plane}-project')
                target = self.project if plane == 'root' else child
                try:
                    with self.before_open('.decisions.md.tmp.', lambda: self.silence(target)) as fired:
                        refusal = self.refusal(lambda: self.publish_component(sid, f'quiet-{plane}', child=child))
                finally:
                    (target / 'Work/markers/silence').unlink(missing_ok=True)
                self.assertTrue(fired)
                self.assertIsNotNone(refusal, 'published although silenced during the writes')
                self.assertRegex(str(refusal), 'silence')
                self.assertEqual(self.contents(child), (START_ACTIVE, START_DECISIONS))
                self.assertFalse((child / 'Work/session-state/.memory-publication-transaction.json').exists())
                self.assertEqual(self.rows(sid), [])

    def test_the_lock_held_through_the_writes_is_the_authorized_directorys_own(self):
        """O12b: the name leads elsewhere while the lock is opened and back before the locked check.

        The publication may go ahead (the name leads to the directory again), but only under that
        directory's own lock, so the project's own saves stay serialized with it.
        """
        sid = self.room()
        other = self.destination('nested', 9)
        moved = self.project / 'moved-egregore'
        real_flock = fcntl.flock
        swapped, lock_free = [], []

        def away():
            os.rename(self.egregore, moved)
            os.symlink(other, self.egregore, target_is_directory=True)
            swapped.append(True)

        def back_after_locking(fd, operation):
            result = real_flock(fd, operation)
            if swapped == [True] and operation & fcntl.LOCK_EX:
                os.unlink(self.egregore)
                os.rename(moved, self.egregore)
                swapped.append(False)
            return result

        def probe_the_lock():
            fd = os.open(self.egregore / 'Work/session-state/.memory-publication.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                real_flock(fd, fcntl.LOCK_UN)
                lock_free.append(True)
            except BlockingIOError:
                lock_free.append(False)
            finally:
                os.close(fd)
        with self.before_open('.memory-publication.lock', away), \
                mock.patch.object(fcntl, 'flock', back_after_locking), \
                self.before_open('.activeContext.md.tmp.', probe_the_lock):
            self.publish_component(sid, 'egregore')
        self.assertEqual(swapped, [True, False])
        self.assertEqual(lock_free, [False], "the authorized directory's lock was free during the writes")
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertEqual(self.contents(other), (OTHER_ACTIVE, OTHER_DECISIONS))
        self.assertEqual([row['project_id'] for row in self.rows(sid)], ['egregore-project'])

    def test_memory_folder_replaced_mid_publication_restores_and_refuses(self):
        """O15b: the held Memory/ renamed and a stand-in made in its place, between the two writes."""
        sid = self.room()
        old = self.egregore / 'Memory.old'

        def swap_memory():
            os.rename(self.egregore / 'Memory', old)
            (self.egregore / 'Memory').mkdir()
        with self.before_open('.decisions.md.tmp.', swap_memory) as fired:
            refusal = self.refusal(lambda: self.publish_component(sid, 'egregore'))
        self.assertTrue(fired)
        self.assertIsNotNone(refusal, 'published while the name led to another Memory folder')
        self.assertRegex(str(refusal), 'Memory was replaced during publication')
        self.assertEqual(os.listdir(self.egregore / 'Memory'), [])
        self.assertEqual(tuple((old / name).read_text() for name in closure.MEMORY_FILES),
                         (START_ACTIVE, START_DECISIONS))
        self.assertFalse((self.egregore / 'Work/session-state/.memory-publication-transaction.json').exists())
        self.assertEqual(self.rows(sid), [])

    def test_a_crashed_component_publication_is_recovered_by_the_projects_own_publisher(self):
        """The bound writer keeps publish()'s journal: a kill between the two writes is recovered by path (pin)."""
        class Killed(BaseException):
            pass

        def kill():
            raise Killed()
        sid = self.room()
        with self.before_open('.decisions.md.tmp.', kill), self.assertRaises(Killed):
            self.publish_component(sid, 'egregore')
        journal = self.egregore / 'Work/session-state/.memory-publication-transaction.json'
        self.assertTrue(journal.exists())
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, START_DECISIONS))
        self.assertTrue(memory_v2.recover_publication(self.egregore))
        self.assertFalse(journal.exists())
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def changed_identity_while_waiting(self, new_id, pattern):
        sid = self.room()
        expected = self.child_digests(self.egregore)
        config = self.egregore / '.asha/config.json'
        inodes = self.inodes(self.egregore)
        with self.child_lock_held(self.egregore) as (waiting, release):
            worker, outcome = self.in_thread(lambda: self.publish_component(sid, 'egregore', expected=expected))
            self.assertTrue(waiting.wait(10))
            config.write_text(json.dumps(dict(json.loads(config.read_text()), project_id=new_id)))
            release()   # the publisher passes its lock only now, after the change
            worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome[0], StoreError)
        self.assertRegex(str(outcome[0]), pattern)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.inodes(self.egregore), inodes)   # refused under the lock, before anything was written
        self.assertEqual(self.rows(sid), [])
        self.assertIsNone(self.hub.show(sid)['memory_saved_at'])

    def test_reinitialized_with_another_project_id_while_waiting_refuses(self):
        """O19, review r2 finding 2: the save is never recorded under the identity first authorized."""
        self.changed_identity_while_waiting('replacement-child-project', 'project_id changed')

    def test_given_the_acting_project_id_while_waiting_refuses(self):
        """O20 (pin: refused at dd051e7b too)."""
        self.changed_identity_while_waiting('novel-project', "shares this session's project_id")


class ComponentAuthorityTests(ComponentFixture):
    def assert_refused(self, sid, path, pattern, child):
        before = self.contents(child)
        root_before = self.digests()
        with self.assertRaisesRegex(StoreError, pattern):
            self.publish_component(sid, path, child=child, expected=self.child_digests(child))
        with self.assertRaisesRegex(StoreError, pattern):
            self.read_component(sid, path)
        self.assertEqual(self.contents(child), before)
        self.assertEqual(self.digests(), root_before)
        self.assertEqual(self.rows(sid), [])

    def test_undeclared_nested_project_is_accepted(self):
        """Revised ruling: the manifest grants nothing; a nested v2 project at any depth qualifies."""
        familiar = self.component('familiar', 'familiar-project')
        feed = self.component('egregore/plugins/feed', 'feed-project')
        sid = self.room()
        self.publish_component(sid, 'familiar', child=familiar)
        self.publish_component(sid, str(feed), child=feed)
        self.assertEqual(self.contents(familiar), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertEqual(self.contents(feed), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertEqual([(row['project_id'], row['receipt']['component']) for row in self.rows(sid)],
                         [('familiar-project', 'familiar'), ('feed-project', 'egregore/plugins/feed')])
        self.assertIsNone(self.hub.show(sid)['memory_saved_at'])

    def test_project_without_a_manifest_publishes_to_its_nested_project(self):
        (self.project / '.asha/workspace.json').unlink()
        sid = self.room()
        result = self.publish_component(sid, 'egregore')
        self.assertEqual(result['component']['project_id'], 'egregore-project')
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))

    def test_malformed_manifest_is_ignored(self):
        (self.project / '.asha/workspace.json').write_text('{"version": 1, "repositories": [')
        sid = self.room()
        self.publish_component(sid, 'egregore')
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))

    def test_project_under_a_hidden_folder_is_refused(self):
        """Copies under .trash/, .git/, .claude/ and similar never qualify, even when a manifest declares them."""
        sid = self.room()
        copies = {rel: self.component(rel, rel.replace('/', '-').strip('.') + '-copy')
                  for rel in ('.trash/egregore', '.git/modules/scry', '.claude/worktrees/x', '.hidden-project')}
        os.symlink(self.project / '.trash/egregore', self.project / 'copy-link')   # a plain name, a hidden target
        self.declare('egregore', 'research/fle-ale', 'scry', *copies, 'copy-link')
        for rel, child in copies.items():
            with self.subTest(path=rel):
                self.assert_refused(sid, rel, 'hidden', child)
                self.assert_refused(sid, str(child), 'hidden', child)
        self.assert_refused(sid, 'copy-link', 'hidden', copies['.trash/egregore'])

    def test_hidden_spellings_are_judged_by_the_real_path(self):
        """Review r2 finding 3: the rule is the real folder structure, never PATH's spelling.

        Each spelling below resolves to the plain project ``egregore``, so each publishes there: a hidden
        name cancelled by ``..``, a hidden link cancelled by ``..``, a visible link whose chain passes
        through a hidden link, and a hidden link itself. (The plain-named link to a hidden real path is
        refused above.)
        """
        (self.project / '.hidden').mkdir()
        os.symlink(self.egregore, self.project / '.alias')
        os.symlink(self.project / '.alias', self.project / 'visible-alias')
        sid = self.room()
        spellings = ('.hidden/../egregore', '.alias/../egregore', 'visible-alias', '.alias')
        for count, spelling in enumerate(spellings, 1):
            with self.subTest(path=spelling):
                result = self.publish_component(sid, spelling)
                self.assertEqual(result['component']['path'], 'egregore')
                self.assertEqual(result['component']['destination'], str(self.egregore / 'Memory'))
                rows = self.rows(sid)
                self.assertEqual(len(rows), count)
                self.assertEqual({row['project_id'] for row in rows}, {'egregore-project'})
        self.assertEqual(self.contents(self.egregore), (CHILD_ACTIVE, CHILD_DECISIONS))
        self.assertIsNone(self.hub.show(sid)['memory_saved_at'])

    def test_project_under_another_plane_marker_is_refused(self):
        """A Control task marker between the root and the project (the retired engine left these) owns its plane."""
        marker = self.project / 'research/.asha/control-task.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('{"stale": true}')
        sid = self.room()
        self.assert_refused(sid, 'research/fle-ale', 'not its own Memory plane', self.fle)

    def test_link_resolving_outside_the_root_is_refused(self):
        outside = self.component('outside-repo', 'outside-project', base=self.root)
        os.symlink(outside, self.project / 'linked')
        sid = self.room()
        self.assert_refused(sid, 'linked', "outside this session's project root", outside)
        self.assert_refused(sid, str(outside), "outside this session's project root", outside)

    def test_dotdot_after_a_link_follows_the_link(self):
        """PATH names what the OS names: `linked/..` is the link target's parent, not the root."""
        outside = self.component('elsewhere/outside-repo', 'outside-project', base=self.root)
        os.symlink(outside, self.project / 'linked')
        sid = self.room()
        with self.assertRaisesRegex(StoreError, 'does not exist'):
            self.publish_component(sid, 'linked/../egregore')
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_link_to_the_root_itself_is_refused(self):
        os.symlink(self.project, self.project / 'self')
        sid = self.room()
        with self.assertRaisesRegex(StoreError, "session's own project root"):
            self.publish_component(sid, 'self', child=self.project, expected=self.digests())
        self.assertEqual(self.rows(sid), [])

    def test_explicit_save_publisher_is_not_widened(self):
        """Option B was declined: a workspace Room's explicit save still refuses a component plane."""
        sid = self.room()
        with mock.patch('lib.control.session_hub.Hub', return_value=self.hub), \
                mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)), \
                mock.patch.dict(os.environ, dict(self.env, ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='1'),
                                clear=True):
            with self.assertRaisesRegex(StoreError, 'outside this project plane'):
                memory_v2.publish(self.egregore, CHILD_ACTIVE, CHILD_DECISIONS)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_child_session_publishes_to_a_project_beneath_it(self):
        """A child is a project like any other: it may write down, never up or across (below)."""
        feed = self.component('egregore/plugins/feed', 'feed-project')
        sid = self.launch(project=str(self.egregore), profile='room')['session_id']
        result = self.publish_component(sid, 'plugins/feed', child=feed)
        self.assertEqual(self.contents(feed), (CHILD_ACTIVE, CHILD_DECISIONS))
        row, = self.rows(sid)
        self.assertEqual(row['project_id'], 'feed-project')
        self.assertEqual(row['receipt']['acting_project_id'], 'egregore-project')
        self.assertEqual(result['component']['acting_root'], str(self.egregore))

    def test_child_session_cannot_publish_to_its_ancestor(self):
        sid = self.launch(project=str(self.egregore), profile='room')['session_id']
        before = self.digests()
        with self.assertRaisesRegex(StoreError, "outside this session's project root"):
            self.publish_component(sid, str(self.project), child=self.project, expected=self.digests())
        self.assertEqual(self.digests(), before)
        self.assertEqual(self.rows(sid), [])

    def test_child_session_cannot_publish_to_a_sibling(self):
        sid = self.launch(project=str(self.egregore), profile='room')['session_id']
        for path in ('../scry', str(self.scry)):
            with self.subTest(path=path):
                with self.assertRaisesRegex(StoreError, "outside this session's project root"):
                    self.publish_component(sid, path, child=self.scry)
                self.assertEqual(self.contents(self.scry), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_component_sharing_the_root_project_id_is_refused(self):
        twin = self.component('twin', 'novel-project')
        spaced = self.component('spaced', ' novel-project ')
        self.declare('egregore', 'twin', 'spaced')
        sid = self.room()
        self.assert_refused(sid, 'twin', 'project_id', twin)
        self.assert_refused(sid, 'spaced', 'project_id', spaced)

    def test_child_link_to_a_sibling_is_refused(self):
        os.symlink(self.scry, self.egregore / 'scry-link')
        sid = self.launch(project=str(self.egregore), profile='room')['session_id']
        with self.assertRaisesRegex(StoreError, "outside this session's project root"):
            self.publish_component(sid, 'scry-link', child=self.scry)
        self.assertEqual(self.contents(self.scry), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_component_without_v2_config_is_refused(self):
        bare = self.component('bare', None, config=False)
        legacy = self.component('legacy', 'legacy-project')
        (legacy / '.asha/config.json').write_text(json.dumps({'initialized': True, 'memory_version': 1,
                                                              'project_id': 'legacy-project'}))
        self.declare('egregore', 'bare', 'legacy')
        sid = self.room()
        self.assert_refused(sid, 'bare', 'Memory v2', bare)
        self.assert_refused(sid, 'legacy', 'Memory v2', legacy)

    def test_missing_component_path_is_refused(self):
        sid = self.room()
        with self.assertRaisesRegex(StoreError, 'component path does not exist'):
            self.publish_component(sid, 'servitor', child=self.egregore)
        self.assertEqual(self.rows(sid), [])


class ComponentCliTests(ComponentFixture):
    def test_cli_passes_component_to_read_and_publish(self):
        from lib.control import hub_cli
        digest = 'a' * 64
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), \
                mock.patch.object(self.hub, 'handoff_read', return_value={}) as read, \
                mock.patch.object(self.hub, 'handoff', return_value={}) as handoff:
            self.assertEqual(hub_cli.dispatch(['handoff', '--read', '--component', 'egregore', '--json'],
                                              env=self.env), 0)
            self.assertEqual(read.call_args.kwargs, {'component': 'egregore'})
            self.assertEqual(hub_cli.dispatch(['handoff', '--component', 'egregore', '--active-file', '/a',
                                               '--decisions-file', '/d', '--expected-active', digest,
                                               '--expected-decisions', 'none', '--json'], env=self.env), 0)
            self.assertEqual(handoff.call_args.kwargs['component'], 'egregore')
            self.assertEqual(handoff.call_args.kwargs['expected'],
                             {'activeContext.md': digest, 'decisions.md': None})
            self.assertEqual(hub_cli.dispatch(['handoff', '--read', '--json'], env=self.env), 0)
            self.assertEqual(read.call_args.kwargs, {'component': None})

    def test_cli_refuses_component_with_request_without_writing(self):
        from lib.control import hub_cli
        sid = self.room()
        rid = self.hub.request_close(sid, wait=60)['closure']['request_id']
        active, decisions = self.child_drafts()
        expected = self.child_digests(self.egregore)
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), self.acting_as(sid):
            code = hub_cli.dispatch(['handoff', '--request', rid, '--component', 'egregore', '--active-file', active,
                                     '--decisions-file', decisions, '--expected-active', expected['activeContext.md'],
                                     '--expected-decisions', expected['decisions.md'], '--json'], env=self.env)
        self.assertEqual(code, 2)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])

    def test_cli_read_component_refuses_options_it_would_ignore(self):
        """Review r2 finding 4: `--read --component` validates its options before reading, as publication does."""
        import contextlib
        import io
        from lib.control import hub_cli
        sid = self.room()
        rid = self.hub.request_close(sid, wait=60)['closure']['request_id']
        active, decisions = self.child_drafts()
        digest = self.child_digests(self.egregore)['activeContext.md']
        refused = (['--request', rid], ['--outcome', 'blocked'], ['--outcome', 'no-durable-update'],
                   ['--detail', 'why'], ['--active-file', active], ['--decisions-file', decisions],
                   ['--expected-active', digest], ['--expected-decisions', 'none'])
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), self.acting_as(sid):
            for extra in refused:
                with self.subTest(option=extra[0], value=extra[1][:16]):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = hub_cli.dispatch(['handoff', '--read', '--component', 'egregore', *extra, '--json'],
                                                env=self.env)
                    self.assertEqual(code, 2)
                    self.assertEqual(out.getvalue(), '')
                    self.assertIn(extra[0], err.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):   # the bare read still answers
                self.assertEqual(hub_cli.dispatch(['handoff', '--read', '--component', 'egregore', '--json'],
                                                  env=self.env), 0)
        self.assertEqual(json.loads(out.getvalue())['component']['project_id'], 'egregore-project')
        row = self.hub.get(sid)
        self.assertEqual(row['lifecycle'], 'closing')
        self.assertEqual(row['closure']['request_id'], rid)
        self.assertEqual(self.rows(sid), [])

    def test_cli_publish_component_refuses_an_outcome(self):
        """The publication branch already refused these; pinned at the CLI beside the read branch."""
        from lib.control import hub_cli
        sid = self.room()
        active, decisions = self.child_drafts()
        expected = self.child_digests(self.egregore)
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), self.acting_as(sid):
            code = hub_cli.dispatch(['handoff', '--component', 'egregore', '--outcome', 'blocked', '--detail', 'x',
                                     '--active-file', active, '--decisions-file', decisions,
                                     '--expected-active', expected['activeContext.md'],
                                     '--expected-decisions', expected['decisions.md'], '--json'], env=self.env)
        self.assertEqual(code, 2)
        self.assertEqual(self.contents(self.egregore), (START_ACTIVE, START_DECISIONS))
        self.assertEqual(self.rows(sid), [])


if __name__ == '__main__':
    import unittest
    unittest.main()
