"""Real Git regressions for source import task decisions."""
import json
from pathlib import Path
import tempfile
import unittest

from tests import test_workspace as fixture


def binding(key):
    return dict(schema_version=1, provider='sbertrek', role='AN', key=key,
                confirmation='Analyst explicitly confirms this test scope')


class SourceTaskGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = fixture.CodaWorkspaceTests()
        self.workspace, self.source, self.remote, self.environment = self.fixture.prepare_workspace(self.root, with_analysis_task=False)
        self.documents = self.workspace / 'documents'

    def git(self, root, *args):
        result = fixture.run('git', '-C', str(root), *args, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def change(self, files):
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git(self.source, 'add', '--', *files)
        self.git(self.source, 'commit', '-m', 'RSCON-111, RSCON-222: Prepare source fixture')
        self.git(self.source, 'push', 'origin', 'main')

    def sync(self):
        result = fixture.run('python3', str(fixture.ROOT / 'scripts/workspace.py'), '--root',
                             str(self.workspace), 'sync', env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)['analytics_exchange']

    def decide(self, request, **decision):
        Path(request['task_decisions_file']).write_text(json.dumps(dict(
            source_commit=request['source_commit'], base_commit=request['base_commit'], **decision)))

    def test_missing_delivery_stops_before_commit_and_resumes_same_import(self):
        self.change({'features/one/requirements.md': '# One\n'})
        before = self.git(self.documents, 'rev-parse', 'HEAD')
        source_before = self.git(self.source, 'rev-parse', 'HEAD')
        exchange = self.sync()
        self.assertEqual(exchange['next_action'], 'ask-analysis-task')
        request = exchange['source_import']
        self.assertEqual(request['status'], 'awaiting-analysis-task')
        self.assertIn('features/one', request['next_question'])
        checkout = Path(request['checkout'])
        self.assertEqual(self.git(checkout, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.git(self.remote, 'show-ref', '--heads'), before + ' refs/heads/main')
        self.decide(request, files={'features/one/analysis-task.json': binding('RSCON-111')})
        ready = self.sync()['source_import']
        self.assertEqual(ready['status'], 'awaiting-merge')
        self.assertEqual(ready['request_branch'], request['request_branch'])
        self.assertIn('RSCON-111:', self.git(checkout, 'log', '-1', '--format=%s'))
        self.assertEqual(json.loads((checkout / 'features/one/analysis-task.json').read_text())['key'], 'RSCON-111')
        self.assertEqual(self.sync()['source_import']['request_commit'], ready['request_commit'])
        self.assertEqual(self.git(self.documents, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), source_before)

    def test_carried_delivery_and_quarter_bindings_need_no_question(self):
        self.change({
            'features/one/requirements.md': '# One\n',
            'features/one/analysis-task.json': json.dumps(binding('RSCON-111')),
            'planning/2026-Q4/notes.md': '# Plan\n',
            'planning/task-bindings.json': json.dumps(dict(schema_version=1, quarters={'2026-Q4': binding('RSCON-222')})),
        })
        request = self.sync()['source_import']
        self.assertEqual(request['status'], 'awaiting-merge')
        title = self.git(Path(request['checkout']), 'log', '-1', '--format=%s')
        self.assertIn('RSCON-111', title)
        self.assertIn('RSCON-222', title)
        self.assertNotIn('next_question', request)

    def test_generic_answer_cannot_bypass_missing_delivery(self):
        self.change({'features/one/requirements.md': '# One\n'})
        request = self.sync()['source_import']
        self.decide(request, analysis_task=binding('RSCON-111'))
        again = self.sync()['source_import']
        self.assertEqual(again['status'], 'awaiting-analysis-task')
        self.assertEqual(self.git(Path(again['checkout']), 'rev-parse', 'HEAD'), request['base_commit'])

    def test_generic_import_requires_explicit_snapshot_decision(self):
        self.change({'context/shared.txt': 'Updated\n'})
        request = self.sync()['source_import']
        self.assertEqual(request['status'], 'awaiting-analysis-task')
        self.decide(request, analysis_task=binding('RSCON-111'))
        ready = self.sync()['source_import']
        self.assertEqual(ready['status'], 'awaiting-merge')
        self.assertIn('RSCON-111:', self.git(Path(ready['checkout']), 'log', '-1', '--format=%s'))

    def test_stale_decision_cannot_create_commit(self):
        self.change({'context/shared.txt': 'Updated\n'})
        request = self.sync()['source_import']
        self.decide(request, analysis_task=binding('RSCON-111'))
        path = Path(request['task_decisions_file'])
        decision = json.loads(path.read_text())
        decision['source_commit'] = '0' * 40
        path.write_text(json.dumps(decision))
        blocked = self.sync()['source_import']
        self.assertEqual(blocked['status'], 'awaiting-analysis-task')
        self.assertEqual(self.git(Path(blocked['checkout']), 'rev-parse', 'HEAD'), request['base_commit'])

    def test_old_prepared_commit_without_task_is_not_pushed(self):
        self.change({'context/shared.txt': 'Updated\n'})
        request = self.sync()['source_import']
        checkout = Path(request['checkout'])
        # Reproduce an import prepared before the task policy without bypassing
        # today's hook: write the historical commit object directly.
        tree = self.git(checkout, 'rev-parse', request['source_commit'] + '^{tree}')
        commit = self.git(checkout, '-c', 'user.name=Legacy fixture', '-c', 'user.email=fixture@example.test',
                          'commit-tree', tree, '-p', request['base_commit'], '-p', request['source_commit'],
                          '-m', 'Legacy import without task')
        self.git(checkout, 'update-ref', 'HEAD', commit)
        self.git(checkout, 'read-tree', '--reset', '-u', 'HEAD')
        result = fixture.run('python3', str(fixture.ROOT / 'scripts/workspace.py'), '--root',
                             str(self.workspace), 'sync', env=self.environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('автоматическая перезапись истории запрещена', result.stdout)
        self.assertEqual(self.git(self.remote, 'show-ref', '--heads'), request['base_commit'] + ' refs/heads/main')
