from datetime import date
import unittest

import test_actual_progress_sources as sources

OVERLAY = sources.OVERLAY


class ActualProgressCompletionTests(unittest.TestCase):
    setUp = sources.ActualProgressSourcesTests.setUp
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    snapshot = sources.ActualProgressSourcesTests.snapshot
    quarter = sources.ActualProgressSourcesTests.quarter

    def bounded_tasks(self, actual_start="", actual_finish=""):
        self.write_tasks([
            f"| | ITEM-100 | List | real | FE | 5 | F1 | 2026-10-01 | 2026-10-07 | {actual_start} | {actual_finish} | done | 100 | STORY-COHORT |",
            "| QA-COHORT | | Check | real | QA | 2 | Q2 | 2026-10-01 | 2026-10-02 | | | done | 100 | STORY-COHORT |",
        ])
        lines = self.registry.read_text().splitlines()
        lines[0] += " Completed By |"
        lines[1] += "---|"
        for index in range(2, len(lines)):
            lines[index] += " 2026-08-27 |"
        self.registry.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_boundary_is_not_an_interval_or_resource_reservation(self):
        self.bounded_tasks()
        tasks = OVERLAY.load_tasks(self.feature)
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 9), {"FE": ["F1"], "QA": ["Q2"]})
        self.assertEqual(schedules, {})
        self.quarter()
        content = self.target.read_text()
        self.assertIn("[TASK_ITEM_100_FE] happens at 2026/08/27", content)
        self.assertNotIn("[TASK_ITEM_100_FE] ends", content)
        self.assertNotIn("on {F1}", content)
        self.assertIn("завершено к 2026-08-27", content)
        self.assertIn("[STORY_STORY_COHORT] starts 2026/09/01", content)
        self.assertIn("[STORY_STORY_COHORT] is 100% completed", content)
        before = self.snapshot()
        self.quarter()
        self.assertEqual(self.snapshot(), before)

    def test_bound_allows_no_planned_dates(self):
        self.bounded_tasks()
        self.registry.write_text(self.registry.read_text().replace("2026-10-01", "").replace("2026-10-07", "").replace("2026-10-02", ""))
        self.quarter()
        self.assertIn("[TASK_QA_COHORT] happens at 2026/08/27", self.target.read_text())

    def test_known_interval_is_preserved(self):
        self.bounded_tasks("2026-08-10", "2026-08-12")
        self.quarter()
        content = self.target.read_text()
        self.assertIn("[TASK_ITEM_100_FE] on {F1} starts 2026/08/10", content)
        self.assertIn("[TASK_ITEM_100_FE] ends 2026/08/12", content)
        self.assertNotIn("[TASK_ITEM_100_FE] happens at", content)

    def test_partial_actual_dates_do_not_invent_an_interval(self):
        for start, finish in (("2026-08-10", ""), ("", "2026-08-12")):
            with self.subTest(start=start, finish=finish):
                self.bounded_tasks(start, finish)
                tasks = OVERLAY.load_tasks(self.feature)
                self.assertEqual(OVERLAY.task_schedules(tasks, set(), date(2026, 9, 9), {}), {})
                self.quarter()
                content = self.target.read_text()
                self.assertIn("[TASK_ITEM_100_FE] happens at 2026/08/27", content)
                self.assertNotIn("[TASK_ITEM_100_FE] ends", content)
                if start:
                    self.assertIn("[STORY_STORY_COHORT] starts 2026/08/10", content)

    def test_invalid_bound_preserves_existing_outputs(self):
        self.bounded_tasks()
        original = self.registry.read_text()
        variants = [
            original.replace("2026-08-27", "not-a-date"),
            original.replace("2026-08-27", "2026-02-30"),
            original.replace("2026-08-27", "2026/08/27"),
            original.replace("| done |", "| planned |"),
            original.replace("| 100 |", "| 99 |"),
            original.replace("| | | done |", "| 2026-08-28 | | done |"),
            original.replace("| | | done |", "| invalid | | done |"),
            original.replace("| | | done |", "| 2026-08-12 | 2026-08-10 | done |"),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.registry.write_text(variant)
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(self.snapshot(), before)

    def test_cancelled_tasks_do_not_affect_schedule_role_or_progress(self):
        self.bounded_tasks()
        self.registry.write_text(self.registry.read_text() + "| CANCELLED | | BE Removed | real | BE | 99 | B1 | 2026-10-01 | 2026-10-07 | | | cancelled | 0 | STORY-COHORT | |\n")
        self.map.write_text(self.map.read_text().replace("QA-COHORT |", "QA-COHORT, CANCELLED |"))
        tasks = OVERLAY.load_tasks(self.feature)
        story = OVERLAY.load_story_map(self.feature)[0]
        self.assertEqual(OVERLAY.story_type(story, tasks), "FE")
        self.assertEqual(OVERLAY.story_progress(story, tasks), 100)
        self.assertEqual(OVERLAY.task_schedules(tasks, set(), date(2026, 9, 9), {}), {})
        self.quarter()
        self.assertIn("Excluded task: CANCELLED; status=cancelled", self.target.read_text())
        self.assertNotIn("as [TASK_CANCELLED]", self.target.read_text())

    def test_only_excluded_tasks_keep_baseline_without_replacements(self):
        for status in ("cancelled", "canceled", "superseded"):
            with self.subTest(status=status):
                self.write_tasks()
                self.registry.write_text(self.registry.read_text().replace("in-review", status).replace("in-progress", status))
                self.quarter()
                content = self.target.read_text()
                self.assertIn("as [STORY_STORY_COHORT]", content)
                self.assertIn("[STORY_STORY_COHORT] is 0% completed", content)
                self.assertNotIn("as [TASK_", content)

    def test_missing_start_without_bound_still_blocks(self):
        self.write_tasks()
        self.registry.write_text(self.registry.read_text().replace("2026-09-01", ""))
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
