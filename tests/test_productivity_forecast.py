from dataclasses import replace
from datetime import date
from pathlib import Path
import tempfile
import unittest
from test_actual_progress_overlay import OVERLAY as tool


def task(task_id, role, estimate, start, executor):
    return tool.Task(task_id=task_id, tracker_key='', summary=task_id, kind='real',
                     role=role, estimate=estimate, executor=executor, planned_start=start,
                     planned_finish='', actual_start='', actual_finish='', status='planned',
                     progress=0, related_stories=[])


class ProductivityForecastTests(unittest.TestCase):
    def schedule(self, tasks, coefficients, closed=None):
        return tool.task_schedules(tasks, set(), date(2026, 10, 27),
                                   {'BE': ['B1', 'B2']}, closed,
                                   resource_coefficients=coefficients)

    def test_future_work_divides_by_individual_output_and_caps_at_one(self):
        t = task('future/BE', 'BE', 3, '', 'B1')
        self.assertEqual(self.schedule({'t': t}, {'B1': .428571})['t'].finish, date(2026, 11, 4))
        self.assertEqual(self.schedule({'t': t}, {'B1': 2})['t'].finish, date(2026, 10, 29))

    def test_completed_intervals_are_unchanged(self):
        t = replace(task('done/BE', 'BE', 20, '', 'B1'), actual_start='2026-10-19',
                    actual_finish='2026-10-21', status='done', progress=100)
        self.assertEqual(self.schedule({'t': t}, {})['t'], self.schedule({'t': t}, {'B1': .1})['t'])

    def test_only_confirmed_remaining_work_is_scaled_and_vacation_respected(self):
        t = replace(task('active/BE', 'BE', 10, '', 'B1'), actual_start='2026-10-19',
                    status='in_progress', progress=80)
        result = self.schedule({'t': t}, {'B1': .5}, {'B1': {date(2026, 10, 28)}})['t']
        self.assertEqual(result.start, date(2026, 10, 19))
        self.assertEqual(result.finish, date(2026, 11, 2))
        self.assertIn('остатка 2', result.resource_note)

    def test_unknown_progress_is_not_treated_as_zero(self):
        t = replace(task('active/BE', 'BE', 10, '', 'B1'), actual_start='2026-10-19',
                    status='in_progress', progress=None)
        original = self.schedule({'t': t}, {})['t']
        result = self.schedule({'t': t}, {'B1': .5})['t']
        self.assertEqual((result.start, result.finish), (original.start, original.finish))
        self.assertIn('остаток неизвестен', result.resource_note)

    def test_auto_assignment_compares_candidate_specific_finishes(self):
        t = task('future/BE', 'BE', 3, '', 'TBD_B')
        result = self.schedule({'t': t}, {'B1': .25, 'B2': 1})['t']
        self.assertEqual(result.assignee, 'B2')
        self.assertEqual(result.finish, date(2026, 10, 29))

    def test_roster_loading_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'planning').mkdir()
            p = root / 'planning/team.md'
            for value in ('0', '-1', 'nan', 'inf', 'no'):
                p.write_text('| Resource | Personal coefficient |\n|---|---|\n| B1 | '+value+' |\n')
                with self.assertRaises(ValueError):
                    tool.load_resource_coefficients(root)
            p.write_text('| Resource | Personal coefficient |\n|---|---|\n| B1 | 2 |\n| B2 | 0.5 |\n')
            self.assertEqual(tool.load_resource_coefficients(root), {'B1': 1, 'B2': .5})
