from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "trackerctl.py"


class TrackerCtlTests(unittest.TestCase):
    def run_tool(
        self,
        state: Path,
        *arguments: str,
        expected: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            (sys.executable, str(SCRIPT), *arguments),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(state)},
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def prepare_config(self, state: Path) -> Path:
        initialized = self.run_tool(state, "init-config")
        path = Path(json.loads(initialized.stdout)["path"])
        config = json.loads(path.read_text(encoding="utf-8"))
        config["setup_complete"] = True
        config["jira_enabled"] = True
        config["projects"] = {"sbertrek": ["SBER"], "jira": ["JIRA"]}
        config["issue_pairs"] = {"SBER-1": "JIRA-11"}
        config["development_issue_types"] = ["development-task", "Story"]
        config["participants"] = {
            "sbertrek": {
                "dev-s": {"canonical_id": "B1", "role": "developer"},
                "qa-s": {"canonical_id": "Q1", "role": "tester"},
                "unknown": {"canonical_id": "U1", "role": "other"},
            },
            "jira": {
                "dev-j": {"canonical_id": "B1", "role": "developer"},
                "qa-j": {"canonical_id": "Q1", "role": "tester"},
            },
        }
        config["status_rules"]["completed"] = ["Выполнена"]
        config["status_rules"]["excluded"] = ["Отменена", "Удалена"]
        self.write_json(path, config)
        return path

    def collection(self, provider: str) -> dict:
        return {
            "history": {"state": "complete", "reason": None},
            "epic_links": {"state": "complete", "reason": None},
            "release_links": {"state": "complete", "reason": None},
            "counterpart_lookup": {
                "state": "not-applicable" if provider == "sbertrek" else "complete",
                "reason": None,
            },
            "epic_neighbors": {"state": "complete", "reason": None},
            "not_found_keys": [],
            "expanded_epic_keys": ["EPIC-1"],
        }

    def snapshots(self) -> tuple[dict, dict]:
        sber = {
            "schema_version": 1,
            "provider": "sbertrek",
            "captured_at": "2026-08-26T10:00:00+00:00",
            "scope": {
                "projects": ["SBER"],
                "query": "key in (...)",
                "seed_keys": ["SBER-1", "SBER-3", "SBER-4"],
                "seed_evidence": [
                    {"key": "SBER-1", "source": "features/example/actual-progress.md"},
                    {"key": "SBER-3", "source": "features/example/actual-progress.md"},
                    {"key": "SBER-4", "source": "features/example/actual-progress.md"},
                ],
                "expected_epic_keys": ["EPIC-1"],
                "expected_release_keys": ["REL-1"],
            },
            "collection": self.collection("sbertrek"),
            "issues": [
                {
                    "key": "SBER-1",
                    "summary": "Основная задача",
                    "description": "Реализовать часть функциональности.",
                    "issue_type": "Development-Task",
                    "status": "Тестирование",
                    "assignee": {"id": "qa-s", "name": "Тестировщик"},
                    "estimate": None,
                    "epic": {"key": "EPIC-1", "name": "Функциональность N"},
                    "releases": [{"key": "REL-1", "name": "Релиз 1"}],
                    "discovery": "seed",
                    "updated_at": "2026-08-26T09:00:00+00:00",
                    "history": [
                        {
                            "at": "2026-08-25T12:00:00+00:00",
                            "field": "assignee",
                            "from": {"id": "dev-s"},
                            "to": {"id": "qa-s"},
                        }
                    ],
                },
                {
                    "key": "SBER-2",
                    "summary": "Отменённая задача",
                    "issue_type": "development-task",
                    "status": "Отменена",
                    "assignee": {"id": "qa-s"},
                    "epic": {"key": "EPIC-1", "name": "Функциональность N"},
                    "releases": [],
                    "discovery": "epic-neighbor",
                    "feature_relevance": "proposed",
                    "relevance_basis": "Прямая связь с известной задачей SBER-1",
                    "history": [
                        {
                            "at": "2026-08-25T13:00:00+00:00",
                            "field": "assignee",
                            "from": {"id": "dev-s"},
                            "to": {"id": "qa-s"},
                        }
                    ],
                },
                {
                    "key": "SBER-3",
                    "summary": "Story как единица разработки",
                    "issue_type": "Story",
                    "status": "Тестирование",
                    "assignee": {"id": "qa-s"},
                    "epic": None,
                    "releases": [{"key": "REL-2", "name": "Релиз 2"}],
                    "discovery": "seed",
                    "history": [
                        {
                            "at": "2026-08-25T14:00:00+00:00",
                            "field": "assignee",
                            "from": {"id": "dev-s"},
                            "to": {"id": "qa-s"},
                        }
                    ],
                },
                {
                    "key": "SBER-4",
                    "summary": "Неизвестный исполнитель",
                    "issue_type": "development-task",
                    "status": "В работе",
                    "assignee": {"id": "unknown"},
                    "epic": None,
                    "releases": [],
                    "discovery": "seed",
                    "history": [],
                },
            ],
        }
        jira = {
            "schema_version": 1,
            "provider": "jira",
            "captured_at": "2026-08-26T10:00:00+00:00",
            "scope": {
                "projects": ["JIRA"],
                "query": "key in (...)",
                "seed_keys": [],
                "seed_evidence": [],
                "expected_epic_keys": ["EPIC-1"],
                "expected_release_keys": ["REL-1"],
            },
            "collection": self.collection("jira"),
            "issues": [
                {
                    "key": "JIRA-11",
                    "counterpart_key": "SBER-1",
                    "summary": "Другое название из Jira",
                    "description": "Реализовать часть функциональности.",
                    "issue_type": "development-task",
                    "status": "В разработке",
                    "assignee": {"id": "qa-j", "name": "Тестировщик"},
                    "estimate": {"value": 5, "unit": "story-points"},
                    "epic": {"key": "EPIC-1", "name": "Функциональность N"},
                    "releases": [{"name": "Релиз 1", "key": "REL-1"}],
                    "discovery": "counterpart",
                    "updated_at": "2026-08-26T09:30:00+00:00",
                    "history": [
                        {
                            "at": "2026-08-25T12:00:00+00:00",
                            "field": "assignee",
                            "from": {"id": "dev-j"},
                            "to": {"id": "qa-j"},
                        },
                        {
                            "at": "2026-08-24T12:00:00+00:00",
                            "field": "estimate",
                            "from": None,
                            "to": {"value": 5, "unit": "story-points"},
                        }
                    ],
                },
                {
                    "key": "JIRA-ONLY",
                    "summary": "Есть только в Jira",
                    "issue_type": "development-task",
                    "status": "В работе",
                    "discovery": "feature-search-candidate",
                    "feature_relevance": "ambiguous",
                    "relevance_basis": "Совпадает предметная область, прямой связи нет",
                    "history": [],
                },
            ],
        }
        return sber, jira

    def test_reconcile_enriches_from_jira_and_preserves_sbertrek_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, jira = self.snapshots()
            started = json.loads(self.run_tool(state, "begin").stdout)
            sber_path = Path(started["sbertrek_input"])
            jira_path = Path(started["jira_input"])
            self.write_json(sber_path, sber)
            self.write_json(jira_path, jira)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                "--jira",
                str(jira_path),
                "--run-id",
                started["run_id"],
            )
            output = json.loads(result.stdout)
            reconciled = json.loads(Path(output["result"]).read_text(encoding="utf-8"))
            issues = {issue["key"]: issue for issue in reconciled["issues"]}

            primary = issues["SBER-1"]
            self.assertEqual(primary["status"], "Тестирование")
            self.assertEqual(primary["estimate"], {"value": 5, "unit": "story-points"})
            self.assertEqual(primary["field_sources"]["estimate"], "jira")
            self.assertIn("estimate", primary["enriched_from_jira"])
            self.assertIn("history", primary["enriched_from_jira"])
            self.assertIn("status", primary["conflicting_fields"])
            self.assertNotIn("issue_type", primary["conflicting_fields"])
            self.assertNotIn("assignee", primary["conflicting_fields"])
            assignee_events = [
                event for event in primary["history"] if event["field"] == "assignee"
            ]
            self.assertEqual(assignee_events[0]["sources"], ["sbertrek", "jira"])
            self.assertEqual(
                primary["development_state"]["state"],
                "development-completed-handoff",
            )
            self.assertEqual(issues["SBER-2"]["development_state"]["state"], "excluded")
            self.assertEqual(
                issues["SBER-3"]["development_state"]["state"],
                "development-completed-handoff",
            )
            self.assertEqual(issues["SBER-4"]["development_state"]["state"], "unknown")
            self.assertIn(
                {"kind": "jira-only", "jira_key": "JIRA-ONLY"},
                reconciled["discrepancies"],
            )
            self.assertTrue(
                any(item["kind"] == "jira-newer" for item in reconciled["discrepancies"])
            )
            status_conflict = next(
                item
                for item in reconciled["discrepancies"]
                if item.get("kind") == "field-conflict" and item.get("field") == "status"
            )
            self.assertEqual(status_conflict["sbertrek_value"], "Тестирование")
            self.assertEqual(status_conflict["jira_value"], "В разработке")
            self.assertEqual(
                reconciled["counts"]["discrepancies"],
                len(reconciled["discrepancies"]),
            )
            self.assertEqual(reconciled["counts"]["sbertrek"], len(sber["issues"]))
            self.assertEqual(reconciled["counts"]["jira"], len(jira["issues"]))
            self.assertEqual(reconciled["release_proposals"], [{"key": "REL-2", "name": "Релиз 2"}])

            report = Path(output["report"]).read_text(encoding="utf-8")
            self.assertIn("## Эпики", report)
            self.assertIn("## Релизы", report)
            self.assertIn("EPIC-1", report)
            self.assertIn("REL-1", report)
            self.assertIn("epic-neighbor", report)
            self.assertIn("Релизы, отсутствующие в actual-progress", report)
            for kind, count in reconciled["counts"]["discrepancies_by_kind"].items():
                self.assertIn(f"`{kind}`: **{count}**", report)
            self.assertFalse(output["project_changed"])
            self.assertFalse(output["tracker_changed"])
            self.assertEqual(Path(output["result"]).parent, sber_path.parent.parent)

    def test_unconfigured_story_type_does_not_use_handoff_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["development_issue_types"] = ["development-task"]
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            sber["issues"] = [sber["issues"][2]]
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(state, "reconcile", "--sbertrek", str(sber_path))
            reconciled = json.loads(
                Path(json.loads(result.stdout)["result"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                reconciled["issues"][0]["development_state"]["state"],
                "not-inferred",
            )
            self.assertFalse(reconciled["jira_used"])
            self.assertIn("jira-unavailable", reconciled["limitations"])
            self.assertFalse(
                any(item["kind"].startswith("jira-pair-") for item in reconciled["discrepancies"])
            )

    def test_explicit_completed_status_applies_to_unconfigured_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["development_issue_types"] = ["development-task"]
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            story = sber["issues"][2]
            story["status"] = "Выполнена"
            sber["issues"] = [story]
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(state, "reconcile", "--sbertrek", str(sber_path))
            reconciled = json.loads(
                Path(json.loads(result.stdout)["result"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                reconciled["issues"][0]["development_state"]["state"],
                "completed-by-status",
            )

    def test_handoff_history_is_ordered_by_instant_not_timestamp_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            issue = sber["issues"][0]
            issue["assignee"] = {"id": "unknown"}
            issue["history"] = [
                {
                    "at": "2026-08-25T12:30:00+03:00",
                    "field": "assignee",
                    "from": {"id": "dev-s"},
                    "to": {"id": "qa-s"},
                },
                {
                    "at": "2026-08-25T10:00:00+00:00",
                    "field": "assignee",
                    "from": {"id": "qa-s"},
                    "to": {"id": "dev-s"},
                },
            ]
            sber["issues"] = [issue]
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(state, "reconcile", "--sbertrek", str(sber_path))
            reconciled = json.loads(
                Path(json.loads(result.stdout)["result"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                reconciled["issues"][0]["development_state"]["state"],
                "unknown",
            )

    def test_invalid_history_timestamp_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            sber["issues"][0]["history"][0]["at"] = "not-a-timestamp"
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                expected=2,
            )
            self.assertIn("должно иметь корректный at", result.stderr)

    def test_invalid_participant_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["participants"]["sbertrek"]["dev-s"]["role"] = "backend"
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                expected=2,
            )
            self.assertIn("Неизвестная роль", result.stderr)

    def test_conflicting_roles_for_one_participant_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["participants"]["jira"]["dev-j"]["role"] = "tester"
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                expected=2,
            )
            self.assertIn("Противоречащие роли", result.stderr)

    def test_begin_blocks_default_config_until_interactive_setup_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            initialized = json.loads(self.run_tool(state, "init-config").stdout)
            self.assertFalse(initialized["setup_complete"])

            status = json.loads(self.run_tool(state, "config-status", expected=3).stdout)
            self.assertEqual(status["status"], "tracker-config-incomplete")
            self.assertTrue(status["must_stop"])
            self.assertEqual(status["allowed_next_action"], "ask-user")
            self.assertEqual(status["next_question"], "Какие проекты SberTrek входят в область чтения?")

            blocked = self.run_tool(state, "begin", expected=2)
            self.assertIn("Первичная настройка трекеров не завершена", blocked.stderr)
            self.assertFalse((state / "tracker-runs").exists())

    def test_empty_legacy_config_is_migrated_back_to_unconfigured_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            legacy = {
                "schema_version": 1,
                "primary_provider": "sbertrek",
                "projects": {"sbertrek": [], "jira": []},
                "issue_pairs": {},
                "development_issue_types": ["development-task"],
                "participants": {"sbertrek": {}, "jira": {}},
                "status_rules": {
                    "completed": [],
                    "excluded": ["Отменена", "Удалена"],
                },
            }
            config_path = state / "tracker-config.json"
            self.write_json(config_path, legacy)

            status = json.loads(self.run_tool(state, "config-status", expected=3).stdout)
            migrated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn("development_issue_types", status["gaps"])
            self.assertIn("status_rules.excluded", status["gaps"])
            self.assertEqual(migrated["development_issue_types"], [])
            self.assertEqual(migrated["status_rules"]["excluded"], [])
            self.assertIsNone(migrated["jira_enabled"])
            self.assertFalse(migrated["setup_complete"])

    def test_configuration_commands_prepare_templates_without_project_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            self.run_tool(state, "init-config")
            saved = json.loads(
                self.run_tool(state, "set-projects", "--provider", "sbertrek", "SBER").stdout
            )
            self.assertTrue(saved["must_stop"])
            self.assertEqual(saved["allowed_next_action"], "ask-user")
            self.assertEqual(
                saved["next_question"],
                "Jira доступна для дополнительного чтения на этой рабочей области?",
            )
            self.run_tool(state, "set-jira-mode", "enabled")
            self.run_tool(state, "set-projects", "--provider", "jira", "JIRA")
            self.run_tool(state, "set-issue-types", "story", "task")
            self.run_tool(state, "set-statuses", "--kind", "completed", "done", "resolved")
            self.run_tool(state, "set-statuses", "--kind", "excluded", "cancelled", "deleted")
            completed = json.loads(self.run_tool(state, "complete-config").stdout)
            self.assertFalse(completed["must_stop"])
            self.assertEqual(completed["allowed_next_action"], "begin")
            ready = json.loads(self.run_tool(state, "config-status").stdout)
            self.assertEqual(ready["status"], "tracker-config-ready")
            self.assertFalse(ready["must_stop"])
            self.assertEqual(ready["allowed_next_action"], "begin")

            started = json.loads(self.run_tool(state, "begin").stdout)
            sber = json.loads(Path(started["sbertrek_input"]).read_text(encoding="utf-8"))
            jira = json.loads(Path(started["jira_input"]).read_text(encoding="utf-8"))
            self.assertEqual(sber["collection"]["history"]["state"], "pending")
            self.assertEqual(sber["collection"]["counterpart_lookup"]["state"], "not-applicable")
            self.assertEqual(jira["collection"]["counterpart_lookup"]["state"], "pending")
            self.assertFalse(started["project_changed"])
            self.assertFalse(started["tracker_changed"])

    def test_jira_can_be_disabled_without_creating_an_input_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            self.run_tool(state, "init-config")
            self.run_tool(state, "set-projects", "--provider", "sbertrek", "SBER")
            self.run_tool(state, "set-jira-mode", "disabled")
            self.run_tool(state, "set-issue-types", "story")
            self.run_tool(state, "set-statuses", "--kind", "completed", "done")
            self.run_tool(state, "set-statuses", "--kind", "excluded", "cancelled")
            self.run_tool(state, "complete-config")

            started = json.loads(self.run_tool(state, "begin").stdout)
            self.assertIsNone(started["jira_input"])
            self.assertFalse((Path(started["sbertrek_input"]).parent / "jira.json").exists())

    def test_reconcile_rejects_pending_collection_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            sber["collection"]["history"]["state"] = "pending"
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                expected=2,
            )
            self.assertIn("complete, unavailable или not-applicable", result.stderr)

    def test_text_search_result_cannot_be_promoted_to_seed_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            sber["scope"]["seed_evidence"] = []
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                expected=2,
            )
            self.assertIn("не имеют аналитического источника", result.stderr)

    def test_reconcile_rejects_direct_counterpart_that_was_not_looked_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, jira = self.snapshots()
            jira["issues"] = []
            jira["collection"]["expanded_epic_keys"] = []
            sber_path = root / "sber.json"
            jira_path = root / "jira.json"
            self.write_json(sber_path, sber)
            self.write_json(jira_path, jira)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                "--jira",
                str(jira_path),
                expected=2,
            )
            self.assertIn("Прямая Jira-пара JIRA-11", result.stderr)
            self.assertIn("не прочитана", result.stderr)

    def test_reconcile_reports_unavailable_and_empty_collection_limitations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            issue = sber["issues"][3]
            issue["assignee"] = None
            sber["issues"] = [issue]
            sber["scope"]["expected_epic_keys"] = []
            sber["scope"]["expected_release_keys"] = []
            sber["collection"]["epic_links"] = {
                "state": "unavailable",
                "reason": "MCP не возвращает поле эпика",
            }
            sber["collection"]["expanded_epic_keys"] = []
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            output = json.loads(
                self.run_tool(state, "reconcile", "--sbertrek", str(sber_path)).stdout
            )
            reconciled = json.loads(Path(output["result"]).read_text(encoding="utf-8"))
            self.assertIn("sbertrek-epic_links-unavailable", reconciled["limitations"])
            self.assertIn("sbertrek-history-returned-no-events", reconciled["limitations"])
            self.assertIn("jira-unavailable", reconciled["limitations"])

    def test_unknown_participant_blocks_reconcile_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            del config["participants"]["sbertrek"]["qa-s"]
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            sber["issues"] = [sber["issues"][0]]
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek",
                str(sber_path),
                expected=2,
            )
            self.assertIn("provider=sbertrek", result.stderr)
            self.assertIn("account_id=qa-s", result.stderr)
            self.assertIn("set-participant", result.stderr)


if __name__ == "__main__":
    unittest.main()
