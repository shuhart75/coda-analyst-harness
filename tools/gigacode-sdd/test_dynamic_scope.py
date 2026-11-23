"""Behavioral tests for scope acquisition, handoff, preserved WIP and recovery."""
from copy import deepcopy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from controller import read, save
from dynamic_scope import Manager, ScopeWait
from workspace import git, WorkspaceError
import test_execution_cycle as portfolio


class DynamicScopeTests(unittest.TestCase):
    setUp = portfolio.ExecutionPortfolioTests.setUp
    result = portfolio.ExecutionPortfolioTests.result
    session = portfolio.ExecutionPortfolioTests.session
    checks = portfolio.ExecutionPortfolioTests.checks
    review = portfolio.ExecutionPortfolioTests.review
    run_task = portfolio.ExecutionPortfolioTests.run_task
    records = portfolio.ExecutionPortfolioTests.records

    def test_unassigned_change_is_checked_reviewed_and_integrated(self):
        self.primary_only = True
        original_plan = (self.runner.root/'plan.json').read_bytes()
        def execute(root, *args, **kwargs):
            (Path(root)/'helper.txt').write_text('technical helper')
            return self.session(root, *args, **kwargs)
        accepted = self.run_task(session=execute)
        self.assertIn('helper.txt', accepted['patch']['paths'])
        self.assertGreater(self.check_count, 0)
        self.assertGreater(self.review_count, 0)
        with patch.object(self.runner, 'guard'):
            self.runner.integrate(self.task, accepted)
        self.assertEqual((self.runner.main/'helper.txt').read_text(), 'technical helper')
        self.assertEqual((self.runner.root/'plan.json').read_bytes(), original_plan)

    def test_future_owner_waits_for_expanded_current_task(self):
        manager = self.runner.scopes
        other = self.fixture.plan['tasks'][1]
        self.assertTrue(manager.try_start(self.task))
        manager.expand(self.task, self.record, ['frontend/value.txt'])
        self.assertFalse(manager.try_start(other))
        manager.finish(self.task)
        self.assertTrue(manager.try_start(other))

    def test_active_owner_causes_yield_and_durable_fifo_reacquisition(self):
        manager = self.runner.scopes
        other = self.fixture.plan['tasks'][1]
        self.assertTrue(manager.try_start(other))
        self.assertTrue(manager.try_start(self.task))
        with self.assertRaises(ScopeWait):
            manager.expand(self.task, self.record, ['frontend/value.txt'])
        manager.finish(self.task)
        self.assertFalse(manager.try_start(self.task))
        manager.finish(other)
        restored = Manager(self.runner)
        self.assertFalse(restored.try_start(other))
        self.assertTrue(restored.try_start(self.task))
        self.assertFalse(restored.try_start(other))

    def test_mutual_expansion_releases_holdings_and_has_one_winner(self):
        manager = self.runner.scopes
        other = self.fixture.plan['tasks'][1]
        manager.try_start(self.task)
        manager.try_start(other)
        with self.assertRaises(ScopeWait):
            manager.expand(self.task, self.record, ['frontend/value.txt'])
        with self.assertRaises(ScopeWait):
            manager.expand(other, self.record, ['backend/value.txt'])
        manager.finish(self.task)
        manager.finish(other)
        self.assertFalse(manager.try_start(other))
        self.assertTrue(manager.try_start(self.task))
        manager.finish(self.task)
        self.assertTrue(manager.try_start(other))

    def test_simultaneous_claims_never_both_acquire(self):
        manager = self.runner.scopes
        tasks = self.fixture.plan['tasks']
        for task in tasks:
            manager.try_start(task)
        barrier = threading.Barrier(2)
        results = []
        def claim(task):
            barrier.wait(timeout=5)
            try:
                manager.expand(task, self.record, ['shared.txt'])
                results.append('acquired')
            except ScopeWait:
                results.append('waiting')
        threads = [threading.Thread(target=claim, args=(task,)) for task in tasks]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        self.assertCountEqual(results, ['acquired', 'waiting'])

    def test_all_owners_checked_and_accepted_owner_checks_retained(self):
        plan = deepcopy(self.fixture.plan)
        other = plan['tasks'][1]
        other['writes'] = ['shared.txt']
        third = dict(deepcopy(other), id='third')
        plan['tasks'].append(third)
        save(self.runner.root/'plan.json', plan)
        self.runner.state['tasks']['fe']['status'] = 'done'
        self.runner.state['tasks']['third'] = {'status': 'running'}
        manager = self.runner.scopes
        manager.try_start(third)
        with self.assertRaises(ScopeWait):
            manager.expand(self.task, self.record, ['shared.txt'])
        self.assertEqual(manager.data['tasks']['be']['owners'], ['fe', 'third'])
        for check in other['checks']:
            self.assertIn(check, manager.effective(self.task)['checks'])

    def test_refresh_keeps_old_wip_and_reapplies_to_new_base(self):
        manager = self.runner.scopes
        manager.expand(self.task, self.record, ['helper.txt'])
        (self.work/'helper.txt').write_text('preserved helper')
        (self.work/'backend/value.txt').write_text('2')
        (self.runner.main/'frontend/value.txt').write_text('3')
        git(self.runner.main, 'add', '--', 'frontend/value.txt')
        git(self.runner.main, 'commit', '-m', 'Accepted other task')
        value = dict(self.record, stage='review', checks=[{'ok': True}], review={'verdict':'pass'})
        slot = self.runner.root/'refresh'; slot.mkdir()
        self.assertTrue(manager.refresh(self.task, value, slot))
        new = Path(value['workspace'])
        self.assertNotEqual(new, self.work)
        self.assertEqual((new/'frontend/value.txt').read_text(), '3')
        self.assertEqual((new/'helper.txt').read_text(), 'preserved helper')
        self.assertEqual((new/'backend/value.txt').read_text(), '2')
        self.assertEqual((self.work/'frontend/value.txt').read_text(), '0')
        self.assertEqual(value['stage'], 'execute')
        self.assertNotIn('checks', value)
        self.assertNotIn('review', value)

    def test_refresh_conflicts_are_preserved_and_cannot_pass_checks(self):
        manager = self.runner.scopes
        manager.expand(self.task, self.record, ['frontend/value.txt'])
        (self.work/'frontend/value.txt').write_text('ours\n')
        (self.runner.main/'frontend/value.txt').write_text('theirs\n')
        git(self.runner.main, 'add', '--', 'frontend/value.txt')
        git(self.runner.main, 'commit', '-m', 'Accepted owner')
        value = dict(self.record)
        slot = self.runner.root/'refresh'; slot.mkdir()
        manager.refresh(self.task, value, slot)
        self.assertEqual((self.work/'frontend/value.txt').read_text(), 'ours\n')
        with self.assertRaisesRegex(ValueError, 'merge conflict'):
            manager.verify_conflicts(value)
        (Path(value['workspace'])/'frontend/value.txt').write_text('resolved\n')
        manager.verify_conflicts(value)

    def test_refresh_preserves_unanswered_business_question(self):
        manager = self.runner.scopes
        (self.runner.main/'backend/value.txt').write_text('3')
        git(self.runner.main, 'add', '--', 'backend/value.txt')
        git(self.runner.main, 'commit', '-m', 'Accepted technical repair')
        value = dict(self.record, status='waiting-user', question_id='business-decision')
        slot = self.runner.root/'refresh'; slot.mkdir()
        self.assertTrue(manager.refresh(self.task, value, slot))
        self.assertEqual(value['status'], 'waiting-user')
        self.assertEqual(value['question_id'], 'business-decision')
        self.assertIn('workspace_snapshot', value)

    def test_explicit_denial_is_not_overridden_by_automatic_expansion(self):
        import operator_questions as oq
        from scope_repair import DENY
        key = oq.request(self.runner.root, self.task['id'], 'scope-repair-denied', 'Разрешить?',
                         action={'kind': 'scope-repair-v1', 'files': [{'path': 'helper.txt'}]})
        oq.answer(self.runner.root, key, DENY)
        with self.assertRaisesRegex(WorkspaceError, 'explicitly denied'):
            self.runner.scopes.expand(self.task, self.record, ['helper.txt'])
        self.assertFalse(self.runner.scopes.data['tasks'])

    def test_request_before_edit_reserves_path_without_question(self):
        self.primary_only = True
        asked = False
        def execute(root, *args, **kwargs):
            nonlocal asked
            if not asked:
                import json
                asked = True
                result = self.result(root, kwargs['model'])
                result['text'] = json.dumps({'scope_request': {'paths': ['helper.txt'], 'reason': 'Missing test helper'}})
                return result
            (Path(root)/'helper.txt').write_text('helper')
            return self.session(root, *args, **kwargs)
        accepted = self.run_task(session=execute)
        import operator_questions as oq
        self.assertFalse(oq.questions(self.runner.root, pending_only=True))
        self.assertIn('helper.txt', accepted['patch']['paths'])

    def test_saved_scope_request_resumes_when_owner_releases_without_commit(self):
        import json
        self.primary_only = True
        other = self.fixture.plan['tasks'][1]
        self.runner.scopes.try_start(other)
        self.runner.scopes.try_start(self.task)
        def ask(root, *args, **kwargs):
            result = self.result(root, kwargs['model'])
            result['text'] = json.dumps({'scope_request': {'paths': ['frontend/value.txt'], 'reason': 'Repair'}})
            with Path(result['events_path']).open('a') as stream:
                stream.write(json.dumps({'type': 'result', 'subtype': 'success', 'result': result['text']})+'\n')
            return result
        with self.assertRaises(ScopeWait):
            self.run_task(session=ask)
        self.runner.scopes.finish(self.task)
        self.runner.scopes.finish(other)
        self.assertTrue(self.runner.scopes.try_start(self.task))
        self.assertEqual(self.run_task()['status'], 'accepted')

    def test_protected_paths_and_directories_do_not_expand(self):
        manager = self.runner.scopes
        for name in ['AGENTS.md', '../escape', 'backend', '.env', 'requirements.md',
                     'backend/openspec/specs/a/spec.md', 'backend/openspec/context/rules.md']:
            with self.subTest(name=name), self.assertRaises(WorkspaceError):
                manager.expand(self.task, self.record, [name])
        self.assertFalse(manager.data['tasks'])

    def test_legacy_scope_fatal_retries_without_discarding_changes(self):
        self.primary_only = True
        def old_guard(*args, **kwargs):
            raise WorkspaceError('Change outside task ownership: helper.txt')
        def execute(root, *args, **kwargs):
            (Path(root)/'helper.txt').write_text('keep')
            return self.session(root, *args, **kwargs)
        with patch.object(self.runner.scopes, 'discover', side_effect=old_guard):
            with self.assertRaises(WorkspaceError): self.run_task(session=execute)
        accepted = self.run_task(session=execute)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertTrue(list((self.runner.root/'execution-cycles').glob('*/legacy-scope-fatal-*.json')))
        self.assertFalse(list((self.runner.root/'execution-cycles').glob('*/fatal.json')))

    def test_portfolio_releases_resources_and_resumes_after_owner(self):
        self.primary_only = True
        manager = self.runner.scopes
        other = self.fixture.plan['tasks'][1]
        self.assertTrue(manager.try_start(other))
        self.assertTrue(manager.try_start(self.task))
        def execute(root, *args, **kwargs):
            (Path(root)/'frontend/value.txt').write_text('4')
            return self.session(root, *args, **kwargs)
        with self.assertRaises(ScopeWait): self.run_task(session=execute)
        self.assertFalse(self.runner.stop.is_set())
        self.assertFalse(list((self.runner.root/'execution-cycles').glob('*/fatal.json')))
        manager.finish(self.task)
        manager.finish(other)
        self.assertTrue(manager.try_start(self.task))
        self.assertEqual(self.run_task(session=execute)['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
