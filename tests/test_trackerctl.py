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
            "jira_enabled": jira, "projects": {"sbertrek": ["RSCON"], "jira": ["RSCON"] if jira else []},
            "development_issue_types": ["story", "task"], "participants": mapping,
            "status_rules": {
                "sbertrek": {"completed": ["done"], "excluded": ["cancelled"]},
                "jira": {"completed": ["Done"], "excluded": ["Cancelled"]},
            },
        })

    def begin(self, state: Path, *, jira: bool = True, participants: bool = True) -> str:
        self.config(state, jira=jira, participants=participants)
        payload = self.run_tool(
            state, "begin", "--feature", "cohorts",
            "--known-key", "RSCON-6845=features/cohorts/actual-progress.md",
        )
        return payload["run_id"]

    def page(
        self, state: Path, run_id: str, provider: str, keys: list[str],
        *, links: list[str] | None = None, page: int = 1, last: bool = True,
        cursor: str | None = None, next_cursor: str | None = None,
    ) -> dict:
        evidence = f"mcp:{provider}:active-search:page-{page}"
        self.mcp_log(
            state, run_id, provider, "inventory", evidence,
            page=page, returned=len(keys), summary="active inventory page",
        )
        args = [
            "inventory-page", "--run-id", run_id, "--provider", provider,
            "--query", "project=RSCON AND unfinished=true", "--scope-project", "RSCON",
            "--unfinished-confirmed", "--page-number", str(page),
            "--evidence", evidence,
        ]
        if cursor:
            args += ["--cursor", cursor]
        if last:
            args += ["--last-page"]
        else:
            args += ["--next-cursor", next_cursor or f"cursor-{page + 1}"]
        for issue_key in keys:
            args += ["--key", issue_key]
        for link in links or []:
            args += ["--jira-link", link]
        return self.run_tool(state, *args)

    def mcp_log(
        self, state: Path, run_id: str, provider: str, operation: str, evidence: str,
        *, outcome: str = "success", page: int | None = None,
        issue_key: str | None = None, returned: int | None = None,
        summary: str = "test MCP call",
    ) -> dict:
        args = [
            "mcp-log", "--run-id", run_id, "--provider", provider,
            "--operation", operation, "--outcome", outcome,
            "--evidence", evidence, "--summary", summary,
        ]
        if page is not None:
            args += ["--page-number", str(page)]
        if issue_key:
            args += ["--key", issue_key]
        if returned is not None:
            args += ["--returned-count", str(returned)]
        return self.run_tool(state, *args)

    def issue_args(
        self, run_id: str, provider: str, issue_key: str, evidence: str,
        *, relevance: str = "relevant", selected_by: str = "description-match",
        assignee: str | None = None, estimate: str | None = None,
        epic: str | None = None, summary: str | None = None, status: str = "active",
    ) -> list[str]:
        args = [
            "record-issue", "--run-id", run_id, "--provider", provider,
            "--key", issue_key, "--evidence", evidence,
            "--summary", summary or f"Issue {issue_key}", "--description", "Cohorts feature",
            "--issue-type", "story", "--status", status,
            "--assignee-state", "value" if assignee else "absent",
            "--estimate-state", "value" if estimate else "absent",
            "--epic-state", "value" if epic else "absent", "--releases-state", "absent",
            "--relevance", relevance, "--relevance-basis", "test evidence",
            "--selected-by", selected_by,
        ]
        if assignee:
            args += ["--assignee-id", assignee, "--assignee-name", assignee]
        if estimate:
            args += ["--estimate", estimate, "--estimate-unit", "person-days"]
        if epic:
            args += ["--epic-key", epic, "--epic-name", f"Epic {epic}"]
        return args

    def add_issue(self, state: Path, run_id: str, provider: str, issue_key: str, **kwargs) -> dict:
        return self.run_tool(
            state, *self.issue_args(run_id, provider, issue_key, f"mcp:{provider}:active-search:page-1", **kwargs)
        )

    def history(self, state: Path, run_id: str, provider: str, issue_key: str, *, event: bool = False) -> None:
        if event:
            self.run_tool(
                state, "history-event", "--run-id", run_id, "--provider", provider,
                "--key", issue_key, "--at", "2026-08-27T10:00:00+00:00",
                "--field", "assignee", "--from-id", f"{provider[0]}-dev",
                "--to-id", f"{provider[0]}-qa",
            )
        call = f"mcp:{provider}:history:{issue_key}"
        self.mcp_log(state, run_id, provider, "history", call, issue_key=issue_key, summary="issue history")
        self.run_tool(
            state, "history-complete", "--run-id", run_id, "--provider", provider,
            "--key", issue_key, "--state", "complete",
            "--evidence", call,
        )

    def complete_basic_run(self, state: Path, *, participants: bool = True) -> tuple[str, Path]:
        run_id = self.begin(state, participants=participants)
        self.page(state, run_id, "sbertrek", ["RSCON-6845"], links=["RSCON-6845=RSCON-2902"])
        self.page(state, run_id, "jira", ["RSCON-2902"])
        self.add_issue(state, run_id, "sbertrek", "RSCON-6845", assignee="s-dev", estimate="5", epic="RSCON-6854", summary="Sber title")
        self.add_issue(state, run_id, "jira", "RSCON-2902", selected_by="linked-counterpart", assignee="j-dev", estimate="8", epic="RSCON-2911", summary="Jira title")
        self.run_tool(state, "selection-complete", "--run-id", run_id)
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
            self.assertTrue(payload["must_stop"])
            self.assertEqual(payload["next_question"], payload["response_contract"]["text"])
            self.assertEqual(payload["gaps"][0], "projects.sbertrek")

    def test_begin_creates_durable_run_status_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            run_id = self.begin(state)
            status = state / "tracker-runs" / run_id / "run-status.json"
            self.assertTrue(status.is_file())
            self.assertEqual(json.loads(status.read_text())["status"], "tracker-read-collecting")
            self.assertTrue((status.parent / "input" / "sbertrek.json").is_file())
            self.assertTrue((status.parent / "input" / "jira.json").is_file())
            log = status.parent / "tracker-session-log.md"
            self.assertTrue(log.is_file())
            self.assertIn("active-inventory-v2", log.read_text(encoding="utf-8"))

    def test_begin_records_feature_and_known_key_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            run_id = self.begin(state)
            snapshot = json.loads((state / "tracker-runs" / run_id / "input" / "sbertrek.json").read_text())
            self.assertEqual(snapshot["protocol"], "active-inventory-v2")
            self.assertEqual(snapshot["scope"]["feature"], "cohorts")
            self.assertEqual(snapshot["scope"]["known_key_evidence"][0]["key"], "RSCON-6845")

    def test_inventory_requires_prior_mcp_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            payload = self.run_tool(
                state, "inventory-page", "--run-id", run_id, "--provider", "sbertrek",
                "--query", "project=RSCON AND unfinished=true", "--scope-project", "RSCON",
                "--unfinished-confirmed", "--page-number", "1", "--last-page",
                "--evidence", "mcp:sbertrek:active-search:unlogged", expected=2,
            )
            self.assertIn("mcp-log", payload["error"])
            retry = self.run_tool(
                state, "inventory-page", "--run-id", run_id, "--provider", "sbertrek",
                "--query", "project=RSCON AND unfinished=true", "--scope-project", "RSCON",
                "--unfinished-confirmed", "--page-number", "1", "--last-page",
                "--evidence", "mcp:sbertrek:active-search:unlogged", expected=2,
            )
            self.assertIn("mcp-log", retry["error"])
            log = state / "tracker-runs" / run_id / "tracker-session-log.md"
            self.assertIn("command=inventory-page", log.read_text(encoding="utf-8"))

    def test_schema_four_config_is_reused_and_legacy_pairs_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.config(state)
            config_path = state / "tracker-config.json"
            config = json.loads(config_path.read_text())
            config["issue_pairs"] = {"RSCON-1": "RSCON-2"}
            self.write(config_path, config)
            payload = self.run_tool(state, "config-status")
            self.assertEqual(payload["status"], "tracker-config-ready")
            migrated = json.loads(config_path.read_text())
            self.assertNotIn("issue_pairs", migrated)
            self.assertEqual(migrated["participants"]["sbertrek"]["s-dev"]["team_id"], "BE1")

    def test_inventory_requires_exact_scope_and_unfinished_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            common = ["inventory-page", "--run-id", run_id, "--provider", "sbertrek", "--query", "all", "--scope-project", "OTHER", "--page-number", "1", "--last-page", "--evidence", "mcp:sbertrek:search:1"]
            self.run_tool(state, *common, expected=2)
            common[common.index("OTHER")] = "RSCON"
            self.run_tool(state, *common, expected=2)

    def test_inventory_is_paginated_and_accepts_one_evidence_per_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            first = self.page(state, run_id, "sbertrek", ["RSCON-1"], last=False)
            self.assertEqual(first["inventory_state"], "collecting")
            second = self.page(state, run_id, "sbertrek", ["RSCON-2"], page=2, cursor="cursor-2")
            self.assertEqual(second["key_count"], 2)
            snapshot = json.loads((state / "tracker-runs" / run_id / "input" / "sbertrek.json").read_text())
            self.assertEqual(len(snapshot["inventory"]["pages"]), 2)

    def test_inventory_rejects_wrong_continuation_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-1"], last=False)
            args = [
                "inventory-page", "--run-id", run_id, "--provider", "sbertrek",
                "--query", "project=RSCON AND unfinished=true", "--scope-project", "RSCON",
                "--unfinished-confirmed", "--page-number", "2", "--cursor", "wrong",
                "--last-page", "--evidence", "mcp:sbertrek:active-search:page-2",
            ]
            self.run_tool(state, *args, expected=2)

    def test_inventory_does_not_require_detail_for_every_returned_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-1", "RSCON-2", "RSCON-3"])
            self.page(state, run_id, "jira", [])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1")
            payload = self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.assertEqual(payload["selected"]["sbertrek"], 1)

    def test_inventory_page_can_be_detail_evidence_for_returned_full_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-6845"])
            payload = self.add_issue(state, run_id, "sbertrek", "RSCON-6845")
            self.assertEqual(payload["status"], "tracker-issue-recorded")

    def test_exact_detail_call_is_logged_before_issue_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-6845"])
            call = "mcp:sbertrek:issue-detail:RSCON-6845"
            self.mcp_log(
                state, run_id, "sbertrek", "issue-detail", call,
                issue_key="RSCON-6845", summary="exact issue card",
            )
            payload = self.run_tool(
                state, *self.issue_args(run_id, "sbertrek", "RSCON-6845", call)
            )
            self.assertEqual(payload["status"], "tracker-issue-recorded")

    def test_record_issue_rejects_key_outside_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", [])
            self.run_tool(state, *self.issue_args(run_id, "sbertrek", "RSCON-9", "mcp:sbertrek:get:RSCON-9"), expected=2)

    def test_ambiguous_relevance_asks_one_exact_question(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-7"]); self.page(state, run_id, "jira", [])
            self.add_issue(state, run_id, "sbertrek", "RSCON-7", relevance="ambiguous", selected_by="ambiguous")
            payload = self.run_tool(state, "selection-complete", "--run-id", run_id, expected=3)
            self.assertIn("RSCON-7", payload["next_question"])
            self.assertEqual(payload["next_question"], payload["response_contract"]["text"])
            self.run_tool(state, "decide-relevance", "--run-id", run_id, "--provider", "sbertrek", "--key", "RSCON-7", "--relevance", "irrelevant", "--basis", "Ответ аналитика")
            self.run_tool(state, "selection-complete", "--run-id", run_id)

    def test_link_closure_uses_only_sbertrek_jira_object_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-6845"], links=["RSCON-6845=RSCON-2902"])
            self.page(state, run_id, "jira", ["RSCON-2902"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845")
            blocked = self.run_tool(state, "selection-complete", "--run-id", run_id, expected=2)
            self.assertIn("linked-counterpart-not-selected", blocked["error"])
            self.add_issue(state, run_id, "jira", "RSCON-2902", selected_by="linked-counterpart")
            self.run_tool(state, "selection-complete", "--run-id", run_id)

    def test_active_known_key_must_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-6845"])
            self.page(state, run_id, "jira", [])
            payload = self.run_tool(state, "selection-complete", "--run-id", run_id, expected=2)
            self.assertIn("active-known-key-not-selected", payload["error"])

    def test_completed_selection_cannot_be_extended(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-1", "RSCON-2"])
            self.page(state, run_id, "jira", [])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.run_tool(state, *self.issue_args(run_id, "sbertrek", "RSCON-2", "mcp:sbertrek:active-search:page-1"), expected=2)

    def test_equal_own_keys_do_not_create_pair_without_jira_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-1"])
            self.page(state, run_id, "jira", ["RSCON-1"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1")
            self.add_issue(state, run_id, "jira", "RSCON-1", selected_by="description-match")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-1"); self.history(state, run_id, "jira", "RSCON-1")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            result = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(result["counts"]["matched"], 0)

    def test_history_is_permitted_only_for_selected_relevant_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-1", "RSCON-2"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1")
            self.add_issue(state, run_id, "sbertrek", "RSCON-2", relevance="irrelevant", selected_by="description-match")
            self.run_tool(state, "history-complete", "--run-id", run_id, "--provider", "sbertrek", "--key", "RSCON-2", "--state", "complete", "--evidence", "mcp:sbertrek:history:RSCON-2", expected=2)

    def test_finalize_requires_history_only_for_relevant_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)
            self.page(state, run_id, "sbertrek", ["RSCON-1", "RSCON-2"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1")
            self.add_issue(state, run_id, "sbertrek", "RSCON-2", relevance="irrelevant", selected_by="description-match")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek", expected=2)
            self.history(state, run_id, "sbertrek", "RSCON-1")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")

    def test_reconcile_pairs_by_jira_object_and_preserves_sbertrek_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_basic_run(state)
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["matched"], 1)
            result = json.loads((root / "reconciled.json").read_text())
            issue = result["issues"][0]
            self.assertEqual(issue["summary"], "Sber title")
            self.assertEqual(issue["estimate"], {"value": 5.0, "unit": "story-points"})
            self.assertEqual(issue["epic"]["key"], "RSCON-6854")
            self.assertEqual(issue["assignee"]["id"], "s-dev")
            self.assertEqual({item["field"] for item in issue["conflicts"]}, {"summary", "assignee", "estimate", "epic"})

    def test_jira_fills_missing_sbertrek_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-10"], links=["RSCON-10=RSCON-20"])
            self.page(state, run_id, "jira", ["RSCON-20"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-10")
            self.add_issue(state, run_id, "jira", "RSCON-20", selected_by="linked-counterpart", estimate="3")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-10"); self.history(state, run_id, "jira", "RSCON-20")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek"); self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            self.run_tool(state, "reconcile", "--run-id", run_id)
            result = json.loads((state / "tracker-runs" / run_id / "reconciled.json").read_text())
            self.assertEqual(result["issues"][0]["estimate"]["value"], 3.0)
            self.assertEqual(result["issues"][0]["field_sources"]["estimate"], "jira")

    def test_developer_handoff_marks_development_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)
            self.page(state, run_id, "sbertrek", ["RSCON-1"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1", assignee="s-qa")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-1", event=True)
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "reconcile", "--run-id", run_id)
            result = json.loads((state / "tracker-runs" / run_id / "reconciled.json").read_text())
            self.assertEqual(result["issues"][0]["development"]["basis"], "developer-handoff")

    def test_unknown_participant_question_comes_only_from_selected_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False, participants=False)
            self.page(state, run_id, "sbertrek", ["RSCON-1", "RSCON-2"])
            self.add_issue(state, run_id, "sbertrek", "RSCON-1", assignee="selected-user")
            self.add_issue(state, run_id, "sbertrek", "RSCON-2", relevance="irrelevant", selected_by="description-match", assignee="ignored-user")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-1")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id, expected=3)
            self.assertIn("selected-user", payload["next_question"])
            self.assertNotIn("ignored-user", payload["next_question"])

    def test_set_participant_is_guarded_by_pending_question(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False, participants=False)
            self.run_tool(state, "set-participant", "--run-id", run_id, "--provider", "sbertrek", "--account-id", "guess", "--team-id", "B1", expected=2)

    def test_jira_inventory_can_be_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-6845"])
            call = "mcp:jira:capability-discovery:failed"
            self.mcp_log(state, run_id, "jira", "capability-discovery", call, outcome="error", summary="MCP unavailable")
            self.run_tool(state, "inventory-unavailable", "--run-id", run_id, "--provider", "jira", "--reason", "MCP unavailable", "--evidence", call)
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-6845")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("jira-active-inventory-unavailable", payload["limitations"])

    def test_known_key_missing_from_active_inventory_is_visible_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state, jira=False)
            self.page(state, run_id, "sbertrek", [])
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertEqual(payload["counts"]["merged"], 0)
            self.assertIn("known-key-not-in-active-inventory:RSCON-6845", payload["limitations"])

    def test_explicit_link_to_inactive_jira_is_visible_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            self.page(state, run_id, "sbertrek", ["RSCON-6845"], links=["RSCON-6845=RSCON-2902"])
            self.page(state, run_id, "jira", [])
            self.add_issue(state, run_id, "sbertrek", "RSCON-6845")
            self.run_tool(state, "selection-complete", "--run-id", run_id)
            self.history(state, run_id, "sbertrek", "RSCON-6845")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "sbertrek")
            self.run_tool(state, "snapshot-finalize", "--run-id", run_id, "--provider", "jira")
            payload = self.run_tool(state, "reconcile", "--run-id", run_id)
            self.assertIn("linked-jira-not-in-active-inventory:RSCON-6845=RSCON-2902", payload["limitations"])
            result = json.loads((state / "tracker-runs" / run_id / "reconciled.json").read_text())
            self.assertEqual(result["issues"][0]["jira_key"], "RSCON-2902")

    def test_report_contains_independent_epic_and_release_groupings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_basic_run(state)
            self.run_tool(state, "reconcile", "--run-id", run_id)
            result = json.loads((root / "reconciled.json").read_text())
            self.assertEqual(result["groupings"]["epics"]["RSCON-6854"], ["RSCON-6845"])
            self.assertEqual(result["groupings"]["releases"]["unassigned"], ["RSCON-6845"])
            report = (root / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Группировка по эпикам", report)
            self.assertIn("## Группировка по релизам", report)

    def test_success_creates_session_log_and_all_result_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id, root = self.complete_basic_run(state)
            self.run_tool(state, "reconcile", "--run-id", run_id)
            for name in ("tracker-session-log.md", "run-status.json", "reconciled.json", "report.md", "completion-status.json"):
                self.assertTrue((root / name).is_file(), name)
            log = (root / "tracker-session-log.md").read_text(encoding="utf-8")
            self.assertIn("mcp:sbertrek:active-search:page-1", log)
            self.assertIn("command=reconcile; exit=0", log)
            result = self.run_tool(state, "result-status", "--run-id", run_id)
            self.assertTrue(result["final_response_allowed"])
            self.assertEqual(result["paths"]["session_log"], str(root / "tracker-session-log.md"))
            self.assertEqual(result["paths"]["completion_status"], str(root / "completion-status.json"))

    def test_old_protocol_completion_cannot_authorize_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)
            root = state / "tracker-runs" / run_id
            self.write(root / "completion-status.json", {"schema_version": 9, "status": "tracker-read-reconciled"})
            self.run_tool(state, "result-status", "--run-id", run_id, expected=2)

    def test_runtime_writes_stay_under_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); before = {item.relative_to(ROOT) for item in ROOT.rglob("*") if item.is_file()}
            self.begin(state)
            after = {item.relative_to(ROOT) for item in ROOT.rglob("*") if item.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
