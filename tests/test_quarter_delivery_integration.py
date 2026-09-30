"""Exercise migrated quarter delivery roots through existing workflow entry points."""
from __future__ import annotations

import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_actual_progress_sources as source_fixtures
ROOT = source_fixtures.ROOT

sys.path.insert(0, str(ROOT / 'scripts'))
import collaboration
import execution_collaboration
import layoutctl
import project_layout
import tracker_execution
import tracker_release
import tracker_scope


class QuarterDeliveryIntegrationTests(unittest.TestCase):
    write_tasks = source_fixtures.ActualProgressSourcesTests.write_tasks

    def setUp(self):
        source_fixtures.ActualProgressSourcesTests.setUp(self)
        self.project = self.root
        self.q4key = 'kib-deployments-q4'
        self.q3key = 'kib-deployments'
        self.feature.rename(self.root / 'features' / self.q4key)
        self.feature = self.root / 'features' / self.q4key
        self.gantt.parent.rename(self.root / 'planning/2026-Q4')
        self.gantt = self.root / 'planning/2026-Q4/gantt'
        (self.gantt / 'includes/actual-progress/FEATURE-cohorts.puml').rename(
            self.gantt / f'includes/actual-progress/FEATURE-{self.q4key}.puml')
        (self.feature / 'requirements.md').write_text('# Q4 расширение\n')
        (self.feature / 'planning/estimates-2026-Q3.md').write_text('Immutable earlier estimate\n')
        q3 = self.root / 'features' / self.q3key
        (q3 / 'execution').mkdir(parents=True)
        (q3 / 'requirements.md').write_text('# Q3 MVP\n')
        (q3 / 'execution/tasks.md').write_text((self.feature / 'execution/tasks.md').read_text().replace('ITEM-100', 'ITEM-300').replace('QA-COHORT', 'QA-MVP'))
        plan = self.gantt / 'quarter-plan.puml'
        self.approved_bytes = plan.read_bytes()
        snapshot = self.root / 'planning/approved-plans/2026-Q4.json'
        snapshot.parent.mkdir()
        snapshot.write_text(json.dumps({'files': {'planning/2026-Q4/gantt/quarter-plan.puml': hashlib.sha256(self.approved_bytes).hexdigest()}}))
        self.snapshot_bytes = snapshot.read_bytes()
        self.git('init', '-b', 'feature/migrate')
        self.git('config', 'user.name', 'Integration tests')
        self.git('config', 'user.email', 'integration@example.invalid')
        self.git('add', '--all')
        self.git('commit', '-m', 'Prepare delivery integration fixtures')
        assignments = {
            self.q3key: {'feature_id': self.q3key, 'delivery_id': 'mvp', 'quarter': '2026-Q3'},
            self.q4key: {'feature_id': self.q3key, 'delivery_id': 'expansion', 'quarter': '2026-Q4'},
        }
        layoutctl.apply(self.root, layoutctl.plan(self.root, assignments), True)
        self.feature = project_layout.feature_root(self.root, self.q4key)
        self.gantt = project_layout.quarter_root(self.root, '2026-Q4') / 'gantt'
        self.git('add', '--all')
        self.git('commit', '-m', 'Migrate integration fixtures')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def generate(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/sync-quarter-gantt.py'), str(self.gantt), '--actual-only'],
                                capture_output=True, text=True, env={**os.environ, 'HARNESS_TODAY': '2026-09-09'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_registry_scope_and_release_ownership_use_delivery_identity(self):
        paths = tracker_execution.registry_paths(self.root)
        self.assertEqual({project_layout.delivery_key(self.root, path) for path in paths}, {self.q3key, self.q4key})
        self.assertEqual(set(tracker_scope.select_features(self.root, '2026-Q4', None)), {self.q4key})
        selected = tracker_scope.preview_scope(self.root, 'jira', '2026-Q4', None)
        self.assertIn('ITEM-100', json.dumps(selected))
        self.assertNotIn('ITEM-300', json.dumps(selected))
        result = {'scope': {'kind': 'release', 'provider': 'jira', 'ids': ['release-one']},
                  'issues': [{'jira_key': 'ITEM-100', 'sbertrek_key': None, 'summary': 'FE Expansion', 'task_role': 'FE'},
                             {'jira_key': 'ITEM-300', 'sbertrek_key': None, 'summary': 'FE MVP', 'task_role': 'FE'}]}
        owners = tracker_release.release_owners(self.root, result)
        self.assertEqual({item['key']: item['features'] for item in owners['items']},
                         {'ITEM-100': [self.q4key], 'ITEM-300': [self.q3key]})
        self.assertTrue(owners['ownership_ready'])

    def test_actual_generation_reads_migrated_delivery_and_preserves_plan(self):
        self.generate()
        target = self.gantt / f'includes/actual-progress/FEATURE-{self.q4key}.puml'
        self.assertIn('TASK_QA_COHORT', target.read_text())
        self.assertNotIn('QA_MVP', target.read_text())
        self.assertEqual((self.gantt / 'quarter-plan.puml').read_bytes(), self.approved_bytes)
        exported = (self.gantt / 'actual-progress-confluence.puml').read_text()
        self.assertNotIn('!include', exported)
        self.assertIn('TASK_QA_COHORT', exported)

    def test_approved_snapshot_retains_historical_paths_and_hashes(self):
        snapshot = self.root / 'planning/approved-plans/2026-Q4.json'
        self.assertEqual(snapshot.read_bytes(), self.snapshot_bytes)
        for old_path, expected in json.loads(snapshot.read_text())['files'].items():
            resolved = project_layout.project_path(self.root, old_path)
            self.assertTrue(resolved.is_file())
            self.assertEqual(hashlib.sha256(resolved.read_bytes()).hexdigest(), expected)
        checked = subprocess.run([sys.executable, str(ROOT / 'scripts/validate-planning.py'), str(self.root)],
                                 text=True, capture_output=True)
        self.assertNotIn('approved plan file missing', checked.stdout + checked.stderr)
        self.assertNotIn('approved plan was modified', checked.stdout + checked.stderr)
        (self.gantt / 'quarter-plan.puml').write_text('Unauthorized approved-plan change\n')
        changed = subprocess.run([sys.executable, str(ROOT / 'scripts/validate-planning.py'), str(self.root)],
                                 text=True, capture_output=True)
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn('approved plan was modified', changed.stdout + changed.stderr)

    def test_execution_save_checks_physical_paths_against_delivery_scope(self):
        self.generate()
        workspace = self.root / 'test-harness'
        (workspace / '.workspace-state').mkdir(parents=True)
        (workspace / '.workspace-state/active-mode.md').write_text('mode: execution-update\n')
        work = {'status': 'active', 'branch': 'feature/migrate', 'feature': self.q4key,
                'execution_scope': {'features': [self.q4key], 'quarters': ['2026-Q4'], 'run_ids': []}}
        source = (self.feature / 'execution/tasks.md').relative_to(self.root).as_posix()
        checks = execution_collaboration.check_save(workspace, self.root, work, [source], [], collaboration)
        self.assertEqual(checks['paths'], [{'path': source, 'kind': 'execution-source'}])
        self.assertEqual(checks['confluence_verified'], ['2026-Q4'])
        self.assertFalse(checks['baseline_review_required'])
        self.assertEqual(checks['baseline_release_candidates'], [])
        from baseline_releases import observe
        observe(self.root, {'release_id': 'ready-release', 'membership_complete': True,
                            'membership_evidence': 'reviewed full membership',
                            'tasks': [{'id': 'closed-task', 'closed': True, 'evidence': 'explicit closure'}]})
        pending = execution_collaboration.check_save(workspace, self.root, work, [source], [], collaboration)
        self.assertTrue(pending['baseline_review_required'])
        self.assertEqual(pending['baseline_release_candidates'][0]['release_id'], 'ready-release')
        other = (project_layout.feature_root(self.root, self.q3key) / 'execution/tasks.md').relative_to(self.root).as_posix()
        with self.assertRaisesRegex(ValueError, 'outside confirmed execution scope'):
            execution_collaboration.check_save(workspace, self.root, work, [other], [], collaboration)

    def test_historical_estimate_guard_survives_physical_migration(self):
        self.generate()
        workspace = self.root / 'test-harness'
        (workspace / '.workspace-state').mkdir(parents=True)
        (workspace / '.workspace-state/active-mode.md').write_text('mode: execution-update\n')
        work = {'status': 'active', 'branch': 'feature/migrate', 'feature': self.q4key,
                'execution_scope': {'features': [self.q4key], 'quarters': ['2026-Q4'],
                                    'run_ids': [], 'include_planning': True}}
        target = self.feature / 'planning/estimates-2026-Q3.md'
        target.write_text('Changed immutable estimate\n')
        # The independent quarter-draft gate is outside this path-ownership check.
        with patch.object(execution_collaboration, 'check_draft_planning'):
            with self.assertRaisesRegex(ValueError, 'Historical estimate archive is immutable'):
                execution_collaboration.check_save(workspace, self.root, work,
                    [target.relative_to(self.root).as_posix()], [], collaboration)

    def test_exchange_unaudited_delivery_fails_before_any_artifact_write(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*')
                  if path.is_file() and '.git' not in path.parts}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/requirements-exchange.py'),
                                 'prepare', str(self.root), self.q4key], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Публикация запрещена', result.stdout + result.stderr)
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*')
                 if path.is_file() and '.git' not in path.parts}
        self.assertEqual(before, after)
        exchange = import_module('requirements-exchange')
        exchange.require_exchange_delivery_binding(self.root, self.q3key)
        with self.assertRaisesRegex(ValueError, 'Публикация запрещена'):
            exchange.prepare_in_exchange(self.root / 'requirements-exchange', self.root,
                                         self.q4key, b'target', 'target', 'analyst')

    def test_requirements_status_resolves_separate_quarter_roots(self):
        for key in (self.q3key, self.q4key):
            directory = project_layout.feature_root(self.root, key)
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/requirementsctl.py'), 'status', str(self.root), key],
                                    capture_output=True, text=True)
            self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
            payload = json.loads(result.stdout)['state']
            self.assertEqual(payload['feature'], key)
            self.assertEqual(payload['requirements_sha256'], hashlib.sha256((directory / 'requirements.md').read_bytes()).hexdigest())
        self.assertNotEqual(project_layout.feature_root(self.root, self.q3key), project_layout.feature_root(self.root, self.q4key))


if __name__ == '__main__':
    unittest.main()
