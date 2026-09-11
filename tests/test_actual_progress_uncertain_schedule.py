from copy import deepcopy
from datetime import date
import json
import unittest

import test_actual_progress_sources as sources


OVERLAY = sources.OVERLAY


class UncertainScheduleTests(unittest.TestCase):
    def task(self, role="FE", status="unknown", progress=None):
        return OVERLAY.Task("WORK/" + role, "WORK", role + " work", "real", role, 2,
                            "F1" if role == "FE" else "Q1", "2026-09-07", "2026-09-08",
                            "", "", status, progress, [])

    def test_unknown_does_not_pin_old_window_or_change_facts(self):
        for status in ("unknown", "in_progress", "created", "code_review", "planned"):
            with self.subTest(status=status):
                task = self.task(status=status)
                before = deepcopy(task)
                schedule = OVERLAY.task_schedules({task.task_id: task}, set(), date(2026, 9, 11), OVERLAY.DEFAULT_TEAM_RESOURCES)
                self.assertEqual(schedule[task.task_id].start, date(2026, 9, 11))
                self.assertEqual(schedule[task.task_id].finish, date(2026, 9, 14))
                self.assertEqual(task, before)
                text = "\n".join(OVERLAY.render_task(task, schedule))
                self.assertNotIn("% completed", text)
                if status != "planned":
                    self.assertIn("прогноз; фактическое начало неизвестно", text)
                    self.assertNotIn("Shifted not-started", text)

    def test_unknown_status_with_legacy_zero_also_uses_forecast(self):
        task = self.task(progress=0)
        self.assertFalse(OVERLAY.is_not_started(task))
        self.assertTrue(OVERLAY.needs_forecast_schedule(task))

    def test_confirmed_actual_start_is_not_shifted_even_with_unknown_progress(self):
        task = self.task(status="in_progress")
        task.actual_start = "2026-09-07"
        schedule = OVERLAY.task_schedules({task.task_id: task}, set(), date(2026, 9, 11), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(schedule[task.task_id].start, date(2026, 9, 7))
        self.assertNotIn("фактическое начало неизвестно", "\n".join(OVERLAY.render_task(task, schedule)))

    def test_unknown_qa_uses_feature_forecast_and_resource_capacity(self):
        frontend = self.task()
        qa = self.task("QA")
        other = self.task("QA", "planned", 0)
        other.task_id = "OTHER/QA"
        tasks = {"feature/" + task.task_id: task for task in (frontend, qa, other)}
        schedule = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 11), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertGreaterEqual(schedule["feature/WORK/QA"].start, date(2026, 9, 11))
        intervals = sorted((schedule[key].start, schedule[key].finish) for key in ("feature/WORK/QA", "feature/OTHER/QA"))
        self.assertGreater(intervals[1][0], intervals[0][1])
        self.assertEqual(qa.status, "unknown")

    def test_unknown_forecast_does_not_overlap_known_work_on_same_resource(self):
        known = self.task(status="in_progress", progress=50)
        known.task_id = "KNOWN/FE"
        known.actual_start, known.planned_finish = "2026-09-11", "2026-09-14"
        unknown = self.task()
        schedules = OVERLAY.task_schedules({task.task_id: task for task in (known, unknown)}, set(), date(2026, 9, 11), OVERLAY.DEFAULT_TEAM_RESOURCES)
        self.assertEqual(schedules[unknown.task_id].start, date(2026, 9, 15))


class RoleStartSourceTests(unittest.TestCase):
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    snapshot = sources.ActualProgressSourcesTests.snapshot
    quarter = sources.ActualProgressSourcesTests.quarter

    def setUp(self):
        sources.ActualProgressSourcesTests.setUp(self)
        self.write_tasks([
            "| | ITEM-100 | List | real | FE | 5 | F1 | 2026-09-01 | 2026-09-04 | | | in_progress | unknown | STORY-COHORT |",
            "| QA-COHORT | | Check | real | QA | 2 | Q2 | 2026-09-03 | 2026-09-09 | | | unknown | unknown | STORY-COHORT |",
        ])
        self.starts = self.feature / "execution/role-starts.json"
        self.payload = {"schema_version": 1, "roles": {"FE": {
            "actual_start": "2026-09-07", "analyst_confirmed": True, "source": "execution/tasks.md",
        }}}
        self.starts.write_text(json.dumps(self.payload))

    def test_role_start_anchors_plan_without_assigning_dates_to_tasks(self):
        before = self.registry.read_bytes()
        self.quarter()
        content = self.target.read_text()
        self.assertIn("[STORY_STORY_COHORT] starts 2026/09/07", content)
        self.assertIn("[STORY_STORY_COHORT] ends 2026/09/11", content)
        self.assertIn("[TASK_ITEM_100_FE] ends 2026/09/15", content)
        self.assertIn("фактическое начало неизвестно", content)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertTrue(all(not task.actual_start for task in OVERLAY.load_tasks(self.feature).values()))
        expanded = "\n".join(sources.EXPANDER.expand_file(self.gantt / "actual-progress.puml", [])).rstrip() + "\n"
        self.assertEqual((self.gantt / "actual-progress-confluence.puml").read_text(), expanded)

    def test_invalid_role_start_preserves_every_output(self):
        variants = []
        for field, value in (("actual_start", "2026-02-30"), ("actual_start", "2026/09/07"),
                             ("analyst_confirmed", False), ("source", "../outside.md"),
                             ("source", "missing.md"), ("source", "")):
            payload = deepcopy(self.payload)
            payload["roles"]["FE"][field] = value
            variants.append(payload)
        for value in (True, 0, 2):
            variants.append({**self.payload, "schema_version": value})
        variants.extend([{"schema_version": 1, "roles": {}}, {**self.payload, "unexpected": True}])
        for payload in variants:
            with self.subTest(payload=payload):
                self.starts.write_text(json.dumps(payload))
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(self.snapshot(), before)

    def test_actual_task_before_role_start_is_a_conflict(self):
        tasks = OVERLAY.load_tasks(self.feature)
        tasks["ITEM-100/FE"].actual_start = "2026-09-01"
        with self.assertRaisesRegex(ValueError, "противоречит"):
            OVERLAY.load_role_starts(self.feature, tasks)

    def test_later_known_task_does_not_replace_earlier_role_start(self):
        tasks = OVERLAY.load_tasks(self.feature)
        tasks["ITEM-100/FE"].actual_start = "2026-09-08"
        self.assertEqual(OVERLAY.load_role_starts(self.feature, tasks), {"FE": date(2026, 9, 7)})

    def test_symlink_and_duplicate_roles_are_rejected(self):
        self.starts.unlink()
        self.starts.symlink_to(self.registry)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            OVERLAY.load_role_starts(self.feature, {})
        self.starts.unlink()
        self.starts.write_text('{"schema_version":1,"roles":{"FE":{},"FE":{}}}')
        with self.assertRaisesRegex(ValueError, "Повторяющийся"):
            OVERLAY.load_role_starts(self.feature, {})


if __name__ == "__main__":
    unittest.main()
