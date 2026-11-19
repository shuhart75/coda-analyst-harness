"""Operator decisions restore progress without silently widening task ownership."""
import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from controller import Blocked, read, save, digest
from workspace import git
import operator_questions as oq
import scope_repair as sr
import test_execution_cycle as portfolio


class ControlParsingTests(unittest.TestCase):
    def test_report_with_error_diff_and_legacy_question(self):
        request = {'subject': 'task-scope-expansion', 'request': 'Разрешить правку?',
                   'diagnosis': 'Missing bean', 'patch': {'file': 'src/test/Test.java'}}
        report = '**Отчёт**\n```\nNo bean\n```\n```diff\n+ import Metrics;\n```\n```json\n' + json.dumps({'operator_question': request}) + '\n```'
        kind, parsed = oq.control_result(report)
        self.assertEqual(kind, 'operator_question')
        self.assertEqual(parsed['topic'], request['subject'])
        self.assertEqual(parsed['question'], request['request'])
        self.assertEqual(parsed['patch'], request['patch'])

    def test_ambiguous_or_malformed_control_is_a_question_not_an_action(self):
        for report in ('```json\n{"scope_request":\n```',
                       '```json\n{"service_action":{},"operator_question":{"question":"why"}}\n```'):
            kind, value = oq.control_or_question(report)
            self.assertEqual(kind, 'operator_question')
            self.assertIn('model_output', value['details'])
        self.assertIsNone(oq.control_result('Ordinary question?\n```diff\n+ {"service_action":{}}\n```'))
        self.assertIsNone(oq.control_result('{"example":{"service_action":{}}}'))


