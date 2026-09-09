from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ForecastExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.gantt = self.root / "planning/2026-Q3/gantt"
        self.feature = self.root / "features/optimizer"
        self.context = self.feature / "planning/planning-context.md"
        self.context.parent.mkdir(parents=True)
        self.context.write_text("Outside Q3 forecast. Next quarter undecided.\n", encoding="utf-8")
        self.plan = self.gantt / "includes/quarter-plan/FEATURE-optimizer.puml"
        self.plan.parent.mkdir(parents=True)
        self.plan.write_text(
            "' FEATURE: Optimizer\n[Original plan] as [OPT_PLAN] starts 2026/08/03\n"
            "[OPT_PLAN] lasts 5 days\n", encoding="utf-8",
        )
        self.config = self.gantt / "actual-progress-features.json"
        self.decision = {
            "state": "outside-quarter", "analyst_confirmed": True,
            "reason": "Not expected in Q3; cancellation or transfer undecided.",
            "source": "features/optimizer/planning/planning-context.md",
        }
        self.payload = {
            "schema_version": 2, "features": {}, "forecast_exclusions": {"optimizer": self.decision},
        }
        self.write_config()
        for view in ("quarter-plan", "commander-plan", "actual-progress", "actual-progress-confluence"):
            (self.gantt / f"{view}.puml").write_text(f"Previous {view}\n", encoding="utf-8")

    def write_config(self) -> None:
        self.config.write_text(json.dumps(self.payload), encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def run_generator(self, success: bool = True, standalone: bool = False) -> subprocess.CompletedProcess:
        command = [sys.executable, str(ROOT / "scripts/sync-quarter-gantt.py"), str(self.gantt), "--actual-only"]
        if standalone:
            command = [sys.executable, str(ROOT / "scripts/sync-actual-progress-overlay.py"), str(self.root), "2026-Q3"]
        result = subprocess.run(
            command, text=True, capture_output=True,
            env={**os.environ, "HARNESS_TODAY": "2026-09-09"},
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assert_blocked_without_writes(self, message: str, standalone: bool = False) -> None:
        before = self.snapshot()
        result = self.run_generator(success=False, standalone=standalone)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_plan_only_exclusion_preserves_plan_and_exposes_decision_in_both_exports(self) -> None:
        before = self.snapshot()
        self.run_generator()
        after = self.snapshot()
        changed = {path for path in before if before[path] != after[path]}
        self.assertEqual(changed, {
            "planning/2026-Q3/gantt/actual-progress.puml",
            "planning/2026-Q3/gantt/actual-progress-confluence.puml",
        })
        self.assertEqual(set(before), set(after))
        view = (self.gantt / "actual-progress.puml").read_text()
        export = (self.gantt / "actual-progress-confluence.puml").read_text()
        self.assertIn("(PLAN; вне прогноза 2026-Q3)", view)
        self.assertIn(self.decision["reason"], view)
        self.assertIn(self.decision["source"], view)
        self.assertIn("!include includes/quarter-plan/FEATURE-optimizer.puml", view)
        self.assertIn("[OPT_PLAN] starts 2026/08/03", export)
        self.assertIn("(PLAN; вне прогноза 2026-Q3)", export)
        self.assertNotIn("!include", export)
        self.assertNotIn("TASK_", export)
        self.assertNotIn("% completed", export)
        self.assertFalse((self.feature / "planning/actualization.md").exists())
        self.run_generator()
        self.assertEqual(self.snapshot(), after)

    def test_commander_baseline_takes_precedence_without_rewriting_either_plan(self) -> None:
        commander = self.gantt / "includes/commander-plan/FEATURE-optimizer.puml"
        commander.parent.mkdir()
        commander.write_text(self.plan.read_text().replace("OPT_PLAN", "OPT_COMMANDER"), encoding="utf-8")
        before = self.snapshot()
        self.run_generator()
        export = (self.gantt / "actual-progress-confluence.puml").read_text()
        self.assertIn("OPT_COMMANDER", export)
        self.assertNotIn("OPT_PLAN", export)
        self.assertEqual(commander.read_bytes(), before[str(commander.relative_to(self.root))])
        self.assertEqual(self.plan.read_bytes(), before[str(self.plan.relative_to(self.root))])

    def test_unconfirmed_invalid_or_misspelled_decisions_block(self) -> None:
        original = dict(self.decision)
        for updates in (
            {"analyst_confirmed": False}, {"analyst_confirmed": "true"},
            {"state": "cancelled"}, {"state": "Q4"}, {"reason": ""},
            {"reason": "reason\n!include other.puml"}, {"source": ""},
            {"reason": "reason\u2028!include other.puml"},
            {"source": "../outside.md"}, {"source": str(self.context)},
            {"source": "features/optimizer/planning/missing.md"},
            {"unexpected": "ignored"},
        ):
            with self.subTest(updates=updates):
                self.payload["forecast_exclusions"]["optimizer"] = {**original, **updates}
                self.write_config()
                self.assert_blocked_without_writes("ERROR:")
        for field in original:
            with self.subTest(missing=field):
                self.payload["forecast_exclusions"]["optimizer"] = {
                    key: value for key, value in original.items() if key != field
                }
                self.write_config()
                self.assert_blocked_without_writes("нужны state")

    def test_source_must_exist_be_nonempty_and_resolve_inside_project(self) -> None:
        self.context.write_text("", encoding="utf-8")
        self.assert_blocked_without_writes("источник решения")
        self.context.unlink()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "decision.md"
            external.write_text("External decision", encoding="utf-8")
            self.context.symlink_to(external)
            self.assert_blocked_without_writes("внутри проекта")

    def test_schema_one_mapping_remains_supported_but_cannot_carry_exclusions(self) -> None:
        self.payload["schema_version"] = 1
        self.write_config()
        self.assert_blocked_without_writes("поля конфигурации")
        del self.payload["forecast_exclusions"]
        self.write_config()
        self.assert_blocked_without_writes("нет непустой planning/actualization.md")

    def test_duplicate_json_keys_and_invalid_shapes_block(self) -> None:
        self.config.write_text(
            '{"schema_version":2,"features":{},"forecast_exclusions":{},"forecast_exclusions":{}}',
            encoding="utf-8",
        )
        self.assert_blocked_without_writes("Повторяющийся ключ JSON")
        for payload in (
            [], {"schema_version": True, "features": {}},
            {"schema_version": 3, "features": {}},
            {**self.payload, "forecast_exclusions": []},
            {**self.payload, "forecast_exclusions": {"../optimizer": self.decision}},
            {**self.payload, "features": {"optimizer": "../optimizer"}},
        ):
            with self.subTest(payload=payload):
                self.config.write_text(json.dumps(payload), encoding="utf-8")
                self.assert_blocked_without_writes("ERROR:")

    def test_existing_sources_and_overlays_cannot_be_hidden(self) -> None:
        for relative in (
            "features/optimizer/planning/actualization.md",
            "features/optimizer/execution-context.md",
            "features/optimizer/execution/tasks.md",
            "features/optimizer/execution/task-candidates.md",
            "features/optimizer/execution/actual-progress.md",
            "features/optimizer/execution/tasks/LOCAL.md",
            "features/optimizer/slices/legacy/execution/tasks.md",
            "features/optimizer/slices/legacy/execution/task-candidates.md",
            "features/optimizer/slices/legacy/execution/tasks/LOCAL.md",
            "planning/2026-Q3/gantt/includes/actual-progress/FEATURE-optimizer.puml",
        ):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Existing evidence\n", encoding="utf-8")
                self.assert_blocked_without_writes("не может скрыть")
                self.assert_blocked_without_writes("не может скрыть", standalone=True)
                path.unlink()

    def test_deleted_approved_map_cannot_be_hidden_with_exclusion(self) -> None:
        snapshot = self.root / "planning/approved-plans/2026-Q2.json"
        snapshot.parent.mkdir()
        snapshot.write_text(json.dumps({"actualization_baseline": {
            "features/optimizer/planning/actualization.md": [["STORY-OPT", "2026-08-03", "5"]],
        }}), encoding="utf-8")
        self.assert_blocked_without_writes("approved actualization baseline")

    def test_missing_feature_baseline_and_nested_include_block_without_writes(self) -> None:
        self.payload["features"]["optimizer"] = "absent"
        self.write_config()
        self.assert_blocked_without_writes("функциональность не найдена")
        self.payload["features"].clear()
        self.write_config()
        self.plan.unlink()
        self.assert_blocked_without_writes("нет исходного")
        self.plan.write_text("", encoding="utf-8")
        self.assert_blocked_without_writes("исходный план пуст")
        self.plan.write_text("!include missing.puml\n", encoding="utf-8")
        self.assert_blocked_without_writes("Included file not found")

    def test_other_missing_maps_still_block_with_exclusion_configured(self) -> None:
        unrelated = self.root / "features/unrelated"
        unrelated.mkdir()
        include = self.plan.with_name("FEATURE-unrelated.puml")
        include.write_text("Existing unrelated plan\n", encoding="utf-8")
        self.assert_blocked_without_writes("нет непустой planning/actualization.md")

    def test_exclusion_uses_overlay_slug_and_validates_duplicate_execution_mapping(self) -> None:
        renamed = self.root / "features/optimizer-execution"
        self.feature.rename(renamed)
        self.payload["features"]["optimizer"] = "optimizer-execution"
        self.decision["source"] = "features/optimizer-execution/planning/planning-context.md"
        self.write_config()
        self.run_generator()
        self.assertIn("FEATURE-optimizer.puml", (self.gantt / "actual-progress.puml").read_text())
        self.payload["features"]["other"] = "optimizer-execution"
        self.write_config()
        self.assert_blocked_without_writes("нескольким файлам Ганта")

    def test_exclusion_is_quarter_local(self) -> None:
        next_gantt = self.root / "planning/2026-Q4/gantt"
        next_plan = next_gantt / "includes/quarter-plan/FEATURE-optimizer.puml"
        next_plan.parent.mkdir(parents=True)
        next_plan.write_text(self.plan.read_text(), encoding="utf-8")
        self.gantt = next_gantt
        self.assert_blocked_without_writes("нет непустой planning/actualization.md")

    def test_standalone_overlay_does_not_create_sources_or_erase_outputs(self) -> None:
        before = self.snapshot()
        self.run_generator(standalone=True)
        self.assertEqual(self.snapshot(), before)

    def test_active_feature_renders_normally_alongside_excluded_plan(self) -> None:
        feature = self.root / "features/active"
        mapping = feature / "planning/actualization.md"
        mapping.parent.mkdir(parents=True)
        mapping.write_text(
            "| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| STORY-ACTIVE | Active work | 2026-09-01 | 5 | materialized | explicit | LOCAL-FE | | |\n",
            encoding="utf-8",
        )
        registry = feature / "execution/tasks.md"
        registry.parent.mkdir()
        registry.write_text(
            "| Task ID | Jira | Summary | Kind | Role | Estimate (дн) | Executor | Planned Start | Planned Finish | Actual Start | Actual Finish | Status | Progress % | Related Stories |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| LOCAL-FE | | Active work | real | FE | 5 | F1 | | | 2026-08-31 | 2026-09-07 | completed | 100 | STORY-ACTIVE |\n",
            encoding="utf-8",
        )
        self.payload["features"]["active-plan"] = "active"
        self.write_config()
        before = self.snapshot()
        self.run_generator()
        export = (self.gantt / "actual-progress-confluence.puml").read_text()
        self.assertIn("TASK_LOCAL_FE", export)
        self.assertIn("100% completed", export)
        self.assertIn("OPT_PLAN", export)
        self.assertTrue((self.gantt / "includes/actual-progress/FEATURE-active-plan.puml").exists())
        for path in (mapping, registry, self.context, self.config, self.plan):
            self.assertEqual(path.read_bytes(), before[str(path.relative_to(self.root))])
        expanded = self.root / "expanded.puml"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/expand-plantuml-includes.py"),
             str(self.gantt / "actual-progress.puml"), str(expanded)],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(expanded.read_text(), export)
        registry.write_text(registry.read_text().replace("| FE | 5 |", "| FE | |"), encoding="utf-8")
        self.assert_blocked_without_writes("уточни оценку у аналитика")

    def test_removing_decision_reinstates_missing_source_gate(self) -> None:
        self.run_generator()
        self.payload["forecast_exclusions"].clear()
        self.write_config()
        self.assert_blocked_without_writes("нет непустой planning/actualization.md")


if __name__ == "__main__":
    unittest.main()
