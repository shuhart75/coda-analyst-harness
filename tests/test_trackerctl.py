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
    def run_tool(self, state: Path, *args: str, expected: int = 0) -> dict:
        result = subprocess.run(
            (sys.executable, str(SCRIPT), *args), text=True, capture_output=True,
            env={**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(state)}, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def write(self, path: Path, payload: dict) -> None:
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
        participants: bool = True,
    ) -> dict:
        self.config(state, jira=jira, participants=participants)
        args = [
            "begin", "--scope-kind", kind, "--scope-provider", provider,
            "--label", "Когорты", "--scope-source", "Запрос аналитика",
        ]
        for value in ids:
            args += ["--scope-id", value]
        return self.run_tool(state, *args)

    def snapshot(self, state: Path, run_id: str, provider: str) -> dict:
        return json.loads((state / "tracker-runs" / run_id / "input" / f"{provider}.json").read_text(encoding="utf-8"))

    def query_page(
        self, state: Path, run_id: str, provider: str, keys: list[str],
        *, page: int = 1, last: bool = True, cursor: str | None = None,
        next_cursor: str | None = None, outcome: str = "success",
    ) -> tuple[dict, str]:
        query = self.snapshot(state, run_id, provider)["query"]["exact"]
        evidence = f"mcp:{provider}:query:page-{page}"
        self.run_tool(
            state, "mcp-log", "--run-id", run_id, "--provider", provider,
            "--operation", "query", "--outcome", outcome,
            "--evidence", evidence, "--summary", "targeted query",
            "--query", query, "--page-number", str(page),
            "--returned-count", str(len(keys)),
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

    def issue_args(
        self, run_id: str, provider: str, key: str, evidence: str,
        *, jira_key: str | None = None, assignee: str | None = None,
        estimate: str | None = None, epic: str | None = None,
        summary: str | None = None, status: str = "active",
    ) -> list[str]:
        args = [
            "record-issue", "--run-id", run_id, "--provider", provider,
            "--key", key, "--evidence", evidence,
            "--summary", summary or f"Issue {key}", "--description", "Cohorts feature",
            "--issue-type", "story", "--status", status,
            "--assignee-state", "value" if assignee else "absent",
            "--estimate-state", "value" if estimate else "absent",
            "--epic-state", "value" if epic else "absent", "--releases-state", "absent",
        ]
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

    def history(self, state: Path, run_id: str, provider: str, key: str, *, event: bool = False) -> None:
        if event:
            self.run_tool(
                state, "history-event", "--run-id", run_id, "--provider", provider,
                "--key", key, "--at", "2026-08-27T10:00:00+00:00",
                "--field", "assignee", "--from-id", f"{provider[0]}-dev",
                "--to-id", f"{provider[0]}-qa",
            )
        evidence = f"mcp:{provider}:history:{key}"
        self.run_tool(
            state, "mcp-log", "--run-id", run_id, "--provider", provider,
            "--operation", "history", "--outcome", "success",
            "--evidence", evidence, "--summary", "issue history", "--key", key,
        )
        self.run_tool(
            state, "history-complete", "--run-id", run_id, "--provider", provider,
            "--key", key, "--state", "complete", "--evidence", evidence,
        )

    def complete_sber_task_run(self, state: Path, *, participants: bool = True) -> tuple[str, Path]:
        begin = self.begin(state, participants=participants)
        run_id = begin["run_id"]
        _, sber_evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
        self.add_issue(
            state, run_id, "sbertrek", "RSCON-6845", sber_evidence,
            jira_key="RSCON-2902", assignee="s-dev", estimate="5",
            epic="RSCON-6854", summary="Sber title",
        )
        counterpart = self.run_tool(state, "collection-advance", "--run-id", run_id)
        self.assertEqual(counterpart["next_query"]["query"], 'key IN ("RSCON-2902")')
        _, jira_evidence = self.query_page(state, run_id, "jira", ["RSCON-2902"])
        self.add_issue(
            state, run_id, "jira", "RSCON-2902", jira_evidence,
            assignee="j-dev", estimate="8", epic="RSCON-2911", summary="Jira title",
        )
        self.run_tool(state, "collection-advance", "--run-id", run_id)
        self.history(state, run_id, "sbertrek", "RSCON-6845")
        self.history(state, run_id, "jira", "RSCON-2902")
        self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
        self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
        return run_id, state / "tracker-runs" / run_id

    def test_config_stop_gate_asks_exactly_one_question(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.run_tool(state, "init-config")
            payload = self.run_tool(state, "config-status", expected=3)
            self.assertEqual(payload["next_question"], payload["response_contract"]["text"])
            self.assertEqual(payload["gaps"][0], "projects.sbertrek")

    def test_sbertrek_task_scope_generates_exact_unit_tql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self.begin(Path(temp), ids=("RSCON-6849", "RSCON-6848"))
            self.assertEqual(payload["protocol"], "targeted-tracker-v1")
            self.assertEqual(payload["next_query"]["provider"], "sbertrek")
            self.assertEqual(payload["next_query"]["query"], 'unit = "RSCON-6848" or unit = "RSCON-6849"')
            self.assertTrue(payload["next_query"]["exact_query_required"])

    def test_sbertrek_epic_scope_generates_required_link_tql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = self.begin(Path(temp), kind="epic", ids=("RSCON-6607",))
            self.assertEqual(payload["next_query"]["query"], 'unit IN linkedUnitsOf("unit = \'RSCON-6607\'", "Состоит из")')

    def test_jira_task_scope_generates_exact_key_jql_then_sbertrek_counterpart_tql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            begin = self.begin(state, provider="jira", ids=("RSCON-2906", "RSCON-2905"))
            run_id = begin["run_id"]
            self.assertEqual(begin["next_query"]["query"], 'key IN ("RSCON-2905", "RSCON-2906")')
            _, evidence = self.query_page(state, run_id, "jira", ["RSCON-2905", "RSCON-2906"])
            self.add_issue(state, run_id, "jira", "RSCON-2905", evidence)
            self.add_issue(state, run_id, "jira", "RSCON-2906", evidence)
            payload = self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.assertEqual(payload["next_query"]["query"], 'issue_key = "RSCON-2905" or issue_key = "RSCON-2906"')

    def test_jira_epic_scope_has_controlled_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            begin = self.begin(state, provider="jira", kind="epic", ids=("RSCON-2911",))
            run_id = begin["run_id"]
            query = begin["next_query"]["query"]
            self.assertEqual(query, 'parent = "RSCON-2911"')
            evidence = "mcp:jira:query:parent-error"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "jira",
                "--operation", "query", "--outcome", "error", "--evidence", evidence,
                "--summary", "parent unsupported", "--query", query,
                "--page-number", "1", "--returned-count", "0",
            )
            payload = self.run_tool(state, "jira-epic-fallback", "--run-id", run_id, "--evidence", evidence)
            self.assertEqual(payload["next_query"]["query"], '"Epic Link" = "RSCON-2911"')
            self.assertEqual(payload["next_query"]["method"], "epic-link")

    def test_mcp_log_rejects_arbitrary_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state); run_id = begin["run_id"]
            payload = self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek",
                "--operation", "query", "--outcome", "success",
                "--evidence", "mcp:sbertrek:query:wrong", "--summary", "wrong",
                "--query", "unit contains cohorts", "--page-number", "1",
                "--returned-count", "1", expected=2,
            )
            self.assertIn("точный TQL", payload["error"])

    def test_query_page_requires_prior_successful_mcp_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state); run_id = begin["run_id"]
            payload = self.run_tool(
                state, "query-page", "--run-id", run_id, "--provider", "sbertrek",
                "--query", begin["next_query"]["query"], "--page-number", "1",
                "--last-page", "--evidence", "mcp:sbertrek:query:missing",
                "--key", "RSCON-6845", expected=2,
            )
            self.assertIn("mcp-log", payload["error"])

    def test_paginated_query_requires_exact_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.query_page(state, run_id, "sbertrek", ["RSCON-6845"], last=False, next_cursor="next")
            query = self.snapshot(state, run_id, "sbertrek")["query"]["exact"]
            evidence = "mcp:sbertrek:query:page-2"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek",
                "--operation", "query", "--outcome", "success", "--evidence", evidence,
                "--summary", "page 2", "--query", query, "--page-number", "2", "--returned-count", "0",
            )
            self.run_tool(
                state, "query-page", "--run-id", run_id, "--provider", "sbertrek",
                "--query", query, "--page-number", "2", "--cursor", "wrong",
                "--last-page", "--evidence", evidence, expected=2,
            )

    def test_collection_requires_card_for_every_returned_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            payload = self.run_tool(state, "collection-advance", "--run-id", run_id, expected=2)
            self.assertIn("RSCON-6845", payload["error"])

    def test_epic_scope_rejects_multiple_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.config(state)
            payload = self.run_tool(
                state, "begin", "--scope-kind", "epic", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-6607", "--scope-id", "RSCON-6608",
                "--label", "Когорты", "--scope-source", "Запрос аналитика", expected=2,
            )
            self.assertIn("ровно один", payload["error"])

    def test_jira_cannot_be_source_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.config(state, jira=False)
            payload = self.run_tool(
                state, "begin", "--scope-kind", "tasks", "--scope-provider", "jira",
                "--scope-id", "RSCON-2902", "--label", "Когорты",
                "--scope-source", "Запрос аналитика", expected=2,
            )
            self.assertIn("Jira отключена", payload["error"])

    def test_no_sbertrek_jira_key_skips_counterpart_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845", evidence)
            payload = self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.assertEqual(payload["status"], "tracker-read-history")
            jira = self.snapshot(state, run_id, "jira")
            self.assertEqual(jira["query"]["state"], "skipped")

    def test_sbertrek_counterpart_must_point_into_jira_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            begin = self.begin(state, provider="jira", ids=("RSCON-2902",))
            run_id = begin["run_id"]
            _, jira_evidence = self.query_page(state, run_id, "jira", ["RSCON-2902"])
            self.add_issue(state, run_id, "jira", "RSCON-2902", jira_evidence)
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            _, sber_evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(
                state, run_id, "sbertrek", "RSCON-6845", sber_evidence,
                jira_key="RSCON-9999",
            )
            payload = self.run_tool(state, "collection-advance", "--run-id", run_id, expected=2)
            self.assertIn("вне исходной Jira-области", payload["error"])

    def test_scope_key_not_returned_becomes_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            self.query_page(state, run_id, "sbertrek", [])
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("scope-key-not-returned:sbertrek:RSCON-6845", payload["limitations"])

    def test_history_unavailable_requires_logged_error_and_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845", evidence)
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            call = "mcp:sbertrek:history:RSCON-6845:error"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek",
                "--operation", "history", "--outcome", "error", "--evidence", call,
                "--summary", "history unavailable", "--key", "RSCON-6845",
            )
            self.run_tool(
                state, "history-complete", "--run-id", run_id,
                "--provider", "sbertrek", "--key", "RSCON-6845",
                "--state", "unavailable", "--reason", "no permission",
                "--evidence", call,
            )
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("sbertrek-history-unavailable:RSCON-6845", payload["limitations"])

    def test_finalize_blocks_pending_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845", evidence)
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            payload = self.run_tool(
                state, "snapshot-finalize", "--run-id", run_id,
                "--provider", "sbertrek", expected=2,
            )
            self.assertIn("history.pending", payload["error"])

    def test_finalized_snapshot_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845", evidence)
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-6845")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            payload = self.run_tool(
                state, "history-event", "--run-id", run_id, "--provider", "sbertrek",
                "--key", "RSCON-6845", "--at", "2026-08-27T10:00:00+00:00",
                "--field", "status", "--from-value", "active", "--to-value", "done",
                expected=2,
            )
            self.assertIn("неизменяем", payload["error"])

    def test_run_status_repeats_exact_next_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state)
            payload = self.run_tool(state, "run-status", "--run-id", begin["run_id"], expected=2)
            self.assertEqual(payload["next_query"], begin["next_query"])

    def test_old_snapshot_protocol_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state); run_id = begin["run_id"]
            path = state / "tracker-runs" / run_id / "input" / "sbertrek.json"
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["protocol"] = "active-inventory-v2"
            self.write(path, snapshot)
            self.run_tool(state, "run-status", "--run-id", run_id, expected=2)

    def test_sbertrek_issue_key_is_only_pairing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            begin = self.begin(state, ids=("RSCON-1",)); run_id = begin["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-1"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1", evidence)
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-1")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["matched"], 0)

    def test_reconcile_preserves_sbertrek_and_reports_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_task_run(state)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["matched"], 1)
            result = json.loads((root / "reconciled.json").read_text(encoding="utf-8"))
            item = result["issues"][0]
            self.assertEqual(item["summary"], "Sber title")
            self.assertEqual(item["estimate"], {"value": 5.0, "unit": "story-points"})
            self.assertEqual(item["epic"]["key"], "RSCON-6854")
            self.assertEqual(item["assignee"]["id"], "s-dev")
            self.assertEqual({entry["field"] for entry in item["conflicts"]}, {"summary", "assignee", "estimate", "epic"})

    def test_jira_fills_missing_sbertrek_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state, ids=("RSCON-10",)); run_id = begin["run_id"]
            _, se = self.query_page(state, run_id, "sbertrek", ["RSCON-10"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-10", se, jira_key="RSCON-20")
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            _, je = self.query_page(state, run_id, "jira", ["RSCON-20"])
            self.add_issue(state, run_id, "jira", "RSCON-20", je, estimate="3")
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-10"); self.history(state, run_id, "jira", "RSCON-20")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            self.run_tool(state, "reconcile", "--run-id", run_id)
            result = json.loads((state / "tracker-runs" / run_id / "reconciled.json").read_text(encoding="utf-8"))
            self.assertEqual(result["issues"][0]["estimate"]["value"], 3.0)
            self.assertEqual(result["issues"][0]["field_sources"]["estimate"], "jira")

    def test_developer_handoff_marks_development_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state, jira=False); run_id = begin["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845", evidence, assignee="s-qa")
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-6845", event=True)
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "reconcile", "--run-id", run_id)
            result = json.loads((state / "tracker-runs" / run_id / "reconciled.json").read_text(encoding="utf-8"))
            self.assertEqual(result["issues"][0]["development"]["basis"], "developer-handoff")

    def test_unknown_participants_are_asked_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, _ = self.complete_sber_task_run(state, participants=False)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=3)
            self.assertIn("s-dev", payload["next_question"])
            self.assertNotIn("j-dev", payload["next_question"])
            self.run_tool(state, "set-participant", "--run-id", run_id, "--provider", "sbertrek", "--account-id", "s-dev", "--team-id", "B1")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=3)
            self.assertIn("j-dev", payload["next_question"])

    def test_secondary_query_may_be_unavailable_with_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state); run_id = begin["run_id"]
            _, evidence = self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845", evidence, jira_key="RSCON-2902")
            counterpart = self.run_tool(state, "collection-advance", "--run-id", run_id)
            query = counterpart["next_query"]["query"]
            call = "mcp:jira:query:unavailable"
            self.run_tool(
                state, "mcp-log", "--run-id", run_id, "--provider", "jira",
                "--operation", "query", "--outcome", "error", "--evidence", call,
                "--summary", "unavailable", "--query", query, "--page-number", "1", "--returned-count", "0",
            )
            self.run_tool(state, "query-unavailable", "--run-id", run_id, "--provider", "jira", "--reason", "no access", "--evidence", call)
            self.run_tool(state, "collection-advance", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-6845")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("jira-targeted-query-unavailable", payload["limitations"])

    def test_success_creates_log_scope_and_all_result_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_sber_task_run(state)
            self.run_tool(state, "reconcile", "--run-id", run_id)
            for name in ("tracker-session-log.md", "scope.json", "run-status.json", "reconciled.json", "report.md", "completion-status.json"):
                self.assertTrue((root / name).is_file(), name)
            log = (root / "tracker-session-log.md").read_text(encoding="utf-8")
            self.assertIn("targeted-tracker-v1", log)
            self.assertIn('unit = "RSCON-6845"', log)
            self.assertIn("command=reconcile; exit=0", log)
            payload = self.run_tool(state, "result-status", "--run-id", run_id)
            self.assertTrue(payload["final_response_allowed"])

    def test_old_completion_cannot_authorize_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            root = state / "tracker-runs" / run_id
            self.write(root / "completion-status.json", {"protocol": "active-inventory-v2", "status": "tracker-read-reconciled"})
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
