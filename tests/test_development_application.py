from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_development_application import development_updates, check_development


class DevelopmentApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        source = self.project / 'decision.txt'
        source.write_text('Accept development dates and statuses; retain QA separately.')
        self.decision = {'kind': 'accept-dates-and-statuses', 'analyst_confirmed': True,
                         'source': {'file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                    'quote': source.read_text()}}
        self.row = {'feature': 'example', 'task_id': 'DEV', 'role': 'FE', 'registry': 'tasks.md',
                    'current': {'Status': 'planned', 'Progress %': '0', 'Actual Start': '', 'Actual Finish': ''},
                    'history': {'state': 'in-progress', 'started_at': '2026-09-28T10:26:45+00:00',
                                'progress_percent': None}}
        self.comparison = {'rows': [self.row]}

    def review(self):
        return {'comparison': self.comparison, 'development_decision': self.decision,
                'development_application': development_updates(self.comparison, self.decision)}

    def write_registry(self, start='', status='planned', progress='0'):
        (self.project / 'tasks.md').write_text(
            '| Task ID | Role | Actual Start | Actual Finish | Status | Progress % |\n'
            '|---|---|---|---|---|---|\n'
            f'| DEV | FE | {start} | | {status} | {progress} |\n')

    def test_unknown_progress_is_not_zero_and_status_aliases_are_supported(self):
        self.write_registry()
        review = self.review()
        self.assertEqual({error['field'] for error in check_development(self.project, review)},
                         {'Actual Start', 'Status', 'Progress %'})
        self.write_registry('2026-09-28', 'in_progress', 'unknown')
        self.assertEqual(check_development(self.project, review), [])
        self.write_registry('2026-09-28', 'in_progress', '0')
        self.assertEqual(check_development(self.project, review)[0]['field'], 'Progress %')

    def test_missing_decision_and_missing_row_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, 'development_decision'):
            check_development(self.project, {'comparison': self.comparison})
        self.write_registry()
        (self.project / 'tasks.md').write_text((self.project / 'tasks.md').read_text().replace('| DEV |', '| OTHER |'))
        self.assertIn('missing', check_development(self.project, self.review())[0]['reason'])

    def test_keep_current_and_dates_only_preserve_the_selected_fields(self):
        self.decision['kind'] = 'keep-current'
        self.write_registry()
        self.assertEqual(check_development(self.project, self.review()), [])
        self.decision['kind'] = 'accept-dates'
        self.write_registry('2026-09-28')
        self.assertEqual(check_development(self.project, self.review()), [])

    def test_unknown_dates_preserve_confirmed_dates_without_converting_bounds(self):
        self.row['current']['Actual Start'] = '2026-09-20'
        self.row['history'] = {'state': 'completed', 'progress_percent': 100,
                               'started_by': '2026-09-28T00:00:00+00:00',
                               'completed_by': '2026-09-30T00:00:00+00:00'}
        fields = self.review()['development_application'][0]['expected_registry_fields']
        self.assertEqual(fields['Actual Start'], '2026-09-20')
        self.assertEqual(fields['Actual Finish'], '')
        self.assertEqual(fields['Completed By'], '')
        self.assertEqual(fields['Progress %'], '100')

    def test_custom_decision_requires_exact_scope_and_unchanged_evidence(self):
        self.decision['kind'] = 'custom'
        with self.assertRaisesRegex(ValueError, 'every shown'):
            self.review()
        self.decision['overrides'] = [{'feature': 'example', 'task_id': 'DEV', 'fields': {'Status': 'planned'}}]
        self.review()
        self.decision['overrides'].append(copy.deepcopy(self.decision['overrides'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.review()
        Path(self.decision['source']['file']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.review()

    def test_confirmed_deletion_is_checked_and_invalid_custom_facts_are_rejected(self):
        self.row['proposed_action'] = 'delete-current-execution'
        self.decision['deletions'] = [{'feature': 'example', 'task_id': 'DEV'}]
        self.write_registry()
        self.assertEqual(check_development(self.project, self.review())[0]['reason'], 'Confirmed deletion not applied')
        (self.project / 'tasks.md').write_text(
            '| Task ID | Role | Status |\n|---|---|---|\n| QA | QA | planned |\n')
        self.assertEqual(check_development(self.project, self.review()), [])
        self.decision['kind'] = 'custom'
        for fields in ({'Progress %': 'NaN'}, {'Progress %': '101'}, {'Actual Start': 'yesterday'}, {'Status': 'guess'}):
            self.decision.pop('deletions', None)
            self.decision['overrides'] = [{'feature': 'example', 'task_id': 'DEV', 'fields': fields}]
            with self.assertRaises(ValueError):
                self.review()
