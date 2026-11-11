from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-actual-progress-overlay.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("actual_progress_overlay", SCRIPT)
assert SPEC and SPEC.loader
OVERLAY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OVERLAY
SPEC.loader.exec_module(OVERLAY)


class ActualProgressRoleWorkItemsTests(unittest.TestCase):
    def write_feature(self, root: Path, rows: list[str]) -> Path:
        feature = root / "features" / "sample"
        execution = feature / "slices" / "delivery" / "execution"
        execution.mkdir(parents=True)
        (execution / "tasks.md").write_text(
            "\n".join([
                "| SberTrek | Summary | Kind | Role | Estimate (дн) | Executor | Planned Start | Planned Finish | Actual Start | Actual Finish | Status | Progress % | Related Stories | Details |",
                "|---|---|---|---|---:|---|---|---|---|---|---|---:|---|---|",
                *rows,
                "",
            ]),
            encoding="utf-8",
        )
        planning = feature / "planning"
        planning.mkdir(parents=True)
        (planning / "actualization.md").write_text(
            "\n".join([
                "| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |",
                "|---|---|---|---:|---|---|---|---|---|",
                "| STORY-1 | Shared delivery | 2026-09-01 | 3 | real | explicit | RSCON-100 | | |",
                "",
            ]),
            encoding="utf-8",
        )
        return feature

    def test_one_tracker_issue_expands_to_one_task_per_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature = self.write_feature(Path(temp), [
                "| RSCON-100 | AN Shared delivery | real | AN | 1 | A1 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
                "| RSCON-100 | [FE] Списковая форма [legacy] | real | FE | 2.5 | B1 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
                "| RSCON-100 | Shared delivery | real | QA | 4 | Q1 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
            ])
            tasks = OVERLAY.load_tasks(feature)
            self.assertEqual(set(tasks), {"RSCON-100/AN", "RSCON-100/FE", "RSCON-100/QA"})
            self.assertEqual(tasks["RSCON-100/FE"].estimate, 2.5)
            story = OVERLAY.load_story_map(feature)[0]
            self.assertEqual(
                OVERLAY.mapped_task_ids(story, tasks),
                ["RSCON-100/AN", "RSCON-100/FE", "RSCON-100/QA"],
            )
            schedules = OVERLAY.task_schedules(
                tasks, set(), date(2026, 9, 1), OVERLAY.DEFAULT_TEAM_RESOURCES,
            )
            self.assertEqual(schedules["RSCON-100/FE"].assignee, "B1")
            content = OVERLAY.render_feature(feature, "sample", set(), tasks, schedules)
            assert content
            self.assertIn("TASK_RSCON_100_AN", content)
            self.assertIn("TASK_RSCON_100_FE", content)
            self.assertIn("TASK_RSCON_100_QA", content)
            self.assertIn("[RSCON-100 · FE Списковая форма legacy · релиз не уточнён]", content)
            self.assertNotIn("[[FE]", content)
            self.assertIn("[RSCON-100 · QA Shared delivery · релиз не уточнён (прогноз QA)]", content)

    def test_duplicate_tracker_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature = self.write_feature(Path(temp), [
                "| RSCON-100 | FE first | real | FE | 2 | F1 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
                "| RSCON-100 | FE duplicate | real | FE | 3 | F2 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
            ])
            with self.assertRaisesRegex(ValueError, "Duplicate execution work item: RSCON-100/FE"):
                OVERLAY.load_tasks(feature)


def task(task_id: str, role: str, estimate: int, start: str, executor: str) -> object:
    return OVERLAY.Task(
        task_id=task_id,
        tracker_key=task_role_suffix(task_id)[0],
        summary=task_id,
        kind="real",
        role=role,
        estimate=estimate,
        executor=executor,
        planned_start=start,
        planned_finish="",
        actual_start="",
        actual_finish="",
        status="planned",
        progress=0,
        related_stories=["STORY-ONE"],
    )


def task_role_suffix(task_id: str) -> tuple[str, str]:
    return tuple(task_id.rsplit("/", 1)) if "/" in task_id else (task_id, "")


class QaFeatureSchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = {"AN": ["A1"], "BE": ["B1"], "FE": ["F1"], "QA": ["Q1"]}

    def test_qa_effort_ends_at_regression_deadline(self) -> None:
        tasks = {
            "ITEM/FE": task("ITEM/FE", "FE", 5, "2026-09-21", "F1"),
            "ITEM/QA": task("ITEM/QA", "QA", 2, "", "Q1"),
        }

        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 4), self.resources)

        self.assertEqual(date(2026, 9, 14), schedules["ITEM/QA"].start)
        self.assertEqual(date(2026, 9, 15), schedules["ITEM/QA"].finish)

    def test_short_qa_does_not_reserve_regression_wait_as_work(self) -> None:
        tasks = {
            "ITEM/FE": task("ITEM/FE", "FE", 2, "2026-09-21", "F1"),
            "ITEM/QA": task("ITEM/QA", "QA", 2, "", "Q1"),
        }

        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 4), self.resources)

        self.assertEqual(date(2026, 9, 9), schedules["ITEM/QA"].start)
        self.assertEqual(date(2026, 9, 10), schedules["ITEM/QA"].finish)

    def test_qa_is_rendered_last(self) -> None:
        tasks = {
            "ITEM/FE": task("ITEM/FE", "FE", 5, "2026-09-21", "F1"),
            "OTHER/FE": task("OTHER/FE", "FE", 7, "2026-09-21", "F1"),
            "ITEM/QA": task("ITEM/QA", "QA", 2, "", "Q1"),
        }
        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 4), self.resources)
        feature_dir = Path(self.id())

        original_story_loader = OVERLAY.load_story_map
        try:
            OVERLAY.load_story_map = lambda _: [
                OVERLAY.StoryMap("STORY-ONE", "Story", "2026-09-21", 5, "materialized", "explicit", list(tasks), [], [])
            ]
            rendered = OVERLAY.render_feature(feature_dir, "feature", set(), tasks, schedules)
        finally:
            OVERLAY.load_story_map = original_story_loader

        assert rendered
        self.assertLess(rendered.index("TASK_ITEM_FE"), rendered.index("TASK_ITEM_QA"))
        self.assertLess(rendered.index("TASK_OTHER_FE"), rendered.index("TASK_ITEM_QA"))

    def test_feature_scope_does_not_invent_be_dependency(self) -> None:
        tasks = {
            "feature/BACKEND-ONE": task("BACKEND-ONE", "BE", 5, "2026-09-07", "B1"),
            "feature/FRONTEND-ONE": task("FRONTEND-ONE", "FE", 2, "2026-09-07", "F1"),
        }

        schedules = OVERLAY.task_schedules(tasks, set(), date(2026, 9, 4), self.resources)

        self.assertEqual(date(2026, 9, 4), schedules["feature/FRONTEND-ONE"].start)


if __name__ == "__main__":
    unittest.main()
