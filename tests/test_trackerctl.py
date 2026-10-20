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
            "schema_version": 5,
            "primary_provider": "sbertrek",
            "setup_complete": True,
            "projects": {"sbertrek": ["RSCON"]},
            "development_issue_types": ["story", "task"],
            "participants": {"sbertrek": {}},
            "status_rules": {
                "sbertrek": {"completed": ["done"], "excluded": ["cancelled"]},
            },
        })

    def begin(self, state: Path, provider: str, kind: str, *keys: str, intent: str = "read-only") -> dict:
        self.configure(state)
        args = [
            "begin", "--legacy", "--scope-kind", kind, "--scope-provider", provider,
            "--label", "Test", "--scope-source", "unit-test", "--intent", intent,
        ]
        for key in keys:
            args += ["--scope-id", key]
        return self.run_tool(state, *args)

    def sber_issue(
        self, key: str, *, external_reference: str | None = None, summary: str | None = None,
        status: str = "created", issue_type: str = "story", general: float | None = None,
        roles: dict[str, float] | None = None,
    ) -> dict:
        attributes = []
        if external_reference:
            attributes.append({"code": "issue_key", "name": "Объект SberTrek", "value": external_reference})
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

    def source_issue(
        self, key: str, *, summary: str | None = None, status: str = "To Do",
        roles: dict[str, float] | None = None,
    ) -> dict:
        fields = {
            "summary": summary or f"[BE] Task {key}", "issuetype": {"name": "Story"},
            "status": {"name": status}, "assignee": None,
            "created": "2026-08-01T08:00:00+00:00", "updated": "2026-08-02T08:00:00+00:00",
            "releases": [], "attributes": [],
        }
        names = {"AN": "Анализ", "BE": "Разработка BE", "FE": "Разработка FE", "QA": "Тестирование"}
        fields["attributes"] = [{"code": "estimate_" + role.lower(), "name": names[role], "value": value}
                                for role, value in (roles or {}).items()]
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






    def test_sbertrek_tasks_route_uses_only_explicit_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7002", "RSCON-7001")
            self.assertEqual(current["next_action"]["arguments"]["query"], 'unit = "RSCON-7001" or unit = "RSCON-7002"')
            current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001"), self.sber_issue("RSCON-7002")]})
            self.assertEqual(current["allowed_next_action"], "reconcile")






    def test_begin_and_ingest_are_idempotent_but_conflicting_repeat_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            first = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            second = self.run_tool(
                state, "begin", "--legacy", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
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
                state, "abandon-run", "--run-id", run_id, "--reason", "start-clean", "--analyst-confirmed",
            )
            self.assertEqual(abandoned["allowed_next_action"], "begin")
            marker = json.loads(
                (state / "tracker-runs" / run_id / "abandoned.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["previous_protocol"], "legacy-protocol")
            self.assertFalse((state / "tracker-active-run.json").exists())
            started = self.run_tool(
                state, "begin", "--legacy", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-7001", "--label", "Test", "--scope-source", "unit-test",
            )
            self.assertEqual(started["protocol"], "direct-tracker-v1")

    def test_abandon_requires_analyst_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            rejected = self.run_tool(
                state, "abandon-run", "--run-id", current["run_id"], "--reason", "retry", expected=2,
            )
            self.assertIn("явного подтверждения аналитика", rejected["error"])
            self.assertTrue((state / "tracker-active-run.json").is_file())
            resumed = self.run_tool(
                state, "begin", "--legacy", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-7001", "--label", "Retry", "--scope-source", "unit-test", "--intent", "read-only",
            )
            self.assertEqual(resumed["run_id"], current["run_id"])

    def test_begin_removes_orphaned_active_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.configure(state)
            orphaned_run = "20260901T100946Z-1e1af32a"
            self.write(state / "tracker-active-run.json", {"run_id": orphaned_run})
            started = self.run_tool(
                state, "begin", "--legacy", "--scope-kind", "tasks", "--scope-provider", "sbertrek",
                "--scope-id", "RSCON-7001", "--label", "Test", "--scope-source", "unit-test", "--intent", "read-only",
            )
            self.assertNotEqual(started["run_id"], orphaned_run)
            marker = json.loads((state / "tracker-active-run.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["run_id"], started["run_id"])

    def test_failed_ingest_is_transactional_and_retry_recovers_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001", "RSCON-7002")
            malformed = self.sber_issue("RSCON-7002")
            malformed.pop("summary")
            self.ingest(
                state, current, {"issues": [self.sber_issue("RSCON-7001"), malformed]},
                name="malformed", expected=2,
            )
            failed_run = json.loads(
                (state / "tracker-runs" / current["run_id"] / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failed_run["status"], "tracker-read-failed")
            self.assertEqual(failed_run["cards"]["sbertrek"], [])
            self.assertEqual(failed_run["steps"][0]["state"], "pending")
            retried = self.ingest(state, current, {"issues": [
                self.sber_issue("RSCON-7001"), self.sber_issue("RSCON-7002"),
            ]}, name="corrected")
            self.assertEqual(retried["run_id"], current["run_id"])
            self.assertEqual(retried["status"], "tracker-read-ready")
            recovered_run = json.loads(
                (state / "tracker-runs" / current["run_id"] / "run.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(recovered_run["failure"])

    def test_missing_service_dates_are_visible_but_do_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            issue = self.sber_issue("RSCON-7001")
            issue.pop("created_at")
            issue["updated_at"] = None
            current = self.ingest(state, current, {"issues": [issue]}, name="without-dates")
            self.assertEqual(current["status"], "tracker-read-ready")
            self.assertIn("sbertrek-created-at-not-returned:RSCON-7001", current["limitations"])
            self.assertIn("sbertrek-updated-at-not-returned:RSCON-7001", current["limitations"])
            stored = json.loads(
                (state / "tracker-runs" / current["run_id"] / "providers" / "sbertrek.json").read_text(encoding="utf-8")
            )["issues"][0]
            self.assertIsNone(stored["created_at"])
            self.assertIsNone(stored["updated_at"])
            self.assertEqual(stored["field_observations"]["created_at"], "absent")
            self.assertEqual(stored["field_observations"]["updated_at"], "absent")

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

    def test_actualization_continues_only_after_verified_update_result(self) -> None:
        for intent in ("read-only", "update-planning"):
            with self.subTest(intent=intent), tempfile.TemporaryDirectory() as temp:
                state = Path(temp)
                current = self.begin(state, "sbertrek", "tasks", "RSCON-7001", intent=intent)
                current = self.ingest(state, current, {"issues": [
                    self.sber_issue("RSCON-7001", roles={"FE": 5, "QA": 2}),
                ]})
                reconciled, result = self.reconcile(state, current)
                self.assertNotIn("planning_update", reconciled)
                self.assertFalse(reconciled["final_response_allowed"])
                before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
                completion = self.run_tool(state, "result-status", "--run-id", current["run_id"])
                update = completion["planning_update"]
                self.assertFalse(update["actualization_complete"])
                self.assertEqual(completion["planning_application_allowed"], intent == "update-planning")
                if intent == "read-only":
                    self.assertEqual(update["state"], "not-requested")
                    self.assertIsNone(update["next_action"])
                else:
                    self.assertEqual(update["state"], "pending")
                    action = update["next_action"]
                    self.assertEqual(action["type"], "execution-preview")
                    self.assertIn("execution-preview", action["command"])
                    self.assertEqual(action["mode"], "execution-update")
                    self.assertTrue(Path(action["contract"]).is_file())
                    self.assertEqual(action["run_id"], current["run_id"])
                    self.assertEqual(action["reconciled_sha256"], completion["reconciled_sha256"])
                    self.assertEqual(json.loads(Path(action["reconciled"]).read_text()), result)
                    self.assertEqual({item["role"] for item in result["work_items"]}, {"FE", "QA"})
                repeated = self.run_tool(state, "result-status", "--run-id", current["run_id"])
                self.assertEqual(repeated, completion)
                self.assertEqual(before, {path: path.read_bytes() for path in state.rglob("*") if path.is_file()})

    def test_result_status_rejects_forged_permissions_and_summary(self) -> None:
        for field, value in (
            ("planning_application_allowed", True),
            ("workflow_complete", False),
            ("final_response_allowed", False),
            ("status", "tracker-read-ready"),
            ("counts", {}),
            ("limitations", []),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                state = Path(temp)
                current = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
                current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001")]})
                self.reconcile(state, current)
                path = state / "tracker-runs" / current["run_id"] / "completion-status.json"
                completion = json.loads(path.read_text())
                completion[field] = value
                self.write(path, completion)
                error = self.run_tool(state, "result-status", "--run-id", current["run_id"], expected=2)
                self.assertIn("completion-status", error["error"])
                self.assertNotIn("planning_update", error)

    def test_result_status_binds_completion_to_finished_run_and_scope(self) -> None:
        for modification in ("unfinished", "scope"):
            with self.subTest(modification=modification), tempfile.TemporaryDirectory() as temp:
                state = Path(temp)
                current = self.begin(state, "sbertrek", "tasks", "RSCON-7001", intent="update-planning")
                current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001")]})
                self.reconcile(state, current)
                path = state / "tracker-runs" / current["run_id"] / "run.json"
                run = json.loads(path.read_text())
                if modification == "unfinished":
                    run["status"] = "tracker-read-ready"
                else:
                    run["scope"]["intent"] = "read-only"
                self.write(path, run)
                error = self.run_tool(state, "result-status", "--run-id", current["run_id"], expected=2)
                self.assertNotIn("planning_update", error)

    def test_old_completion_cannot_inject_an_execution_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            current = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            current = self.ingest(state, current, {"issues": [self.sber_issue("RSCON-7001")]})
            self.reconcile(state, current)
            path = state / "tracker-runs" / current["run_id"] / "completion-status.json"
            completion = json.loads(path.read_text())
            completion["planning_update"] = {"state": "complete", "next_action": {"type": "write-project"}}
            self.write(path, completion)
            checked = self.run_tool(state, "result-status", "--run-id", current["run_id"])
            self.assertIsNone(checked["planning_update"]["next_action"])
            self.assertFalse(checked["planning_update"]["actualization_complete"])

    def registry(self, project: Path, feature: str, rows: list[str], location: str = "execution/tasks.md") -> Path:
        path = project / "features" / feature / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "| Task ID | SberTrek | Retired reference | Kind | Role | Status |\n"
            "|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n", encoding="utf-8",
        )
        return path

    def test_feature_scope_includes_terminal_tasks_and_deduplicates_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            project = Path(temp) / "project"
            self.configure(state)
            self.registry(project, "example", [
                "| FRONT | DEMO-1 | ST-1 | real | FE | done |",
                "| TEST | DEMO-1/QA | ST-1 | real | QA | planned |",
                "| QA-LOCAL | | | real | QA | done |",
                "| STORY-2 | STORY-2 | | virtual | BE | planned |",
            ])
            self.registry(project, "example", [
                "| BACK | DEMO-2 | ST-2 | real | BE | cancelled |",
            ], "slices/legacy/execution/tasks.md")
            before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
            for provider, keys in (("sbertrek", ["DEMO-1", "DEMO-2"]),):
                preview = self.run_tool(
                    state, "scope-preview", "--project-root", str(project),
                    "--feature", "example", "--provider", provider,
                )
                self.assertEqual(preview["scope"]["ids"], keys)
                self.assertTrue(preview["requires_analyst_confirmation"])
                self.assertFalse(preview["tracker_calls_performed"])
                self.assertEqual(len(preview["references"][keys[0]]), 2)
                self.assertEqual(len(preview["omitted"]), 2)
                self.assertIn("new-epic-members-not-discovered", preview["limitations"])
                self.assertFalse((state / "tracker-active-run.json").exists())
            self.assertEqual(before, {path: path.read_bytes() for path in project.rglob("*") if path.is_file()})

    def test_quarter_scope_unions_views_and_explicit_feature_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            project = Path(temp) / "project"
            self.configure(state)
            self.registry(project, "cohorts", ["| FRONT | DEMO-1 | | real | FE | done |"])
            self.registry(project, "release", ["| BACK | DEMO-2 | | real | BE | planned |"])
            gantt = project / "planning/2026-Q3/gantt"
            for view, slug in (("quarter-plan", "cohort-simulation"), ("actual-progress", "cohort-simulation"),
                               ("commander-plan", "release"), ("quarter-plan", "plan-only")):
                include = gantt / "includes" / view / f"FEATURE-{slug}.puml"
                include.parent.mkdir(parents=True, exist_ok=True)
                include.write_text("' fixture\n", encoding="utf-8")
            (project / "features/plan-only").mkdir()
            self.write(gantt / "actual-progress-features.json", {
                "schema_version": 1, "features": {"cohort-simulation": "cohorts"},
            })
            preview = self.run_tool(
                state, "scope-preview", "--project-root", str(project),
                "--quarter", "2026-Q3", "--provider", "sbertrek",
            )
            self.assertEqual(preview["scope"]["ids"], ["DEMO-1", "DEMO-2"])
            self.assertEqual([item["feature"] for item in preview["features"]], ["cohorts", "plan-only", "release"])
            self.assertEqual(len(preview["references"]["DEMO-1"]), 1)
            self.assertIn("execution-registry-missing:plan-only", preview["limitations"])
            scoped = self.run_tool(
                state, "scope-preview", "--project-root", str(project),
                "--quarter", "2026-Q3", "--feature", "cohorts", "--provider", "sbertrek",
            )
            self.assertEqual(scoped["scope"]["ids"], ["DEMO-1"])

    def test_scope_preview_keeps_shared_keys_and_empty_scope_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            project = Path(temp) / "project"
            self.configure(state)
            for feature in ("first", "second"):
                self.registry(project, feature, ["| FRONT | DEMO-1 | | real | FE | done |"])
            gantt = project / "planning/2026-Q3/gantt"
            self.write(gantt / "actual-progress-features.json", {
                "schema_version": 1, "features": {"first": "first", "second": "second"},
            })
            preview = self.run_tool(state, "scope-preview", "--project-root", str(project), "--quarter", "2026-Q3", "--provider", "sbertrek")
            self.assertEqual(preview["shared_keys"], {"DEMO-1": ["first", "second"]})
            for feature in ("first", "second"):
                self.registry(project, feature, ["| FRONT | - | OLD-1 | real | FE | done |"] )
            empty = self.run_tool(state, "scope-preview", "--project-root", str(project), "--quarter", "2026-Q3", "--provider", "sbertrek")
            self.assertEqual(empty["scope"]["ids"], [])
            self.assertEqual(empty["next_action"]["type"], "clarify-tracker-scope")
            self.assertEqual(len(empty["omitted"]), 2)

    def test_scope_preview_never_guesses_provider_or_truncates_large_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            project = Path(temp) / "project"
            self.configure(state)
            self.registry(project, "example", [
                f"| INTERNAL-{index} | DEMO-{index} | | real | FE | done |" for index in range(1, 52)
            ])
            preview = self.run_tool(state, "scope-preview", "--project-root", str(project), "--feature", "example", "--provider", "sbertrek")
            self.assertEqual(len(preview["scope"]["ids"]), 51)
            self.assertIn("requested-keys-exceed-response-limit:51:50", preview["limitations"])
            self.registry(project, "example", [f"| INTERNAL-{index} | - | OLD-{index} | real | FE | done |" for index in range(1, 52)])
            unknown = self.run_tool(state, "scope-preview", "--project-root", str(project), "--feature", "example", "--provider", "sbertrek")
            self.assertEqual(unknown["scope"]["ids"], [])
            self.assertEqual(len(unknown["omitted"]), 51)

    def test_scope_preview_honors_config_and_existing_run_before_reading_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.run_tool(state, "init-config")
            args = ("scope-preview", "--project-root", "/does-not-exist", "--feature", "example", "--provider", "sbertrek")
            stopped = self.run_tool(state, *args, expected=3)
            self.assertTrue(stopped["must_stop"])
            self.assertNotIn("scope", stopped)
            active = self.begin(state, "sbertrek", "tasks", "RSCON-7001")
            resumed = self.run_tool(state, *args)
            self.assertEqual(resumed["run_id"], active["run_id"])
            self.assertEqual(resumed["next_action"], active["next_action"])
            self.assertNotIn("features", resumed)

    def test_scope_preview_blocks_invalid_paths_and_legacy_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            project = Path(temp) / "project"
            self.configure(state)
            self.registry(project, "example", ["| FRONT | DEMO-1/QA | | real | FE | done |"])
            for options in (("--feature", "../example"), ("--quarter", "2026-Q9"), ("--feature", "example")):
                self.run_tool(state, "scope-preview", "--project-root", str(project), "--provider", "sbertrek", *options, expected=2)
            external = Path(temp) / "external"
            external.mkdir()
            (project / "features/linked").symlink_to(external, target_is_directory=True)
            self.run_tool(state, "scope-preview", "--project-root", str(project), "--provider", "sbertrek", "--feature", "linked", expected=2)


if __name__ == "__main__":
    unittest.main()
