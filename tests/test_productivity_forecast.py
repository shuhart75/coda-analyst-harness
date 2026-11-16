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
        self.assertIsNone(result.finish)
        self.assertIn('срок завершения неизвестны', result.resource_note)

    def test_auto_assignment_compares_candidate_specific_finishes(self):
        t = task('future/BE', 'BE', 3, '', 'TBD_B')
        result = self.schedule({'t': t}, {'B1': .25, 'B2': 1})['t']
        self.assertEqual(result.assignee, 'B2')
        self.assertEqual(result.finish, date(2026, 10, 29))

    def test_default_coefficient_recalculates_remaining_work_from_today(self):
        active = replace(task('active', 'BE', 10, '', 'B1'), actual_start='2026-10-01',
                         planned_finish='2026-10-14', status='in-progress', progress=50)
        implicit = self.schedule({'active': active}, {})['active']
        explicit = self.schedule({'active': active}, {'B1': 1})['active']
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit.finish, date(2026, 11, 2))
        self.assertEqual(len(implicit.allocations), 5)
        self.assertTrue(all(day >= date(2026, 10, 27) for day, _ in implicit.allocations))

    def test_remaining_work_without_executor_uses_available_resource(self):
        active = replace(task('active', 'BE', 10, '', ''), actual_start='2026-10-01',
                         status='in-progress', progress=50)
        result = self.schedule({'active': active}, {'B1': .5, 'B2': 1})['active']
        self.assertEqual(result.assignee, 'B2')
        self.assertEqual(result.finish, date(2026, 11, 2))
        self.assertEqual(active.executor, '')

    def test_active_tasks_share_capacity_without_reserving_each_others_old_forecasts(self):
        first = replace(task('first', 'BE', 4, '', 'B1'), actual_start='2026-10-01',
                        planned_finish='2026-12-31', status='in-progress', progress=50)
        second = replace(first, task_id='second')
        result = self.schedule({'first': first, 'second': second}, {})
        self.assertEqual(result['first'].finish, date(2026, 10, 28))
        self.assertEqual(result['second'].finish, date(2026, 10, 30))
        self.assertFalse(set(result['first'].allocations) & set(result['second'].allocations))

    def test_unknown_remaining_work_cannot_be_overbooked(self):
        active = replace(task('active', 'BE', 2, '', 'B1'), actual_start='2026-10-01',
                         status='in-progress', progress=None)
        future = task('future', 'BE', 2, '', 'B1')
        with self.assertRaisesRegex(ValueError, 'доступность исполнителей неизвестна'):
            self.schedule({'active': active, 'future': future}, {})
        flexible = replace(future, executor='TBD_B')
        result = self.schedule({'active': active, 'future': flexible}, {})
        self.assertEqual(result['future'].assignee, 'B2')
        with self.assertRaisesRegex(ValueError, 'доступность исполнителей неизвестна'):
            self.schedule({'active': replace(active, executor=''), 'future': flexible}, {})

    def test_completed_work_cannot_get_completion_from_planned_dates(self):
        done = replace(task('done', 'BE', 5, '2026-10-01', 'B1'),
                       planned_finish='2026-10-07', status='completed', progress=100)
        for actual_start in ('', '2026-10-01'):
            with self.subTest(actual_start=actual_start):
                with self.assertRaisesRegex(ValueError, 'не плановая дата'):
                    self.schedule({'done': replace(done, actual_start=actual_start)}, {})
        self.assertEqual(self.schedule({'done': replace(done, completed_by='2026-10-15')}, {}), {})

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
