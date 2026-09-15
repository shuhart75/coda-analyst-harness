from copy import deepcopy
from datetime import date
import json
import unittest

import test_actual_progress_sources as sources
from actual_progress_layout import load_layout


OVERLAY = sources.OVERLAY


class ActualLayoutTests(unittest.TestCase):
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    snapshot = sources.ActualProgressSourcesTests.snapshot
    quarter = sources.ActualProgressSourcesTests.quarter

    def setUp(self):
        sources.ActualProgressSourcesTests.setUp(self)
        self.layout = self.gantt / "actual-progress-layout.json"
        (self.gantt / "decision.md").write_text("Confirmed legacy order and display titles.\n")
        self.payload = {"schema_version": 1, "analyst_confirmed": True, "source": "decision.md",
                        "project_start": "2026-07-01", "sections": [{"feature": "cohorts", "title": "Справочник когорт",
                        "include": "includes/actual-progress/FEATURE-cohorts.puml"}]}
        self.save_layout()

    def save_layout(self):
        self.layout.write_text(json.dumps(self.payload), encoding="utf-8")

    def test_title_start_and_scope_are_preserved_and_repeatable(self):
        unused = self.gantt / "includes/quarter-plan/FEATURE-unused.puml"
        unused.parent.mkdir(parents=True)
        unused.write_text("Unselected historical planning feature\n")
        before = self.snapshot()
        self.quarter()
        content = (self.gantt / "actual-progress-confluence.puml").read_text()
        self.assertIn("-- Справочник когорт --", content)
        self.assertIn("Project starts 2026-07-01", content)
        self.assertNotIn("Unused", content)
        self.assertEqual(unused.read_bytes(), before[str(unused.relative_to(self.root))])
        after = self.snapshot()
        self.quarter()
        self.assertEqual(after, self.snapshot())

    def test_extra_actual_include_cannot_be_silently_hidden(self):
        (self.actual / "FEATURE-unreviewed.puml").write_text("Unreviewed existing actual work\n")
        before = self.snapshot()
        result = self.quarter(success=False)
        self.assertIn("Состав actual-progress", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_invalid_layout_blocks_without_writes(self):
        original = deepcopy(self.payload)
        for change in ({"schema_version": True}, {"analyst_confirmed": False}, {"source": "../decision.md"},
                       {"source": "absent.md"}, {"project_start": "yesterday"}, {"sections": []}):
            with self.subTest(change=change):
                self.payload = {**original, **change}
                self.save_layout()
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(before, self.snapshot())

    def test_duplicate_features_and_titles_and_wrong_paths_block(self):
        original = deepcopy(self.payload)
        for change in ({"feature": "../cohorts"}, {"title": "Title\n-- injected --"},
                       {"include": "includes/quarter-plan/FEATURE-cohorts.puml"}):
            with self.subTest(change=change):
                self.payload = deepcopy(original)
                self.payload["sections"][0].update(change)
                self.save_layout()
                with self.assertRaises(ValueError):
                    load_layout(self.gantt)
        self.payload = deepcopy(original)
        self.payload["sections"].append(deepcopy(original["sections"][0]))
        self.save_layout()
        self.quarter(success=False)

    def test_preamble_cannot_duplicate_sections(self):
        preamble = self.gantt / "preamble/common.puml"
        preamble.parent.mkdir()
        preamble.write_text("-- Unreviewed duplicate --\n")
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())

    def test_root_symlink_alias_resolves_like_macos(self):
        alias = self.root.parent / (self.root.name + "-alias")
        alias.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(alias.unlink)
        self.assertEqual(load_layout(self.gantt), load_layout(alias / "planning/2026-Q3/gantt"))

    def test_layout_source_symlink_and_duplicate_json_keys_block(self):
        source = self.gantt / "decision.md"
        source.unlink()
        source.symlink_to(self.registry)
        with self.assertRaises(ValueError):
            load_layout(self.gantt)
        self.layout.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            load_layout(self.gantt)


class PriorityScheduleTests(unittest.TestCase):
    def task(self, name, duration, start, role="FE", actual=False):
        return OVERLAY.Task(name + "/" + role, name, role + " work", "real", role, duration,
                            {"FE": "F1", "BE": "B1", "QA": "Q1"}[role], start, "",
                            start if actual else "", "", "in_progress" if actual else "planned", 50 if actual else 0, [])

    def schedule(self, high, low):
        tasks = {"high/" + high.task_id: high, "low/" + low.task_id: low}
        return OVERLAY.task_schedules(tasks, set(), date(2026, 9, 7), OVERLAY.DEFAULT_TEAM_RESOURCES, {"high": 0, "low": 1})

    def test_priority_beats_earlier_low_priority_plan(self):
        for role in ("BE", "FE", "QA"):
            with self.subTest(role=role):
                high = self.task("HIGH", 2, "2026-09-09", role)
                low = self.task("LOW", 5, "2026-09-07", role)
                schedule = self.schedule(high, low)
                self.assertEqual(schedule["high/" + high.task_id].start, date(2026, 9, 9))
                self.assertEqual(schedule["low/" + low.task_id].start, date(2026, 9, 11))

    def test_lower_priority_can_fill_gap_without_delaying_higher(self):
        high = self.task("HIGH", 2, "2026-09-09")
        low = self.task("LOW", 2, "2026-09-07")
        schedule = self.schedule(high, low)
        self.assertEqual(schedule["low/LOW/FE"].finish, date(2026, 9, 8))
        self.assertEqual(schedule["high/HIGH/FE"].start, date(2026, 9, 9))

    def test_confirmed_lower_priority_actual_dates_remain_fixed(self):
        high = self.task("HIGH", 2, "2026-09-07")
        low = self.task("LOW", 5, "2026-09-07", actual=True)
        schedule = self.schedule(high, low)
        self.assertEqual(schedule["low/LOW/FE"].start, date(2026, 9, 7))
        self.assertEqual(schedule["high/HIGH/FE"].start, date(2026, 9, 14))


if __name__ == "__main__":
    unittest.main()
