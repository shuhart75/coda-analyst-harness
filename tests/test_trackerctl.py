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
        config["issue_pairs"] = {"SBER-1": "JIRA-11"}
        config["development_issue_types"] = ["development-task", "Story"]
        config["participants"] = {
            "sbertrek": {
                "dev-s": {"canonical_id": "B1", "role": "developer"},
                "qa-s": {"canonical_id": "Q1", "role": "tester"},
            },
            "jira": {
                "dev-j": {"canonical_id": "B1", "role": "developer"},
                "qa-j": {"canonical_id": "Q1", "role": "tester"},
            },
        }
        config["status_rules"]["completed"] = ["Выполнена"]
        self.write_json(path, config)
        return path

    def snapshots(self) -> tuple[dict, dict]:
        sber = {
            "schema_version": 1,
            "provider": "sbertrek",
            "captured_at": "2026-08-26T10:00:00+00:00",
            "scope": {"projects": ["SBER"], "query": "key in (...)"},
            "issues": [
                {
                    "key": "SBER-1",
                    "summary": "Основная задача",
                    "description": "Реализовать часть функциональности.",
                    "issue_type": "development-task",
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
            "scope": {"projects": ["JIRA"], "query": "key in (...)"},
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

            report = Path(output["report"]).read_text(encoding="utf-8")
            self.assertIn("## Эпики", report)
            self.assertIn("## Релизы", report)
            self.assertIn("EPIC-1", report)
            self.assertIn("REL-1", report)
            self.assertIn("epic-neighbor", report)
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
            self.assertEqual(reconciled["limitations"], ["jira-unavailable"])
            self.assertFalse(
                any(item["kind"] == "jira-pair-missing" for item in reconciled["discrepancies"])
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


if __name__ == "__main__":
    unittest.main()
