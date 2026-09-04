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


class DirectTrackerWorkflowTests(unittest.TestCase):
    def run_tool(self, state: Path, *args: str, expected: int = 0) -> dict:
        result = subprocess.run(
            (sys.executable, str(SCRIPT), *args), text=True, capture_output=True,
            env={**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(state)}, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def write(self, path: Path, payload: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def configure(self, state: Path) -> None:
        self.write(state / "tracker-config.json", {
            "schema_version": 4,
            "primary_provider": "sbertrek",
            "setup_complete": True,
            "jira_enabled": True,
            "projects": {"sbertrek": ["RSCON"], "jira": ["RSCON"]},
            "development_issue_types": ["story", "task"],
            "participants": {"sbertrek": {}, "jira": {}},
            "status_rules": {
                "sbertrek": {"completed": ["done"], "excluded": ["cancelled"]},
                "jira": {"completed": ["Done"], "excluded": ["Cancelled"]},
            },
        })

    def begin(self, state: Path, provider: str, kind: str, *keys: str) -> dict:
        self.configure(state)
        args = [
            "begin", "--scope-kind", kind, "--scope-provider", provider,
            "--label", "Test", "--scope-source", "unit-test", "--intent", "read-only",
        ]
        for key in keys:
            args += ["--scope-id", key]
        return self.run_tool(state, *args)

    def sber_issue(
        self, key: str, *, jira_key: str | None = None, summary: str | None = None,
        status: str = "created", issue_type: str = "story", general: float | None = None,
        roles: dict[str, float] | None = None,
    ) -> dict:
        attributes = []
        if jira_key:
            attributes.append({"code": "issue_key", "name": "Объект Jira", "value": jira_key})
        if general is not None:
            attributes.append({"code": "story_points", "name": "Относительная сложность", "value": general})
        names = {"AN": "Анализ", "BE": "Разработка BE", "FE": "Разработка FE", "QA": "Тестирование"}
        for role, value in (roles or {}).items():
            attributes.append({"code": f"estimate_{role.lower()}", "name": names[role], "value": value})
        return {
            "key": key, "summary": summary or f"[{next(iter(roles or {'BE': 1}))}] Task {key}",
            "suit": {"code": issue_type, "name": issue_type.title()},
            "status": {"code": status, "name": status}, "attributes": attributes,
            "created_at": "2026-08-01T08:00:00+00:00", "updated_at": "2026-08-02T08:00:00+00:00",
        }

    def jira_issue(
        self, key: str, *, summary: str | None = None, status: str = "To Do",
        roles: dict[str, float] | None = None,
    ) -> dict:
        fields = {
            "summary": summary or f"[BE] Task {key}", "issuetype": {"name": "Story"},
            "status": {"name": status}, "assignee": None,
            "created": "2026-08-01T08:00:00+00:00", "updated": "2026-08-02T08:00:00+00:00",
            "fixVersions": [],
        }
        ids = {"AN": "customfield_15062", "BE": "customfield_15014", "FE": "customfield_15015", "QA": "customfield_15064"}
        for role, value in (roles or {}).items():
            fields[ids[role]] = value
        return {"key": key, "fields": fields}

    def ingest(self, state: Path, payload: dict, response: object, *, expected: int = 0, name: str = "response") -> dict:
        path = self.write(state / "responses" / f"{name}.json", response)
        return self.run_tool(
            state, "ingest", "--run-id", payload["run_id"],
            "--step-id", payload["next_action"]["step_id"], "--response-file", str(path),
            "--response-source", "mcp-file", expected=expected,
        )

    def reconcile(self, state: Path, payload: dict) -> tuple[dict, dict]:
        completion = self.run_tool(state, "reconcile", "--run-id", payload["run_id"])
        result = json.loads((state / "tracker-runs" / payload["run_id"] / "reconciled.json").read_text(encoding="utf-8"))
        return completion, result

    def test_config_gate_asks_one_question(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.run_tool(state, "init-config")
            payload = self.run_tool(state, "config-status", expected=3)
            self.assertEqual(payload["next_question"], payload["response_contract"]["text"])

    def test_sbertrek_epic_to_jira_and_role_work_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "epic", "RSCON-7000")
            self.assertEqual(current["protocol"], "direct-tracker-v1")
            self.assertEqual(current["next_action"]["arguments"]["query"], 'unit IN linkedUnitsOf("unit = \'RSCON-7000\'", "Состоит из")')
            current = self.ingest(state, current, {"issues": [
                self.sber_issue("RSCON-7001", jira_key="RSCON-3001", summary="[FE] Form", general=5),
                self.sber_issue("RSCON-7002", summary="[BE] Local", roles={"BE": 2}),
            ]}, name="sber")
            self.assertEqual(current["next_action"]["provider"], "jira")
            self.assertEqual(current["next_action"]["arguments"]["jql"], 'key IN ("RSCON-3001")')
            current = self.ingest(state, current, {"issues": [self.jira_issue("RSCON-3001", roles={"FE": 8, "QA": 3})]}, name="jira")
            reconciliation, result = self.reconcile(state, current)
            self.assertFalse(reconciliation["final_response_allowed"])
            self.assertEqual(reconciliation["allowed_next_action"], "result-status")
            self.assertNotIn("response_contract", reconciliation)
            completion = self.run_tool(state, "result-status", "--run-id", current["run_id"])
            self.assertTrue(completion["final_response_allowed"])
            self.assertEqual(result["counts"]["matched"], 1)
            paired = next(item for item in result["issues"] if item["sbertrek_key"] == "RSCON-7001")
            self.assertEqual(paired["role_estimates"]["FE"]["value"], 5)
            self.assertEqual(paired["role_estimates"]["QA"]["value"], 3)
            self.assertEqual(paired["role_estimates"]["QA"]["source"], "jira")
            self.assertIn("role_estimates.FE", {item["field"] for item in result["discrepancies"]})
            summaries = {item["summary"] for item in result["work_items"]}
            self.assertIn("FE Form", summaries)
            self.assertIn("BE Local", summaries)
            official = completion["response_contract"]["text"]
            self.assertIn("Задачи:", official)
            self.assertIn("SberTrek RSCON-7001; Jira RSCON-3001; [FE] Form", official)
            self.assertIn("оценки: AN -, BE -, FE 5.0, QA 3.0", official)
            status = self.run_tool(state, "run-status", "--run-id", current["run_id"])
            self.assertEqual(status["allowed_next_action"], "result-status")
            self.assertFalse(status["final_response_allowed"])

    def test_sbertrek_tasks_route_uses_only_explicit_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7002", "RSCON-7001")
            self.assertEqual(current["next_action"]["arguments"]["query"], 'unit = "RSCON-7001" or unit = "RSCON-7002"')
            current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001"), self.sber_issue("RSCON-7002")]})
            self.assertEqual(current["allowed_next_action"], "reconcile")

    def test_jira_tasks_reverse_lookup_uses_issue_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "jira", "tasks", "RSCON-3002", "RSCON-3001")
            self.assertEqual(current["next_action"]["arguments"]["jql"], 'key IN ("RSCON-3001", "RSCON-3002")')
            current = self.ingest(state, current, {"issues": [self.jira_issue("RSCON-3001"), self.jira_issue("RSCON-3002")]}, name="jira")
            self.assertEqual(current["next_action"]["arguments"]["query"], 'issue_key = "RSCON-3001" or issue_key = "RSCON-3002"')
            current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001", jira_key="RSCON-3001")]}, name="sber")
            _, result = self.reconcile(state, current)
            self.assertEqual(result["counts"]["matched"], 1)
            self.assertEqual(result["counts"]["issues"], 2)

    def test_jira_assignee_with_display_name_only_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "jira", "tasks", "RSCON-3001")
            issue = self.jira_issue("RSCON-3001")
            issue["fields"]["assignee"] = {"display_name": "Иван Реутов"}
            current = self.ingest(state, current, {"issues": [issue]}, name="jira")
            current = self.ingest(state, current, {"issues": []}, name="sber")
            _, result = self.reconcile(state, current)
            self.assertEqual(result["issues"][0]["assignee"], {"id": None, "name": "Иван Реутов"})

    def test_jira_epic_discovers_sbertrek_epic_then_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "jira", "epic", "RSCON-3000")
            self.assertEqual(current["next_action"]["arguments"]["jql"], '"Epic Link" = "RSCON-3000"')
            current = self.ingest(state, current, {"issues": [self.jira_issue("RSCON-3001")]}, name="jira-members")
            self.assertEqual(current["next_action"]["arguments"]["query"], 'issue_key = "RSCON-3000"')
            discovery = self.sber_issue("RSCON-7000", jira_key="RSCON-3000", issue_type="epic")
            current = self.ingest(state, current, {"issues": [discovery]}, name="discovery")
            self.assertEqual(current["next_action"]["arguments"]["query"], 'unit IN linkedUnitsOf("unit = \'RSCON-7000\'", "Состоит из")')
            current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001", jira_key="RSCON-3001")]}, name="sber-members")
            self.assertEqual(current["allowed_next_action"], "reconcile")
            _, result = self.reconcile(state, current)
            self.assertEqual(result["counts"]["matched"], 1)

    def test_jira_epic_without_sbertrek_counterpart_still_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "jira", "epic", "RSCON-3000")
            current = self.ingest(state, current, {"issues": [self.jira_issue("RSCON-3001")]}, name="jira")
            current = self.ingest(state, current, {"issues": []}, name="empty")
            self.assertEqual(current["allowed_next_action"], "reconcile")
            self.assertIn("sbertrek-counterpart-epic-not-found:RSCON-3000", current["limitations"])

    def test_confirmed_missing_jira_counterpart_excludes_sbertrek_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001", "RSCON-7002")
            current = self.ingest(state, current, {"issues": [
                self.sber_issue("RSCON-7001", jira_key="RSCON-3001"),
                self.sber_issue("RSCON-7002"),
            ]}, name="sber")
            error = self.write(state / "responses" / "error.json", {"error": "An issue with key 'RSCON-3001' does not exist for field 'key'."})
            current = self.run_tool(
                state, "ingest-error", "--run-id", current["run_id"],
                "--step-id", current["next_action"]["step_id"], "--error-file", str(error),
            )
            _, result = self.reconcile(state, current)
            self.assertEqual(result["counts"]["excluded"], 1)
            self.assertEqual(result["counts"]["issues"], 1)
            self.assertEqual(result["issues"][0]["sbertrek_key"], "RSCON-7002")

    def test_begin_and_ingest_are_idempotent_but_conflicting_repeat_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            first = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            second = self.run_tool(
                state, "begin", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-7001", "--label", "Other label", "--scope-source", "rephrased-request", "--intent", "read-only",
            )
            self.assertEqual(first["run_id"], second["run_id"])
            response = {"issues": [self.sber_issue("RSCON-7001")]}
            response_path = self.write(state / "responses" / "same.json", response)
            current = self.run_tool(state, "ingest", "--run-id", first["run_id"], "--step-id", first["next_action"]["step_id"], "--response-file", str(response_path), "--response-source", "mcp-file")
            repeated = self.run_tool(state, "ingest", "--run-id", first["run_id"], "--step-id", first["next_action"]["step_id"], "--response-file", str(response_path), "--response-source", "mcp-file")
            self.assertEqual(repeated["ingest"], "idempotent-no-op")
            changed = self.write(state / "responses" / "changed.json", {"issues": [self.sber_issue("RSCON-7001", summary="Changed")]})
            failed = self.run_tool(state, "ingest", "--run-id", first["run_id"], "--step-id", first["next_action"]["step_id"], "--response-file", str(changed), "--response-source", "mcp-file", expected=2)
            self.assertIn("другой ответ", failed["error"])
            status = self.run_tool(state, "run-status", "--run-id", current["run_id"])
            self.assertEqual(status["status"], "tracker-read-failed")

    def test_exactly_fifty_results_is_visible_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            keys = [f"RSCON-{7000 + index}" for index in range(50)]
            current = self.begin(state, "sbertrek", "epic", "RSCON-6999")
            current = self.ingest(state, current, {"issues": [self.sber_issue(key) for key in keys]})
            self.assertIn("sbertrek-result-limit-reached:50", current["limitations"])

    def test_legacy_active_run_can_be_abandoned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.configure(state)
            run_id = "20260901T100946Z-1e1af32a"
            self.write(state / "tracker-runs" / run_id / "run.json", {
                "protocol": "legacy-protocol", "run_id": run_id, "status": "collecting",
            })
            self.write(state / "tracker-active-run.json", {"run_id": run_id})
            abandoned = self.run_tool(
                state, "abandon-run", "--run-id", run_id, "--reason", "start-clean",
            )
            self.assertEqual(abandoned["allowed_next_action"], "begin")
            marker = json.loads(
                (state / "tracker-runs" / run_id / "abandoned.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["previous_protocol"], "legacy-protocol")
            self.assertFalse((state / "tracker-active-run.json").exists())
            started = self.run_tool(
                state, "begin", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-7001", "--label", "Test", "--scope-source", "unit-test",
            )
            self.assertEqual(started["protocol"], "direct-tracker-v1")

    def test_corrupt_json_fails_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            path = state / "responses" / "broken.json"
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")
            self.run_tool(state, "ingest", "--run-id", current["run_id"], "--step-id", current["next_action"]["step_id"], "--response-file", str(path), "--response-source", "mcp-file", expected=2)
            status = self.run_tool(state, "run-status", "--run-id", current["run_id"])
            self.assertEqual(status["status"], "tracker-read-failed")

    def test_result_status_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001")]})
            reconciliation, _ = self.reconcile(state, current)
            self.assertEqual(reconciliation["allowed_next_action"], "result-status")
            clean = self.run_tool(state, "result-status", "--run-id", current["run_id"])
            self.assertEqual(clean["response_contract"]["type"], "emit-verbatim")
            report = Path(clean["paths"]["report"])
            report.write_text("changed\n", encoding="utf-8")
            self.run_tool(state, "result-status", "--run-id", current["run_id"], expected=2)


if __name__ == "__main__":
    unittest.main()
