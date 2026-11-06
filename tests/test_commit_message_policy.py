from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from commit_message_policy import (require_valid_commit_message, delivery_message,
    delivery_analysis_task, keys_for_paths, validate_binding, staged_keys,
    validate_file, is_harness_repository)
from workspace import install_commit_message_hook


from analysis_task_fixture import task_binding


def binding(key='RSCON-123', kind='delivery', quarter='2026-Q4'):
    return task_binding(key, kind, quarter if kind == 'planning' else None)


class CommitMessagePolicyTests(unittest.TestCase):
    def test_harness_is_exempt_but_separate_analytics_repository_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            message = root / 'message.txt'
            message.write_text('Уточнить правила обвязки\n')
            self.assertTrue(is_harness_repository(ROOT))
            validate_file(message, ROOT)
            analytics = root / 'analytics'
            subprocess.run(['git', 'init', '-b', 'main', str(analytics)], check=True, capture_output=True)
            (analytics / '.analyst-workspace.json').write_text('{}')
            self.assertFalse(is_harness_repository(analytics))
            with self.assertRaises(ValueError):
                validate_file(message, analytics)

    def test_title_requires_key_and_description(self):
        for message in ['Обновить правила', 'Обновить правила\n\nRSCON-123',
                        'rscon-123: Обновить', 'RSCON-123', '# comment\nRSCON-123: Change']:
            with self.subTest(message=message), self.assertRaises(ValueError):
                require_valid_commit_message(message)
        require_valid_commit_message('RSCON-123: Обновить правила\n\nBE RSCON-999')
        require_valid_commit_message('Merge RSCON-123: Integrate accepted changes')

    def test_all_delivery_keys_required(self):
        with self.assertRaises(ValueError):
            require_valid_commit_message('RSCON-999: Update', ['RSCON-123'])
        with self.assertRaises(ValueError):
            require_valid_commit_message('RSCON-123: Update', ['RSCON-123', 'RSCON-456'])
        require_valid_commit_message('RSCON-123, RSCON-456: Update', ['RSCON-123', 'RSCON-456'])

    def test_binding_rejects_wrong_role_provider_and_missing_confirmation(self):
        for patch in [dict(suit='story'), dict(provider='jira'), dict(confirmation=''), dict(key='rscon-123')]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_binding({**binding(), **patch})

    def test_delivery_message_and_missing_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            with self.assertRaises(ValueError):
                delivery_analysis_task(project, 'demo')
            owner = project / 'features/demo'
            owner.mkdir(parents=True)
            (owner / 'analysis-task.json').write_text(json.dumps(binding()))
            self.assertEqual(delivery_message(project, ['demo'], 'Update'), 'RSCON-123: Update')

    def test_quarter_and_exchange_ownership(self):
        root = 'quarters/2026-Q4/features/demo/deliveries/mvp'
        files = {'delivery-index.json': json.dumps(dict(schema_version=1, deliveries={
            'demo-q4': dict(path=root)})), root + '/analysis-task.json': json.dumps(binding()),
            'requirements-exchange/demo-q4/manifest.json': json.dumps(dict(analysis_task=binding()))}
        for path in [root + '/requirements.md', 'delivery-index.json',
                     'requirements-exchange/demo-q4/revisions/001/requirements.md']:
            self.assertEqual(keys_for_paths([path], files.get), {'RSCON-123'})

    def test_unregistered_quarter_directory_still_requires_analysis_task(self):
        path = 'quarters/2026-Q4/features/demo/deliveries/mvp/requirements.md'
        with self.assertRaises(ValueError):
            keys_for_paths([path], lambda _: None)

    def test_real_hook_uses_index_and_blocks_wrong_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            def git(*args):
                return subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True)
            self.assertEqual(git('init', '-b', 'main').returncode, 0)
            git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            owner = project / 'features/demo'
            owner.mkdir(parents=True)
            assoc = owner / 'analysis-task.json'
            assoc.write_text(json.dumps(binding()))
            (owner / 'requirements.md').write_text('Requirements\n')
            git('add', '--', 'features/demo/requirements.md', 'features/demo/analysis-task.json')
            install_commit_message_hook(project, ROOT / 'scripts/commit_message_policy.py')
            assoc.write_text(json.dumps(binding('RSCON-999')))
            for message in ['No key', 'RSCON-999: Wrong task']:
                self.assertNotEqual(git('commit', '-m', message).returncode, 0)
            committed = git('commit', '-m', 'RSCON-123: Add requirements')
            self.assertEqual(committed.returncode, 0, committed.stderr)
            assoc.write_text(json.dumps(binding()))
            git('rm', '--', 'features/demo/analysis-task.json')
            with self.assertRaises(ValueError):
                staged_keys(project)
            git('rm', '-r', '--', 'features/demo')
            self.assertEqual(staged_keys(project), {'RSCON-123'})
            self.assertEqual(git('commit', '-m', 'RSCON-123: Remove delivery').returncode, 0)

    def test_planning_uses_one_task_for_whole_quarter_and_mixed_work_requires_both(self):
        root = 'quarters/2026-Q4/features/demo/deliveries/mvp'
        files = {'delivery-index.json': json.dumps(dict(schema_version=1, deliveries={
            'demo': dict(path=root, quarter='2026-Q4')})),
            root + '/analysis-task.json': json.dumps(binding()),
            'planning/task-bindings.json': json.dumps(dict(schema_version=1,
                quarters={'2026-Q4': binding('RSCON-456', 'planning')}))}
        for path in ['quarters/2026-Q4/quarter/README.md',
                     'quarters/2026-Q4/gantt/quarter-plan.puml', root + '/planning/estimates.md']:
            self.assertEqual(keys_for_paths([path], files.get), {'RSCON-456'})
        self.assertEqual(keys_for_paths([root + '/requirements.md',
            'quarters/2026-Q4/gantt/actual-progress.puml'], files.get), {'RSCON-123', 'RSCON-456'})

    def test_missing_planning_binding_asks_specific_quarter(self):
        with self.assertRaisesRegex(ValueError, r'Разреши задачу.*2026-Q4'):
            keys_for_paths(['planning/2026-Q4/quarter/README.md'], lambda _: None)
        with self.assertRaisesRegex(ValueError, 'К какому кварталу'):
            keys_for_paths(['planning/team.md'], lambda _: None)

    def test_hook_does_not_use_unstaged_quarter_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            def git(*args):
                return subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            path = project / 'planning/2026-Q4/quarter/README.md'
            path.parent.mkdir(parents=True); path.write_text('Planning\n')
            git('add', '--', str(path))
            from analysis_task_fixture import write_planning_binding
            write_planning_binding(project, ('2026-Q4',), key='RSCON-456')
            install_commit_message_hook(project, ROOT / 'scripts/commit_message_policy.py')
            rejected = git('commit', '-m', 'RSCON-456: Plan')
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('Разреши задачу', rejected.stderr)
            git('add', '--', 'planning/task-bindings.json')
            self.assertNotEqual(git('commit', '-m', 'RSCON-123: Wrong task').returncode, 0)
            committed = git('commit', '-m', 'RSCON-456: Plan')
            self.assertEqual(committed.returncode, 0, committed.stderr)
            (project / 'planning/task-bindings.json').write_text(json.dumps(dict(schema_version=1, quarters={})))
            git('add', '--', 'planning/task-bindings.json')
            self.assertNotEqual(git('commit', '-m', 'RSCON-456: Remove quarter').returncode, 0)
            git('rm', '-f', '--', 'planning/task-bindings.json')
            self.assertNotEqual(git('commit', '-m', 'RSCON-456: Remove binding').returncode, 0)

    def test_existing_custom_hook_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(['git', 'init', '-b', 'main', str(repository)], check=True, capture_output=True)
            hook = repository / '.git/hooks/commit-msg'
            original = '#!/bin/sh\nexit 0\n'
            hook.write_text(original); hook.chmod(0o755)
            with self.assertRaises(ValueError):
                install_commit_message_hook(repository, ROOT / 'scripts/commit_message_policy.py')
            self.assertEqual(hook.read_text(), original)


if __name__ == '__main__':
    unittest.main()
