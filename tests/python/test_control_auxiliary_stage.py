from pathlib import Path
import json
import subprocess
from types import SimpleNamespace
import unittest

from lib.control.database import ControlDatabase
from lib.control.jj import ColocationIntentStore
from lib.control.orchestration.authority import AUTHORITY_CONTRACT
from lib.control.prune import PruneRecordStore
from lib.control.registry_migration import stage_registries
from lib.control.stage_ledger import iter_ledger
from lib.control.store import StoreError
from lib.control.transaction import CreationJournalStore, MaterializationOwnershipStore
from tests.python import test_control_registry_stage as stage_tests
from tests.python import test_control_increment2 as journal_tests
from tests.python.test_orchestration_model import initiative


class AuxiliaryStageTests(unittest.TestCase):
    def setUp(self):
        stage_tests.RegistryStageTests.setUp(self)
        fixture = SimpleNamespace(temp=SimpleNamespace(name=str(self.root)), config=self.config,
                                  task_id=self.task['task_id'])
        self.journal = journal_tests.JournalStoreTests.journal(fixture)
        CreationJournalStore(self.config).save(self.journal)
        self.ownership = MaterializationOwnershipStore(self.config).write(self.task['task_id'], 'a' * 64, [[1, 2, 3, 4]])
        self.authority = {'contract': AUTHORITY_CONTRACT, 'authority_id': self.room['room_id'],
            'label': 'retained-grant', 'repository': initiative()['scope']['repository'],
            'constraints': {'scope_prefixes': ['lib'], 'max_nodes': 2, 'harnesses': ['claude'],
                            'max_attempts_per_node': 1, 'require_headless': True},
            'auto_activate': False, 'created_at': '2026-01-01T00:00:00Z', 'revoked_at': '2026-01-02T00:00:00Z'}
        authority_dir = self.config.tasks_dir.parent / 'authorities'
        authority_dir.mkdir(mode=0o700)
        self.authority_path = authority_dir / (self.room['room_id'] + '.json')
        self.authority_path.write_text(json.dumps(self.authority, indent=3))
        # The legacy revoke writer can leave 0644 files beneath its 0700 root.
        self.authority_path.chmod(0o644)
        PruneRecordStore(self.config).write(self.task['task_id'], {'workspace_removed': True})
        self.repository = self.root / 'git-source'
        subprocess.run(['git', 'init', '-q', str(self.repository)], check=True)
        intents = ColocationIntentStore(self.config)
        intents.begin(self.repository)
        (self.repository / '.jj').mkdir()
        intents.mark_verified(self.repository)
        self.intent = intents.read(self.repository)

    staged_config = stage_tests.RegistryStageTests.staged_config

    def test_import_preserves_auxiliary_bytes_and_observed_ownership_artifact(self):
        source = self.config.tasks_dir.parent
        before = {str(p.relative_to(source)): p.read_bytes() for folder in ('transactions', 'authorities', 'prunes', 'repository-inits')
                  for p in (source / folder).iterdir() if p.is_file()}
        manifest = stage_registries(self.config, self.target)
        for domain in ('creation-journals', 'authorities', 'prunes', 'repository-inits'):
            self.assertEqual(manifest['counts'][domain], 1)
        with ControlDatabase(self.staged_config()) as db, db.transaction() as c:
            for item in iter_ledger(c, 'records', manifest['records']):
                if item['domain'] not in ('creation-journals', 'authorities', 'prunes', 'repository-inits'):
                    continue
                payload = c.execute('SELECT payload FROM records WHERE domain=? AND scope=? AND record_key=?',
                                    (item['domain'], item['scope'], item['key'])).fetchone()[0].encode()
                self.assertEqual(payload, before[item['path']])
            artifacts = list(iter_ledger(c, 'artifacts', manifest['artifacts']))
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]['kind'], 'ownership')
        self.assertEqual(artifacts[0]['source_file_fact'], self.ownership['file_fact'])
        copied = self.staged_config().tasks_dir.parent / artifacts[0]['path']
        self.assertEqual(copied.read_bytes(), before[artifacts[0]['path']])
        self.assertEqual(Path(self.ownership['path']).read_bytes(), copied.read_bytes())
        self.assertEqual(MaterializationOwnershipStore(self.config).read(self.ownership), [[1, 2, 3, 4]])

    def test_identical_bytes_on_replaced_ownership_inode_refuse_staging(self):
        changed = False
        def replace_sidecar(domain, key):
            nonlocal changed
            if domain == 'authorities' and not changed:
                path = Path(self.ownership['path'])
                raw = path.read_bytes()
                path.rename(path.with_suffix('.old'))
                path.write_bytes(raw)
                path.chmod(0o600)
                # Keep directory membership unchanged while changing its inode.
                path.with_suffix('.old').unlink()
                changed = True
        with self.assertRaisesRegex(StoreError, 'ownership artifact identity changed'):
            stage_registries(self.config, self.target, after_import=replace_sidecar)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get('registry-migration', 'control', 'stage'))

    def test_auxiliary_foreign_identity_and_symlink_refuse_import(self):
        self.authority['authority_id'] = '22222222-2222-4222-8222-222222222222'
        self.authority_path.write_text(json.dumps(self.authority))
        with self.assertRaisesRegex(StoreError, 'identity differs'):
            stage_registries(self.config, self.target)
        self.authority_path.unlink()
        self.authority_path.symlink_to(self.root / 'outside')
        with self.assertRaises(StoreError):
            stage_registries(self.config, self.root / 'symlink-stage')

    def test_legacy_authority_umask_modes_and_empty_flock_file_are_preserved(self):
        self.authority_path.chmod(0o640)
        lock = self.authority_path.parent / '.lock'
        lock.write_bytes(b'')
        lock.chmod(0o664)
        manifest = stage_registries(self.config, self.target)
        self.assertEqual(manifest['counts']['authorities'], 1)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.authority_path.stat().st_mode & 0o777, 0o640)

    def test_writable_authority_document_still_refuses_import(self):
        self.authority_path.chmod(0o664)
        with self.assertRaisesRegex(StoreError, 'unsafe auxiliary migration record mode'):
            stage_registries(self.config, self.target)
