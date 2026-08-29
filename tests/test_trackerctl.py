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

    def issue_args(
        self, run_id: str, provider: str, key: str, evidence: str, *,
        jira_key: str | None = None, assignee: str | None = None,
        estimate: str | None = None, epic: str | None = None,
        summary: str | None = None, status: str = "active",
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
            self.assertIn("read-mcp-documentation", job["forbidden_operations"])
            self.assertIn("read-returned-issues-one-by-one", job["forbidden_operations"])

    def test_collector_brief_contains_only_job_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); begin = self.begin(state)
            payload = self.run_tool(state, "collector-brief", "--run-id", begin["run_id"])
            self.assertIn("не читай документацию MCP", payload["prompt"])
            self.assertNotIn('unit = "', payload["prompt"])

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
            state = Path(temp); run_id = self.begin(state)["run_id"]
            payload = self.run_tool(state, "mcp-log", "--run-id", run_id, "--provider", "sbertrek", "--operation", "query", "--outcome", "success", "--evidence", "mcp:sbertrek:query:wrong", "--summary", "wrong", "--query", "unit contains cohorts", "--page-number", "1", "--returned-count", "1", expected=2)
            self.assertIn("точный TQL", payload["error"])
            help_text = subprocess.run((sys.executable, str(SCRIPT), "mcp-log", "--help"), text=True, capture_output=True, check=True).stdout
            self.assertNotIn("capability-discovery", help_text)
            self.assertNotIn("issue-detail", help_text)

    def test_collector_complete_requires_every_returned_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); run_id = self.begin(state)["run_id"]
            self.query_page(state, run_id, "sbertrek", ["RSCON-6845"])
            payload = self.run_tool(state, "collector-complete", "--run-id", run_id, "--provider", "sbertrek", expected=2)
            self.assertIn("RSCON-6845", payload["error"])

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
