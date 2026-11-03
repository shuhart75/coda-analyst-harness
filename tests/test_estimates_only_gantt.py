from datetime import date
from unittest.mock import patch
import unittest

import test_actual_progress_sources as sources
from execution_collaboration import path_kind


class EstimatesOnlyTests(unittest.TestCase):
    setUp = sources.ActualProgressSourcesTests.setUp
    quarter = sources.ActualProgressSourcesTests.quarter
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    snapshot = sources.ActualProgressSourcesTests.snapshot

    def test_full_generation_separates_forecasts_and_keeps_sources(self):
        team = self.root / 'planning/team.md'
        team.write_text('| Resource | Personal coefficient |\n|---|---|\n| F1 | 0.5 |\n')
        self.write_tasks([
            '| | ITEM-100 | List | real | FE | 4 | F1 | 2026-09-09 | | | | todo | 0 | STORY-COHORT |',
            '| QA-COHORT | | QA | real | QA | 2 | Q2 | | | | | todo | 0 | STORY-COHORT |',
        ])
        before = {p: p.read_bytes() for p in (self.registry, self.map, team,
                  self.gantt / 'quarter-plan.puml', self.gantt / 'commander-plan.puml')}
        self.quarter()
        ordinary = (self.gantt / 'actual-progress-confluence.puml').read_text()
        alternate = (self.gantt / 'actual-progress-estimates-only.puml').read_text()
        self.assertIn('[TASK_ITEM_100_FE] ends 2026/09/18', ordinary)
        self.assertIn('[TASK_ITEM_100_FE] ends 2026/09/14', alternate)
        self.assertIn('[TASK_ITEM_100_FE] ends 2026/09/18', self.target.read_text())
        self.assertNotIn('!include', alternate)
        expected = '\n'.join(sources.EXPANDER.expand_file(
            self.gantt / 'actual-progress-estimates-only.puml', [])).rstrip() + '\n'
        self.assertEqual(expected, (self.gantt / 'actual-progress-estimates-only-confluence.puml').read_text())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_unit_coefficients_apply_to_remaining_work_without_mutating_default(self):
        (self.root / 'planning/team.md').write_text(
            '| Resource | Personal coefficient |\n|---|---|\n| F1 | 0.25 |\n')
        with patch.object(sources.OVERLAY, 'harness_today', return_value=date(2026, 9, 9)):
            normal = sources.OVERLAY.prepare_outputs(self.root, '2026-Q3', ['cohorts'])
            alternate = sources.OVERLAY.prepare_outputs(self.root, '2026-Q3', ['cohorts'], estimates_only=True)
            again = sources.OVERLAY.prepare_outputs(self.root, '2026-Q3', ['cohorts'])
        self.assertEqual(normal, again)
        self.assertIn('[TASK_ITEM_100_FE] ends 2026/09/15', normal[self.target])
        self.assertIn('[TASK_ITEM_100_FE] ends 2026/09/10', alternate[self.target])
        self.assertIn('[TASK_ITEM_100_FE] on {F1} starts 2026/09/01', alternate[self.target])

    def test_invalid_input_preserves_both_versions(self):
        self.quarter()
        self.registry.write_text('invalid registry\n')
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())

    def test_new_views_belong_to_selected_execution_quarter(self):
        scope = {'features': [], 'quarters': ['2026-Q4']}
        for name in ('actual-progress-estimates-only.puml', 'actual-progress-estimates-only-confluence.puml'):
            self.assertEqual(path_kind('planning/2026-Q4/gantt/' + name, scope), 'generated-quarter-view')
            self.assertIsNone(path_kind('planning/2026-Q3/gantt/' + name, scope))
