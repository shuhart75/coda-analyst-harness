import copy
import json
from pathlib import Path
import tempfile
import unittest

import history_understanding_trial as trial


class HistoryUnderstandingTests(unittest.TestCase):
    def answer(self):
        return {'schema_version': 1, 'cases': [
            {'id': case['id'], 'facts': copy.deepcopy(case['facts']),
             'evidence': {field: [case['text']] for field in case['support']},
             'explanation': 'Вывод по событиям; отсутствующие даты оставлены неизвестными.'}
            for case in trial.cases()]}

    def test_expected_and_equivalent_timezone(self):
        answer = self.answer()
        self.assertEqual(trial.check(answer), [])
        answer['cases'][2]['facts']['dev_started_by'] = '2026-08-05T15:00:00+03:00'
        self.assertEqual(trial.check(answer), [])

    def test_semantic_mistakes_are_reported(self):
        mistakes = [('A', 'dev_started_at', '2026-08-01T09:00:00Z'),
                    ('B', 'dev_started_at', '2026-08-01T09:00:00Z'),
                    ('C', 'dev_started_at', '2026-08-05T12:00:00Z'),
                    ('D', 'dev_state', 'completed'),
                    ('D', 'cancelled_at', '2026-08-06T11:50:00Z'),
                    ('E', 'dev_finished_at', '2026-08-05T09:00:00Z'),
                    ('E', 'qa_state', 'in-progress')]
        for identifier, field, value in mistakes:
            with self.subTest(case=identifier, field=field):
                answer = self.answer()
                next(row for row in answer['cases'] if row['id'] == identifier)['facts'][field] = value
                self.assertTrue(any(f'{identifier}.{field}' in error for error in trial.check(answer)))

    def test_evidence_is_required_and_source_bound(self):
        for quote in ['invented event', 'История полная.', 'ADD assigned_to: developer.']:
            answer = self.answer()
            answer['cases'][0]['evidence']['dev_started_at'] = [quote]
            self.assertTrue(trial.check(answer))
        answer = self.answer()
        answer['cases'][0]['evidence'] = {}
        self.assertTrue(trial.check(answer))

    def test_invalid_shapes_dates_and_duplicates(self):
        for value in [None, [], {}, {'schema_version': 1, 'cases': None}]:
            self.assertTrue(trial.check(value))
        for value in [42, '2026-08-02', 'not-a-date', True]:
            answer = self.answer()
            answer['cases'][0]['facts']['dev_started_at'] = value
            self.assertTrue(trial.check(answer))
        answer = self.answer()
        answer['cases'].append(answer['cases'][0])
        self.assertTrue(trial.check(answer))

    def test_fresh_directory_and_portable_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = trial.create(root / 'new-trial')
            self.assertEqual({path.name for path in destination.iterdir()},
                             {'AGENT-TASK.md', 'RULES.md', 'histories.json'})
            inputs = json.loads((destination / 'histories.json').read_text())
            self.assertTrue(all(set(item) == {'id', 'history'} for item in inputs))
            before = {path.name: path.read_bytes() for path in destination.iterdir()}
            with self.assertRaises(ValueError):
                trial.create(destination)
            answer = root / 'renamed-answer.json'
            answer.write_text(json.dumps(self.answer()), encoding='utf-8')
            self.assertEqual(trial.verify(answer)['status'], 'history-understanding-passed')
            self.assertEqual(before, {path.name: path.read_bytes() for path in destination.iterdir()})
            link = root / 'link'
            link.symlink_to(root / 'missing')
            with self.assertRaises(ValueError):
                trial.create(link)


if __name__ == '__main__':
    unittest.main()
