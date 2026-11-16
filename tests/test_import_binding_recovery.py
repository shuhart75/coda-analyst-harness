"""Recovery keeps the pending import and leaves source and analytics/main intact."""
import hashlib
import json
import unittest
from pathlib import Path

from tests import test_receiver_task_sync as receiver_tests
from tests import test_workspace as fixture
from analysis_task_fixture import task_binding
import commit_tasks as tasks


class ImportBindingRecoveryTests(unittest.TestCase):
    setUp = receiver_tests.ReceiverTaskSyncTests.setUp
    git = receiver_tests.ReceiverTaskSyncTests.git
    sync = receiver_tests.ReceiverTaskSyncTests.sync
    root_path = 'quarters/2026-Q3/features/demo/deliveries/demo'
    legacy = dict(schema_version=1, provider='sbertrek', role='AN', key='RSCON-8222', confirmation='да')

    def prepare(self, previous, other=False, baseline=False):
        folder = self.source / self.root_path
        folder.mkdir(parents=True)
        (folder / 'requirements.md').write_text('# Demo\n')
        if previous is not None:
            (folder / 'analysis-task.json').write_text(json.dumps(previous) + '\n')
        entries = {'demo': {'path': self.root_path, 'quarter': '2026-Q3', 'feature_id': 'demo', 'delivery_id': 'demo'}}
        if other:
            other_root = self.root_path.replace('/demo', '/other')
            entries['other'] = {'path': other_root, 'quarter': '2026-Q3', 'feature_id': 'other', 'delivery_id': 'other'}
            (self.source / other_root).mkdir(parents=True)
            (self.source / other_root / 'requirements.md').write_text('# Other\n')
            self.git(self.source, 'add', '--', other_root)
        (self.source / 'delivery-index.json').write_text(json.dumps({'schema_version': 1, 'deliveries': entries}) + '\n')
        if baseline:
            path = self.source / 'baseline/current/domain/demo.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Baseline demo\n')
            self.git(self.source, 'add', '--', 'baseline/current/domain/demo.md')
        self.git(self.source, 'add', '--', self.root_path, 'delivery-index.json')
        self.git(self.source, 'commit', '-m', 'RSCON-8222: Carry source delivery')
        self.git(self.source, 'push', 'origin', 'main')
        self.source_head = self.git(self.source, 'rev-parse', 'HEAD')
        self.base_head = self.git(self.documents, 'rev-parse', 'HEAD')
        self.pending = self.sync()
        self.assertEqual(self.pending['status'], 'awaiting-analysis-task')
        self.assertEqual(self.pending['task_requests'], [])
        self.target = self.pending['task_binding_targets'][0]
        self.task_state = self.workspace / '.workspace-state/commit-tasks/demo.json'
        self.resolve()

    def resolve(self, owner='demo', key='RSCON-8304', kind='delivery'):
        self.binding = {**task_binding(owner_id=owner, key=key, kind=kind),
                        'owner': {'type': 'delivery', 'id': owner}}
        raw = self.workspace / '.workspace-state/card.json'
        raw.write_text(json.dumps([dict(self.binding, status='open')]))
        state = tasks.request('delivery', owner, kind, 'demo', self.binding['epic'], 'confirmed epic')
        tasks.ingest_search(state, json.loads(raw.read_text()), items_pointer='',
            fields={key: '/' + key for key in ('key', 'summary', 'suit', 'epic', 'status')},
            closed_values=['closed'], complete=True, step=state['step_id'])
        if state['status'] == 'analyst-selection-required':
            tasks.select(state, self.binding['key'], self.binding['confirmation'])
        self.binding = state['binding']
        tasks.save_state(self.task_state, state, raw)

    def bind(self, *extra):
        return fixture.run('python3', str(fixture.ROOT / 'scripts/repository-exchange.py'),
            '--root', str(self.workspace), 'bind-import-task', '--task-state', str(self.task_state),
            '--expected-source', self.pending['source_commit'], '--expected-base', self.pending['base_commit'],
            *extra, env=self.environment)

    def test_legacy_binding_is_reviewed_then_migrated_in_same_import(self):
        self.prepare(self.legacy)
        self.assertEqual(self.target['status'], 'legacy')
        old_bytes = (self.source / self.root_path / 'analysis-task.json').read_bytes()
        digest = hashlib.sha256(old_bytes).hexdigest()
        self.assertEqual(self.target['binding_sha256'], digest)
        self.assertIn('--confirmation', self.target['bind_task_command'])
        decision = Path(self.pending['task_decisions_file'])
        for flags in [(), ('--expected-binding-sha256', '0' * 64, '--confirmation', 'replace'),
                      ('--expected-binding-sha256', digest),
                      ('--expected-binding-sha256', digest, '--confirmation', 'ANALYST_CONFIRMATION')]:
            self.assertNotEqual(self.bind(*flags).returncode, 0)
            self.assertFalse(decision.exists())
        args = ('--expected-binding-sha256', digest, '--confirmation', 'Replace reviewed legacy binding')
        result = self.bind(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = decision.read_bytes()
        audit = json.loads(saved)['binding_migrations'][self.root_path + '/analysis-task.json']
        self.assertEqual(audit['previous_binding'], self.legacy)
        self.assertEqual(audit['previous_sha256'], digest)
        self.assertTrue(audit['evidence'])
        self.assertEqual(self.bind(*args).returncode, 0)
        self.assertEqual(saved, decision.read_bytes())
        resumed = self.sync()
        self.assertEqual(resumed['status'], 'awaiting-merge')
        self.assertEqual(resumed['request_branch'], self.pending['request_branch'])
        checkout = Path(resumed['checkout'])
        self.assertEqual(json.loads((checkout / self.root_path / 'analysis-task.json').read_text()), self.binding)
        self.assertIn('RSCON-8304', self.git(checkout, 'log', '-1', '--format=%s'))
        self.assertEqual(self.git(self.documents, 'rev-parse', 'HEAD'), self.base_head)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.source_head)
        self.assertEqual((self.source / self.root_path / 'analysis-task.json').read_bytes(), old_bytes)
        self.assertEqual(self.sync()['request_commit'], resumed['request_commit'])

    def test_missing_binding_needs_no_fabricated_request(self):
        self.prepare(None)
        self.assertEqual(self.target['status'], 'missing')
        self.assertEqual(self.bind().returncode, 0)
        self.assertEqual(self.sync()['status'], 'awaiting-merge')

    def test_other_owner_and_changed_decision_are_rejected(self):
        self.prepare(self.legacy)
        args = ('--expected-binding-sha256', self.target['binding_sha256'], '--confirmation', 'replace')
        self.resolve(owner='unrelated')
        self.assertNotEqual(self.bind(*args).returncode, 0)
        self.assertFalse(Path(self.pending['task_decisions_file']).exists())
        self.resolve()
        self.assertEqual(self.bind(*args).returncode, 0)
        saved = Path(self.pending['task_decisions_file']).read_bytes()
        self.resolve(key='RSCON-9999')
        self.assertNotEqual(self.bind(*args).returncode, 0)
        self.assertEqual(Path(self.pending['task_decisions_file']).read_bytes(), saved)

    def test_invalid_modern_binding_cannot_use_legacy_migration(self):
        self.prepare({**task_binding(owner_id='demo'), 'kind': 'baseline'})
        result = self.bind('--expected-binding-sha256', self.target['binding_sha256'], '--confirmation', 'replace')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(self.pending['task_decisions_file']).exists())

    def test_valid_modern_binding_is_not_replaced_while_another_owner_waits(self):
        self.prepare(task_binding(owner_id='demo', key='RSCON-1111'), other=True)
        self.assertEqual(self.target['status'], 'existing')
        self.assertNotEqual(self.bind().returncode, 0)
        self.assertFalse(Path(self.pending['task_decisions_file']).exists())

    def test_multiple_owners_preserve_decisions_and_pending_branch(self):
        self.prepare(self.legacy, other=True)
        args = ('--expected-binding-sha256', self.target['binding_sha256'], '--confirmation', 'replace')
        self.assertEqual(self.bind(*args).returncode, 0)
        again = self.sync()
        self.assertEqual(again['status'], 'awaiting-analysis-task')
        self.assertEqual(again['source_commit'], self.pending['source_commit'])
        first = json.loads(Path(self.pending['task_decisions_file']).read_text())
        self.task_state = self.task_state.with_name('other.json')
        self.resolve(owner='other', key='RSCON-8305')
        self.assertEqual(self.bind().returncode, 0)
        final = json.loads(Path(self.pending['task_decisions_file']).read_text())
        self.assertEqual(final['binding_migrations'], first['binding_migrations'])
        for path, binding in first['files'].items():
            self.assertEqual(final['files'][path], binding)
        resumed = self.sync()
        self.assertEqual(resumed['status'], 'awaiting-merge')
        self.assertEqual(resumed['request_branch'], self.pending['request_branch'])
        title = self.git(Path(resumed['checkout']), 'log', '-1', '--format=%s')
        self.assertIn('RSCON-8304', title)
        self.assertIn('RSCON-8305', title)

    def test_baseline_uses_separate_task_and_explicit_exact_paths(self):
        self.prepare(self.legacy, baseline=True)
        args = ('--expected-binding-sha256', self.target['binding_sha256'], '--confirmation', 'replace')
        self.assertEqual(self.bind(*args).returncode, 0)
        resumed = self.sync()
        self.assertEqual(resumed['status'], 'awaiting-analysis-task')
        path = 'baseline/current/domain/demo.md'
        self.assertEqual(resumed['task_work_targets'][0]['path'], path)
        self.task_state = self.task_state.with_name('baseline.json')
        self.resolve(kind='baseline', key='RSCON-8303')
        decision = Path(self.pending['task_decisions_file'])
        saved = decision.read_bytes()
        for flags in [(), ('--path', path), ('--path', 'baseline/', '--confirmation', 'owner'),
                      ('--path', self.root_path + '/requirements.md', '--confirmation', 'owner')]:
            self.assertNotEqual(self.bind(*flags).returncode, 0)
            self.assertEqual(decision.read_bytes(), saved)
        flags = ('--path', path, '--confirmation', 'Demo owns this baseline path')
        result = self.bind(*flags)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = decision.read_bytes()
        self.assertEqual(self.bind(*flags).returncode, 0)
        self.assertEqual(decision.read_bytes(), saved)
        resumed = self.sync()
        self.assertEqual(resumed['status'], 'awaiting-merge')
        title = self.git(Path(resumed['checkout']), 'log', '-1', '--format=%s')
        self.assertIn('RSCON-8303', title)
        self.assertIn('RSCON-8304', title)
        self.assertEqual(self.git(self.documents, 'rev-parse', 'HEAD'), self.base_head)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.source_head)
