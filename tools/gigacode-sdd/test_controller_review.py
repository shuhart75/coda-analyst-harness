"""Recovery and independent review regression cases found during controller review."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import controller
import test_controller as fixtures
from workspace import git, snapshot, clone_snapshot, export_patch, commit_changes, apply_patch


class ControllerReviewTests(unittest.TestCase):
    setUp = fixtures.ControllerE2E.setUp
    task = fixtures.ControllerE2E.task

    def accepted_patch(self):
        work = self.runner.root / 'manual-worker'
        clone_snapshot(self.runner.main, work)
        base = git(work, 'rev-parse', 'HEAD')
        (work / 'backend/value.txt').write_text('2', encoding="utf-8")
        evidence = export_patch(work, base, ['backend/value.txt'], self.runner.root / 'accepted.patch')
        record = {'status': 'accepted', 'attempts': 1, 'workspace': str(work), 'base': base, 'patch': evidence}
        self.runner.state['tasks']['be'] = record
        return self.plan['tasks'][0], record

    def test_integrate_resumes_exact_applied_patch_after_hook_failure(self):
        task, record = self.accepted_patch()
        with patch.object(controller, 'commit_changes', side_effect=RuntimeError('hook unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'hook unavailable'):
                self.runner.integrate(task, record)
        self.assertEqual((self.runner.main / 'backend/value.txt').read_text(encoding="utf-8"), '2')
        self.assertEqual(record['status'], 'accepted')
        resumed = controller.Runner(self.config)
        recovered = resumed.state['tasks']['be']
        resumed.integrate(task, recovered)
        self.assertEqual(recovered['status'], 'done')
        self.assertFalse(snapshot(resumed.main)['status'])
        self.assertEqual(snapshot(self.source), self.before)

    def test_integrate_recovers_commit_before_state_write(self):
        task, record = self.accepted_patch()
        record['integration_base'] = git(self.runner.main, 'rev-parse', 'HEAD')
        self.runner.persist()
        apply_patch(self.runner.main, record['patch']['patch'])
        head = commit_changes(self.runner.main, 'TEST-1 completed before crash')
        resumed = controller.Runner(self.config)
        recovered = resumed.state['tasks']['be']
        resumed.integrate(task, recovered)
        self.assertEqual(recovered['integrated_head'], head)
        self.assertEqual(resumed.state['main_head'], head)

    def test_integrate_never_discards_divergent_dirty_changes(self):
        task, record = self.accepted_patch()
        record['integration_base'] = git(self.runner.main, 'rev-parse', 'HEAD')
        self.runner.persist()
        (self.runner.main / 'backend/value.txt').write_text('unexpected user change', encoding="utf-8")
        before = snapshot(self.runner.main)
        with self.assertRaisesRegex(controller.Blocked, 'differs'):
            self.runner.integrate(task, record)
        self.assertEqual(snapshot(self.runner.main), before)

    def test_commit_hook_cannot_change_reviewed_result(self):
        task, record = self.accepted_patch()
        hook = self.runner.main / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\necho altered-by-hook > backend/value.txt\ngit add -- backend/value.txt\n', encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaisesRegex(controller.Blocked, 'accepted patch exactly|dirty changes'):
            self.runner.integrate(task, record)
        self.assertNotEqual(record['status'], 'done')
        self.assertIn('altered-by-hook', (self.runner.main / 'backend/value.txt').read_text(encoding="utf-8"))
        self.assertEqual(snapshot(self.source), self.before)

    def test_original_checkout_git_config_is_protected(self):
        git(self.source, 'config', 'core.hooksPath', '.changed-by-another-session')
        with self.assertRaisesRegex(controller.Blocked, 'Source/protected Git configuration'):
            self.runner.guard()

    def test_changed_config_blocks_during_run(self):
        cfg = json.loads(Path(self.config).read_text(encoding="utf-8"))
        cfg['jobs'] = 3
        controller.save(self.config, cfg)
        with self.assertRaisesRegex(controller.Blocked, 'Configuration changed'):
            self.runner.guard()

    def test_plan_review_changes_retries_automatically_with_feedback(self):
        self.runner.state['preflight'] = {'config_sha256': controller.digest(self.config), 'passed': True}
        calls = []
        def session(root, cwd, prompt, role, name):
            calls.append((str(root), prompt, role))
            return {'ok': True, 'text': json.dumps(self.plan)}
        reviews = [
            {'verdict': 'changes', 'findings': ['Clarify SDD prerequisites'], 'coverage': {'REQ-BE': 'gap', 'REQ-FE': 'covered'}},
            {'verdict': 'pass', 'findings': [], 'coverage': {'REQ-BE': 'covered', 'REQ-FE': 'covered'}}]
        with patch.object(self.runner, 'session', side_effect=session), patch.object(self.runner, 'review', side_effect=reviews):
            self.runner.plan()
        self.assertEqual(self.runner.state['status'], 'planned')
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][0], calls[1][0])
        self.assertIn('Clarify SDD prerequisites', calls[1][1])

    def test_check_cannot_change_git_hook_configuration(self):
        check = {'cwd': '.', 'argv': ['git', 'config', 'core.hooksPath', '.disabled-hooks']}
        with self.assertRaisesRegex(controller.Blocked, 'Git configuration or hooks'):
            self.runner.checks(self.runner.main, [check], 'bad-check')

    def test_stall_fingerprint_ignores_random_evidence_log_locations(self):
        left = [{'returncode': 1, 'ok': False, 'log': '/run-1/output.log'}]
        right = [{'returncode': 1, 'ok': False, 'log': '/run-2/output.log'}]
        self.assertEqual(controller.stable_feedback(left), controller.stable_feedback(right))
        self.assertNotEqual(controller.stable_feedback(left), controller.stable_feedback([{'returncode': 2, 'ok': False}]))

    def test_start_performs_only_missing_stages(self):
        actions = []
        def preflight():
            actions.append('preflight')
            self.runner.state['preflight'] = {'config_sha256': controller.digest(self.config), 'passed': True}
        def plan():
            actions.append('plan')
            self.runner.state['tasks'] = {'be': {'status': 'pending'}}
        with patch.object(self.runner, 'preflight', side_effect=preflight), patch.object(self.runner, 'plan', side_effect=plan), patch.object(self.runner, 'run', side_effect=lambda retry: actions.append('run')):
            self.runner.start()
            self.runner.start()
        self.assertEqual(actions, ['preflight', 'plan', 'run', 'run'])

    def test_worker_checkpoint_restores_attempts_and_feedback(self):
        task, record = self.accepted_patch()
        work = Path(record['workspace'])
        updated = {**record, 'attempts': 4, 'feedback': 'real failure', 'status': 'running'}
        controller.save(work.parent / (work.name + '-checkpoint.json'), updated)
        recovered = self.runner.checkpoint(record)
        self.assertEqual(recovered['attempts'], 4)
        self.assertEqual(recovered['feedback'], 'real failure')


if __name__ == '__main__':
    unittest.main()