class ScopeRepairTests(unittest.TestCase):
    setUp = portfolio.ExecutionPortfolioTests.setUp
    result = portfolio.ExecutionPortfolioTests.result
    session = portfolio.ExecutionPortfolioTests.session
    checks = portfolio.ExecutionPortfolioTests.checks
    review = portfolio.ExecutionPortfolioTests.review
    run_task = portfolio.ExecutionPortfolioTests.run_task
    records = portfolio.ExecutionPortfolioTests.records

    def setup_scope(self):
        prior = deepcopy(self.task)
        prior['id'] = 'prepare'
        self.task['writes'] = ['backend/new.txt']
        self.task['depends_on'] = ['prepare']
        plan = deepcopy(self.fixture.plan)
        plan['tasks'].insert(0, prior)
        save(self.runner.root/'plan.json', plan)
        self.runner.state['tasks'] = {'prepare': {'status': 'done'}, 'be': {'status': 'running'}}
        self.value = {**self.record, 'model': 'DeepSeek-V4.1-Flash-1017'}
        self.plan_bytes = (self.runner.root/'plan.json').read_bytes()

    def approve(self):
        key = sr.prepare(self.runner, self.task, self.value, {'paths': ['value.txt'], 'reason': 'Missing dependency'})
        oq.answer(self.runner.root, key, sr.APPROVE)
        return key

    def test_exact_file_approval_preserves_plan_and_denial_preserves_scope(self):
        self.setup_scope()
        key = sr.prepare(self.runner, self.task, self.value, {'paths': ['value.txt']})
        self.assertEqual(sr.effective_task(self.runner, self.task, self.value)['writes'], ['backend/new.txt'])
        oq.answer(self.runner.root, key, sr.DENY)
        self.assertEqual(sr.effective_task(self.runner, self.task, self.value)['writes'], ['backend/new.txt'])
        self.assertEqual((self.runner.root/'plan.json').read_bytes(), self.plan_bytes)

    def test_approval_binds_file_base_and_answer(self):
        self.setup_scope()
        key = self.approve()
        effective = sr.effective_task(self.runner, self.task, self.value)
        self.assertEqual(effective['writes'], ['backend/new.txt', 'backend/value.txt'])
        self.assertEqual(self.task['writes'], ['backend/new.txt'])
        self.assertEqual(self.value['scope_grants'], [key])
        self.assertEqual((self.runner.root/'plan.json').read_bytes(), self.plan_bytes)
        changed = dict(self.value, base='wrong')
        with self.assertRaises(Blocked):
            sr.effective_task(self.runner, self.task, changed)
        (self.runner.main/'backend/value.txt').write_text('changed concurrently')
        git(self.runner.main, 'add', '--', 'backend/value.txt')
        git(self.runner.main, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-m', 'concurrent change')
        with self.assertRaises(Blocked):
            sr.effective_task(self.runner, self.task, self.value)

    def test_unsafe_unowned_and_independent_paths_are_rejected(self):
        self.setup_scope()
        for name in ('../value.txt', '/tmp/value.txt', '*.txt', ':(glob)backend/*', 'GIGACODE.md', 'missing.txt', 'frontend/value.txt'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                sr.prepare(self.runner, self.task, self.value, {'paths': [name]})
        plan = read(self.runner.root/'plan.json')
        plan['tasks'].append({**deepcopy(plan['tasks'][0]), 'id': 'independent'})
        save(self.runner.root/'plan.json', plan)
        with self.assertRaises(ValueError):
            sr.prepare(self.runner, self.task, self.value, {'paths': ['value.txt']})

    def test_ambiguous_file_and_symlink_do_not_receive_grant(self):
        self.setup_scope()
        (self.work/'value.txt').write_text('ambiguous')
        with self.assertRaises(ValueError):
            sr.prepare(self.runner, self.task, self.value, {'paths': ['value.txt']})
        (self.work/'value.txt').unlink()
        try:
            (self.work/'alias.txt').symlink_to(self.work/'backend/value.txt')
        except OSError:
            return  # Native Windows may not permit creating symlinks.
        with self.assertRaises(ValueError):
            sr.prepare(self.runner, self.task, self.value, {'paths': ['alias.txt']})

    def test_tampered_action_is_not_authorized_by_old_answer(self):
        self.setup_scope()
        key = self.approve()
        path = self.runner.root/'operator-questions'/(key + '.json')
        question = read(path)
        question['action']['files'][0]['path'] = 'frontend/value.txt'
        save(path, question)
        with self.assertRaises(Blocked):
            sr.effective_task(self.runner, self.task, self.value)

    def test_scope_approval_executes_checks_reviews_and_integrates(self):
        self.setup_scope()
        self.primary_only = True
        def ask(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.result(root, model)
            result['text'] = json.dumps({'scope_request': {'paths': ['value.txt'], 'reason': 'Missing bean'}})
            return result
        with self.assertRaises(oq.WaitingForOperator) as caught:
            self.run_task(session=ask)
        self.assertEqual(self.check_count, 0)
        oq.answer(self.runner.root, caught.exception.question_ids[0], sr.APPROVE)
        accepted = self.run_task()
        self.assertGreater(self.check_count, 0)
        self.assertGreater(self.review_count, 0)
        self.assertEqual(accepted['reviewer_model'], 'GLM-5.3-Flash')
        with patch.object(self.runner, 'guard'):
            self.runner.integrate(self.task, accepted)
            # A crash after commit but before final status save resumes the same patch.
            accepted['status'] = 'accepted'
            self.runner.integrate(self.task, accepted)
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '2')
        self.assertEqual((self.runner.root/'plan.json').read_bytes(), self.plan_bytes)

    def test_controller_migrates_unassigned_question_then_checks_approved_repair(self):
        self.setup_scope()
        self.primary_only = True
        plan = read(self.runner.root/'plan.json')
        plan['tasks'][0]['writes'] = ['backend/other.txt']
        save(self.runner.root/'plan.json', plan)
        def ask(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.result(root, model)
            result['text'] = json.dumps({'scope_request': {'paths': ['value.txt'], 'reason': 'Missing bean'}})
            return result
        with patch.object(sr, 'validate_owners', side_effect=ValueError('No accepted predecessor owns the requested file: backend/value.txt')):
            with self.assertRaises(oq.WaitingForOperator) as caught:
                self.run_task(session=ask)
        old = caught.exception.question_ids[0]
        with self.assertRaises(oq.WaitingForOperator) as caught:
            self.run_task(session=lambda *a, **kw: self.fail('Migration must not execute'))
        new = caught.exception.question_ids[0]
        self.assertNotEqual(old, new)
        self.assertEqual(len(oq.questions(self.runner.root, pending_only=True)), 1)
        oq.answer(self.runner.root, new, sr.APPROVE)
        accepted = self.run_task()
        self.assertIn(new, accepted['scope_grants'])
        self.assertGreater(self.check_count, 0)
        self.assertGreater(self.review_count, 0)
        with patch.object(self.runner, 'guard'):
            self.runner.integrate(self.task, accepted)
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '2')

    def test_saved_audited_question_is_recovered_without_session(self):
        self.primary_only = True
        question = json.dumps({'operator_question': {'subject': 'name', 'request': 'Какое имя?'}})
        def ask(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.session(root, cwd, prompt, role, name, model, stop_event)
            result['text'] = question
            with Path(result['events_path']).open('a') as stream:
                stream.write(json.dumps({'type': 'result', 'subtype': 'success', 'result': question}) + '\n')
            return result
        with self.assertRaises(oq.WaitingForOperator):
            self.run_task(session=ask)
        # Simulate the old parser missing the question and proceeding to checks.
        for path in (self.runner.root/'execution-cycles').glob('*/*/candidate.json'):
            value = read(path)
            if value['model'] == 'DeepSeek-V4.1-Flash-1017':
                value.update(status='pending', stage='execute')
                value.pop('handled_question_result', None)
                value.pop('question_id', None)
                save(path, value)
        with self.assertRaises(oq.WaitingForOperator):
            self.run_task(session=lambda *a, **kw: self.fail('Recovery must not execute'))
        value = next(v for v in self.records() if v['model'] == 'DeepSeek-V4.1-Flash-1017')
        self.assertEqual(value['status'], 'waiting-user')
        self.assertEqual(value['failures'], 0)
        oq.answer(self.runner.root, value['question_id'], 'Учебная проверка')
        self.assertEqual(self.run_task()['status'], 'accepted')


class UnassignedRepairTests(unittest.TestCase):
    setUp = ScopeRepairTests.setUp
    setup_scope = ScopeRepairTests.setup_scope
    approve = ScopeRepairTests.approve
    def setup_unassigned(self):
        self.setup_scope()
        plan = read(self.runner.root/'plan.json')
        plan['tasks'][0]['writes'] = ['backend/other.txt']
        save(self.runner.root/'plan.json', plan)
        self.plan_bytes = (self.runner.root/'plan.json').read_bytes()

    def test_unassigned_tracked_file_requires_exact_approval(self):
        self.setup_unassigned()
        key = self.approve()
        q = next(q for q in oq.questions(self.runner.root) if q['id'] == key)
        self.assertEqual(q['action']['files'][0]['owners'], [])
        self.assertEqual(sr.effective_task(self.runner, self.task, self.value)['writes'],
                         ['backend/new.txt', 'backend/value.txt'])
        self.assertEqual((self.runner.root/'plan.json').read_bytes(), self.plan_bytes)

    def test_downstream_only_owner_cannot_be_reassigned_as_unowned(self):
        self.setup_unassigned()
        plan = read(self.runner.root/'plan.json')
        plan['tasks'].append(dict(self.task, id='later', depends_on=['be'], writes=['backend/value.txt']))
        save(self.runner.root/'plan.json', plan)
        with self.assertRaises(ValueError):
            self.approve()

    def test_old_unanswered_question_migrates_once_and_recovers_after_crash(self):
        self.setup_unassigned()
        key = oq.request(self.runner.root, self.task['id'], 'scope-unavailable-old', 'Old question',
            details={'reason': 'No accepted predecessor owns the requested file: backend/value.txt',
                     'model_request': {'paths': ['backend/value.txt'], 'reason': 'Missing bean'}})
        self.value.update(status='waiting-user', question_id=key)
        self.assertTrue(sr.refresh_pending(self.runner, self.task, self.value))
        new = self.value['question_id']
        self.assertNotEqual(key, new)
        self.assertEqual([q['id'] for q in oq.questions(self.runner.root, pending_only=True)], [new])
        self.assertFalse(sr.refresh_pending(self.runner, self.task, self.value))
        self.value['question_id'] = key  # Crash before candidate checkpoint persisted.
        self.assertTrue(sr.refresh_pending(self.runner, self.task, self.value))
        self.assertEqual(self.value['question_id'], new)
        self.assertEqual(sr.effective_task(self.runner, self.task, self.value)['writes'], ['backend/new.txt'])
        oq.answer(self.runner.root, new, sr.APPROVE)
        self.assertIn('backend/value.txt', sr.effective_task(self.runner, self.task, self.value)['writes'])

    def test_prior_answer_is_not_reinterpreted_as_new_approval(self):
        self.setup_unassigned()
        key = oq.request(self.runner.root, self.task['id'], 'scope-unavailable-old', 'Old question',
            details={'reason': 'No accepted predecessor owns the requested file: backend/value.txt',
                     'model_request': {'paths': ['backend/value.txt']}})
        oq.answer(self.runner.root, key, 'да')
        self.value.update(status='waiting-user', question_id=key)
        self.assertFalse(sr.refresh_pending(self.runner, self.task, self.value))
        self.assertEqual(sr.effective_task(self.runner, self.task, self.value)['writes'], ['backend/new.txt'])


if __name__ == '__main__':
    unittest.main()
