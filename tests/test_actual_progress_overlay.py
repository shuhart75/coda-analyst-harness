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
                "| Jira | Summary | Kind | Role | Estimate (дн) | Executor | Planned Start | Planned Finish | Actual Start | Actual Finish | Status | Progress % | Related Stories | Details |",
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
            self.assertEqual(schedules["RSCON-100/FE"].assignee, "F1")
            content = OVERLAY.render_feature(feature, "sample", set(), tasks, schedules)
            assert content
            self.assertIn("TASK_RSCON_100_AN", content)
            self.assertIn("TASK_RSCON_100_FE", content)
            self.assertIn("TASK_RSCON_100_QA", content)
            self.assertIn("[FE Списковая форма legacy]", content)
            self.assertNotIn("[[FE]", content)
            self.assertIn("[QA Shared delivery]", content)

    def test_duplicate_tracker_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature = self.write_feature(Path(temp), [
                "| RSCON-100 | FE first | real | FE | 2 | F1 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
                "| RSCON-100 | FE duplicate | real | FE | 3 | F2 | 2026-09-01 | | | | planned | 0 | STORY-1 | |",
            ])
            with self.assertRaisesRegex(ValueError, "Duplicate execution work item: RSCON-100/FE"):
                OVERLAY.load_tasks(feature)


if __name__ == "__main__":
    unittest.main()
