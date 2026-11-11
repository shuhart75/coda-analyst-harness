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
        # Same existing resolver output format, not a separate binding command.
        Path(first['task_decisions_file']).write_text(json.dumps({
            'source_commit': first['source_commit'], 'base_commit': first['base_commit'],
            'files': {base + '/analysis-task.json': task_binding(owner_id='demo')}}))
        third = self.sync()
        self.assertEqual(third['status'], 'awaiting-merge')
        self.assertEqual(third['request_branch'], first['request_branch'])
        self.assertEqual(before, self.git(self.documents, 'rev-parse', 'HEAD'))
        fourth = self.sync()
        self.assertEqual(third['request_commit'], fourth['request_commit'])
