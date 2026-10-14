from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_actual_progress_sources as sources

GENERATOR = sources.load_tool('sync-quarter-gantt')


class TeamAvailabilityTests(unittest.TestCase):
    def test_quarter_clipping_contiguous_days_and_roster_filter(self):
        closed = {'B1': {date(2026, 9, 30) + timedelta(days=i) for i in range(4)},
                  'F1': {date(2026, 12, 31), date(2027, 1, 1)},
                  'B9': {date(2026, 10, 1)}}
        overlay = SimpleNamespace(load_resource_closed_days=lambda _: closed,
                                  load_team_resources=lambda _: {'BE': ['B1'], 'FE': ['F1']})
        text = '\n'.join(GENERATOR.availability_lines(Path('/project/quarters/2026-Q4/gantt'), overlay))
        self.assertEqual(text.count('] starts '), 2)
        self.assertIn('starts 2026/10/01', text)
        self.assertIn('ends 2026/10/03', text)
        self.assertIn('ends 2026/12/31', text)
        for absent in ('B9', '2026/09/30', '2027/01/01', ' on {', '% completed', 'requires', 'is off'):
            self.assertNotIn(absent, text)

    def test_empty_quarter_has_no_section(self):
        overlay = SimpleNamespace(load_resource_closed_days=lambda _: {'B1': {date(2026, 9, 30)}},
                                  load_team_resources=lambda _: {'BE': ['B1']})
        self.assertEqual(GENERATOR.availability_lines(Path('/project/planning/2026-Q4/gantt'), overlay), [])

    def test_disjoint_intervals_remain_separate(self):
        overlay = SimpleNamespace(load_resource_closed_days=lambda _: {'B1': {date(2026, 10, 1), date(2026, 10, 3)}},
                                  load_team_resources=lambda _: {'BE': ['B1']})
        lines = GENERATOR.availability_lines(Path('/project/planning/2026-Q4/gantt'), overlay)
        self.assertEqual(sum('] starts ' in line for line in lines), 2)


class GeneratedAvailabilityTests(unittest.TestCase):
    setUp = sources.ActualProgressSourcesTests.setUp
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    quarter = sources.ActualProgressSourcesTests.quarter

    def test_actual_only_adds_calendar_without_changing_tasks_or_plans(self):
        self.quarter()
        task_bytes = self.target.read_bytes()
        plans = {p: p.read_bytes() for p in self.gantt.glob('*-plan.puml')}
        (self.root / 'planning/team.md').write_text(
            '| Resource | Closed intervals |\n|---|---|\n'
            '| B1 | 2026-09-15..2026-09-18 |\n', encoding='utf-8')
        self.quarter()
        root = (self.gantt / 'actual-progress.puml').read_text()
        self.assertIn('-- Отпуска и недоступность команды --', root)
        self.assertIn('[CALENDAR_OFF_B1_20260915] ends 2026/09/18', root)
        self.assertEqual(self.target.read_bytes(), task_bytes)
        for path, content in plans.items():
            self.assertEqual(path.read_bytes(), content)
        expected = '\n'.join(sources.EXPANDER.expand_file(self.gantt / 'actual-progress.puml', [])).rstrip() + '\n'
        self.assertEqual((self.gantt / 'actual-progress-confluence.puml').read_text(), expected)
        self.quarter()
        self.assertEqual((self.gantt / 'actual-progress.puml').read_text(), root)
