import copy
import unittest
from unittest.mock import patch

from acceptance_wait import validate
from controller import RetryableSessionError
from review_format import response_object


class AcceptanceWaitTests(unittest.TestCase):
    def setUp(self):
        self.task = {'id': 'integration', 'requirements': ['REQ-1']}
        self.report = {'verdict': 'blocked', 'findings': ['No source'], 'acceptance_blockers': [{
            'finding_index': 0, 'kind': 'analyst-decision', 'requirements': ['REQ-1'],
            'next_step': 'Choose source', 'answer_refs': [{'id': 'answer-1', 'quote': 'defer'}]}]}

    def test_only_real_answer_and_exact_quote_can_park_without_question(self):
        with patch('acceptance_wait.answers', return_value=[{'id': 'answer-1', 'answer': {'text': 'defer'}}]):
            self.assertEqual(validate(None, self.task, self.report), self.report['acceptance_blockers'])
            for field, value in [('id', 'fabricated'), ('quote', 'approved')]:
                report = copy.deepcopy(self.report)
                report['acceptance_blockers'][0]['answer_refs'][0][field] = value
                with self.assertRaises(RetryableSessionError):
                    validate(None, self.task, report)

    def test_missing_unassigned_and_incomplete_blockers_require_output_repair(self):
        with patch('acceptance_wait.answers', return_value=[]):
            for items in ([], [{'finding_index': 0, 'kind': 'manual-check', 'requirements': ['REQ-1'],
                        'next_step': 'Inspect', 'answer_refs': [], 'question': 'Passed?'}],
                         [{'finding_index': 0, 'kind': 'analyst-decision', 'requirements': ['OTHER'],
                        'next_step': 'Choose', 'answer_refs': [], 'question': 'Which?'}]):
                with self.assertRaises(RetryableSessionError):
                    validate(None, self.task, {**self.report, 'acceptance_blockers': items})
            with self.assertRaises(RetryableSessionError):
                validate(None, self.task, {**self.report, 'verdict': 'pass'})

    def test_review_checkboxes_are_not_competing_json(self):
        report = {'verdict': 'blocked', 'findings': ['Missing proof'], 'coverage': {'REQ-1': 'unknown'}}
        import json
        for text in ('Checked [x], left [ ] and []:\n' + json.dumps(report),
                     'Checked [x]\n```json\n' + json.dumps(report) + '\n```\nUnfinished [ ]'):
            self.assertEqual(response_object(text), report)

    def test_manual_decision_still_requires_actual_check_result(self):
        from acceptance_wait import park
        from test_manual_verification import CHECK
        report = copy.deepcopy(self.report)
        item = report['acceptance_blockers'][0]
        item.update(kind='manual-check', question='Совпали результаты?',
                    manual_check={**CHECK, 'requirements': ['REQ-1']})
        with patch('acceptance_wait.answers', return_value=[{'id': 'answer-1', 'answer': {'text': 'defer'}}]), \
                patch('acceptance_wait.questions.request', return_value='manual-question') as request:
            validated = validate(None, self.task, report)
            value = {'model': 'reviewed-candidate'}
            park(None, self.task, value, report, validated)
            self.assertEqual(value['acceptance_wait']['question_ids'], ['manual-question'])
            self.assertEqual(request.call_args.kwargs['manual_check']['url'], CHECK['url'])
            self.assertEqual(value['status'], 'external-blocker')

    def test_review_parser_never_selects_between_reports_or_nested_fragments(self):
        for text in ('{"verdict":"pass"} {"verdict":"blocked"}',
                     '{broken {"verdict":"pass"}}', '[{"verdict":"pass"}]',
                     '{"verdict":"blocked","verdict":"pass"}',
                     '```json\n{"verdict":"pass"}\n```\n```json\n{}\n```'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                response_object(text)


if __name__ == '__main__':
    unittest.main()
