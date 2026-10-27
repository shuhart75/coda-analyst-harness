from dataclasses import replace
from datetime import date
import unittest
from unittest.mock import patch
from pathlib import Path
from test_productivity_forecast import tool, task


class QaRegressionTailTests(unittest.TestCase):
    resources = {'BE': ['B1', 'B2'], 'FE': ['F1'], 'QA': ['Q1', 'Q2']}

    def schedule(self, tasks, today=date(2026, 10, 27), closed=None, unavailable=None, coefficients=None):
        return tool.task_schedules(tasks, closed or set(), today, self.resources,
                                   unavailable, resource_coefficients=coefficients)

    def done(self, key, finish, role='BE'):
        return replace(task(key, role, 1, '', ''), status='completed', progress=100,
                       actual_finish=finish)

    def test_last_backend_not_first_frontend_and_holiday_tail(self):
        tasks = {'f': self.done('f', '2026-10-20', 'FE'),
                 'b': self.done('b', '2026-10-23'),
                 'q': task('q', 'QA', 5, '', 'Q1')}
        result = self.schedule(tasks, closed={date(2026, 10, 26)})['q']
        self.assertEqual(result.finish, date(2026, 10, 29))
        self.assertEqual(result.start, date(2026, 10, 22))

    def test_weekend_finish_excludes_completion_day(self):
        self.assertEqual(tool.qa_end_after(date(2026, 10, 25), set()), date(2026, 10, 28))

    def test_active_qa_repositions_window_but_keeps_source_and_progress(self):
        qa = replace(task('q', 'QA', 8, '', 'Q1'), status='in-progress',
                     actual_start='2026-10-01', progress=50)
        tasks = {'b': self.done('b', '2026-10-23'), 'q': qa}
        result = self.schedule(tasks, today=date(2026, 11, 20))
        self.assertEqual(result['q'].start, date(2026, 10, 19))
        self.assertEqual(result['q'].finish, date(2026, 10, 28))
        self.assertEqual(qa.actual_start, '2026-10-01')
        self.assertEqual(qa.progress, 50)
        self.assertIn('[TASK_Q] is 50% completed', '\n'.join(tool.render_task(qa, result)))
        self.assertIn('прогноз QA', '\n'.join(tool.render_task(qa, result)))

    def test_unknown_progress_is_not_replaced(self):
        qa = replace(task('q', 'QA', 4, '', ''), status='in-progress',
                     actual_start='2026-10-01', progress=None)
        result = self.schedule({'b': self.done('b', '2026-10-23'), 'q': qa})
        rendered = '\n'.join(tool.render_task(qa, result))
        self.assertIn('прогресс неизвестен', rendered)
        self.assertNotIn('% completed', rendered)

    def test_completed_qa_does_not_move(self):
        qa = replace(self.done('q', '2026-10-20', 'QA'), actual_start='2026-10-15')
        result = self.schedule({'b': self.done('b', '2026-10-23'), 'q': qa})
        self.assertEqual(result['q'].start, date(2026, 10, 15))
        self.assertEqual(result['q'].finish, date(2026, 10, 20))

    def test_local_group_ignores_later_task_from_other_group(self):
        qa = replace(task('q', 'QA', 3, '', 'Q1'), qa_members=('first',))
        result = self.schedule({'scope/first': self.done('first', '2026-10-20'),
                                'scope/last': self.done('last', '2026-11-20'), 'scope/q': qa})
        self.assertEqual(result['scope/q'].finish, date(2026, 10, 23))

    def test_release_spans_features_and_ignores_other_release(self):
        qa = replace(task('q', 'QA', 3, '', 'Q1'), release_id='release-a')
        result = self.schedule({'one/b': replace(self.done('b', '2026-10-20'), release_id='release-a'),
                                'two/b': replace(self.done('b', '2026-10-23'), release_id='release-a'),
                                'three/b': replace(self.done('b', '2026-11-20'), release_id='release-b'),
                                'one/q': qa})
        self.assertEqual(result['one/q'].finish, date(2026, 10, 28))

    def test_missing_completion_is_not_invented(self):
        dev = replace(self.done('b', ''), completed_by='2026-10-23')
        with self.assertRaisesRegex(ValueError, 'неизвестно окончание'):
            self.schedule({'b': dev, 'q': task('q', 'QA', 3, '', 'Q1')})

    def test_unavailable_resource_does_not_silently_shift_strict_end(self):
        with self.assertRaisesRegex(ValueError, 'нет свободного QA'):
            self.schedule({'b': self.done('b', '2026-10-23'), 'q': task('q', 'QA', 3, '', 'Q1')},
                          unavailable={'Q1': {date(2026, 10, 28)}})

    def test_created_work_uses_coefficient_without_inventing_progress(self):
        dev = replace(task('b', 'BE', 3, '', 'B1'), status='created', progress=None)
        result = self.schedule({'b': dev, 'q': task('q', 'QA', 3, '', 'Q1')}, coefficients={'B1': .5})
        self.assertEqual(result['b'].finish, date(2026, 11, 3))
        self.assertEqual(result['q'].finish, date(2026, 11, 6))
        self.assertIsNone(dev.progress)

    def test_qa_row_is_last_even_if_window_starts_before_development(self):
        tasks = {'b': self.done('b', '2026-10-23'), 'q': task('q', 'QA', 20, '', 'Q1')}
        schedules = self.schedule(tasks)
        story = tool.StoryMap('STORY', 'Story', '2026-10-01', 3, 'real', 'explicit', ['b', 'q'], [], [])
        with patch.object(tool, 'load_story_map', return_value=[story]):
            rendered = tool.render_feature(Path('/nonexistent'), 'example', set(), tasks, schedules)
        self.assertLess(rendered.index('as [TASK_B]'), rendered.index('as [TASK_Q]'))
