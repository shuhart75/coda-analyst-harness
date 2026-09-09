from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location("source_test_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OVERLAY = load_tool("sync-actual-progress-overlay")
EXPANDER = load_tool("expand-plantuml-includes")
BASELINE = load_tool("actualization_baseline")
HEADER = "| Task ID | Jira | Summary | Kind | Role | Estimate (дн) | Executor | Planned Start | Planned Finish | Actual Start | Actual Finish | Status | Progress % | Related Stories |"
SEPARATOR = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"


class ActualProgressSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.gantt = self.root / "planning/2026-Q3/gantt"
        self.actual = self.gantt / "includes/actual-progress"
        self.actual.mkdir(parents=True)
        self.feature = self.root / "features/cohorts"
        (self.feature / "execution").mkdir(parents=True)
        (self.feature / "planning").mkdir()
        self.registry = self.feature / "execution/tasks.md"
        self.map = self.feature / "planning/actualization.md"
        self.write_tasks()
        self.map.write_text(
            "| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| STORY-COHORT | Delivery | 2026-09-01 | 5 | materialized | explicit | ITEM-100/FE, QA-COHORT | | |\n",
            encoding="utf-8",
        )
        self.target = self.actual / "FEATURE-cohorts.puml"
        self.target.write_text("existing manual overlay\n", encoding="utf-8")
        for name in ("quarter-plan", "commander-plan", "actual-progress", "actual-progress-confluence"):
            (self.gantt / (name + ".puml")).write_text("preserved " + name + "\n", encoding="utf-8")

    def write_tasks(self, rows: list[str] | None = None) -> None:
        if rows is None:
            rows = [
                "| | ITEM-100 | List | real | FE | 5 | F1 | 2026-09-01 | 2026-09-04 | 2026-09-01 | | in-review | 75 | STORY-COHORT |",
                "| QA-COHORT | | Cross-feature check | real | QA | 2 | Q2 | 2026-09-03 | 2026-09-09 | 2026-09-03 | | in-progress | 30 | STORY-COHORT |",
            ]
        self.registry.write_text("\n".join([HEADER, SEPARATOR, *rows, ""]), encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def quarter(self, success: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/sync-quarter-gantt.py"), str(self.gantt), "--actual-only"],
            text=True, capture_output=True, env={**os.environ, "HARNESS_TODAY": "2026-09-09"},
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_root_registry_supports_local_real_qa_without_tracker_key(self) -> None:
        tasks = OVERLAY.load_tasks(self.feature)
        self.assertEqual(set(tasks), {"ITEM-100/FE", "QA-COHORT"})
        self.assertEqual(tasks["QA-COHORT"].tracker_key, "")
        self.assertEqual(tasks["QA-COHORT"].progress, 30)
        outputs = OVERLAY.prepare_outputs(self.root, "2026-Q3", ["cohorts"])
        self.assertIn("TASK_QA_COHORT", outputs[self.target])
        self.assertIn("30% completed", outputs[self.target])
        self.assertEqual(OVERLAY.story_progress(OVERLAY.load_story_map(self.feature)[0], tasks), 62)

    def test_root_and_legacy_registry_merge_without_hidden_precedence(self) -> None:
        legacy = self.feature / "slices/legacy/execution/tasks.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            "| Jira | Summary | Kind | Role | Estimate (дн) | Planned Start | Status | Progress % |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| ITEM-200 | Additional | real | BE | 2 | 2026-09-01 | planned | 0 |\n",
            encoding="utf-8",
        )
        self.assertIn("ITEM-200/BE", OVERLAY.load_tasks(self.feature))
        legacy.write_text(legacy.read_text().replace("ITEM-200", "ITEM-100").replace("| BE |", "| FE |"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            OVERLAY.load_tasks(self.feature)

    def test_duplicate_tracker_role_with_different_local_ids_is_rejected(self) -> None:
        self.registry.write_text(self.registry.read_text().replace(
            "| QA-COHORT | | Cross-feature check | real | QA |",
            "| OTHER | ITEM-100 | Cross-feature check | real | FE |",
        ), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate tracker role"):
            OVERLAY.load_tasks(self.feature)

    def test_missing_map_does_not_delete_or_partially_write_outputs(self) -> None:
        broken = self.root / "features/broken"
        broken.mkdir()
        old = self.actual / "FEATURE-broken.puml"
        old.write_text("manual work\n", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "actualization"):
            OVERLAY.prepare_outputs(self.root, "2026-Q3", ["cohorts", "broken"])
        self.assertEqual(self.snapshot(), before)
        self.quarter(success=False)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_feature_preserves_existing_overlay(self) -> None:
        self.target.rename(self.actual / "FEATURE-unknown.puml")
        before = self.snapshot()
        self.assertIn("actual-progress-features.json", self.quarter(success=False).stderr)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_overlay_to_feature_mapping_preserves_output_name(self) -> None:
        self.target.rename(self.actual / "FEATURE-cohort-simulation.puml")
        (self.gantt / "actual-progress-features.json").write_text(json.dumps({
            "schema_version": 1, "features": {"cohort-simulation": "cohorts"},
        }), encoding="utf-8")
        self.quarter()
        self.assertFalse(self.target.exists())
        self.assertIn("TASK_QA_COHORT", (self.actual / "FEATURE-cohort-simulation.puml").read_text())

    def test_actual_only_preserves_approved_plans_and_export_matches_expansion(self) -> None:
        approved = {name: (self.gantt / (name + ".puml")).read_bytes() for name in ("quarter-plan", "commander-plan")}
        self.quarter()
        for name, content in approved.items():
            self.assertEqual((self.gantt / (name + ".puml")).read_bytes(), content)
        expanded = "\n".join(EXPANDER.expand_file(self.gantt / "actual-progress.puml", [])).rstrip() + "\n"
        self.assertEqual((self.gantt / "actual-progress-confluence.puml").read_text(), expanded)
        before = self.snapshot()
        self.quarter()
        self.assertEqual(self.snapshot(), before)

    def test_missing_nested_include_blocks_before_overlay_publication(self) -> None:
        preamble = self.gantt / "preamble/common.puml"
        preamble.parent.mkdir()
        preamble.write_text("!include absent.puml\n", encoding="utf-8")
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(self.snapshot(), before)

    def test_unresolved_story_task_and_related_story_references_block(self) -> None:
        original = self.map.read_text()
        for contents in (original.replace("ITEM-100/FE", "ABSENT"), original.replace("STORY-COHORT", "STORY-OTHER")):
            with self.subTest(contents=contents):
                self.map.write_text(contents, encoding="utf-8")
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(self.snapshot(), before)

    def test_incomplete_canonical_rows_do_not_guess_values(self) -> None:
        original = self.registry.read_text()
        for replacement in ("", "5d", "nan", "-1"):
            with self.subTest(estimate=replacement):
                self.registry.write_text(original.replace("| 5 | F1 |", f"| {replacement} | F1 |"), encoding="utf-8")
                with self.assertRaises(ValueError):
                    OVERLAY.load_tasks(self.feature)
        self.registry.write_text(original.replace("| 75 |", "| 175 |"), encoding="utf-8")
        with self.assertRaises(ValueError):
            OVERLAY.load_tasks(self.feature)

    def test_alias_collision_is_not_silently_rendered(self) -> None:
        self.registry.write_text(self.registry.read_text() + (
            "| QA_COHORT | | Duplicate alias | real | QA | 1 | Q2 | 2026-09-03 | | 2026-09-03 | | in-progress | 30 | STORY-COHORT |\n"
        ), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "PlantUML"):
            OVERLAY.prepare_outputs(self.root, "2026-Q3", ["cohorts"])

    def test_individual_cards_do_not_replace_missing_registry(self) -> None:
        self.registry.unlink()
        cards = self.feature / "execution/tasks"
        cards.mkdir()
        (cards / "ITEM-100.md").write_text("75 percent\n", encoding="utf-8")
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(self.snapshot(), before)

    def test_incomplete_story_map_does_not_guess_baseline_or_state(self) -> None:
        original = self.map.read_text()
        for invalid in (
            original.replace("| 5 | materialized |", "| | materialized |"),
            original.replace("| 5 | materialized |", "| many | materialized |"),
            original.replace("| materialized |", "| unknown |"),
        ):
            with self.subTest(invalid=invalid):
                self.map.write_text(invalid, encoding="utf-8")
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(self.snapshot(), before)

    def test_local_id_cannot_shadow_another_tasks_tracker_key(self) -> None:
        self.registry.write_text(self.registry.read_text().replace("QA-COHORT", "ITEM-100"), encoding="utf-8")
        self.map.write_text(self.map.read_text().replace("QA-COHORT", "ITEM-100"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "неоднозначная"):
            OVERLAY.prepare_outputs(self.root, "2026-Q3", ["cohorts"])

    def test_write_failure_restores_previous_files(self) -> None:
        before = self.snapshot()
        real_write = OVERLAY.atomic_write
        failed = False
        def fail_once(path, content):
            nonlocal failed
            if path.name == "actual-progress.puml" and not failed:
                failed = True
                raise OSError("injected write failure")
            real_write(path, content)
        with patch.object(OVERLAY, "atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected"):
                OVERLAY.publish_outputs({
                    self.target: "new overlay\n",
                    self.actual / "FEATURE-new.puml": "new file\n",
                    self.gantt / "actual-progress.puml": "new view\n",
                })
        self.assertEqual(self.snapshot(), before)

    def absent_baseline(self) -> None:
        self.map.write_text(
            "## Mapping\n\n"
            "| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On | Baseline State |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| STORY-COHORT | Delivery | | | materialized | explicit | ITEM-100/FE, QA-COHORT | | | absent |\n",
            encoding="utf-8",
        )

    def test_explicit_absent_baseline_renders_tasks_without_plan_bars(self) -> None:
        self.absent_baseline()
        approved = {name: (self.gantt / (name + ".puml")).read_bytes() for name in ("quarter-plan", "commander-plan")}
        before = self.map.read_bytes()
        self.quarter()
        content = self.target.read_text()
        self.assertNotIn("[PLAN ", content)
        self.assertNotIn("as [STORY_", content)
        self.assertIn("No approved baseline: STORY-COHORT; progress=62%", content)
        self.assertIn("TASK_ITEM_100_FE", content)
        self.assertIn("TASK_QA_COHORT", content)
        self.assertEqual(self.map.read_bytes(), before)
        for name, contents in approved.items():
            self.assertEqual((self.gantt / (name + ".puml")).read_bytes(), contents)
        expanded = "\n".join(EXPANDER.expand_file(self.gantt / "actual-progress.puml", [])).rstrip() + "\n"
        self.assertEqual((self.gantt / "actual-progress-confluence.puml").read_text(), expanded)
        snapshot = self.snapshot()
        self.quarter()
        self.assertEqual(self.snapshot(), snapshot)

    def test_absent_baseline_is_independent_of_actualization_state(self) -> None:
        self.absent_baseline()
        original = self.map.read_text()
        for state in ("virtual", "mixed", "materialized", "done"):
            with self.subTest(state=state):
                self.map.write_text(original.replace("| materialized |", f"| {state} |"), encoding="utf-8")
                story = OVERLAY.load_story_map(self.feature)[0]
                self.assertEqual(story.baseline_state, "absent")
                self.assertIsNone(story.baseline_duration)
                self.assertEqual(story.state, state)
                self.assertNotIn("[PLAN ", OVERLAY.prepare_outputs(self.root, "2026-Q3", ["cohorts"])[self.target])

    def test_absent_baseline_with_dates_or_ambiguous_state_blocks(self) -> None:
        self.absent_baseline()
        original = self.map.read_text()
        for contents in (
            original.replace("| Delivery | | |", "| Delivery | 2026-09-01 | |"),
            original.replace("| Delivery | | |", "| Delivery | | 5 |"),
            original.replace("| absent |", "| unknown |"),
            original.replace("| absent |", "| |"),
        ):
            with self.subTest(contents=contents):
                self.map.write_text(contents, encoding="utf-8")
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(self.snapshot(), before)

    def test_mixed_baselines_preserve_existing_plan_bars(self) -> None:
        self.absent_baseline()
        self.map.write_text(self.map.read_text() +
            "| STORY-BASELINE | Planned | 2026-09-01 | 5 | virtual | explicit | | | | present |\n",
            encoding="utf-8",
        )
        self.quarter()
        content = self.target.read_text()
        self.assertIn("as [STORY_STORY_BASELINE]", content)
        self.assertNotIn("as [STORY_STORY_COHORT]", content)
        self.assertEqual(BASELINE.baseline_rows(self.map), [["STORY-BASELINE", "2026-09-01", "5"]])

    def test_approved_baseline_cannot_be_removed_using_absent(self) -> None:
        snapshots = self.root / "planning/approved-plans"
        snapshots.mkdir()
        expected = [["STORY-COHORT", "2026-09-01", "5"]]
        (snapshots / "2026-Q3.json").write_text(json.dumps({
            "actualization_baseline": {"features/cohorts/planning/actualization.md": expected},
        }), encoding="utf-8")
        self.absent_baseline()
        before = self.snapshot()
        self.assertIn("approved actualization baseline was modified", self.quarter(success=False).stderr)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-planning.py"), str(self.root)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("approved actualization baseline was modified", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_absent_baseline_does_not_waive_missing_qa_estimate(self) -> None:
        self.absent_baseline()
        self.registry.write_text(self.registry.read_text().replace("| QA | 2 |", "| QA | |"), encoding="utf-8")
        before = self.snapshot()
        self.assertIn("уточни оценку у аналитика", self.quarter(success=False).stderr)
        self.assertEqual(self.snapshot(), before)

    def test_absent_baseline_still_requires_explicit_task_membership(self) -> None:
        self.absent_baseline()
        self.map.write_text(self.map.read_text().replace(
            "| materialized | explicit | ITEM-100/FE, QA-COHORT |",
            "| virtual | explicit | |",
        ), encoding="utf-8")
        self.registry.write_text(self.registry.read_text().replace("STORY-COHORT", ""), encoding="utf-8")
        before = self.snapshot()
        self.assertIn("отсутствует состав истории", self.quarter(success=False).stderr)
        self.assertEqual(self.snapshot(), before)

    def test_qa_candidate_estimate_is_not_defaulted(self) -> None:
        candidate = self.feature / "execution/task-candidates.md"
        for header, row in (
            ("Candidate ID | Summary | Role | Estimate (дн)", "LOCAL-QA | QA Check | QA |"),
            ("Идентификатор | Краткое описание | Роль | Оценка (дн)", "LOCAL-QA | QA Check | QA |"),
        ):
            with self.subTest(header=header):
                candidate.write_text(f"| {header} |\n|---|---|---|---|\n| {row} |\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "LOCAL-QA.*уточни оценку у аналитика"):
                    OVERLAY.load_tasks(self.feature)

    def test_legacy_qa_estimate_is_never_defaulted(self) -> None:
        legacy = self.feature / "slices/legacy/execution/tasks.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            "| Jira | Summary | Kind | Role | Estimate (дн) | Planned Start | Status | Progress % |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| ITEM-200 | QA Additional | real | QA | | 2026-09-01 | planned | 0 |\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "уточни оценку у аналитика"):
            OVERLAY.load_tasks(self.feature)

    def test_qa_label_omits_source_number_but_keeps_traceability(self) -> None:
        original = self.registry.read_text()
        for summary in ("[FE] List", "ITEM-100 FE List", "QA ITEM-100/QA FE List", "QA List"):
            with self.subTest(summary=summary):
                self.registry.write_text(original.replace(
                    "| QA-COHORT | | Cross-feature check |", f"| | ITEM-100 | {summary} |",
                ), encoding="utf-8")
                tasks = OVERLAY.load_tasks(self.feature)
                self.assertEqual(tasks["ITEM-100/QA"].tracker_key, "ITEM-100")
                self.assertEqual(tasks["ITEM-100/QA"].estimate, 2)
                self.assertEqual(tasks["ITEM-100/FE"].estimate, 5)
                self.assertEqual(OVERLAY.role_prefixed_summary(tasks["ITEM-100/QA"]), "QA List")
                schedules = OVERLAY.task_schedules(tasks, set(), OVERLAY.harness_today(), OVERLAY.DEFAULT_TEAM_RESOURCES)
                self.assertTrue(OVERLAY.render_task(tasks["ITEM-100/QA"], schedules)[0].startswith("[QA List] as [TASK_ITEM_100_QA]"))

    def test_absent_baseline_does_not_waive_missing_task_dates(self) -> None:
        self.absent_baseline()
        self.registry.write_text(self.registry.read_text().replace(
            "| 2026-09-03 | 2026-09-09 | 2026-09-03 |", "| | | |",
        ), encoding="utf-8")
        before = self.snapshot()
        self.assertIn("дата начала", self.quarter(success=False).stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
