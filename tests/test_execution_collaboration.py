from __future__ import annotations

from importlib import import_module
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_workspace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collaboration
from execution_collaboration import check_save

COLLABORATION = Path(collaboration.__file__)


class ExecutionCollaborationTests(unittest.TestCase):
    create_seed = test_workspace.CodaWorkspaceTests.create_seed
    configure_identity = test_workspace.CodaWorkspaceTests.configure_identity

    def git(self, repository, *args):
        result = test_workspace.run('git', '-C', str(repository), *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def prepare_workspace(self, root):
        workspace, _, remote, environment = test_workspace.CodaWorkspaceTests.prepare_workspace(self, root)
        project = workspace / 'documents'
        environment['ANALYST_HARNESS_STATE_ROOT'] = str(workspace / '.workspace-state')
        feature = project / 'features/registry'
        feature.mkdir(parents=True)
        (feature / 'requirements.md').write_text('# Requirements\n')
        self.git(project, 'add', '--', 'features/registry/requirements.md')
        self.git(project, 'commit', '-m', 'Prepare feature fixture')
        self.git(project, 'push', 'origin', 'main')
        return workspace, project, remote, environment

    def collaboration(self, workspace, environment, *args):
        result = test_workspace.run(sys.executable, str(COLLABORATION), '--root', str(workspace),
                                    *args, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace, self.project, self.remote, self.environment = self.prepare_workspace(Path(self.temporary.name))
        self.collaboration(self.workspace, self.environment, 'migrate', '--analyst', 'ivan')
        self.collaboration(self.workspace, self.environment, 'start', '--feature', 'registry')
        self.state_path = self.workspace / '.workspace-state/collaboration.json'
        self.mode = self.workspace / '.workspace-state/active-mode.md'
        self.mode.write_text('mode: execution-update\n')
        self.gantt = self.project / 'planning/2026-Q3/gantt'
        self.source = 'features/registry/execution/tasks.md'
        self.other = 'features/other/execution/tasks.md'
        for path in (self.source, self.other):
            target = self.project / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('Initial source\n')
        includes = self.gantt / 'includes/actual-progress'
        includes.mkdir(parents=True)
        for feature in ('registry', 'other'):
            (includes / f'FEATURE-{feature}.puml').write_text(f'[{feature}] happens at 2026/09/01\n')
        (self.gantt / 'actual-progress.puml').write_text(
            '@startgantt\n!include includes/actual-progress/FEATURE-registry.puml\n'
            '!include includes/actual-progress/FEATURE-other.puml\n@endgantt\n')
        self.expand()
        self.git(self.project, 'add', '--', self.source, self.other, 'planning/2026-Q3/gantt')
        self.git(self.project, 'commit', '-m', 'Prepare execution fixtures')
        self.initial_head = self.git(self.project, 'rev-parse', 'HEAD')

    def expand(self):
        lines = import_module('expand-plantuml-includes').expand_file(self.gantt / 'actual-progress.puml', [])
        (self.gantt / 'actual-progress-confluence.puml').write_text('\n'.join(lines).rstrip() + '\n')

    def scope(self, *features):
        args = ['set-execution-scope', '--quarter', '2026-Q3', '--reason', 'Confirmed execution update', '--analyst-confirmed']
        for feature in features or ('registry',):
            args.extend(['--feature', feature])
        return self.collaboration(self.workspace, self.environment, *args)

    def raw(self, *args):
        return test_workspace.run(sys.executable, str(COLLABORATION),
                                      '--root', str(self.workspace), *args, env=self.environment)

    def change(self, path, content='Changed execution source\n'):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def test_one_commit_contains_multiple_sources_and_complete_quarter_views(self):
        self.scope('registry', 'other')
        self.change(self.source)
        self.change(self.other)
        shifted = 'planning/2026-Q3/gantt/includes/actual-progress/FEATURE-other.puml'
        self.change(shifted, '[other] happens at 2026/09/23\n')
        self.expand()
        before = self.git(self.project, 'status', '--porcelain')
        preview = self.collaboration(self.workspace, self.environment, 'save-preview')
        self.assertEqual(preview['confluence_verified'], ['2026-Q3'])
        self.assertEqual(self.git(self.project, 'status', '--porcelain'), before)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD'), self.initial_head)
        command = preview['save_command']
        command[command.index('--message') + 1] = 'Update execution and derived quarter views'
        result = test_workspace.run(*command, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved['branch'], 'feature/registry/ivan')
        self.assertEqual(set(saved['paths']), {self.source, self.other, shifted,
                        'planning/2026-Q3/gantt/actual-progress-confluence.puml'})
        self.assertEqual(self.git(self.project, 'status', '--porcelain'), '')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/feature/registry/ivan'), saved['commit'])

    def test_other_generated_feature_does_not_require_source_scope_or_branch(self):
        self.scope('registry')
        self.change('planning/2026-Q3/gantt/includes/actual-progress/FEATURE-other.puml', '[other] happens at 2026/09/24\n')
        self.expand()
        preview = self.collaboration(self.workspace, self.environment, 'save-preview')
        self.assertTrue(all(item['kind'] == 'generated-quarter-view' for item in preview['paths']))
        self.change(self.other)
        rejected = self.raw('save-preview')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('outside confirmed execution scope', rejected.stdout)

    def test_finish_records_run_completion_only_after_proven_merge(self):
        self.scope()
        run_id = '20260922T085505Z-12345678'
        state = json.loads(self.state_path.read_text())
        state['active_work']['execution_scope']['run_ids'] = [run_id]
        self.state_path.write_text(json.dumps(state))
        branch = self.git(self.project, 'branch', '--show-current')
        self.git(self.project, 'push', '--set-upstream', 'origin', branch)
        self.assertEqual(self.raw('finish').returncode, 2)
        self.assertNotIn('completed_execution_runs', json.loads(self.state_path.read_text()))
        integrator = self.workspace / 'integrator'
        self.git(self.project, 'clone', str(self.remote), str(integrator))
        self.git(integrator, 'config', 'user.name', 'Integrator')
        self.git(integrator, 'config', 'user.email', 'integrator@example.test')
        self.git(integrator, 'merge', '--no-ff', 'origin/' + branch, '-m', 'Accept execution fixtures')
        self.git(integrator, 'push', 'origin', 'main')
        finished = self.collaboration(self.workspace, self.environment, 'finish')
        self.assertEqual(finished['status'], 'feature-work-finished')
        state = json.loads(self.state_path.read_text())
        self.assertIsNone(state['active_work'])
        self.assertEqual(state['completed_execution_runs'][run_id]['state'], 'applied')
        self.assertEqual(state['completed_execution_runs'][run_id]['origin_main'],
                         self.git(self.remote, 'rev-parse', 'main'))

    def test_incomplete_export_and_partial_save_are_blocked_without_staging(self):
        self.scope()
        shifted = 'planning/2026-Q3/gantt/includes/actual-progress/FEATURE-other.puml'
        self.change(shifted, '[other] happens at 2026/09/23\n')
        preview = self.raw('save-preview')
        self.assertIn('Confluence export differs', preview.stdout)
        rejected = self.raw('save', '--path', shifted, '--message', 'Update forecast')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.git(self.project, 'diff', '--cached', '--name-only'), '')
        self.expand()
        rejected = self.raw('save', '--path', shifted, '--message', 'Update forecast')
        self.assertIn('exact-path-set-mismatch', rejected.stdout)
        rejected = self.raw('save', '--path', 'documents/' + shifted, '--message', 'Update forecast')
        self.assertIn('без префикса documents/', rejected.stdout)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD'), self.initial_head)

    def test_scope_does_not_authorize_requirements_baselines_forecasts_or_other_quarters(self):
        self.scope('registry', 'other')
        paths = ['features/registry/requirements.md', 'planning/2026-Q3/gantt/quarter-plan.puml',
                 'planning/2026-Q3/gantt/commander-plan.puml',
                 'planning/2026-Q3/gantt/includes/actual-progress/FORECAST-saved.puml',
                 'planning/2026-Q2/gantt/actual-progress.puml']
        for path in paths:
            target = self.project / path
            previous = target.read_bytes() if target.exists() else None
            self.change(path)
            result = self.raw('save-preview')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(path, result.stdout)
            if previous is None:
                target.unlink()
            else:
                target.write_bytes(previous)

    def test_registration_preserves_files_and_branch_and_requires_confirmation_and_mode(self):
        self.change(self.other)
        before = self.git(self.project, 'status', '--porcelain')
        registered = self.scope('registry', 'other')
        self.assertFalse(registered['analytics_writes_performed'])
        self.assertEqual(self.git(self.project, 'status', '--porcelain'), before)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD'), self.initial_head)
        self.assertNotEqual(self.raw('set-execution-scope', '--feature', 'other', '--quarter', '2026-Q3',
                                    '--reason', 'Test', '--analyst-confirmed').returncode, 0)
        self.assertNotEqual(self.raw('set-execution-scope', '--feature', 'registry', '--quarter', '2026-Q3',
                                    '--reason', 'Test').returncode, 0)
        self.mode.write_text('mode: requirements\n')
        self.assertNotEqual(self.raw('save-preview').returncode, 0)

    def test_registered_tracker_runs_cannot_skip_qa_check(self):
        self.scope()
        state = json.loads(self.state_path.read_text())
        work = state['active_work']
        work['execution_scope']['run_ids'] = ['20260922T085505Z-12345678']
        self.state_path.write_text(json.dumps(state))
        self.assertIn('QA verification is mandatory', self.raw('save-preview').stdout)
        review = self.workspace / 'review.json'
        review.write_text(json.dumps({'run_id': '20260922T085505Z-12345678', 'status': 'history-review-ready',
                                      'features': [{'feature': 'registry'}]}))
        with patch.dict(os.environ, self.environment), self.assertRaisesRegex(ValueError, 'unchanged saved history review'):
            check_save(self.workspace, self.project, work, set(), [str(review)], collaboration)
        with patch.dict(os.environ, self.environment), patch('tracker_qa_application.check_qa_application', return_value=2):
            with self.assertRaisesRegex(ValueError, 'QA application check failed'):
                check_save(self.workspace, self.project, work, set(), [str(review)], collaboration)
        with patch.dict(os.environ, self.environment), patch('tracker_qa_application.check_qa_application', return_value=0):
            checked = check_save(self.workspace, self.project, work, set(), [str(review)], collaboration)
            self.assertEqual(checked['qa_verified_runs'], ['20260922T085505Z-12345678'])
        review.write_text(json.dumps({'run_id': '20260922T085505Z-12345678', 'status': 'history-collection-incomplete'}))
        with patch.dict(os.environ, self.environment), self.assertRaisesRegex(ValueError, 'Complete history review'):
            check_save(self.workspace, self.project, work, set(), [str(review)], collaboration)

    def test_pending_operation_wrong_branch_and_linked_source_are_blocked(self):
        self.scope()
        pending = Path(self.git(self.project, 'rev-parse', '--absolute-git-dir')) / 'CHERRY_PICK_HEAD'
        pending.write_text(self.initial_head + '\n')
        self.assertIn('pending Git operation', self.raw('save-preview').stdout)
        pending.unlink()
        state = json.loads(self.state_path.read_text())
        state['active_work']['branch'] = 'feature/other/ivan'
        self.state_path.write_text(json.dumps(state))
        self.assertNotEqual(self.raw('save-preview').returncode, 0)
        state['active_work']['branch'] = 'feature/registry/ivan'
        self.state_path.write_text(json.dumps(state))
        source = self.project / self.source
        source.unlink()
        source.symlink_to(self.project / self.other)
        self.assertIn('outside confirmed execution scope or linked', self.raw('save-preview').stdout)


if __name__ == '__main__':
    unittest.main()
