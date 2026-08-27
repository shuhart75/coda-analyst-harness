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

    def restrict_to_issues(self, snapshot: dict, issues: list[dict]) -> None:
        snapshot["issues"] = issues
        snapshot["collection"]["history"]["checked_keys"] = [
            issue["key"] for issue in issues
        ]
        seed_keys = [issue["key"] for issue in issues if issue.get("discovery") == "seed"]
        snapshot["scope"]["seed_keys"] = seed_keys
        snapshot["scope"]["seed_evidence"] = [
            item for item in snapshot["scope"]["seed_evidence"] if item["key"] in seed_keys
        ]

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
                "dev-s": {"team_id": "BE1"},
                "qa-s": {"team_id": "QA1"},
                "unknown": {"team_id": "OTHER1"},
            },
            "jira": {
                "dev-j": {"team_id": "BE1"},
                "qa-j": {"team_id": "QA1"},
            },
        }
        config["status_rules"]["sbertrek"]["completed"] = ["Выполнена"]
        config["status_rules"]["sbertrek"]["excluded"] = ["Отменена", "Удалена"]
        config["status_rules"]["jira"]["completed"] = ["Done"]
        config["status_rules"]["jira"]["excluded"] = ["Cancelled"]
        self.write_json(path, config)
        return path

    def collection(self, provider: str) -> dict:
        checked_keys = (
            ["SBER-1", "SBER-2", "SBER-3", "SBER-4"]
            if provider == "sbertrek"
            else ["JIRA-11", "JIRA-ONLY"]
        )
        return {
            "history": {"state": "complete", "reason": None, "failure_kind": None, "evidence": ["mcp:history"], "checked_keys": checked_keys},
            "epic_links": {"state": "complete", "reason": None, "failure_kind": None, "evidence": ["mcp:issue-fields"], "checked_keys": []},
            "release_links": {"state": "complete", "reason": None, "failure_kind": None, "evidence": ["mcp:issue-fields"], "checked_keys": []},
            "counterpart_lookup": {
                "state": "not-applicable" if provider == "sbertrek" else "complete",
                "reason": None,
                "failure_kind": None,
                "evidence": [] if provider == "sbertrek" else ["mcp:direct-read"],
                "checked_keys": [],
            },
            "epic_neighbors": {"state": "complete", "reason": None, "failure_kind": None, "evidence": ["mcp:epic-search"], "checked_keys": []},
            "not_found_keys": [],
            "not_found_evidence": [],
            "expanded_epic_keys": ["EPIC-1"],
        }

    def snapshots(self) -> tuple[dict, dict]:
        sber = {
            "schema_version": 3,
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
                    "field_observations": {
                        "assignee": "value", "estimate": "absent",
                        "epic": "value", "releases": "value",
                    },
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
                    "field_observations": {
                        "assignee": "value", "estimate": "absent",
                        "epic": "value", "releases": "absent",
                    },
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
                    "field_observations": {
                        "assignee": "value", "estimate": "absent",
                        "epic": "absent", "releases": "value",
                    },
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
                    "field_observations": {
                        "assignee": "value", "estimate": "absent",
                        "epic": "absent", "releases": "absent",
                    },
                    "discovery": "seed",
                    "history": [],
                },
            ],
        }
        jira = {
            "schema_version": 3,
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
                    "field_observations": {
                        "assignee": "value", "estimate": "value",
                        "epic": "value", "releases": "value",
                    },
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
                    "assignee": None,
                    "estimate": None,
                    "epic": None,
                    "releases": [],
                    "field_observations": {
                        "assignee": "absent", "estimate": "absent",
                        "epic": "absent", "releases": "absent",
                    },
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
            self.assertTrue(output["workflow_complete"])
            self.assertTrue(output["final_response_allowed"])
            self.assertEqual(output["run_id"], started["run_id"])
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

    def test_epic_assignee_and_estimate_use_sbertrek_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, jira = self.snapshots()
            sber["issues"][0]["estimate"] = {"value": 8, "unit": "person-days"}
            sber["issues"][0]["field_observations"]["estimate"] = "value"
            jira["issues"][0]["assignee"] = {"id": "dev-j", "name": "Разработчик"}
            jira["issues"][0]["estimate"] = {"value": 13, "unit": "SP"}
            jira["issues"][0]["epic"] = {"key": "EPIC-J", "name": "Другой эпик"}
            jira["collection"]["expanded_epic_keys"].append("EPIC-J")
            sber_path = root / "sber.json"
            jira_path = root / "jira.json"
            self.write_json(sber_path, sber)
            self.write_json(jira_path, jira)

            output = json.loads(self.run_tool(
                state,
                "reconcile",
                "--sbertrek", str(sber_path),
                "--jira", str(jira_path),
            ).stdout)
            reconciled = json.loads(Path(output["result"]).read_text(encoding="utf-8"))
            issue = next(item for item in reconciled["issues"] if item["key"] == "SBER-1")

            self.assertEqual(issue["assignee"], {"id": "qa-s", "name": "Тестировщик"})
            self.assertEqual(issue["estimate"], {"value": 8, "unit": "story-points"})
            self.assertEqual(issue["epic"], {"key": "EPIC-1", "name": "Функциональность N"})
            self.assertEqual(
                {field: issue["field_sources"][field] for field in ("assignee", "estimate", "epic")},
                {"assignee": "sbertrek", "estimate": "sbertrek", "epic": "sbertrek"},
            )
            self.assertTrue({"assignee", "estimate", "epic"}.issubset(issue["conflicting_fields"]))
            self.assertEqual(reconciled["merge_policy"]["estimate_unit"], "story-points")
            self.assertEqual(reconciled["merge_policy"]["story_point_person_day_ratio"], 1)

            report = Path(output["report"]).read_text(encoding="utf-8")
            self.assertIn("| Исполнитель | Оценка, SP |", report)
            self.assertIn("Тестировщик (qa-s)", report)
            self.assertIn("8 SP", report)

    def test_jira_fills_missing_epic_assignee_and_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, jira = self.snapshots()
            issue = sber["issues"][0]
            issue["assignee"] = None
            issue["estimate"] = None
            issue["epic"] = None
            issue["field_observations"].update({
                "assignee": "absent",
                "estimate": "absent",
                "epic": "absent",
            })
            sber_path = root / "sber.json"
            jira_path = root / "jira.json"
            self.write_json(sber_path, sber)
            self.write_json(jira_path, jira)

            output = json.loads(self.run_tool(
                state,
                "reconcile",
                "--sbertrek", str(sber_path),
                "--jira", str(jira_path),
            ).stdout)
            reconciled = json.loads(Path(output["result"]).read_text(encoding="utf-8"))
            merged = next(item for item in reconciled["issues"] if item["key"] == "SBER-1")

            self.assertEqual(merged["assignee"], jira["issues"][0]["assignee"])
            self.assertEqual(merged["estimate"], {"value": 5, "unit": "story-points"})
            self.assertEqual(merged["epic"], jira["issues"][0]["epic"])
            self.assertEqual(
                {field: merged["field_sources"][field] for field in ("assignee", "estimate", "epic")},
                {"assignee": "jira", "estimate": "jira", "epic": "jira"},
            )
            self.assertTrue({"assignee", "estimate", "epic"}.issubset(merged["enriched_from_jira"]))

    def test_unconfigured_story_type_does_not_use_handoff_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["development_issue_types"] = ["development-task"]
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            self.restrict_to_issues(sber, [sber["issues"][2]])
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
            self.restrict_to_issues(sber, [story])
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
            self.restrict_to_issues(sber, [issue])
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

    def test_invalid_participant_team_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["participants"]["sbertrek"]["dev-s"]["team_id"] = "backend"
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
            self.assertIn("Командный идентификатор", result.stderr)

    def test_one_team_id_cannot_map_two_accounts_in_same_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["participants"]["jira"]["another-dev"] = {"team_id": "BE1"}
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
            self.assertIn("назначен нескольким аккаунтам jira", result.stderr)

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
            self.assertEqual(status["response_contract"]["text"], status["next_question"])
            self.assertTrue(status["response_contract"]["additional_text_forbidden"])
            self.assertTrue(status["response_contract"]["examples_forbidden"])

            blocked = self.run_tool(state, "begin", expected=2)
            self.assertIn("Первичная настройка трекеров не завершена", blocked.stderr)
            blocked_payload = json.loads(blocked.stdout)
            self.assertFalse(blocked_payload["workflow_complete"])
            self.assertFalse(blocked_payload["final_response_allowed"])
            self.assertEqual(blocked_payload["allowed_next_action"], "run-config-status")
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
            self.assertIn("status_rules.sbertrek.excluded", status["gaps"])
            self.assertEqual(migrated["development_issue_types"], [])
            self.assertEqual(migrated["schema_version"], 3)
            self.assertIsNone(migrated["status_rules"]["sbertrek"]["excluded"])
            self.assertIsNone(migrated["status_rules"]["jira"]["completed"])
            self.assertIsNone(migrated["jira_enabled"])
            self.assertFalse(migrated["setup_complete"])

    def test_completed_v1_config_reopens_provider_specific_status_questions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            legacy = {
                "schema_version": 1,
                "primary_provider": "sbertrek",
                "setup_complete": True,
                "jira_enabled": True,
                "projects": {"sbertrek": ["RSCON"], "jira": ["RSCON"]},
                "issue_pairs": {},
                "development_issue_types": ["story"],
                "participants": {"sbertrek": {}, "jira": {}},
                "status_rules": {
                    "completed": ["Решен", "Done"],
                    "excluded": ["Отменен", "Cancelled"],
                },
            }
            config_path = state / "tracker-config.json"
            self.write_json(config_path, legacy)

            status = json.loads(self.run_tool(state, "config-status", expected=3).stdout)
            migrated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(status["gaps"][0], "status_rules.sbertrek.completed")
            self.assertEqual(
                status["next_question"],
                "Какие статусы SberTrek однозначно означают завершение разработки?",
            )
            self.assertFalse(migrated["setup_complete"])
            self.assertIsNone(migrated["status_rules"]["sbertrek"]["completed"])
            self.assertIsNone(migrated["status_rules"]["jira"]["completed"])

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
            self.run_tool(state, "set-statuses", "--provider", "sbertrek", "--kind", "completed", "done", "resolved")
            self.run_tool(state, "set-statuses", "--provider", "sbertrek", "--kind", "excluded", "cancelled", "deleted")
            self.run_tool(state, "set-statuses", "--provider", "jira", "--kind", "completed", "done", "resolved")
            self.run_tool(state, "set-statuses", "--provider", "jira", "--kind", "excluded", "cancelled", "deleted")
            completed = json.loads(self.run_tool(state, "complete-config").stdout)
            self.assertFalse(completed["must_stop"])
            self.assertEqual(completed["allowed_next_action"], "begin")
            ready = json.loads(self.run_tool(state, "config-status").stdout)
            self.assertEqual(ready["status"], "tracker-config-ready")
            self.assertFalse(ready["must_stop"])
            self.assertEqual(ready["allowed_next_action"], "begin")
            self.assertIsNone(ready["response_contract"])

            started = json.loads(self.run_tool(state, "begin").stdout)
            sber = json.loads(Path(started["sbertrek_input"]).read_text(encoding="utf-8"))
            jira = json.loads(Path(started["jira_input"]).read_text(encoding="utf-8"))
            self.assertEqual(sber["collection"]["history"]["state"], "pending")
            self.assertEqual(sber["collection"]["counterpart_lookup"]["state"], "not-applicable")
            self.assertEqual(jira["collection"]["counterpart_lookup"]["state"], "pending")
            self.assertFalse(started["workflow_complete"])
            self.assertFalse(started["final_response_allowed"])
            self.assertEqual(
                started["required_completion"]["command"],
                f"trackerctl.py reconcile --run-id {started['run_id']}",
            )
            self.assertIn("snapshot-issue", started["recording_commands"])
            self.assertIn("run-status", started["recording_commands"])
            self.assertFalse(started["project_changed"])
            self.assertFalse(started["tracker_changed"])

    def test_incremental_snapshot_commands_complete_run_without_manual_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            self.prepare_config(state)
            started = json.loads(self.run_tool(state, "begin").stdout)
            run_id = started["run_id"]

            self.run_tool(
                state,
                "snapshot-metadata",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--captured-at", "2026-08-26T10:00:00+03:00",
                "--query", "key in (SBER-1)",
                "--seed-evidence", "SBER-1=features/demo/actual-progress.md",
            )
            self.run_tool(
                state,
                "snapshot-issue",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--key", "SBER-1",
                "--counterpart-key", "JIRA-11",
                "--summary", "Основная задача",
                "--description", "Описание",
                "--issue-type", "Story",
                "--status", "Тестирование команды",
                "--assignee-id", "qa-s",
                "--assignee-name", "Тестировщик",
                "--assignee-state", "value",
                "--estimate-value", "5",
                "--estimate-unit", "story-points",
                "--estimate-state", "value",
                "--epic-state", "absent",
                "--releases-state", "absent",
                "--discovery", "seed",
                "--updated-at", "2026-08-26T09:00:00+03:00",
            )
            history = json.loads(self.run_tool(
                state,
                "snapshot-history",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--key", "SBER-1",
                "--at", "2026-08-25T12:00:00+03:00",
                "--field", "assignee",
                "--from-id", "dev-s",
                "--from-name", "Разработчик",
                "--to-id", "qa-s",
                "--to-name", "Тестировщик",
            ).stdout)
            self.assertEqual(history["history_count"], 1)
            history = json.loads(self.run_tool(
                state,
                "snapshot-history",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--key", "SBER-1",
                "--at", "2026-08-25T13:00:00+03:00",
                "--field", "status",
                "--from-value", "В работе",
                "--to-value", "Проверка",
            ).stdout)
            self.assertEqual(history["history_count"], 2)
            for capability in ("history", "epic_links", "release_links", "epic_neighbors"):
                self.run_tool(
                    state,
                    "snapshot-collection",
                    "--run-id", run_id,
                    "--provider", "sbertrek",
                    "--capability", capability,
                    "--state", "complete",
                    "--evidence", f"mcp:{capability}",
                    *(["--checked-key", "SBER-1"] if capability == "history" else []),
                )

            self.run_tool(
                state,
                "snapshot-metadata",
                "--run-id", run_id,
                "--provider", "jira",
                "--captured-at", "2026-08-26T10:01:00+03:00",
                "--query", "key in (JIRA-11)",
            )
            self.run_tool(
                state,
                "snapshot-issue",
                "--run-id", run_id,
                "--provider", "jira",
                "--key", "JIRA-11",
                "--counterpart-key", "SBER-1",
                "--summary", "Основная задача",
                "--issue-type", "Story",
                "--status", "To Do",
                "--assignee-state", "absent",
                "--estimate-state", "absent",
                "--epic-state", "absent",
                "--releases-state", "absent",
                "--discovery", "counterpart",
                "--updated-at", "2026-08-26T09:01:00+03:00",
            )
            for capability in (
                "history", "epic_links", "release_links", "counterpart_lookup", "epic_neighbors",
            ):
                self.run_tool(
                    state,
                    "snapshot-collection",
                    "--run-id", run_id,
                    "--provider", "jira",
                    "--capability", capability,
                    "--state", "complete",
                    "--evidence", f"mcp:{capability}",
                    *(["--checked-key", "JIRA-11"] if capability == "history" else []),
                )

            progress = json.loads(
                self.run_tool(state, "run-status", "--run-id", run_id).stdout
            )
            self.assertEqual(progress["status"], "tracker-run-ready")
            self.assertEqual(progress["allowed_next_action"], "reconcile")
            self.assertFalse(progress["final_response_allowed"])

            reconciled = json.loads(
                self.run_tool(state, "reconcile", "--run-id", run_id).stdout
            )
            self.assertEqual(reconciled["status"], "tracker-read-reconciled")
            self.assertTrue(reconciled["workflow_complete"])
            self.assertTrue(reconciled["final_response_allowed"])
            self.assertEqual(
                json.loads(
                    self.run_tool(state, "run-status", "--run-id", run_id).stdout
                ),
                reconciled,
            )
            self.assertEqual(
                json.loads(
                    self.run_tool(state, "result-status", "--run-id", run_id).stdout
                ),
                reconciled,
            )
            self.assertEqual(
                json.loads(
                    self.run_tool(state, "reconcile", "--run-id", run_id).stdout
                ),
                reconciled,
            )

    def test_jira_can_be_disabled_without_creating_an_input_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            self.run_tool(state, "init-config")
            self.run_tool(state, "set-projects", "--provider", "sbertrek", "SBER")
            self.run_tool(state, "set-jira-mode", "disabled")
            self.run_tool(state, "set-issue-types", "story")
            self.run_tool(state, "set-statuses", "--provider", "sbertrek", "--kind", "completed", "done")
            self.run_tool(state, "set-statuses", "--provider", "sbertrek", "--kind", "excluded", "--none")
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
            blocked = json.loads(result.stdout)
            self.assertFalse(blocked["workflow_complete"])
            self.assertFalse(blocked["final_response_allowed"])
            self.assertEqual(
                blocked["allowed_next_action"],
                "run-status-and-complete-snapshots",
            )
            self.assertEqual(blocked["required_success_status"], "tracker-read-reconciled")

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
            issue["field_observations"]["assignee"] = "not-returned"
            self.restrict_to_issues(sber, [issue])
            sber["scope"]["expected_epic_keys"] = []
            sber["scope"]["expected_release_keys"] = []
            sber["collection"]["epic_links"] = {
                "state": "unavailable",
                "reason": "MCP не возвращает поле эпика",
                "failure_kind": "capability-absent",
                "evidence": ["mcp:schema-inspection"],
                "checked_keys": [],
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
            self.assertIn("sbertrek-assignee-not-returned:1", reconciled["limitations"])
            self.assertIn("jira-unavailable", reconciled["limitations"])

    def test_snapshot_requires_explicit_observation_for_every_critical_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            del sber["issues"][0]["field_observations"]["assignee"]
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state, "reconcile", "--sbertrek", str(sber_path), expected=2
            )
            self.assertIn("явно описывать field_observations", result.stderr)

    def test_complete_history_requires_every_issue_key_to_be_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, _ = self.snapshots()
            sber["collection"]["history"]["checked_keys"].remove("SBER-4")
            sber_path = root / "sber.json"
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state, "reconcile", "--sbertrek", str(sber_path), expected=2
            )
            self.assertIn("history=complete без проверки задач: SBER-4", result.stderr)

    def test_unknown_participant_blocks_reconcile_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            del config["participants"]["sbertrek"]["qa-s"]
            del config["participants"]["sbertrek"]["dev-s"]
            config["jira_enabled"] = False
            self.write_json(config_path, config)
            sber, _ = self.snapshots()
            self.restrict_to_issues(sber, [sber["issues"][0]])
            started = json.loads(self.run_tool(state, "begin").stdout)
            run_id = started["run_id"]
            sber_path = Path(started["sbertrek_input"])
            self.write_json(sber_path, sber)

            result = self.run_tool(
                state,
                "reconcile",
                "--run-id",
                run_id,
                expected=2,
            )
            self.assertIn("provider=sbertrek", result.stderr)
            self.assertIn("account_id=dev-s", result.stderr)
            self.assertIn("set-participant", result.stderr)
            blocked = json.loads(result.stdout)
            self.assertEqual(blocked["allowed_next_action"], "ask-user")
            self.assertNotIn("next_command", blocked)
            self.assertIn("dev-s", blocked["next_question"])
            self.assertEqual(
                blocked["response_contract"]["text"],
                blocked["next_question"],
            )
            first_saved = json.loads(self.run_tool(
                state,
                "set-participant",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--account-id", "dev-s",
                "--team-id", "B1",
            ).stdout)
            self.assertEqual(first_saved["team_id"], "BE1")
            self.assertEqual(first_saved["derived_role"], "developer")
            self.assertEqual(first_saved["allowed_next_action"], "ask-user")
            self.assertIn("qa-s", first_saved["next_question"])

            second_saved = json.loads(self.run_tool(
                state,
                "set-participant",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--account-id", "qa-s",
                "--team-id", "QA1",
            ).stdout)
            self.assertEqual(second_saved["allowed_next_action"], "reconcile")
            reconciled = json.loads(
                self.run_tool(state, "reconcile", "--run-id", run_id).stdout
            )
            self.assertTrue(reconciled["final_response_allowed"])

    def test_run_status_rejects_unresolved_seed_instead_of_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["jira_enabled"] = False
            self.write_json(config_path, config)
            started = json.loads(self.run_tool(state, "begin").stdout)
            run_id = started["run_id"]
            self.run_tool(
                state,
                "snapshot-metadata",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--captured-at", "2026-08-26T10:00:00+03:00",
                "--query", "key = SBER-404",
                "--seed-evidence", "SBER-404=features/demo/actual-progress.md",
            )
            for capability in ("history", "epic_links", "release_links", "epic_neighbors"):
                self.run_tool(
                    state,
                    "snapshot-collection",
                    "--run-id", run_id,
                    "--provider", "sbertrek",
                    "--capability", capability,
                    "--state", "complete",
                    "--evidence", f"mcp:{capability}",
                )

            status = json.loads(
                self.run_tool(state, "run-status", "--run-id", run_id).stdout
            )
            self.assertEqual(status["status"], "tracker-run-incomplete")
            self.assertIn("не прочитаны", status["snapshots"][0]["validation_error"])
            blocked = self.run_tool(
                state, "reconcile", "--run-id", run_id, expected=2
            )
            self.assertIn("SBER-404", blocked.stderr)

    def test_not_found_requires_direct_read_evidence_and_resolves_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            config_path = self.prepare_config(state)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["jira_enabled"] = False
            self.write_json(config_path, config)
            started = json.loads(self.run_tool(state, "begin").stdout)
            run_id = started["run_id"]
            self.run_tool(
                state,
                "snapshot-metadata",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--captured-at", "2026-08-26T10:00:00+03:00",
                "--query", "key = SBER-404",
                "--seed-evidence", "SBER-404=features/demo/actual-progress.md",
            )
            self.run_tool(
                state,
                "snapshot-not-found",
                "--run-id", run_id,
                "--provider", "sbertrek",
                "--key", "SBER-404",
                "--evidence", "mcp:issue.getByKey:404",
            )
            snapshot = json.loads(Path(started["sbertrek_input"]).read_text(encoding="utf-8"))
            self.assertEqual(snapshot["collection"]["not_found_keys"], ["SBER-404"])
            self.assertEqual(
                snapshot["collection"]["not_found_evidence"][0]["evidence"],
                "mcp:issue.getByKey:404",
            )

    def test_collection_claim_requires_call_evidence_and_cannot_mean_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            self.prepare_config(state)
            run_id = json.loads(self.run_tool(state, "begin").stdout)["run_id"]
            missing = self.run_tool(
                state,
                "snapshot-collection",
                "--run-id", run_id,
                "--provider", "jira",
                "--capability", "history",
                "--state", "complete",
                expected=2,
            )
            self.assertIn("--evidence", missing.stderr)
            skipped = self.run_tool(
                state,
                "snapshot-collection",
                "--run-id", run_id,
                "--provider", "jira",
                "--capability", "history",
                "--state", "unavailable",
                "--failure-kind", "capability-absent",
                "--reason", "Jira MCP не вызван",
                "--evidence", "none",
                expected=2,
            )
            self.assertIn("Пропущенный MCP-вызов", skipped.stderr)

    def test_proactive_participant_mapping_without_pending_question_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            self.prepare_config(state)
            run_id = json.loads(self.run_tool(state, "begin").stdout)["run_id"]
            result = self.run_tool(
                state,
                "set-participant",
                "--run-id", run_id,
                "--provider", "jira",
                "--account-id", "invented",
                "--team-id", "FE2",
                expected=2,
            )
            self.assertIn("нет ожидающего вопроса", result.stderr)

    def test_duplicate_counterpart_claims_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, jira = self.snapshots()
            jira["issues"][1]["counterpart_key"] = "SBER-1"
            sber_path = root / "sber.json"
            jira_path = root / "jira.json"
            self.write_json(sber_path, sber)
            self.write_json(jira_path, jira)
            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek", str(sber_path),
                "--jira", str(jira_path),
                expected=2,
            )
            self.assertIn("несколько Jira-counterpart", result.stderr)

    def test_counterpart_lookup_cannot_be_unavailable_after_counterpart_was_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            self.prepare_config(state)
            sber, jira = self.snapshots()
            jira["collection"]["counterpart_lookup"] = {
                "state": "unavailable",
                "reason": "Direct lookup returned a provider error",
                "failure_kind": "call-failed",
                "evidence": ["mcp:get-issue:error"],
            }
            sber_path = root / "sber.json"
            jira_path = root / "jira.json"
            self.write_json(sber_path, sber)
            self.write_json(jira_path, jira)
            result = self.run_tool(
                state,
                "reconcile",
                "--sbertrek", str(sber_path),
                "--jira", str(jira_path),
                expected=2,
            )
            self.assertIn("нельзя объявить unavailable", result.stderr)

    def test_version_two_config_discards_unguarded_participant_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            path = self.prepare_config(state)
            config = json.loads(path.read_text(encoding="utf-8"))
            config["schema_version"] = 2
            config["participants"] = {
                "sbertrek": {"one": {"canonical_id": "FE2", "role": "developer"}},
                "jira": {
                    "two": {"canonical_id": "FE2", "role": "developer"},
                    "three": {"canonical_id": "FE2", "role": "developer"},
                },
            }
            self.write_json(path, config)
            status = json.loads(self.run_tool(state, "config-status").stdout)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "tracker-config-ready")
            self.assertEqual(migrated["schema_version"], 3)
            self.assertEqual(migrated["participants"], {"sbertrek": {}, "jira": {}})


if __name__ == "__main__":
    unittest.main()
