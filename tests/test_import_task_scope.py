from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from analysis_task_fixture import write_planning_binding

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('net_import_exchange', ROOT / 'scripts/repository-exchange.py')
EXCHANGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXCHANGE)


class ImportTaskScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        write_planning_binding(self.repo, ('2026-Q4',), key='RSCON-8222')
        self.commit('Common baseline')
        self.common = self.git('rev-parse', 'HEAD')
        self.owner = 'quarters/2026-Q2/features/approvals/deliveries/approvals'
        # Same accepted content returned through independent source history.
        self.put('delivery-index.json', json.dumps({'schema_version': 1, 'deliveries': {
            'approvals': {'path': self.owner, 'quarter': '2026-Q2'}}}))
        self.put(self.owner + '/requirements.md', 'Accepted requirements\n')
        self.commit('Accept analytical materials')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-c', 'source', self.common)
        self.put('delivery-index.json', (self.git('show', self.base + ':delivery-index.json')))
        self.put(self.owner + '/requirements.md', 'Accepted requirements\n')
        self.commit('Return analytical materials independently')
        self.changed = 'quarters/2026-Q4/gantt/actual-progress.puml'
        self.put(self.changed, 'Updated forecast\n')
        self.commit('Recalculate quarterly planning')
        self.incoming = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        self.state = {'base_commit': self.base, 'source_commit': self.incoming}

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def put(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, message):
        self.git('add', '--all')  # Isolated disposable fixture only.
        self.git('commit', '-m', message)

    def test_diverged_return_requires_only_net_quarter_task(self):
        self.assertNotEqual(self.common, self.base)
        source_paths = self.git('diff', '--name-only', self.common, self.incoming)
        self.assertIn('delivery-index.json', source_paths)
        keys, files = EXCHANGE.import_task_context(self.repo, self.state)
        self.assertEqual(keys, {'RSCON-8222'})
        self.assertEqual(files, {})
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.git('merge', '--no-ff', '--no-commit', self.incoming)
        self.assertEqual(self.git('diff', '--cached', '--name-only', self.base), self.changed)
        self.assertEqual(EXCHANGE.import_task_context(self.repo, self.state)[0], keys)
        self.git('commit', '-m', 'RSCON-8222: Import planning')
        candidate = self.git('rev-parse', 'HEAD')
        from commit_message_policy import keys_for_paths, git_output
        paths = self.git('diff', '--name-only', self.base, candidate).splitlines()
        self.assertEqual(keys_for_paths(paths, lambda path: git_output(self.repo, 'show', candidate + ':' + path)), keys)

    def test_real_requirement_change_still_needs_delivery_task(self):
        self.git('switch', 'source')
        self.put(self.owner + '/requirements.md', 'Changed requirements\n')
        self.commit('Change real delivery scope')
        self.state['source_commit'] = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        with self.assertRaisesRegex(ValueError, 'SA'):
            EXCHANGE.import_task_context(self.repo, self.state)

    def test_receiver_only_material_does_not_expand_import_scope(self):
        self.put('features/receiver-only/requirements.md', 'Receiver-only work\n')
        self.commit('Accept unrelated receiver work')
        self.state['base_commit'] = self.git('rev-parse', 'HEAD')
        self.assertEqual(EXCHANGE.import_task_context(self.repo, self.state)[0], {'RSCON-8222'})
