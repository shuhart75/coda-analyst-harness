from copy import deepcopy
from datetime import date
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_actual_progress_sources as sources

OVERLAY = sources.OVERLAY


class RolePlanBaselinesTests(unittest.TestCase):
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    snapshot = sources.ActualProgressSourcesTests.snapshot
    quarter = sources.ActualProgressSourcesTests.quarter

    def setUp(self):
        sources.ActualProgressSourcesTests.setUp(self)
        self.plan = self.gantt / "quarter-plan.puml"
        self.plan.write_text(
            "@startgantt\nsaturday are closed\nsunday are closed\n"
            "[FE feature] as [SOURCE_FE] on {F1} starts 2026/08/01\n"
            "[SOURCE_FE] ends 2026/08/07\n"
            "[QA feature] as [SOURCE_QA] starts 2026/08/08\n"
            "[SOURCE_QA] ends 2026/08/11\n@endgantt\n", encoding="utf-8",
        )
        self.decision_source = self.gantt / "role-baseline-decision.md"
        self.decision_source.write_text("Confirmed quarter role comparison; no buffer.\n")
        self.config_path = self.gantt / "actual-progress-features.json"
        self.config = {"schema_version": 4, "features": {}, "role_baselines": {"cohorts": {
            "analyst_confirmed": True,
            "reason": "Use quarter bars unchanged",
            "source": self.decision_source.relative_to(self.root).as_posix(),
            "view": "quarter-plan",
            "sha256": {},
            "bars": [{"role": role, "alias": "SOURCE_" + role, "path": self.plan.relative_to(self.root).as_posix()}
                     for role in ("FE", "QA")],
        }}}
        self.refresh_hashes()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def refresh_hashes(self):
        decision = self.config["role_baselines"]["cohorts"]
        _, paths = OVERLAY.load_forecast_scope.__globals__["expanded_with_paths"](self.gantt / (decision["view"] + ".puml"))
        decision["sha256"] = {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in paths}
        calendar = self.gantt / "closed-days.txt"
        decision["sha256"][calendar.relative_to(self.root).as_posix()] = hashlib.sha256(calendar.read_bytes()).hexdigest() if calendar.exists() else None
        self.save_config()

    def test_role_bars_replace_legacy_rendering_but_not_mapping(self):
        original = self.snapshot()
        self.quarter()
        content = self.target.read_text()
        self.assertEqual(content.count("[PLAN "), 2)
        self.assertIn("квартальный план", content)
        self.assertNotIn("as [STORY_", content)
        self.assertIn("Story STORY-COHORT", content)
        self.assertIn("[PLAN_COHORTS_FE] starts 2026/09/01", content)
        self.assertIn("[PLAN_COHORTS_FE] ends 2026/09/07", content)
        self.assertIn("[PLAN_COHORTS_FE] is 75% completed", content)
        self.assertIn("[PLAN_COHORTS_QA] starts 2026/09/03", content)
        self.assertIn("[PLAN_COHORTS_QA] ends 2026/09/04", content)
        self.assertIn("[PLAN_COHORTS_QA] is 30% completed", content)
        for path in (self.map, self.registry, self.plan, self.config_path, self.gantt / "commander-plan.puml"):
            self.assertEqual(path.read_bytes(), original[path.relative_to(self.root).as_posix()])
        expanded = "\n".join(sources.EXPANDER.expand_file(self.gantt / "actual-progress.puml", [])).rstrip() + "\n"
        self.assertEqual((self.gantt / "actual-progress-confluence.puml").read_text(), expanded)
        before = self.snapshot()
        self.quarter()
        self.assertEqual(before, self.snapshot())

    def test_absent_story_baseline_does_not_hide_confirmed_feature_plan(self):
        self.map.write_text(self.map.read_text().replace(
            "| Baseline Start |", "| Baseline State | Baseline Start |",
        ).replace("| Delivery | 2026-09-01 | 5 |", "| Delivery | absent | | |"))
        self.quarter()
        self.assertEqual(self.target.read_text().count("[PLAN "), 2)
        self.assertIn("| absent |", self.map.read_text())

    def test_unknown_role_progress_preserves_plan_window_and_known_other_role(self):
        self.registry.write_text(self.registry.read_text().replace("| 75 |", "| unknown |"), encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.plan, self.map, self.config_path)}
        self.quarter()
        content = self.target.read_text()
        self.assertIn("[PLAN_COHORTS_FE] starts 2026/09/01", content)
        self.assertIn("[PLAN_COHORTS_FE] ends 2026/09/07", content)
        self.assertNotRegex(content, r"\[PLAN_COHORTS_FE\] is \d+% completed")
        self.assertIn("прогресс неизвестен", content)
        self.assertIn("[PLAN_COHORTS_QA] is 30% completed", content)
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original)

    def test_missing_role_tasks_retain_exact_source_window(self):
        self.write_tasks(["| | ITEM-100 | List | real | FE | 5 | F1 | 2026-09-01 | 2026-09-04 | 2026-09-01 | | in-review | 75 | STORY-COHORT |"])
        self.map.write_text(self.map.read_text().replace("ITEM-100/FE, QA-COHORT", "ITEM-100/FE"))
        self.quarter()
        self.assertIn("[PLAN_COHORTS_QA] starts 2026/08/08", self.target.read_text())
        self.assertIn("[PLAN_COHORTS_QA] ends 2026/08/11", self.target.read_text())
        self.assertIn("[PLAN_COHORTS_QA] is 0% completed", self.target.read_text())

    def test_role_progress_uses_all_tasks_with_exact_weights_and_exclusions(self):
        baseline = OVERLAY.load_forecast_scope(self.root, "2026-Q3").role_baselines["cohorts"][0]
        tasks = OVERLAY.load_tasks(self.feature)
        first = tasks["ITEM-100/FE"]
        first.estimate, first.progress = 0.5, 100
        second = deepcopy(first)
        second.task_id, second.estimate, second.progress, second.actual_start = "SECOND", 1.5, 0, "2026-09-03"
        second.related_stories = []
        tasks[second.task_id] = second
        for kind, status in (("candidate", "proposed"), ("real", "cancelled"), ("virtual", "superseded")):
            extra = deepcopy(first)
            extra.task_id, extra.kind, extra.status = status, kind, status
            extra.actual_start = "2026-01-01"
            tasks[extra.task_id] = extra
        rendered = "\n".join(OVERLAY.render_role_baseline(baseline, "cohorts", tasks, {}, set()))
        self.assertIn("starts 2026/09/01", rendered)
        self.assertIn("ends 2026/09/07", rendered)
        self.assertIn("is 25% completed", rendered)

    def test_forecast_start_used_only_without_actual_role_start(self):
        baseline = OVERLAY.load_forecast_scope(self.root, "2026-Q3").role_baselines["cohorts"][0]
        tasks = OVERLAY.load_tasks(self.feature)
        schedules = {"ITEM-100/FE": OVERLAY.ScheduledTask(date(2026, 9, 10), date(2026, 10, 1), "F1", True)}
        tasks["ITEM-100/FE"].actual_start = ""
        rendered = "\n".join(OVERLAY.render_role_baseline(baseline, "cohorts", tasks, schedules, set()))
        self.assertIn("starts 2026/09/10", rendered)
        self.assertIn("ends 2026/09/16", rendered)

    def test_commander_include_is_explicit_source(self):
        included = self.gantt / "includes/commander-plan/FEATURE-cohorts.puml"
        included.parent.mkdir()
        included.write_text(self.plan.read_text().replace("@startgantt\nsaturday are closed\nsunday are closed\n", "").replace("@endgantt\n", ""))
        (self.gantt / "commander-plan.puml").write_text("@startgantt\nsaturday are closed\nsunday are closed\n!include includes/commander-plan/FEATURE-cohorts.puml\n@endgantt\n")
        decision = self.config["role_baselines"]["cohorts"]
        decision["view"] = "commander-plan"
        for bar in decision["bars"]:
            bar["path"] = included.relative_to(self.root).as_posix()
        self.refresh_hashes()
        self.quarter()
        self.assertIn("командирский план", self.target.read_text())
        self.assertIn("duration=5 working days", self.target.read_text())

    def test_config_errors_block_all_writes(self):
        original = deepcopy(self.config)
        variants = []
        for field, value in (("analyst_confirmed", False), ("source", "../outside.md"), ("view", "actual-progress"),
                             ("reason", ""), ("bars", []), ("sha256", {})):
            changed = deepcopy(original)
            changed["role_baselines"]["cohorts"][field] = value
            variants.append(changed)
        for field, value in (("alias", "MISSING"), ("role", "GEN"), ("path", "planning/2026-Q2/gantt/quarter-plan.puml")):
            changed = deepcopy(original)
            changed["role_baselines"]["cohorts"]["bars"][0][field] = value
            variants.append(changed)
        changed = deepcopy(original)
        changed["role_baselines"]["cohorts"]["bars"][1]["role"] = "FE"
        variants.append(changed)
        for schema in (1, 2, 3, 5, True):
            changed = deepcopy(original)
            changed["schema_version"] = schema
            variants.append(changed)
        for bucket in ("preserved_forecasts", "forecast_exclusions"):
            changed = deepcopy(original)
            changed[bucket] = {"cohorts": {}}
            variants.append(changed)
        for variant in variants:
            with self.subTest(config=variant):
                self.config = variant
                self.save_config()
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(before, self.snapshot())

    def test_source_or_calendar_drift_and_invalid_syntax_preserve_outputs(self):
        original = self.plan.read_text()
        for replacement, rehash in ((original + "' changed\n", False),
                                    (original.replace("ends 2026/08/07", "lasts 5 days"), True),
                                    (original.replace("ends 2026/08/07", "ends 2026/07/31"), True),
                                    (original.replace("sunday are closed", "sunday are open"), True)):
            with self.subTest(replacement=replacement):
                self.plan.write_text(replacement)
                if rehash:
                    self.refresh_hashes()
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(before, self.snapshot())
        self.plan.write_text(original)
        self.refresh_hashes()
        (self.gantt / "closed-days.txt").write_text("2026-08-04\n")
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())

    def test_legacy_mapping_validation_is_not_bypassed(self):
        self.registry.write_text(self.registry.read_text().replace("STORY-COHORT", "UNKNOWN"))
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())

    def test_full_generation_cannot_rewrite_bound_plans(self):
        before = self.snapshot()
        result = subprocess.run([sys.executable, str(sources.ROOT / "scripts/sync-quarter-gantt.py"), str(self.gantt)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--actual-only", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_source_and_legacy_snapshot_protection_survives_migration(self):
        snapshots = self.root / "planning/approved-plans"
        snapshots.mkdir()
        baseline_path = snapshots / "2026-Q3.json"
        baseline_path.write_text(json.dumps({"files": {self.plan.relative_to(self.root).as_posix(): "0" * 64}}))
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())
        self.map.write_text("## Mapping\n\n" + self.map.read_text())
        baseline_path.write_text(json.dumps({"actualization_baseline": {self.map.relative_to(self.root).as_posix(): []}}))
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())

    def test_decision_change_during_preparation_blocks_output(self):
        original = OVERLAY.render_feature
        def changed_source(*args, **kwargs):
            content = original(*args, **kwargs)
            self.decision_source.write_text("A different decision\n")
            return content
        before = self.target.read_bytes()
        with patch.object(OVERLAY, "render_feature", side_effect=changed_source):
            with self.assertRaisesRegex(ValueError, "изменился во время генерации"):
                OVERLAY.prepare_outputs(self.root, "2026-Q3", ["cohorts"])
        self.assertEqual(before, self.target.read_bytes())

    def test_shared_alias_cannot_be_claimed_by_two_features(self):
        self.config["role_baselines"]["another"] = deepcopy(self.config["role_baselines"]["cohorts"])
        self.save_config()
        with self.assertRaisesRegex(ValueError, "уже назначен"):
            OVERLAY.load_forecast_scope(self.root, "2026-Q3")

    def test_closed_day_is_counted_and_preserved_on_reanchor(self):
        (self.gantt / "closed-days.txt").write_text("2026-08-04\n")
        self.plan.write_text(self.plan.read_text().replace("sunday are closed", "sunday are closed\n2026/08/04 is closed"))
        self.refresh_hashes()
        self.quarter()
        self.assertIn("duration=4 working days", self.target.read_text())
        self.assertIn("[PLAN_COHORTS_FE] ends 2026/09/04", self.target.read_text())

    def test_conditional_or_overridden_sources_are_rejected(self):
        original = self.plan.read_text()
        for content in (original.replace("[FE feature]", "!if (1)\n[FE feature]") + "!endif\n",
                        original + "[SOURCE_FE] starts 2026/09/01\n",
                        original + "[SOURCE_FE] ends 2026/09/01\n"):
            with self.subTest(content=content):
                self.plan.write_text(content)
                self.refresh_hashes()
                before = self.snapshot()
                self.quarter(success=False)
                self.assertEqual(before, self.snapshot())

    def test_missing_alias_or_unreachable_include_is_not_inferred(self):
        disconnected = self.gantt / "includes/quarter-plan/FEATURE-cohorts.puml"
        disconnected.parent.mkdir()
        disconnected.write_text(self.plan.read_text())
        self.config["role_baselines"]["cohorts"]["bars"][0]["path"] = disconnected.relative_to(self.root).as_posix()
        self.save_config()
        before = self.snapshot()
        self.quarter(success=False)
        self.assertEqual(before, self.snapshot())
