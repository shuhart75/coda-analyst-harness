from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_trackerctl


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from tracker_execution import preview_execution


class TrackerExecutionPreviewTests(unittest.TestCase):
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    configure = test_trackerctl.DirectTrackerWorkflowTests.configure
    write = test_trackerctl.DirectTrackerWorkflowTests.write
    begin = test_trackerctl.DirectTrackerWorkflowTests.begin
    ingest = test_trackerctl.DirectTrackerWorkflowTests.ingest
    sber_issue = test_trackerctl.DirectTrackerWorkflowTests.sber_issue
    reconcile = test_trackerctl.DirectTrackerWorkflowTests.reconcile
    registry = test_trackerctl.DirectTrackerWorkflowTests.registry

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve() / "project"
        self.state = Path(temporary.name).resolve() / "state"
        self.project.mkdir()
        self.git("init", "-q")
        self.configure(self.state)

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
            text=True, capture_output=True, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def save_sources(self) -> None:
        paths = [str(path.relative_to(self.project)) for path in self.project.rglob("*")
                 if path.is_file() and ".git" not in path.relative_to(self.project).parts]
        self.git("add", "--", *paths)
        self.git("commit", "-qm", "Prepare execution sources")

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.project)): path.read_bytes()
                for path in self.project.rglob("*") if path.is_file()}

    def result(self, roles: tuple[str, ...] = ("BE",), *, jira_key: str | None = "JIRA-1") -> dict:
        issue = {"jira_key": jira_key, "sbertrek_key": "ST-1"}
        return {
            "issues": [issue], "excluded": [],
            "work_items": [{**issue, "role": role, "work_item_id": f"{jira_key or 'ST-1'}/{role}"} for role in roles],
        }

    def preview(self, result: dict | None = None, feature: str | None = "owner", quarter: str | None = None) -> dict:
        before = self.snapshot()
        payload = preview_execution(self.project, quarter, feature, result or self.result())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(payload["writes_performed"])
        self.assertFalse(payload["creation_allowed"])
        return payload

    def reasons(self, payload: dict) -> set[str]:
        return {item["reason"] for item in payload["blockers"]}

    def test_exact_pair_and_roles_find_existing_root_and_legacy_rows(self) -> None:
        self.registry(self.project, "owner", ["| CORE | JIRA-1/BE | ST-1 | real | BE | done |"])
        self.registry(self.project, "owner", ["| QA-LOCAL | JIRA-1 | ST-1 | real | QA | planned |"],
                      "slices/legacy/execution/tasks.md")
        self.save_sources()
        payload = self.preview(self.result(("BE", "QA")))
        self.assertTrue(payload["ownership_ready"])
        self.assertEqual(payload["items"][0]["owners"], ["owner"])
        self.assertEqual({row["task_id"] for row in payload["items"][0]["targets"]}, {"CORE", "QA-LOCAL"})
        self.assertEqual(len(payload["registry_sha256"]), 2)

    def test_duplicate_role_in_another_slice_is_blocked_before_writes(self) -> None:
        for location in ("execution/tasks.md", "slices/other/execution/tasks.md"):
            self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"], location)
        self.save_sources()
        payload = self.preview()
        self.assertFalse(payload["ownership_ready"])
        self.assertIn("duplicate-registry-role:BE", self.reasons(payload))
        self.assertEqual(len(payload["items"][0]["targets"]), 2)

    def test_owner_outside_quarter_is_shown_not_silently_reassigned(self) -> None:
        self.registry(self.project, "selected", ["| OTHER | JIRA-2 | ST-2 | real | BE | done |"])
        self.registry(self.project, "real-owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"])
        self.write(self.project / "planning/2026-Q3/gantt/actual-progress-features.json", {
            "schema_version": 1, "features": {"selected": "selected"},
        })
        self.save_sources()
        payload = self.preview(feature=None, quarter="2026-Q3")
        self.assertEqual(payload["items"][0]["owners"], ["real-owner"])
        self.assertIn("owner-outside-selected-scope", self.reasons(payload))

    def test_duplicates_across_features_and_conflicting_tracker_pairs_are_visible(self) -> None:
        self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-2 | real | BE | done |"])
        self.registry(self.project, "other", ["| SECOND | JIRA-2 | ST-1 | real | BE | done |"])
        self.save_sources()
        payload = self.preview()
        self.assertIn("multiple-feature-owners", self.reasons(payload))
        self.assertIn("registry-pair-conflicts-with-reconciliation", self.reasons(payload))
        self.assertEqual(payload["items"][0]["owners"], ["other", "owner"])

    def test_new_qa_role_is_not_placed_into_the_fe_slice_automatically(self) -> None:
        self.registry(self.project, "owner", ["| FRONT | JIRA-1 | ST-1 | real | FE | done |"])
        self.save_sources()
        payload = self.preview(self.result(("FE", "QA")))
        self.assertIn("new-role-needs-confirmation:QA", self.reasons(payload))
        self.assertEqual([row["role"] for row in payload["items"][0]["targets"]], ["FE"])

    def test_internal_id_is_not_a_tracker_mapping_but_blocks_collision(self) -> None:
        self.registry(self.project, "owner", ["| JIRA-1/BE | | | real | BE | done |"])
        self.save_sources()
        payload = self.preview()
        self.assertIn("task-owner-not-confirmed", self.reasons(payload))
        self.assertIn("unconfirmed-internal-id-collision", self.reasons(payload))
        self.assertEqual(payload["items"][0]["targets"], [])

    def test_dirty_or_untracked_owner_is_not_accepted_as_confirmed_source(self) -> None:
        path = self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"])
        self.save_sources()
        path.write_text(path.read_text() + "\nUnreviewed change\n", encoding="utf-8")
        payload = self.preview()
        self.assertIn("uncommitted-owner-registry", self.reasons(payload))
        self.registry(self.project, "owner", ["| TEST | JIRA-1 | ST-1 | real | QA | planned |"],
                      "slices/new/execution/tasks.md")
        payload = self.preview(self.result(("BE", "QA")))
        self.assertIn("uncommitted-owner-registry", self.reasons(payload))

    def test_unrelated_legacy_invalid_key_is_visible_without_inventing_identity(self) -> None:
        self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"])
        self.registry(self.project, "legacy", ["| QA-LOCAL | QA-LOCAL | | real | QA | done |"])
        self.save_sources()
        payload = self.preview()
        self.assertTrue(payload["ownership_ready"])
        self.assertEqual(payload["warnings"][0]["reason"], "invalid-external-key")
        self.assertEqual(payload["warnings"][0]["reference"]["feature"], "legacy")

    def test_deleted_registry_cannot_hide_an_old_owner(self) -> None:
        path = self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"])
        self.save_sources()
        path.unlink()
        payload = self.preview()
        self.assertIn("deleted-or-renamed-registry", self.reasons(payload))
        self.assertIn("task-owner-not-confirmed", self.reasons(payload))

    def test_completed_run_reuses_saved_pairs_without_new_mcp_or_state_writes(self) -> None:
        self.registry(self.project, "owner", ["| CORE | | ST-1 | real | BE | done |"])
        self.save_sources()
        current = self.begin(self.state, "sbertrek", "tasks", "ST-1", intent="update-planning")
        current = self.ingest(self.state, current, {"issues": [self.sber_issue("ST-1", roles={"BE": 2})]})
        self.reconcile(self.state, current)
        before_project = self.snapshot()
        before_state = {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        preview = self.run_tool(self.state, "execution-preview", "--run-id", current["run_id"],
                                "--project-root", str(self.project), "--feature", "owner")
        self.assertTrue(preview["ownership_ready"])
        self.assertEqual(preview["run_id"], current["run_id"])
        self.assertEqual(self.snapshot(), before_project)
        self.assertEqual(before_state, {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()})

    def test_read_only_result_cannot_enter_execution_preview(self) -> None:
        current = self.begin(self.state, "sbertrek", "tasks", "ST-1")
        current = self.ingest(self.state, current, {"issues": [self.sber_issue("ST-1")]})
        self.reconcile(self.state, current)
        blocked = self.run_tool(self.state, "execution-preview", "--run-id", current["run_id"],
                                "--project-root", "/does-not-exist", "--feature", "owner", expected=2)
        self.assertIn("Read-only", blocked["error"])

    def test_reviewed_dirty_registry_requires_exact_head_and_content(self) -> None:
        path = self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"])
        self.save_sources()
        head = self.git("rev-parse", "HEAD").strip()
        path.write_text(path.read_text() + "\nReviewed change\n", encoding="utf-8")
        reviewed = {path.relative_to(self.project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()}
        before = self.snapshot()
        payload = preview_execution(self.project, None, "owner", self.result(), reviewed, head)
        self.assertTrue(payload["ownership_ready"])
        self.assertEqual(payload["reviewed_registry_sha256"], reviewed)
        for bad_head in (None, "0" * 40):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                preview_execution(self.project, None, "owner", self.result(), reviewed, bad_head)
        with self.assertRaisesRegex(ValueError, "версия реестра"):
            preview_execution(self.project, None, "owner", self.result(),
                              {next(iter(reviewed)): "0" * 64}, head)
        self.assertEqual(self.snapshot(), before)

    def test_reviewed_registry_does_not_bypass_ownership_or_new_role_checks(self) -> None:
        self.registry(self.project, "owner", ["| CORE | JIRA-1 | ST-1 | real | BE | done |"])
        self.save_sources()
        path = self.registry(self.project, "outside", ["| DUPLICATE | JIRA-1 | ST-1 | real | BE | done |"])
        reviewed = {path.relative_to(self.project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()}
        payload = preview_execution(self.project, None, "owner", self.result(("BE", "QA")),
                                    reviewed, self.git("rev-parse", "HEAD").strip())
        self.assertNotIn("uncommitted-owner-registry", self.reasons(payload))
        self.assertTrue({"multiple-feature-owners", "owner-outside-selected-scope",
                         "duplicate-registry-role:BE", "new-role-needs-confirmation:QA"}
                        <= self.reasons(payload))
        self.assertFalse(payload["ownership_ready"])

    def test_cli_review_requires_explicit_confirmation_and_exact_paths(self) -> None:
        path = self.registry(self.project, "owner", ["| CORE | | ST-1 | real | BE | done |"])
        self.save_sources()
        path.write_text(path.read_text() + "\nReviewed change\n", encoding="utf-8")
        head = self.git("rev-parse", "HEAD").strip()
        approval = ["--reviewed-registry", path.relative_to(self.project).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest()]
        current = self.begin(self.state, "sbertrek", "tasks", "ST-1", intent="update-planning")
        current = self.ingest(self.state, current, {"issues": [self.sber_issue("ST-1", roles={"BE": 2})]})
        self.reconcile(self.state, current)
        command = ["execution-preview", "--run-id", current["run_id"],
                   "--project-root", str(self.project), "--feature", "owner"]
        for options in (approval, [*approval, "--expected-head", head],
                        [*approval, "--analyst-confirmed"], ["--analyst-confirmed"],
                        ["--expected-head", head],
                        [*approval, *approval, "--expected-head", head, "--analyst-confirmed"]):
            with self.subTest(options=options):
                self.run_tool(self.state, *command, *options, expected=2)
        payload = self.run_tool(self.state, *command, *approval, "--expected-head", head, "--analyst-confirmed")
        self.assertTrue(payload["ownership_ready"])
