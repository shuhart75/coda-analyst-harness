"""Integration cases for optional, independent, resumable finding disputes."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from controller import Blocked, Interrupted, RetryableSessionError, read, save
from execution_cycle import MODELS, _review_copy
import review_resolution as resolution
import test_execution_cycle as fixtures


class DisputeTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExecutionPortfolioTests.setUp(self)
        self.primary_only = True
        self.issues = ['Backend behavior is missing']
        self.dispute_calls = []
        self.break_on = None
        self.bad_once = False
    result = fixtures.ExecutionPortfolioTests.result
    session = fixtures.ExecutionPortfolioTests.session
    checks = fixtures.ExecutionPortfolioTests.checks
    review = fixtures.ExecutionPortfolioTests.review
    run_task = fixtures.ExecutionPortfolioTests.run_task
    records = fixtures.ExecutionPortfolioTests.records

    def negative(self, *args, **kwargs):
        report = self.review(*args, **kwargs)
        return dict(report, verdict='changes', findings=self.issues)

    def dialogue(self, root, cwd, prompt, role, name, model=None, stop_event=None):
        if role == 'review':
            self.assertEqual(model, MODELS[1])  # The author cannot clear their own finding.
            self.assertIn('FOCUSED_REVIEW_DISPUTE', prompt)
            item = json.loads(prompt.split('Disputed finding:\n', 1)[1].split('\n', 1)[0])
            self.dispute_calls.append(item['id'])
            if item['text'] == self.break_on:
                self.runner.stop.set()
                raise Interrupted('test interruption')
            report = {'status': '**withdrawn**', 'reason': 'Existing behavior meets the pinned requirement.',
                      'requirements': ['REQ-BE'], 'evidence': [
                          {'path': 'backend/missing.txt' if self.bad_once else 'backend/value.txt',
                           'quote': 'Реализовано значние **2**'},
                          {'path': '.review-input/packet/requirements.md', 'quote': 'REQ-BE требует 2'}]}
            if self.bad_once:
                self.bad_once = False
            result = self.result(root, model)
            result['text'] = json.dumps(report)
            return result
        if 'REVIEW_FEEDBACK' not in prompt:
            return self.session(root, cwd, prompt, role, name, model, stop_event)
        self.calls.append((str(root), model, prompt))
        items = [{'id': resolution.identity(text)[:16], 'reason': 'Already implemented',
                  'evidence': [{'path': 'backend/value.txt', 'quote': '2'}]} for text in self.issues]
        result = self.result(root, model)
        result['text'] = json.dumps({'review_disputes': items})
        return result

    def test_dispute_requires_explicit_objection_and_independent_decision(self):
        accepted = self.run_task(session=self.dialogue, review=self.negative)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 1, 1))
        self.assertEqual(len(self.dispute_calls), 1)
        self.assertEqual(accepted['review_resolution']['review']['verdict'], 'changes')
        self.assertEqual(accepted['review']['verdict'], 'pass')

    def test_bad_file_reference_retries_only_specific_dispute(self):
        self.bad_once = True
        accepted = self.run_task(session=self.dialogue, review=self.negative)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 1, 1))
        self.assertEqual(len(self.dispute_calls), 2)
        self.assertIn('review-protocol', [h['kind'] for h in accepted['history']])

    def test_completed_decision_survives_interruption_on_next_finding(self):
        self.issues.append('Second alleged defect')
        self.break_on = self.issues[1]
        with self.assertRaises(Interrupted):
            self.run_task(session=self.dialogue, review=self.negative)
        value = next(r for r in self.records() if r['model'] == MODELS[0])
        first = resolution.identity(self.issues[0])[:16]
        self.assertEqual(set(value['review_resolution']['decisions']), {first})
        self.runner.stop.clear()
        self.break_on = None
        accepted = self.run_task(session=self.dialogue, review=self.negative)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(self.dispute_calls.count(first), 1)
        self.assertEqual((self.check_count, self.review_count), (1, 1))

    def test_subset_withdrawal_preserves_undisputed_findings(self):
        value = {'workspace_snapshot': {}}
        resolution.begin(value, {'findings': ['a', 'b'], 'coverage': {'REQ-BE': 'gap'}, 'verdict': 'changes'})
        state = value['review_resolution']
        state['decisions'][resolution.identity('a')[:16]] = {
            'status': 'withdrawn', 'requirements': ['REQ-BE'], 'reason': 'already covered', 'deferred_evidence': []}
        combined = resolution.combined(state)
        self.assertEqual(combined['findings'], ['b'])
        self.assertEqual(combined['verdict'], 'changes')

    def test_changed_code_cannot_reuse_original_dispute_round(self):
        value = {'workspace_snapshot': {'head': 'old'}}
        resolution.begin(value, {'findings': ['a']})
        value['workspace_snapshot'] = {'head': 'new'}
        with self.assertRaisesRegex(ValueError, 'Code changed'):
            resolution.prepare(value, [], self.work)

    def test_withdrawal_needs_pinned_requirement_and_actual_code(self):
        root = _review_copy(self.runner, self.work, self.task)
        result = self.result(root, MODELS[1])
        report = {'status': 'withdrawn', 'reason': 'Already implemented', 'requirements': ['REQ-BE'],
                  'evidence': [{'path': 'backend/value.txt', 'quote': 'implementation'}]}
        result['text'] = json.dumps(report)
        with self.assertRaisesRegex(RetryableSessionError, 'pinned requirement'):
            resolution.validate(self.runner, root, self.task, result, MODELS[1])
        report['evidence'][0]['path'] = '../outside.txt'
        result['text'] = json.dumps(report)
        with self.assertRaises(Blocked):
            resolution.validate(self.runner, root, self.task, result, MODELS[1])

    def test_needs_user_requires_concrete_question(self):
        root = _review_copy(self.runner, self.work, self.task)
        result = self.result(root, MODELS[1])
        report = {'status': 'needs-user', 'reason': 'Business ambiguity', 'requirements': ['REQ-BE'],
                  'evidence': [{'path': 'backend/value.txt', 'quote': 'implementation'}]}
        result['text'] = json.dumps(report)
        with self.assertRaisesRegex(RetryableSessionError, 'concrete question'):
            resolution.validate(self.runner, root, self.task, result, MODELS[1])
        report['question'] = 'Какое из двух правил применяется к этому состоянию?'
        result['text'] = json.dumps(report)
        checked = resolution.validate(self.runner, root, self.task, result, MODELS[1])
        self.assertEqual(checked['question'], report['question'])

    def test_saved_receipt_is_bound_to_specific_objection_and_independent_model(self):
        from controller import digest
        from workspace import snapshot
        from transport import parse_events
        import review_receipt
        accepted = self.run_task()
        value = copy.deepcopy(accepted)
        resolution.begin(value, dict(value['review'], verdict='changes', findings=['a', 'b']))
        state = value['review_resolution']
        root = _review_copy(self.runner, Path(value['workspace']), self.task)
        item = {'id': resolution.identity('a')[:16], 'reason': 'Already implemented',
                'evidence': [{'path': 'backend/value.txt', 'quote': '2'}]}
        state.update(requests=[item], active_id=item['id'])
        value['stage'] = 'adjudicate'
        folder = self.runner.logdir(self.task['id'] + '-adjudicate')
        folder.mkdir(parents=True)
        events = folder/'stdout.jsonl'
        report = {'status': 'withdrawn', 'reason': 'Verified code and requirement', 'requirements': ['REQ-BE'],
                  'evidence': [{'path': 'backend/value.txt', 'quote': '2'},
                               {'path': '.review-input/packet/requirements.md', 'quote': 'REQ-BE'}]}
        events.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'system', 'subtype': 'init', 'model': MODELS[1]},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(report)}]))
        result = parse_events(events)
        result['returncode'] = 0
        prompt = folder/'prompt.txt'
        prompt.write_text(resolution.prompt(self.task, state, item) + '\nController checks:\n' + json.dumps(value['checks']))
        save(folder/'session-receipt.json', {
            'workspace': str(root), 'workspace_snapshot': snapshot(root, normalize_settings=True),
            'model': MODELS[1], 'events_sha256': digest(events), 'result': result,
            'prompt_sha256': digest(prompt), 'decision_sha256': None})
        _, checked = review_receipt.load(self.runner, self.task, value, str(events), MODELS[1])
        self.assertEqual(checked['status'], 'withdrawn')
        with self.assertRaises(Blocked):
            review_receipt.load(self.runner, self.task, value, str(events), MODELS[0])
        state['operator_answers'] = [{'answer': 'New business clarification'}]
        with self.assertRaisesRegex(Blocked, 'different findings or objections'):
            review_receipt.load(self.runner, self.task, value, str(events), MODELS[1])
        state['operator_answers'] = []
        state['requests'][0]['reason'] = 'Different objection'
        with self.assertRaisesRegex(Blocked, 'different findings or objections'):
            review_receipt.load(self.runner, self.task, value, str(events), MODELS[1])

    def test_presentation_aliases_preserve_deferred_obligations(self):
        from review_format import normalize
        report = normalize({'verdict': '**PASS**', 'deferred_evidence': {
            'requirement': 'REQ-BE', 'assignedTo': 'e2e', 'gap': 'Live proof', 'basis': 'Owned downstream'}})
        self.assertEqual(report['verdict'], 'pass')
        self.assertEqual(report['deferred_evidence'][0]['owner'], 'e2e')
        self.assertEqual(report['deferred_evidence'][0]['evidence'], 'Live proof')
        report['deferred_evidence'][0]['owner'] = 'other'
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            normalize(report)
