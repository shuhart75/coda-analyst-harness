from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
OVERLAY = importlib.import_module("sync-actual-progress-overlay")
PLANNING = importlib.import_module("sync-planning-gantt")


def task(identifier, role, estimate, start, progress=0, executor=""):
    return OVERLAY.Task(identifier, identifier.split("/")[0], role + " work", "real", role,
                        estimate, executor, start, "", start if progress else "", "",
                        "in-progress" if progress else "planned", progress, [])


def story(role, duration=5):
    return OVERLAY.StoryMap("STORY-DEMO-" + role, role + " work", "2026-04-01", duration,
                            "materialized", "explicit", [], [], [], role=role)


class PlanFactRoleTests(unittest.TestCase):
    def test_plan_moves_to_role_start_but_does_not_stretch_or_shrink(self):
        plan = story("FE", 5)
        for actual_finish in (date(2026, 9, 8), date(2026, 10, 1)):
            with self.subTest(finish=actual_finish):
                tasks = {"ONE": task("ONE", "FE", 8, "2026-09-07", 100)}
                schedules = {"ONE": OVERLAY.ScheduledTask(date(2026, 9, 7), actual_finish, "F1", False)}
                start, finish = OVERLAY.story_dates(plan, tasks, schedules, {}, {date(2026, 9, 9)})
                self.assertEqual(start, date(2026, 9, 7))
                self.assertEqual(finish, date(2026, 9, 14))
                self.assertEqual(plan.baseline_start, "2026-04-01")
                self.assertEqual(plan.baseline_duration, 5)

    def test_each_role_uses_its_own_first_actual_start(self):
        tasks = {"BE": task("BE", "BE", 3, "2026-09-01", 100),
                 "FE": task("FE", "FE", 3, "2026-09-07", 50),
                 "LATER": task("LATER", "FE", 2, "2026-08-01")}
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 9), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(OVERLAY.story_dates(story("BE"), tasks, schedules, {}, set())[0], date(2026, 9, 1))
        self.assertEqual(OVERLAY.story_dates(story("FE"), tasks, schedules, {}, set())[0], date(2026, 9, 7))

    def test_role_progress_uses_all_role_tasks_and_exact_person_days(self):
        tasks = {"SMALL": task("SMALL", "FE", .25, "2026-09-01", 100),
                 "LARGE": task("LARGE", "FE", 1, "2026-09-01"),
                 "QA": task("QA", "QA", 10, "2026-09-01", 100)}
        self.assertEqual(OVERLAY.story_progress(story("FE"), tasks), 20)
        self.assertEqual(OVERLAY.story_progress(story("QA"), tasks), 100)
        tasks["LARGE"].status = "superseded"
        self.assertEqual(OVERLAY.story_progress(story("FE"), tasks), 100)

    def test_plan_without_role_tasks_preserves_original_window(self):
        self.assertEqual(OVERLAY.story_dates(story("FE", 3), {}, {}, {}, set()),
                         (date(2026, 4, 1), date(2026, 4, 3)))
        self.assertEqual(OVERLAY.story_progress(story("FE"), {}), 0)

    def test_unknown_progress_propagates_without_counting_excluded_or_other_roles(self):
        tasks = {"FE": task("FE", "FE", 3, "2026-09-07", 100),
                 "UNKNOWN": task("UNKNOWN", "FE", 2, "2026-09-07"),
                 "QA": task("QA", "QA", 5, "2026-09-07")}
        tasks["UNKNOWN"].progress = None
        tasks["QA"].progress = None
        self.assertIsNone(OVERLAY.story_progress(story("FE"), tasks))
        for status in ("cancelled", "superseded"):
            tasks["UNKNOWN"].status = status
            self.assertEqual(OVERLAY.story_progress(story("FE"), tasks), 100)
        tasks["UNKNOWN"].status = "planned"
        tasks["UNKNOWN"].kind = "candidate"
        self.assertEqual(OVERLAY.story_progress(story("FE"), tasks), 100)

    def test_qa_uses_earliest_fe_in_feature_not_card_with_qa_estimate(self):
        tasks = {"feature/LONG/FE": task("LONG/FE", "FE", 10, "2026-09-07", executor="F1"),
                 "feature/SHORT/FE": task("SHORT/FE", "FE", 3, "2026-09-07", executor="F2"),
                 "feature/LONG/QA": task("LONG/QA", "QA", 2, "", executor="Q1"),
                 "other/FE": task("FE", "FE", 1, "2026-09-01", 100, "F2")}
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 7), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(schedules["feature/LONG/QA"].start, date(2026, 9, 8))

    def test_multiple_local_qa_tasks_respect_capacity_and_actual_dates(self):
        tasks = {"FE": task("FE", "FE", 3, "2026-09-07", executor="F1"),
                 "QA1": task("QA1", "QA", 2, "", executor="Q1"),
                 "QA2": task("QA2", "QA", 2, "", executor="Q1"),
                 "QA3": task("QA3", "QA", 1, "2026-09-01", 100, "Q1")}
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 7), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(schedules["QA1"].start, date(2026, 9, 8))
        self.assertGreater(schedules["QA2"].start, schedules["QA1"].finish)
        self.assertEqual(schedules["QA3"].start, date(2026, 9, 1))

    def test_be_only_qa_can_start_after_first_be_finishes(self):
        tasks = {"BE": task("BE", "BE", 2, "2026-09-07", executor="B1"),
                 "QA": task("QA", "QA", 1, "", executor="Q1")}
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 7), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(schedules["QA"].start, date(2026, 9, 9))

    def test_follow_up_links_reuse_baselines_and_accept_shared_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            feature = Path(directory)
            (feature / "planning").mkdir()
            path = feature / "planning/actualization.md"
            text = (
                "## Mapping\n\n"
                "| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By |\n"
                "|---|---|---|---|---|---|---|\n"
                "| STORY-001 | Backend original | 2026-04-16 | 16 | materialized | explicit | ORIGINAL-BE |\n"
                "| STORY-002 | Frontend original | 2026-05-22 | 13 | materialized | explicit | ORIGINAL-FE |\n"
                "\n## Q3 follow-up\n\n"
                "| Story ID | Summary | Quarter | Actualization State | Mapping Mode | Replaced By | Notes |\n"
                "|---|---|---|---|---|---|---|\n"
                "| STORY-001 | Backend CSV | 2026-Q3 | materialized | explicit | CSV-BE | |\n"
                "| STORY-002 | Frontend CSV | 2026-Q3 | materialized | explicit | CSV-FE | |\n"
                "| STORY-001, STORY-002 | QA CSV | 2026-Q3 | materialized | explicit | QA-CSV | |\n"
            )
            path.write_text(text, encoding="utf-8")
            plans = OVERLAY.load_story_map(feature)
            self.assertEqual([plan.story_id for plan in plans], ["STORY-001", "STORY-002"])
            self.assertEqual([plan.baseline_duration for plan in plans], [16, 13])
            self.assertEqual(plans[0].summary, "Backend original")
            self.assertEqual(plans[0].additional_tasks, ["CSV-BE", "QA-CSV"])
            tasks = {"CSV-BE": task("CSV-BE", "BE", 2, "2026-09-01", 100),
                     "CSV-FE": task("CSV-FE", "FE", 2, "2026-09-01", 50),
                     "QA-CSV": task("QA-CSV", "QA", 10, "2026-09-01")}
            self.assertEqual(OVERLAY.story_progress(plans[0], tasks), 100)
            self.assertEqual(OVERLAY.story_progress(plans[1], tasks), 50)
            self.assertEqual(path.read_text(), text)

    def test_parallel_actual_work_matches_team_day_plan_duration(self):
        tasks = {"FIRST": task("FIRST", "FE", 5, "2026-09-07", executor="F1"),
                 "SECOND": task("SECOND", "FE", 5, "2026-09-07", executor="F2")}
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 7), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(OVERLAY.story_dates(story("FE", 5), tasks, schedules, {}, set())[1],
                         max(scheduled.finish for scheduled in schedules.values()))


