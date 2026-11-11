import json
import unittest
from pathlib import Path
from tests import test_source_import as source_tests
from tests.test_receiver_tasks import request
from tests import test_workspace as fixture
from analysis_task_fixture import task_binding

class ReceiverTaskSyncTests(unittest.TestCase):
    setUp = source_tests.SourceImportTests.setUp
    git = source_tests.SourceImportTests.git

    def sync(self):
        result = fixture.run('python3', str(fixture.ROOT / 'scripts/workspace.py'),
                             '--root', str(self.workspace), 'sync', env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)['analytics_exchange']['source_import']

    def test_request_blocks_push_then_binding_resumes_same_import(self):
        r = request()
        base = 'quarters/2026-Q4/features/demo/deliveries/demo'
        folder = self.source / base
        folder.mkdir(parents=True)
        (folder / 'analysis-task-request.json').write_text(json.dumps(r) + '\n')
        (folder / 'requirements.md').write_text('# Demo\n')
        index = {'schema_version': 1, 'deliveries': {'demo': {'feature_id': 'demo',
                 'delivery_id': 'demo', 'quarter': '2026-Q4', 'path': base}}}
        (self.source / 'delivery-index.json').write_text(json.dumps(index) + '\n')
        self.git(self.source, 'add', '--', 'delivery-index.json', base + '/analysis-task-request.json', base + '/requirements.md')
        self.git(self.source, 'commit', '-m', 'RSCON-123: Request receiver task')
        self.git(self.source, 'push', 'origin', 'main')
        before = self.git(self.documents, 'rev-parse', 'HEAD')
        first = self.sync()
        self.assertEqual(first['status'], 'awaiting-analysis-task')
        self.assertEqual(first['next_action'], 'resolve-commit-task')
        self.assertEqual(first['task_requests'][0]['request_id'], r['request_id'])
        self.assertNotIn(first['request_branch'], self.git(self.documents, 'ls-remote', '--heads', 'origin'))
        again = self.sync()
        self.assertEqual(first['source_commit'], again['source_commit'])
        self.assertEqual(first['task_requests'], again['task_requests'])
        import commit_tasks as tasks
        binding = task_binding(owner_id='demo')
        task_state = self.workspace / '.workspace-state/commit-tasks/demo.json'
        raw = self.workspace / '.workspace-state/task-response.json'
        raw.write_text(json.dumps([dict(binding, status='open')]))
        resolution = tasks.request('delivery', 'demo', 'delivery', 'demo', binding['epic'], 'confirmed')
        tasks.ingest_search(resolution, json.loads(raw.read_text()), items_pointer='',
            fields={key: '/' + key for key in ('key', 'summary', 'suit', 'epic', 'status')},
            closed_values=['closed'], complete=True, step=resolution['step_id'])
        # Fixture title is the existing title and requires explicit selection.
        if resolution['status'] == 'analyst-selection-required':
            tasks.select(resolution, binding['key'], binding['confirmation'])
        tasks.save_state(task_state, resolution, raw)
        def bind(source=first['source_commit']):
            return fixture.run('python3', str(fixture.ROOT / 'scripts/repository-exchange.py'),
                '--root', str(self.workspace), 'bind-import-task', '--task-state', str(task_state),
                '--expected-source', source, '--expected-base', first['base_commit'], env=self.environment)
        self.assertNotEqual(bind('0' * 40).returncode, 0)
        self.assertFalse(Path(first['task_decisions_file']).exists())
        result = bind()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = Path(first['task_decisions_file']).read_bytes()
        self.assertEqual(bind().returncode, 0)
        self.assertEqual(saved, Path(first['task_decisions_file']).read_bytes())
        raw_archive = Path(resolution['evidence'][0]['path'])
        original = raw_archive.read_bytes()
        raw_archive.write_text('tampered')
        self.assertNotEqual(bind().returncode, 0)
        self.assertEqual(saved, Path(first['task_decisions_file']).read_bytes())
        raw_archive.write_bytes(original)
        third = self.sync()
        self.assertEqual(third['status'], 'awaiting-merge')
        self.assertEqual(third['request_branch'], first['request_branch'])
        self.assertEqual(before, self.git(self.documents, 'rev-parse', 'HEAD'))
        fourth = self.sync()
        self.assertEqual(third['request_commit'], fourth['request_commit'])
