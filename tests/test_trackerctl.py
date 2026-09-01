from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "trackerctl.py"


class TrackerCtlV2Tests(unittest.TestCase):
    def run_tool(self, state: Path, *args: str, expected: int = 0) -> dict:
        result = subprocess.run(
            (sys.executable, str(SCRIPT), *args), text=True, capture_output=True,
            env={**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(state)}, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def write(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def config(self, state: Path, *, jira: bool = True, participants: bool = True) -> None:
        mapping = (
            {
                "sbertrek": {"s-dev": {"team_id": "BE1"}, "s-qa": {"team_id": "QA1"}},
                "jira": {"j-dev": {"team_id": "BE1"}, "j-qa": {"team_id": "QA1"}},
            }
            if participants else {"sbertrek": {}, "jira": {}}
        )
        self.write(state / "tracker-config.json", {
            "schema_version": 4, "primary_provider": "sbertrek", "setup_complete": True,
            "jira_enabled": jira,
            "projects": {"sbertrek": ["RSCON"], "jira": ["RSCON"] if jira else []},
            "development_issue_types": ["story", "task"], "participants": mapping,
            "status_rules": {
                "sbertrek": {"completed": ["done"], "excluded": ["cancelled"]},
                "jira": {"completed": ["Done"], "excluded": ["Cancelled"]},
            },
        })

    def begin(
        self, state: Path, *, provider: str = "sbertrek", kind: str = "tasks",
        ids: tuple[str, ...] = ("RSCON-6845",), jira: bool = True,
        participants: bool = True, intent: str = "read-only",
    ) -> dict:
        self.config(state, jira=jira, participants=participants)
        args = [
            "begin", "--scope-kind", kind, "--scope-provider", provider,
            "--label", "Когорты", "--scope-source", "Запрос аналитика", "--intent", intent,
        ]
        for value in ids:
            args += ["--scope-id", value]
        return self.run_tool(state, *args)

    def root(self, state: Path, run_id: str) -> Path:
        return state / "tracker-runs" / run_id

    def snapshot(self, state: Path, run_id: str, provider: str) -> dict:
        return json.loads((self.root(state, run_id) / "providers" / f"{provider}.json").read_text(encoding="utf-8"))

    def job(self, state: Path, run_id: str, job_id: str) -> dict:
        return json.loads((self.root(state, run_id) / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))

    def active_job(self, state: Path, run_id: str) -> dict | None:
        jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (self.root(state, run_id) / "jobs").glob("*.json")]
        jobs = [job for job in jobs if job["state"] in {"pending", "running"}]
        jobs.sort(key=lambda job: (
            0 if job["kind"] == "provider-collection" else 1,
            0 if job["provider"] == "sbertrek" else 1,
            job["job_id"],
        ))
        return jobs[0] if jobs else None

    def query_page(
        self, state: Path, run_id: str, provider: str, keys: list[str], *,
        page: int = 1, last: bool = True, cursor: str | None = None,
        next_cursor: str | None = None,
    ) -> tuple[dict, str]:
        self.assertEqual(provider, "jira", "SberTrek pages must use structural JSON import")
        query = self.snapshot(state, run_id, provider)["query"]["exact"]
        evidence = f"mcp:{provider}:query:page-{page}"
        self.run_tool(
            state, "mcp-log", "--run-id", run_id, "--provider", provider,
            "--operation", "query", "--outcome", "success", "--evidence", evidence,
            "--summary", "targeted query", "--query", query,
            "--page-number", str(page), "--returned-count", str(len(keys)),
        )
        args = [
            "query-page", "--run-id", run_id, "--provider", provider,
            "--query", query, "--page-number", str(page), "--evidence", evidence,
        ]
        if cursor:
            args += ["--cursor", cursor]
        if last:
            args += ["--last-page"]
        else:
            args += ["--next-cursor", next_cursor or f"cursor-{page + 1}"]
        for key in keys:
            args += ["--key", key]
        return self.run_tool(state, *args), evidence

    def record_absent_counterparts(
        self, state: Path, run_id: str, keys: list[str], *,
        evidence: str = "mcp:jira:query:absent-counterparts",
    ) -> dict:
        query = self.snapshot(state, run_id, "jira")["query"]["exact"]
        summary = "\n".join(
            f"An issue with key '{key}' does not exist for field 'key'."
            for key in keys
        )
        self.run_tool(
            state, "mcp-log", "--run-id", run_id, "--provider", "jira",
            "--operation", "query", "--outcome", "error", "--evidence", evidence,
            "--summary", summary, "--query", query,
            "--page-number", "1", "--returned-count", "0",
        )
        args = [
            "jira-record-absent-counterparts", "--run-id", run_id,
            "--evidence", evidence,
        ]
        for key in keys:
            args += ["--key", key]
        return self.run_tool(state, *args)

    def sber_response_issue(
        self, key: str, *, jira_key: str | None = None, assignee: str | None = None,
        assignee_name: str | None = None,
        estimate: str | None = None, epic: str | None = None,
        summary: str | None = None, status: str = "active",
        include_attributes: bool = True,
    ) -> dict:
        attributes = []
        if assignee:
            attributes.append({
                "code": "assigned_to",
                "value": {"externalId": assignee, "displayName": assignee_name or assignee},
            })
        if estimate:
            attributes.append({"code": "story_points", "value": estimate})
        if jira_key:
            attributes.append({"code": "issue_key", "value": jira_key})
        if epic:
            attributes.append({"code": "epic", "value": {"key": epic, "name": f"Epic {epic}"}})
        issue = {
            "key": key,
            "summary": summary or f"Issue {key}",
            "issue_type": {"code": "story", "name": "Story"},
            "status": {"code": status, "name": status},
            "created_at": "2026-08-01T08:00:00+00:00",
            "updated_at": "2026-08-27T08:00:00+00:00",
        }
        if include_attributes:
            issue["attributes"] = attributes
        return issue

    def ingest_sber_response(
        self, state: Path, run_id: str, issues: list[tuple[str, dict]], *,
        wrapper: bool = False, last: bool = True,
    ) -> dict:
        records = [self.sber_response_issue(key, **values) for key, values in issues]
        payload: object = {"issues": records}
        if wrapper:
            payload = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        response = state / "mcp-responses" / f"{run_id}-sbertrek.json"
        self.write(response, payload)
        args = [
            "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek",
            "--page-number", "1", "--max-results", "50",
            "--evidence", "mcp:sbertrek:query:page-1", "--response-file", str(response),
        ]
        if last:
            args.append("--last-page")
        else:
            args += ["--next-cursor", "cursor-2"]
        return self.run_tool(state, *args)

    def issue_args(
        self, run_id: str, provider: str, key: str, evidence: str, *,
        jira_key: str | None = None, assignee: str | None = None,
        estimate: str | None = None, epic: str | None = None,
        summary: str | None = None, status: str = "active",
        jira_key_state: str | None = None,
    ) -> list[str]:
        args = [
            "record-issue", "--run-id", run_id, "--provider", provider,
            "--key", key, "--evidence", evidence,
            "--summary", summary or f"Issue {key}",
            "--issue-type", "story", "--status", status,
            "--assignee-state", "value" if assignee else "absent",
            "--estimate-state", "value" if estimate else "absent",
            "--epic-state", "value" if epic else "absent", "--releases-state", "absent",
            "--created-at", "2026-08-01T08:00:00+00:00",
            "--updated-at", "2026-08-27T08:00:00+00:00",
        ]
        if provider == "sbertrek":
            args += ["--jira-key-state", jira_key_state or ("value" if jira_key else "absent")]
        if jira_key:
            args += ["--jira-key", jira_key]
        if assignee:
            args += ["--assignee-id", assignee, "--assignee-name", assignee]
        if estimate:
            args += ["--estimate", estimate, "--estimate-unit", "person-days"]
        if epic:
            args += ["--epic-key", epic, "--epic-name", f"Epic {epic}"]
        return args

    def add_issue(self, state: Path, run_id: str, provider: str, key: str, evidence: str, **kwargs) -> dict:
        return self.run_tool(state, *self.issue_args(run_id, provider, key, evidence, **kwargs))

    def collect(self, state: Path, run_id: str, provider: str, issues: list[tuple[str, dict]]) -> dict:
        if provider == "sbertrek":
            self.ingest_sber_response(state, run_id, issues)
        else:
            _, evidence = self.query_page(state, run_id, provider, [key for key, _ in issues])
            for key, values in issues:
                self.add_issue(state, run_id, provider, key, evidence, **values)
        return self.run_tool(state, "collector-complete", "--run-id", run_id, "--provider", provider)

    def complete_history_job(self, state: Path, run_id: str, *, handoff_key: str | None = None) -> None:
        job = self.active_job(state, run_id)
        self.assertIsNotNone(job)
        assert job
        self.assertEqual(job["kind"], "provider-history")
        provider = job["provider"]
        for key in job["keys"]:
            evidence = f"mcp:{provider}:history:{key}"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", provider,
                "--operation", "history", "--outcome", "success", "--evidence", evidence,
                "--summary", "bounded history", "--key", key,
            )
            if key == handoff_key:
                self.run_tool(
                    state, "history-event", "--run-id", run_id, "--provider", provider,
                    "--key", key, "--evidence", evidence, "--at", "2026-08-10T10:00:00+00:00",
                    "--field", "assignee", "--to-id", f"{provider[0]}-dev", "--to-name", "Developer",
                )
                self.run_tool(
                    state, "history-event", "--run-id", run_id, "--provider", provider,
                    "--key", key, "--evidence", evidence, "--at", "2026-08-20T10:00:00+00:00",
                    "--field", "assignee", "--from-id", f"{provider[0]}-dev",
                    "--from-name", "Developer", "--to-id", f"{provider[0]}-qa", "--to-name", "Tester",
                )
            self.run_tool(
                state, "history-complete", "--run-id", run_id, "--provider", provider,
                "--key", key, "--state", "complete", "--evidence", evidence,
            )
        self.run_tool(state, "history-job-complete", "--run-id", run_id, "--job-id", job["job_id"])

    def complete_all_histories(self, state: Path, run_id: str, *, handoff_key: str | None = None) -> None:
        while (job := self.active_job(state, run_id)) is not None:
            self.assertEqual(job["kind"], "provider-history")
            self.complete_history_job(state, run_id, handoff_key=handoff_key)

    def complete_sber_run(
        self, state: Path, *, participants: bool = True,
        intent: str = "read-only", handoff: bool = False,
    ) -> tuple[str, Path]:
        run_id = self.begin(state, participants=participants, intent=intent)["run_id"]
        self.collect(state, run_id, "sbertrek", [
            ("RSCON-6845", {
                "jira_key": "RSCON-2902", "assignee": "s-qa" if handoff else "s-dev",
                "estimate": "5", "epic": "RSCON-6854", "summary": "Sber title",
            }),
        ])
        self.assertEqual((self.active_job(state, run_id) or {}).get("provider"), "jira")
        self.collect(state, run_id, "jira", [
            ("RSCON-2902", {
                "assignee": "j-dev", "estimate": "8", "epic": "RSCON-2911", "summary": "Jira title",
            }),
        ])
        self.complete_all_histories(state, run_id, handoff_key="RSCON-6845" if handoff else None)
        return run_id, self.root(state, run_id)

    def test_config_stop_gate_asks_exactly_one_question(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.run_tool(state, "init-config")
            payload = self.run_tool(state, "config-status", expected=3)
            self.assertEqual(payload["next_question"], payload["response_contract"]["text"])

    def test_begin_creates_isolated_collection_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); payload = self.begin(state, ids=("RSCON-6849", "RSCON-6848"))
            self.assertEqual(payload["protocol"], "targeted-tracker-v2")
            self.assertTrue(payload["delegation_required"])
            job = self.job(state, payload["run_id"], "collection-sbertrek")
            query = 'unit = "RSCON-6848" or unit = "RSCON-6849"'
            self.assertEqual(job["query"]["text"], query)
            self.assertEqual(job["query"]["sha256"], hashlib.sha256(query.encode()).hexdigest())
            self.assertNotIn("role", job)
            self.assertNotIn("purpose", job["query"])
            self.assertNotIn("method", job["query"])
            self.assertIn("read-mcp-documentation", job["forbidden_operations"])
            self.assertIn("read-returned-issues-one-by-one", job["forbidden_operations"])
            self.assertEqual(
                job["response_contract"]["mcp_tool_contract"],
                {
                    "required_capability": "exact-tql-bulk-json-export",
                    "preferred_operation": "issue.exportJson",
                    "query_parameter": "query",
                    "max_results_parameter": "max_results",
                    "max_results": 50,
                    "forbidden_operations": ["issue.search", "issue.getByKey", "link.list"],
                },
            )

    def test_begin_rejects_second_active_run_until_abandoned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            first = self.begin(state)
            payload = self.run_tool(
                state, "begin", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-6846", "--label", "Second",
                "--scope-source", "Запрос аналитика", expected=2,
            )
            self.assertIn(first["run_id"], payload["error"])
            self.assertIn("новый begin запрещён", payload["error"])
            self.run_tool(
                state, "abandon-run", "--run-id", first["run_id"],
                "--reason", "Явно начат новый сеанс",
            )
            second = self.run_tool(
                state, "begin", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-6846", "--label", "Second",
                "--scope-source", "Запрос аналитика",
            )
            self.assertNotEqual(first["run_id"], second["run_id"])

    def test_collector_brief_contains_only_exact_query_and_recording_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state, kind="epic", ids=("RSCON-6854",))
            payload = self.run_tool(state, "collector-brief", "--run-id", begin["run_id"])
            exact = 'unit IN linkedUnitsOf("unit = \'RSCON-6854\'", "Состоит из")'
            self.assertIn(f"ровно этот TQL-запрос без изменений:\n\n{exact}", payload["prompt"])
            self.assertNotIn("Когорты", payload["prompt"])
            self.assertNotIn("эпик", payload["prompt"].casefold())
            self.assertIn("не заменяй запрос поиском по тексту", payload["prompt"].casefold())
            self.assertIn("полный исходный JSON-ответ", payload["prompt"])
            self.assertIn("не передавай fields=null", payload["prompt"].casefold())
            self.assertIn("поле attributes обязательно", payload["prompt"])
            self.assertIn("ingest-query-response", payload["prompt"])
            self.assertIn("issue.exportJson", payload["prompt"])
            self.assertIn("параметре query", payload["prompt"])
            self.assertIn("max_results=50", payload["prompt"])
            self.assertIn("--max-results 50", payload["prompt"])
            self.assertIn("Не используй issue.search", payload["prompt"])
            self.assertIn("issue.getByKey", payload["prompt"])
            self.assertIn("link.list", payload["prompt"])
            self.assertIn("верни ошибку и немедленно остановись", payload["prompt"])
            self.assertIn(f"run_id={begin['run_id']}", payload["prompt"])
            self.assertIn("Не запускай begin", payload["prompt"])
            self.assertIn("Не редактируй scope.json", payload["prompt"])

    def test_collector_brief_uses_same_narrow_prompt_for_all_query_scenarios(self) -> None:
        cases = [
            ("sbertrek", "tasks", ("RSCON-6848", "RSCON-6849"), 'unit = "RSCON-6848" or unit = "RSCON-6849"'),
            ("jira", "tasks", ("RSCON-2905", "RSCON-2906"), 'key IN ("RSCON-2905", "RSCON-2906")'),
            ("jira", "epic", ("RSCON-2911",), 'parent = "RSCON-2911"'),
        ]
        for provider, kind, ids, exact in cases:
            with self.subTest(provider=provider, kind=kind), tempfile.TemporaryDirectory() as temp:
                state = Path(temp); begin = self.begin(state, provider=provider, kind=kind, ids=ids)
                payload = self.run_tool(state, "collector-brief", "--run-id", begin["run_id"])
                self.assertIn(exact, payload["prompt"])
                self.assertNotIn("Когорты", payload["prompt"])

    def test_structural_import_reads_all_22_cards_from_full_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            run_id = self.begin(state, kind="epic", ids=("RSCON-6854",), jira=False)["run_id"]
            issues = [
                (f"RSCON-{6800 + index}", {
                    "jira_key": f"RSCON-{2800 + index}",
                    "estimate": "1",
                    "assignee": "s-dev" if index == 1 else None,
                })
                for index in range(1, 23)
            ]
            payload = self.ingest_sber_response(state, run_id, issues, wrapper=True)
            self.assertEqual(payload["returned_count"], 22)
            snapshot = self.snapshot(state, run_id, "sbertrek")
            self.assertEqual(len(snapshot["issues"]), 22)
            self.assertEqual(len(snapshot["query"]["keys"]), 22)
            self.assertEqual(snapshot["query"]["pages"][0]["recording_method"], "structural-json-import")
            self.assertEqual(snapshot["query"]["pages"][0]["requested_max_results"], 50)
            self.assertRegex(snapshot["query"]["pages"][0]["response_sha256"], r"^[a-f0-9]{64}$")

    def test_structural_import_rejects_non_maximum_sbertrek_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            run_id = self.begin(state, kind="epic", ids=("RSCON-6854",), jira=False)["run_id"]
            response = state / "limited.json"
            self.write(response, {"issues": [self.sber_response_issue("RSCON-6845")]})
            payload = self.run_tool(
                state, "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek",
                "--page-number", "1", "--max-results", "10", "--last-page",
                "--evidence", "mcp:sbertrek:query:limited", "--response-file", str(response),
                expected=2,
            )
            self.assertIn("--max-results 50", payload["error"])

    def test_reconcile_reports_possible_truncation_at_sbertrek_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            run_id = self.begin(state, kind="epic", ids=("RSCON-6854",), jira=False)["run_id"]
            issues = [(f"RSCON-{6800 + index}", {}) for index in range(1, 51)]
            self.collect(state, run_id, "sbertrek", issues)
            self.complete_all_histories(state, run_id)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("sbertrek-export-limit-reached:50", payload["limitations"])

    def test_sbertrek_manual_page_and_card_recording_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            query = self.snapshot(state, run_id, "sbertrek")["query"]["exact"]
            payload = self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek",
                "--operation", "query", "--outcome", "success",
                "--evidence", "mcp:sbertrek:query:manual", "--summary", "manual",
                "--query", query, "--page-number", "1", "--returned-count", "2",
                expected=2,
            )
            self.assertIn("ingest-query-response", payload["error"])

    def test_structural_import_accepts_flat_projected_sbertrek_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, kind="epic", ids=("RSCON-6854",))["run_id"]
            response = state / "projected.json"
            self.write(response, {"items": [{
                "key": "RSCON-6893",
                "summary": "CRUD для когорт",
                "issue_type": "story",
                "status": "code_review",
                "assigned_to": {"externalId": "s-dev", "displayName": "Developer"},
                "story_points": 1,
                "issue_key": "RSCON-2949",
                "releases": [],
                "created_at": "2026-08-26T06:23:00+00:00",
                "updated_at": "2026-08-31T08:08:00+00:00",
                "fields": {},
            }]})
            payload = self.run_tool(
                state, "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek",
                "--page-number", "1", "--max-results", "50", "--last-page", "--evidence", "mcp:sbertrek:query:projected",
                "--response-file", str(response),
            )
            self.assertEqual(payload["returned_count"], 1)
            issue = self.snapshot(state, run_id, "sbertrek")["issues"][0]
            self.assertEqual(issue["jira_key"], "RSCON-2949")
            self.assertEqual(issue["estimate"], {"value": 1.0, "unit": "story-points"})
            self.assertEqual(issue["epic"]["key"], "RSCON-6854")

    def test_structural_import_accepts_real_sbertrek_linked_units_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, kind="epic", ids=("RSCON-6854",))["run_id"]
            response = state / "linked-units.json"
            self.write(response, [{
                "key": "RSCON-6893",
                "summary": "CRUD для когорт",
                "description": "Not retained in the compact snapshot",
                "space": "RSCON",
                "suit": "story",
                "status": "code_review",
                "priority": "normal",
                "labels": ["SDD"],
                "assignee": None,
                "reporter": {"externalId": "reporter"},
                "created_at": "2026-08-26T06:23:00+00:00",
                "updated_at": "2026-08-31T08:08:00+00:00",
                "attributes": [
                    {
                        "code": "assigned_to", "name": "Исполнитель", "type": "user",
                        "value": {
                            "externalId": "s-dev", "firstName": "Арсений",
                            "lastName": "Савочкин", "middleName": "Игоревич",
                            "login": "s-dev", "userDetails": [],
                        },
                    },
                    {"code": "story_points", "name": "Относительная сложность", "type": "non_negative_int", "value": 1},
                    {"code": "issue_key", "name": "Объект Jira", "type": "issue_key", "value": "RSCON-2949"},
                    {"code": "fixversion", "name": "Релиз", "type": "unit", "value": []},
                ],
            }])
            payload = self.run_tool(
                state, "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek",
                "--page-number", "1", "--max-results", "50", "--last-page", "--evidence", "mcp:sbertrek:query:real-shape",
                "--response-file", str(response),
            )
            self.assertEqual(payload["returned_count"], 1)
            issue = self.snapshot(state, run_id, "sbertrek")["issues"][0]
            self.assertEqual(issue["issue_type"], "story")
            self.assertEqual(issue["assignee"], {"id": "s-dev", "name": "Савочкин Арсений Игоревич"})
            self.assertEqual(issue["estimate"], {"value": 1.0, "unit": "story-points"})
            self.assertEqual(issue["jira_key"], "RSCON-2949")
            self.assertEqual(issue["releases"], [])
            self.assertEqual(issue["field_observations"]["releases"], "absent")
            self.assertNotIn("description", issue)

    def test_complete_without_machine_page_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            snapshot_path = self.root(state, run_id) / "providers" / "sbertrek.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["query"].update({"state": "complete", "keys": ["RSCON-6893"]})
            snapshot["issues"] = [{
                "key": "RSCON-6893", "summary": "Issue", "issue_type": "story", "status": "created",
                "assignee": None, "estimate": "1", "epic": "RSCON-6854", "releases": [],
                "created_at": "2026-08-01T08:00:00+00:00", "updated_at": "2026-08-01T08:00:00+00:00",
                "jira_key": "RSCON-2949", "jira_key_state": "present", "field_observations": {},
                "history": {"state": "pending", "evidence": [], "events": [], "reason": None},
            }]
            self.write(snapshot_path, snapshot)
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2,
            )
            self.assertIn("без зарегистрированной страницы", payload["error"])

    def test_structural_import_accepts_a_proven_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            response = state / "empty.json"
            self.write(response, {"issues": []})
            payload = self.run_tool(
                state, "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek",
                "--page-number", "1", "--max-results", "50", "--last-page", "--evidence", "mcp:sbertrek:query:empty",
                "--response-file", str(response),
            )
            self.assertEqual(payload["returned_count"], 0)
            self.run_tool(state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek")

    def test_card_tampering_after_structural_import_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.ingest_sber_response(state, run_id, [("RSCON-6893", {"estimate": "1"})])
            snapshot_path = self.root(state, run_id) / "providers" / "sbertrek.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["issues"][0]["estimate"]["value"] = 99.0
            self.write(snapshot_path, snapshot)
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2,
            )
            self.assertIn("изменены после структурного импорта", payload["error"])

    def test_tampered_job_query_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            path = self.root(state, run_id) / "jobs" / "collection-sbertrek.json"
            job = json.loads(path.read_text())
            job["query"]["text"] = 'unit = "RSCON-9999"'
            self.write(path, job)
            payload = self.run_tool(state, "collector-brief", "--run-id", run_id, expected=2)
            self.assertIn("Контрольная сумма", payload["error"])

    def test_run_status_exposes_only_one_bounded_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            payload = self.run_tool(state, "run-status", "--run-id", run_id)
            self.assertEqual(payload["gaps"], ["collection-sbertrek.pending"])
            self.assertEqual(payload["next_job"]["job_id"], "collection-sbertrek")
            self.assertNotIn("query", payload["next_job"])

    def test_exact_query_variants(self) -> None:
        cases = [
            ("sbertrek", "epic", ("RSCON-6607",), 'unit IN linkedUnitsOf("unit = \'RSCON-6607\'", "Состоит из")'),
            ("jira", "tasks", ("RSCON-2906", "RSCON-2905"), 'key IN ("RSCON-2905", "RSCON-2906")'),
            ("jira", "epic", ("RSCON-2911",), 'parent = "RSCON-2911"'),
        ]
        for provider, kind, ids, expected_query in cases:
            with self.subTest(provider=provider, kind=kind), tempfile.TemporaryDirectory() as temp:
                state = Path(temp); begin = self.begin(state, provider=provider, kind=kind, ids=ids)
                self.assertEqual(self.job(state, begin["run_id"], f"collection-{provider}")["query"]["text"], expected_query)

    def test_sbertrek_job_requests_native_attributes_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            fields = self.job(state, run_id, "collection-sbertrek")["response_contract"]["preferred_fields"]
            self.assertIn("suit", fields)
            self.assertIn("attributes", fields)
            self.assertNotIn("fixversion", fields)
            self.assertNotIn("assigned_to", fields)
            self.assertNotIn("story_points", fields)
            self.assertNotIn("issue_key", fields)
            self.assertNotIn("issue_type", fields)
            self.assertNotIn("releases", fields)

    def test_jira_epic_fallback_updates_job_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, provider="jira", kind="epic", ids=("RSCON-2911",))["run_id"]
            query = self.snapshot(state, run_id, "jira")["query"]["exact"]
            evidence = "mcp:jira:query:parent-error"
            self.run_tool(state, "mcp-log", "--run-id", run_id, "--provider", "jira", "--operation", "query", "--outcome", "error", "--evidence", evidence, "--summary", "parent unsupported", "--query", query, "--page-number", "1", "--returned-count", "0")
            self.run_tool(state, "jira-epic-fallback", "--run-id", run_id, "--evidence", evidence)
            job = self.job(state, run_id, "collection-jira"); fallback = '"Epic Link" = "RSCON-2911"'
            self.assertEqual(job["query"]["text"], fallback)
            self.assertEqual(job["query"]["sha256"], hashlib.sha256(fallback.encode()).hexdigest())

    def test_arbitrary_query_is_rejected_and_exploratory_commands_are_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, provider="jira")["run_id"]
            payload = self.run_tool(state, "mcp-log", "--run-id", run_id, "--provider", "jira", "--operation", "query", "--outcome", "success", "--evidence", "mcp:jira:query:wrong", "--summary", "wrong", "--query", "summary contains cohorts", "--page-number", "1", "--returned-count", "1", expected=2)
            self.assertIn("точный JQL", payload["error"])
            help_text = subprocess.run((sys.executable, str(SCRIPT), "mcp-log", "--help"), text=True, capture_output=True, check=True).stdout
            self.assertNotIn("capability-discovery", help_text)
            self.assertNotIn("issue-detail", help_text)

    def test_only_one_bulk_call_is_allowed_for_each_query_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, provider="jira")["run_id"]
            query = self.snapshot(state, run_id, "jira")["query"]["exact"]
            common = [
                "--run-id", run_id, "--provider", "jira", "--operation", "query",
                "--outcome", "success", "--summary", "targeted query", "--query", query,
                "--page-number", "1", "--returned-count", "1",
            ]
            self.run_tool(state, "mcp-log", *common, "--evidence", "mcp:jira:query:first")
            payload = self.run_tool(
                state, "mcp-log", *common, "--evidence", "mcp:jira:query:second", expected=2,
            )
            self.assertIn("ровно один MCP-вызов", payload["error"])

    def test_query_page_rejects_returned_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, provider="jira")["run_id"]
            query = self.snapshot(state, run_id, "jira")["query"]["exact"]
            evidence = "mcp:jira:query:count-mismatch"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "jira",
                "--operation", "query", "--outcome", "success", "--evidence", evidence,
                "--summary", "targeted query", "--query", query, "--page-number", "1",
                "--returned-count", "2",
            )
            payload = self.run_tool(
                state, "query-page", "--run-id", run_id, "--provider", "jira",
                "--query", query, "--page-number", "1", "--last-page", "--evidence", evidence,
                "--key", "RSCON-6845", expected=2,
            )
            self.assertIn("числом возвращённых ключей", payload["error"])

    def test_collector_complete_requires_every_returned_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.ingest_sber_response(state, run_id, [("RSCON-6845", {})])
            snapshot_path = self.root(state, run_id) / "providers" / "sbertrek.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["issues"] = []
            self.write(snapshot_path, snapshot)
            payload = self.run_tool(state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2)
            self.assertIn("набор карточек", payload["error"])

    def test_compact_provider_file_omits_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-6845", {})])
            issue = self.snapshot(state, run_id, "sbertrek")["issues"][0]
            self.assertNotIn("description", issue)
            self.assertEqual(issue["created_at"], "2026-08-01T08:00:00+00:00")

    def test_counterpart_query_is_derived_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-6845", {"jira_key": "RSCON-2902"})])
            self.assertEqual((self.active_job(state, run_id) or {}).get("provider"), "jira")
            self.assertEqual(self.job(state, run_id, "collection-jira")["query"]["text"], 'key IN ("RSCON-2902")')

    def test_absent_jira_counterpart_excludes_only_linked_sbertrek_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(
                state, ids=("RSCON-1", "RSCON-2", "RSCON-3"),
            )["run_id"]
            self.collect(state, run_id, "sbertrek", [
                ("RSCON-1", {"jira_key": "RSCON-101", "estimate": "5"}),
                ("RSCON-2", {"jira_key": "RSCON-102", "estimate": "8"}),
                ("RSCON-3", {"estimate": "3"}),
            ])
            retry = self.record_absent_counterparts(state, run_id, ["RSCON-102"])
            self.assertEqual(retry["remaining_key_count"], 1)
            self.assertEqual(retry["next_query"]["query"], 'key IN ("RSCON-101")')
            self.collect(state, run_id, "jira", [("RSCON-101", {"estimate": "5"})])
            history_keys = {
                key
                for path in (self.root(state, run_id) / "jobs").glob("history-*.json")
                for key in json.loads(path.read_text(encoding="utf-8"))["keys"]
            }
            self.assertNotIn("RSCON-2", history_keys)
            self.assertIn("RSCON-3", history_keys)
            self.complete_all_histories(state, run_id)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"], {
                "sbertrek": 3, "jira": 1, "matched": 1,
                "excluded": 1, "merged": 2, "discrepancies": 2,
            })
            self.assertEqual(payload["summary"]["story_points_total"], 8.0)
            self.assertEqual(payload["summary"]["excluded_sbertrek_issues"], ["RSCON-2"])
            self.assertEqual(payload["summary"]["absent_jira_counterparts"], ["RSCON-102"])
            result = json.loads((self.root(state, run_id) / "reconciled.json").read_text(encoding="utf-8"))
            self.assertEqual({item["sbertrek_key"] for item in result["issues"]}, {"RSCON-1", "RSCON-3"})
            self.assertEqual(result["excluded_issues"][0]["reason"], "jira-counterpart-absent")
            report = (self.root(state, run_id) / "report.md").read_text(encoding="utf-8")
            self.assertIn("Исключено SberTrek-задач: 1", report)
            self.assertIn("`RSCON-2` исключена", report)

    def test_all_jira_counterparts_absent_still_keeps_unlinked_sbertrek_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(
                state, ids=("RSCON-1", "RSCON-2"),
            )["run_id"]
            self.collect(state, run_id, "sbertrek", [
                ("RSCON-1", {"jira_key": "RSCON-101", "estimate": "5"}),
                ("RSCON-2", {"estimate": "3"}),
            ])
            result = self.record_absent_counterparts(state, run_id, ["RSCON-101"])
            self.assertEqual(result["status"], "jira-counterpart-all-absent")
            self.run_tool(state, "collector-complete", "--run-id", run_id, "--provider", "jira")
            self.complete_all_histories(state, run_id)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["excluded"], 1)
            self.assertEqual(payload["counts"]["merged"], 1)
            self.assertEqual(payload["summary"]["story_points_total"], 3.0)

    def test_absent_counterpart_must_be_named_by_exact_jira_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.collect(state, run_id, "sbertrek", [
                ("RSCON-6845", {"jira_key": "RSCON-2902"}),
            ])
            query = self.snapshot(state, run_id, "jira")["query"]["exact"]
            evidence = "mcp:jira:query:wrong-absence"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "jira",
                "--operation", "query", "--outcome", "error", "--evidence", evidence,
                "--summary", "Temporary Jira failure", "--query", query,
                "--page-number", "1", "--returned-count", "0",
            )
            payload = self.run_tool(
                state, "jira-record-absent-counterparts", "--run-id", run_id,
                "--evidence", evidence, "--key", "RSCON-2902", expected=2,
            )
            self.assertIn("точно совпадать с ошибкой Jira", payload["error"])

    def test_manually_narrowed_counterpart_job_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, ids=("RSCON-1", "RSCON-2"))["run_id"]
            self.collect(state, run_id, "sbertrek", [
                ("RSCON-1", {"jira_key": "RSCON-101"}),
                ("RSCON-2", {"jira_key": "RSCON-102"}),
            ])
            snapshot_path = self.root(state, run_id) / "providers" / "jira.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            narrowed = 'key IN ("RSCON-101")'
            snapshot["query"]["exact"] = narrowed
            self.write(snapshot_path, snapshot)
            job_path = self.root(state, run_id) / "jobs" / "collection-jira.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["query"].update({"text": narrowed, "sha256": hashlib.sha256(narrowed.encode()).hexdigest()})
            self.write(job_path, job)
            _, evidence = self.query_page(state, run_id, "jira", ["RSCON-101"])
            self.add_issue(state, run_id, "jira", "RSCON-101", evidence)
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id,
                "--provider", "jira", expected=2,
            )
            self.assertIn("не покрывает все исходно запрошенные ключи", payload["error"])

    def test_absent_jira_key_is_valid_but_not_returned_blocks_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            result = self.collect(state, run_id, "sbertrek", [("RSCON-6845", {})])
            self.assertEqual(result["status"], "collector-job-complete")
            self.assertEqual(self.snapshot(state, run_id, "jira")["query"]["state"], "skipped")
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.ingest_sber_response(state, run_id, [("RSCON-6845", {"include_attributes": False})])
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2,
            )
            self.assertIn("absent допустим", payload["error"])

    def test_sbertrek_not_returned_optional_field_blocks_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            response = state / "missing-fields.json"
            issue = self.sber_response_issue("RSCON-6845", include_attributes=False)
            issue["issue_key"] = "RSCON-2902"
            self.write(response, {"issues": [issue]})
            self.run_tool(
                state, "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek",
                "--page-number", "1", "--max-results", "50", "--last-page", "--evidence", "mcp:sbertrek:query:missing-fields",
                "--response-file", str(response),
            )
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2,
            )
            self.assertIn("RSCON-6845.estimate", payload["error"])

    def test_placeholder_card_and_unexpected_helper_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            response = state / "placeholder.json"
            self.write(response, {"issues": [self.sber_response_issue("RSCON-6845", summary="RSCON-6845")]})
            payload = self.run_tool(state, "ingest-query-response", "--run-id", run_id, "--provider", "sbertrek", "--page-number", "1", "--max-results", "50", "--last-page", "--evidence", "mcp:sbertrek:query:placeholder", "--response-file", str(response), expected=2)
            self.assertIn("placeholder", payload["error"])

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.ingest_sber_response(state, run_id, [("RSCON-6845", {})])
            (self.root(state, run_id) / "record_cards.py").write_text("# helper\n", encoding="utf-8")
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2,
            )
            self.assertIn("record_cards.py", payload["error"])

    def test_history_is_split_into_bounded_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); ids = tuple(f"RSCON-{index}" for index in range(1, 18)); run_id = self.begin(state, ids=ids, jira=False)["run_id"]
            self.collect(state, run_id, "sbertrek", [(key, {}) for key in ids])
            jobs = [json.loads(path.read_text()) for path in (self.root(state, run_id) / "jobs").glob("history-*.json")]
            self.assertEqual(sorted(len(job["keys"]) for job in jobs), [1, 8, 8])

    def test_history_call_outside_active_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); ids = tuple(f"RSCON-{index}" for index in range(1, 10)); run_id = self.begin(state, ids=ids, jira=False)["run_id"]
            self.collect(state, run_id, "sbertrek", [(key, {}) for key in ids])
            active = self.active_job(state, run_id); assert active
            outside = next(key for key in ids if key not in active["keys"])
            payload = self.run_tool(state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek", "--operation", "history", "--outcome", "success", "--evidence", f"mcp:sbertrek:history:{outside}", "--summary", "outside", "--key", outside, expected=2)
            self.assertIn("активного history-job", payload["error"])

    def test_history_event_requires_prior_real_call_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-6845", {})])
            payload = self.run_tool(
                state, "history-event", "--run-id", run_id, "--provider", "sbertrek",
                "--key", "RSCON-6845", "--evidence", "mcp:sbertrek:history:RSCON-6845",
                "--at", "2026-08-10T10:00:00+00:00", "--field", "status",
                "--from-value", "created", "--to-value", "in_progress", expected=2,
            )
            self.assertIn("mcp-log", payload["error"])

    def test_reconcile_preserves_sbertrek_and_computes_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_run(state, handoff=True)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["matched"], 1)
            item = json.loads((root / "reconciled.json").read_text())["issues"][0]
            self.assertEqual(item["summary"], "Sber title")
            self.assertEqual(item["estimate"], {"value": 5.0, "unit": "story-points"})
            self.assertEqual(item["assignee"]["team_id"], "QA1")
            self.assertEqual(item["assigned_at"], "2026-08-10T10:00:00+00:00")
            self.assertEqual(item["work_started_at"], "2026-08-10T10:00:00+00:00")
            self.assertEqual(item["development"]["basis"], "developer-handoff")
            self.assertEqual({entry["field"] for entry in item["conflicts"]}, {"summary", "assignee", "estimate", "epic"})
            result = json.loads((root / "reconciled.json").read_text(encoding="utf-8"))
            self.assertEqual(result["groupings"]["epics"]["RSCON-6854"], ["RSCON-6845"])
            report = (root / "report.md").read_text(encoding="utf-8")
            self.assertIn("5.0 story-points", report)
            self.assertIn("**RSCON-6854**: RSCON-6845", report)

    def test_developer_handoff_survives_later_non_developer_reassignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            self.collect(state, run_id, "sbertrek", [
                ("RSCON-6845", {"assignee": "s-qa"}),
            ])
            evidence = "mcp:sbertrek:history:RSCON-6845"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek",
                "--operation", "history", "--outcome", "success", "--evidence", evidence,
                "--summary", "bounded history", "--key", "RSCON-6845",
            )
            events = [
                ("2026-08-10T10:00:00+00:00", None, "s-dev"),
                ("2026-08-20T10:00:00+00:00", "s-dev", "s-qa"),
                ("2026-08-21T10:00:00+00:00", "s-qa", "s-qa"),
            ]
            for at, before, after in events:
                args = [
                    "history-event", "--run-id", run_id, "--provider", "sbertrek",
                    "--key", "RSCON-6845", "--evidence", evidence,
                    "--at", at, "--field", "assignee",
                    "--to-id", after, "--to-name", after,
                ]
                if before:
                    args += ["--from-id", before, "--from-name", before]
                self.run_tool(state, *args)
            self.run_tool(
                state, "history-complete", "--run-id", run_id, "--provider", "sbertrek",
                "--key", "RSCON-6845", "--state", "complete", "--evidence", evidence,
            )
            job = self.active_job(state, run_id); assert job
            self.run_tool(state, "history-job-complete", "--run-id", run_id, "--job-id", job["job_id"])
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["summary"]["development_state_counts"], {"completed": 1})

    def test_jira_fills_a_missing_sbertrek_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-6845", {"jira_key": "RSCON-2902"})])
            self.collect(state, run_id, "jira", [("RSCON-2902", {"estimate": "3"})])
            self.complete_all_histories(state, run_id)
            self.run_tool(state, "reconcile", "--run-id", run_id)
            item = json.loads((self.root(state, run_id) / "reconciled.json").read_text())["issues"][0]
            self.assertEqual(item["estimate"], {"value": 3.0, "unit": "story-points"})
            self.assertEqual(item["field_sources"]["estimate"], "jira")

    def test_sbertrek_own_key_without_issue_key_does_not_create_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, ids=("RSCON-1",))["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-1", {})])
            self.complete_all_histories(state, run_id)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["matched"], 0)

    def test_unknown_participants_are_asked_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, _ = self.complete_sber_run(state, participants=False)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=3)
            self.assertIn("s-dev", payload["next_question"])
            self.assertNotIn("j-dev", payload["next_question"])

    def test_multiple_provider_accounts_can_share_one_team_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            run_id = self.begin(
                state, ids=("RSCON-6845", "RSCON-6846"), jira=False,
            )["run_id"]
            self.collect(state, run_id, "sbertrek", [
                ("RSCON-6845", {"assignee": "s-dev", "assignee_name": "Один Участник"}),
                ("RSCON-6846", {"assignee": "s-dev-alias", "assignee_name": "Один Участник"}),
            ])
            self.complete_all_histories(state, run_id)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=3)
            self.assertIn("уже соответствуют BE1", payload["next_question"])
            self.run_tool(
                state, "set-participant", "--run-id", run_id, "--provider", "sbertrek",
                "--account-id", "s-dev-alias", "--team-id", "BE1",
            )
            result = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertTrue(result["workflow_complete"])
            config = json.loads((state / "tracker-config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["participants"]["sbertrek"]["s-dev"]["team_id"], "BE1")
            self.assertEqual(config["participants"]["sbertrek"]["s-dev-alias"]["team_id"], "BE1")

    def test_snapshot_from_another_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state, jira=False); run_id = begin["run_id"]
            path = self.root(state, run_id) / "providers" / "sbertrek.json"
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["run_id"] = "20260101T000000Z-deadbeef"
            self.write(path, snapshot)
            payload = self.run_tool(
                state, "collector-complete", "--run-id", run_id,
                "--provider", "sbertrek", expected=2,
            )
            self.assertIn("другому tracker-run", payload["error"])

    def test_direct_jira_card_edit_blocks_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_run(state)
            path = root / "providers" / "jira.json"
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["issues"][0]["summary"] = "Ручная подмена"
            self.write(path, snapshot)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=2)
            self.assertIn("record-issue", payload["error"])

    def test_direct_history_event_edit_blocks_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_run(state, handoff=True)
            path = root / "providers" / "sbertrek.json"
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["issues"][0]["history"]["events"][0]["at"] = "2026-01-01T00:00:00+00:00"
            self.write(path, snapshot)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=2)
            self.assertIn("изменена вне trackerctl", payload["error"])

    def test_direct_history_event_deletion_blocks_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_run(state, handoff=True)
            path = root / "providers" / "sbertrek.json"
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["issues"][0]["history"]["events"].pop()
            self.write(path, snapshot)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=2)
            self.assertIn("не совпадает с журналом trackerctl", payload["error"])

    def test_secondary_query_unavailable_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-6845", {"jira_key": "RSCON-2902"})])
            query = self.snapshot(state, run_id, "jira")["query"]["exact"]
            evidence = "mcp:jira:query:unavailable"
            self.run_tool(state, "mcp-log", "--run-id", run_id, "--provider", "jira", "--operation", "query", "--outcome", "error", "--evidence", evidence, "--summary", "unavailable", "--query", query, "--page-number", "1", "--returned-count", "0")
            self.run_tool(state, "query-unavailable", "--run-id", run_id, "--provider", "jira", "--reason", "no access", "--evidence", evidence)
            self.run_tool(state, "collector-complete", "--run-id", run_id, "--provider", "jira")
            self.complete_all_histories(state, run_id)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("jira-targeted-query-unavailable", payload["limitations"])

    def test_reconcile_is_blocked_until_all_jobs_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            self.collect(state, run_id, "sbertrek", [("RSCON-6845", {})])
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=2)
            self.assertIn("history-sbertrek-01", payload["error"])

    def test_success_creates_all_v2_files_and_read_only_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_run(state)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            for name in ("tracker-session-log.md", "scope.json", "run-status.json", "reconciled.json", "report.md", "completion-status.json"):
                self.assertTrue((root / name).is_file(), name)
            self.assertTrue((root / "jobs").is_dir())
            self.assertTrue((root / "providers" / "sbertrek.json").is_file())
            self.assertFalse(payload["planning_application_allowed"])
            self.assertEqual(payload["protocol"], "targeted-tracker-v2")
            self.assertEqual(payload["summary"]["story_points_total"], 5.0)
            self.assertEqual(
                sum(payload["summary"]["discrepancy_kind_counts"].values()),
                payload["counts"]["discrepancies"],
            )
            self.assertIn("Суммарная оценка: 5.0 story-points", (root / "report.md").read_text(encoding="utf-8"))
            self.assertTrue(self.run_tool(state, "result-status", "--run-id", run_id)["final_response_allowed"])

    def test_update_planning_intent_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, _ = self.complete_sber_run(state, intent="update-planning")
            self.assertTrue(self.run_tool(state, "reconcile", "--run-id", run_id)["planning_application_allowed"])

    def test_old_protocol_cannot_authorize_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]; root = self.root(state, run_id)
            self.write(root / "completion-status.json", {"protocol": "targeted-tracker-v1", "status": "tracker-read-reconciled"})
            self.run_tool(state, "result-status", "--run-id", run_id, expected=2)

    def test_runtime_writes_stay_under_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            before = {item.relative_to(ROOT) for item in ROOT.rglob("*") if item.is_file()}
            self.begin(state)
            after = {item.relative_to(ROOT) for item in ROOT.rglob("*") if item.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
