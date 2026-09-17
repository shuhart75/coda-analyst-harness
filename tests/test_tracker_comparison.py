from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_comparison import build_comparison, qa_start_from_current


class TrackerComparisonTests(unittest.TestCase):
    def test_saved_confirmation_and_bounds_remain_visible_without_overwrite(self):
        current = {'Actual Start': '2026-08-01', 'Actual Finish': '2026-08-02',
                   'Status': 'done', 'Progress %': '100', 'Details': 'Analyst confirmed'}
        target = {'feature': 'feature', 'task_id': 'DEV', 'role': 'BE', 'registry': 'tasks.md',
                  'saved_facts': current}
        preview = {'items': [{'jira_key': 'TEST-1', 'targets': [target]}],
                   'feature_qa_proposals': [{'feature': 'feature', 'existing_targets': []}]}
        development = {'started_at': None, 'finished_at': None, 'state': 'completed',
                       'started_by': '2026-08-03T10:00:00+03:00', 'completed_by': '2026-08-04T10:00:00+03:00'}
        review = {'feature': 'feature', 'tasks': {'TEST-1/BE': {'development': development,
                                                            'limitations': ['history-incomplete']}},
                  'qa': {}, 'limitations': ['history-incomplete']}
        result = build_comparison(preview, [review], 'jira')
        self.assertIn('Analyst confirmed', result['table'])
        self.assertIn('2026-08-01', result['table'])
        self.assertIsNone(result['rows'][0]['history']['started_at'])
        self.assertEqual(current['Actual Start'], '2026-08-01')
        self.assertNotIn('Status', result['choices'][0]['fields'])
        self.assertIn('Status', result['choices'][1]['fields'])
        self.assertEqual(result['choices'][2]['fields'], [])
        self.assertFalse(result['application_allowed'])
        self.assertEqual(result['qa_start_from_current_execution']['feature']['started_on'], '2026-08-02')

    def test_saved_development_end_not_estimate_or_completion_bound_drives_qa(self):
        rows = [{'task_id': 'DEV', 'role': 'BE', 'current': {'Status': 'done', 'Actual Finish': '2026-07-31'}},
                {'task_id': 'NEXT', 'role': 'FE', 'current': {'Status': 'planned'}},
                {'task_id': 'CANCELLED', 'role': 'BE', 'current': {'Status': 'cancelled', 'Actual Finish': '2026-07-01'}}]
        self.assertEqual(qa_start_from_current(rows, [])['started_on'], '2026-07-31')
        rows[0]['current'] = {'Status': 'done', 'Completed By': '2026-07-31'}
        self.assertIsNone(qa_start_from_current(rows, [])['started_on'])


if __name__ == '__main__':
    unittest.main()
