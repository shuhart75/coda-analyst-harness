from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from test_productivity_forecast import tool, task
import test_actual_progress_sources as sources
from focused_gantt import classify, select, without_deployed_tasks
from execution_collaboration import path_kind
from gantt_releases import apply_task_releases


class FocusSelectionTests(unittest.TestCase):
    def make(self, **changes):
        return replace(task('work', 'BE', 2, '2026-12-01', 'B1'), **changes)

    def test_actual_start_wins_over_planned_status(self):
        self.assertEqual(classify({'t': self.make(actual_start='2026-10-01')}), 'current')

    def test_unknown_qa_progress_needs_start_evidence(self):
        qa = self.make(role='QA', status='in-progress', progress=None)
        self.assertEqual(classify({'q': qa}), 'current')
        self.assertEqual(classify({'q': replace(qa, status='created')}), 'next')
        self.assertEqual(classify({'q': replace(qa, status='unknown')}), 'unknown')

    def test_forecast_and_stale_planned_dates_do_not_start_work(self):
        self.assertEqual(classify({'t': self.make(planned_start='2020-01-01', progress=None)}), 'next')

    def test_completed_does_not_keep_feature_current_or_next(self):
        for values in ({'progress': 100}, {'actual_finish': '2026-10-02'},
                       {'completed_by': '2026-10-02'}, {'status': 'completed'}):
            with self.subTest(values=values):
                self.assertEqual(classify({'t': self.make(actual_start='2026-10-01', **values)}), 'closed')

    def test_partially_finished_feature_can_be_next(self):
        self.assertEqual(classify({'done': self.make(progress=100), 'todo': self.make()}), 'next')

    def test_cancelled_candidates_and_released_work_are_not_current(self):
        for values in ({'status': 'cancelled'}, {'status': 'superseded'}, {'kind': 'candidate'},
                       {'production_confirmed': True}):
            with self.subTest(values=values):
                self.assertEqual(classify({'t': self.make(actual_start='2026-10-01', **values)}), 'closed')
        self.assertEqual(classify({}), 'closed')

    def test_percentage_is_start_evidence_but_unknown_is_not_zero(self):
        self.assertEqual(classify({'t': self.make(status='unknown', progress=25)}), 'current')
        self.assertEqual(classify({'t': self.make(status='unknown', progress=None)}), 'unknown')

    def test_all_current_and_only_highest_priority_next(self):
        entries = [{'feature': name, 'state': state} for name, state in
                   [('closed', 'closed'), ('next', 'next'), ('a', 'current'), ('b', 'current'), ('later', 'next')]]
        self.assertEqual([e['feature'] for e in select(entries)], ['a', 'b', 'next'])
        self.assertEqual(select([]), [])
        self.assertEqual(select([{'state': 'unknown'}]), [])
        self.assertEqual(select([{'state': 'next'}]), [{'state': 'next'}])

    def test_deployed_task_hidden_without_inferring_deployment_from_done(self):
        tasks = {'deployed': self.make(task_id='deployed', production_confirmed=True),
                 'done': self.make(task_id='done', progress=100)}
        text = '[Old] as [TASK_DEPLOYED] starts 2026/10/01\n[TASK_DEPLOYED] ends 2026/10/02\n\n[Done] as [TASK_DONE] starts 2026/10/01\n'
        result = without_deployed_tasks(text, tasks, tool)
        self.assertNotIn('TASK_DEPLOYED', result)
        self.assertIn('TASK_DONE', result)
        apply_task_releases(tasks, [])
        self.assertFalse(tasks['deployed'].production_confirmed)

    def test_focused_paths_are_in_execution_save_scope(self):
        scope = {'features': [], 'quarters': ['2026-Q4']}
        for name in ('actual-progress-focus.puml', 'actual-progress-focus-confluence.puml'):
            self.assertEqual(path_kind('planning/2026-Q4/gantt/'+name, scope), 'generated-quarter-view')
        self.assertIsNone(path_kind('planning/2026-Q3/gantt/actual-progress-focus.puml', scope))


class FocusGenerationTests(unittest.TestCase):
    setUp = sources.ActualProgressSourcesTests.setUp
    quarter = sources.ActualProgressSourcesTests.quarter
    write_tasks = sources.ActualProgressSourcesTests.write_tasks

    def test_automatic_export_parity_no_global_milestones_and_same_dates(self):
        preamble = self.gantt/'preamble/actual-progress.puml'
        preamble.parent.mkdir()
        preamble.write_text('[Deployed release] as [OLD_RELEASE] happens at 2026/09/01\n')
        original = self.registry.read_bytes()
        self.quarter()
        src = self.gantt/'actual-progress-focus.puml'
        export = self.gantt/'actual-progress-focus-confluence.puml'
        text = export.read_text()
        self.assertEqual(text, '\n'.join(sources.EXPANDER.expand_file(src, [])).rstrip()+'\n')
        self.assertNotIn('!include', text)
        self.assertNotIn('OLD_RELEASE', text)
        full = (self.gantt/'actual-progress-confluence.puml').read_text()
        for line in text.splitlines():
            if 'TASK_' in line and (' starts ' in line or ' ends ' in line):
                self.assertIn(line, full)
        self.assertIn('Focus current: cohorts', text)
        self.assertEqual(original, self.registry.read_bytes())
        self.quarter()
        self.assertEqual(export.read_text(), text)

    def test_closed_feature_disappears_and_stale_export_is_replaced(self):
        self.quarter()
        self.write_tasks([
            '| | ITEM-100 | List | real | FE | 5 | F1 | | | 2026-09-01 | 2026-09-05 | completed | 100 | STORY-COHORT |',
            '| QA-COHORT | | Check | real | QA | 2 | Q2 | | | 2026-09-03 | 2026-09-07 | completed | 100 | STORY-COHORT |',
        ])
        self.quarter()
        text = (self.gantt/'actual-progress-focus-confluence.puml').read_text()
        self.assertNotIn('TASK_', text)
        self.assertIn('Нет текущих фич', text)
