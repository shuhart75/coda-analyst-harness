"""Large-input regressions: bounded Git IO with unchanged safety checks."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import git_batch
from sync_progress import progress

spec = importlib.util.spec_from_file_location('performance_exchange', ROOT / 'scripts/repository-exchange.py')
exchange = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exchange)


class SyncPerformanceTests(unittest.TestCase):
    def test_accepted_remote_imports_do_not_trigger_individual_fetches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-qb', 'main', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'Initial'], check=True)
            subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'unused'], check=True)
            commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            heads = {f'source/source-import/{i:040x}': commit for i in range(54)}
            with patch.object(exchange, 'source_import_heads', return_value=heads), \
                 patch.object(exchange, 'git', wraps=exchange.git) as calls, \
                 patch.object(exchange, 'verified_roundtrips', side_effect=AssertionError('Unneeded history scan')):
                result = exchange.prepare_source_import(root, repo, repo, 'documents', False)
            self.assertEqual(result['status'], 'already-contained')
            self.assertFalse(any('fetch' in call.args[1:] for call in calls.call_args_list))

    def test_report_batches_blobs_preserving_removed_requirements_and_unusual_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            context = root / 'context'; context.mkdir()
            names = ['space name.md', 'табуляция\tстрока\n.md'] + [f'modified-{i}.md' for i in range(200)]
            for name in names:
                (context / name).write_text('# REQ-DEMO-001\n## Сценарий исходный\n')
            (context / 'deleted.md').write_text('# REQ-DELETE-001\n')
            git('add', '--', 'context'); git('commit', '-qm', 'Before')
            base = git('rev-parse', 'HEAD')
            for name in names:
                (context / name).write_text('# REQ-DEMO-002\n')
            (context / 'deleted.md').unlink()
            for i in range(1200):
                (context / f'added-{i}.md').write_text('# New file\n')
            git('add', '--', 'context'); git('commit', '-qm', 'After')
            actual = subprocess.run
            with patch.object(subprocess, 'run', wraps=actual) as calls:
                report = exchange.source_import_report(root, base, 'HEAD')
            self.assertEqual(calls.call_count, 4)  # diff, two trees, one batch
            changes = {item['path']: item for item in report['changed_paths']}
            self.assertEqual(len(changes), 1403)
            self.assertEqual(report['deleted_paths'], ['context/deleted.md'])
            for name in names:
                self.assertEqual(changes['context/' + name]['removed_requirements'], ['REQ-DEMO-001'])
                self.assertEqual(changes['context/' + name]['removed_scenarios'], ['## Сценарий исходный'])
            self.assertEqual(changes['context/deleted.md']['removed_requirements'], ['REQ-DELETE-001'])
            self.assertEqual(changes['context/added-0.md']['removed_requirements'], [])

    def test_batch_checks_binary_lengths_and_missing_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', temporary], check=True)
            payload = b'line\n\0\xff\n'
            oid = subprocess.check_output(['git', '-C', temporary, 'hash-object', '-w', '--stdin'], input=payload).decode().strip()
            self.assertEqual(git_batch.read_blobs(root, [oid, oid]), {oid: payload})
            with self.assertRaises(ValueError):
                git_batch.read_blobs(root, ['0' * 40])

    def test_progress_records_phases_failure_and_new_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('sync_progress.time.time', side_effect=[10, 12, 15, 20]):
                progress(root, 'tasks', restart=True)
                progress(root, 'hook')
                progress(root, 'blocked', status='failed', error='fixture')
                payload = json.loads((root / '.workspace-state/sync-progress.json').read_text())
                self.assertEqual(payload['phase_seconds'], {'tasks': 2, 'hook': 3})
                self.assertEqual(payload['status'], 'failed')
                progress(root, 'tasks', restart=True)
                payload = json.loads((root / '.workspace-state/sync-progress.json').read_text())
                self.assertEqual(payload['phase_seconds'], {})
                self.assertEqual(payload['elapsed_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