class PlanningUnitsTests(unittest.TestCase):
    def test_loader_requires_unit_and_writes_explicit_role_to_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planning = root / "features/demo/planning"
            planning.mkdir(parents=True)
            path = planning / "estimates.md"
            original = (
                "| Story ID | Role | Summary | Agreed effort, дн | Max parallelism | Efficiency |\n"
                "|---|---|---|---|---|---|\n"
                "| STORY-DEMO-FE | FE | Interface | 10 | 2 | .5 |\n"
            )
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Estimate Unit"):
                PLANNING.load_stories(root, "demo")
            path.write_text(original.replace("Efficiency |", "Efficiency | Estimate Unit |").replace(
                "|---|---|---|---|---|---|", "|---|---|---|---|---|---|---|",
            ).replace("| .5 |", "| .5 | team-days |"), encoding="utf-8")
            plan = PLANNING.load_stories(root, "demo")[0]
            self.assertEqual(plan.estimate_unit, "team-days")
            scheduled = PLANNING.earliest_slot(plan, date(2026, 9, 7), ["F1", "F2"], {}, {}, {}, set(), 20)
            PLANNING.write_actualization(root, "2026-Q3", "demo", [scheduled])
            mapping = OVERLAY.load_story_map(root / "features/demo")[0]
            self.assertEqual(mapping.role, "FE")
            self.assertEqual(mapping.baseline_duration, 12)

    def schedule(self, unit, buffer=0):
        plan = PLANNING.Story("PLAN-FE", "FE", "Frontend", 10, 2, .5, [], None, unit)
        return PLANNING.earliest_slot(plan, date(2026, 9, 7), ["F1", "F2"],
                                      {"F1": .5, "F2": .5}, {}, {}, set(), buffer)

    def test_team_days_are_not_divided_by_parallelism_or_efficiency(self):
        self.assertEqual(self.schedule("team-days").duration, 10)
        self.assertEqual(self.schedule("team-days", 20).duration, 12)

    def test_legacy_person_days_remain_explicitly_distinct(self):
        self.assertEqual(self.schedule("person-days").duration, 20)

    def test_insufficient_team_does_not_silently_keep_duration(self):
        plan = PLANNING.Story("PLAN-FE", "FE", "Frontend", 10, 2, .5, [], None, "team-days")
        with self.assertRaisesRegex(ValueError, "недостаточно ресурсов"):
            PLANNING.earliest_slot(plan, date(2026, 9, 7), ["F1"], {}, {}, {}, set(), 0)


if __name__ == "__main__":
    unittest.main()
