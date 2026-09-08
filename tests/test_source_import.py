from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_workspace as fixture


class SourceImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = fixture.CodaWorkspaceTests()
        self.workspace, self.source, self.remote, self.environment = self.fixture.prepare_workspace(self.root)
        self.documents = self.workspace / "documents"

    def git(self, repository: Path, *arguments: str) -> str:
        result = fixture.run("git", "-C", str(repository), *arguments, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def sync(self, *arguments: str) -> dict:
        result = fixture.run("python3", str(fixture.ROOT / "scripts/workspace.py"), "--root", str(self.workspace), "sync", *arguments, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def change_source(self, content: str) -> None:
        (self.source / "context/shared.txt").write_text(content, encoding="utf-8")
        self.git(self.source, "add", "--", "context/shared.txt")
        self.git(self.source, "commit", "-m", "Change source content")
        self.git(self.source, "push", "origin", "main")

    def accept(self, branch: str) -> None:
        receiver = self.root / "receiver"
        self.assertEqual(fixture.run("git", "clone", "--quiet", str(self.remote), str(receiver)).returncode, 0)
        self.fixture.configure_identity(receiver)
        self.git(receiver, "merge", "--no-ff", f"origin/{branch}", "-m", "Accept incoming changes")
        self.git(receiver, "push", "origin", "main")

    def test_source_deletion_is_only_a_review_candidate_until_human_merge(self) -> None:
        content = "### REQ-DEMO-001. Useful rule\n#### Сценарий: полезное поведение\nПолезное требование.\n"
        self.change_source(content)
        self.git(self.source, "push", str(self.remote), "main:main")
        self.git(self.documents, "pull", "--ff-only", "origin", "main")
        initial = self.git(self.documents, "rev-parse", "HEAD")
        self.change_source("Shortened source\n")
        result = self.sync()
        request = result["analytics_exchange"]["source_import"]
        self.assertEqual(result["status"], "source-import-pending")
        self.assertFalse(result["all_repositories_synchronized"])
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), initial)
        self.assertEqual((self.documents / "context/shared.txt").read_text(encoding="utf-8"), content)
        changed = request["review"]["changed_paths"][0]
        self.assertEqual(changed["removed_requirements"], ["REQ-DEMO-001"])
        self.assertTrue(changed["removed_scenarios"])
        repeated = self.sync()["analytics_exchange"]["source_import"]
        self.assertEqual(repeated["request_commit"], request["request_commit"])
        blocked = fixture.run("python3", str(fixture.ROOT / "scripts/repository-exchange.py"), "--root", str(self.workspace), "reverse-diff", env=self.environment)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("source-import-pending", blocked.stdout)
        self.accept(request["request_branch"])
        accepted = self.sync()
        self.assertTrue(accepted["all_repositories_synchronized"])
        self.assertEqual((self.documents / "context/shared.txt").read_text(), "Shortened source\n")
        self.assertFalse((self.workspace / ".workspace-state/source-import.json").exists())
        self.assertTrue(Path(accepted["analytics_exchange"]["source_merge"]["acceptance_receipt"]).is_file())
        self.git(self.source, "rm", "--", "context/shared.txt")
        self.git(self.source, "commit", "-m", "Remove source document")
        self.git(self.source, "push", "origin", "main")
        deletion = self.sync()["analytics_exchange"]["source_import"]
        self.assertIn("context/shared.txt", deletion["review"]["deleted_paths"])
        self.assertTrue((self.documents / "context/shared.txt").is_file())

    def test_no_push_and_push_failure_reuse_saved_candidate(self) -> None:
        self.change_source("Incoming\n")
        first = self.sync("--no-push")["analytics_exchange"]["source_import"]
        self.assertEqual(first["status"], "prepared-not-pushed")
        self.assertEqual(self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/codex/source-import/"), "")
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        rejected = self.sync()["analytics_exchange"]["source_import"]
        self.assertEqual(rejected["status"], "prepared-not-pushed")
        self.assertEqual(rejected["request_commit"], first["request_commit"])
        self.assertTrue(rejected["push_error"])
        hook.unlink()
        retried = self.sync()["analytics_exchange"]["source_import"]
        self.assertEqual(retried["request_commit"], first["request_commit"])
        self.assertEqual(retried["status"], "awaiting-merge")

    def test_new_source_does_not_replace_pending_candidate(self) -> None:
        self.change_source("First incoming\n")
        first = self.sync()["analytics_exchange"]["source_import"]
        self.change_source("Second incoming\n")
        second = self.sync()["analytics_exchange"]["source_import"]
        self.assertEqual(second["request_commit"], first["request_commit"])
        self.assertTrue(second["source_has_newer_commit"])
        self.accept(first["request_branch"])
        following = self.sync()["analytics_exchange"]["source_import"]
        self.assertNotEqual(following["request_branch"], first["request_branch"])

    def test_cold_state_reuses_remote_request_after_target_advances(self) -> None:
        self.change_source("Incoming\n")
        first = self.sync()["analytics_exchange"]["source_import"]
        (self.workspace / ".workspace-state/source-import.json").unlink()
        receiver = self.root / "target-update"
        self.assertEqual(fixture.run("git", "clone", "--quiet", str(self.remote), str(receiver)).returncode, 0)
        self.fixture.configure_identity(receiver)
        (receiver / "context/other.txt").write_text("Accepted elsewhere\n")
        self.git(receiver, "add", "--", "context/other.txt")
        self.git(receiver, "commit", "-m", "Update target content")
        self.git(receiver, "push", "origin", "main")
        second = self.sync()["analytics_exchange"]["source_import"]
        self.assertEqual(second["request_commit"], first["request_commit"])
        self.assertEqual((self.documents / "context/shared.txt").read_text(), "base\n")

    def test_local_main_history_cannot_bypass_review(self) -> None:
        (self.documents / "context/shared.txt").write_text("Local only\n")
        self.git(self.documents, "add", "--", "context/shared.txt")
        self.git(self.documents, "commit", "-m", "Local analytical change")
        initial = self.git(self.remote, "rev-parse", "main")
        result = fixture.run("python3", str(fixture.ROOT / "scripts/workspace.py"), "--root", str(self.workspace), "sync", env=self.environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("analytics-unaccepted-history", result.stdout)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), initial)

    def test_lost_push_response_is_reconciled_with_remote_branch(self) -> None:
        self.change_source("Incoming\n")
        prepared = self.sync("--no-push")["analytics_exchange"]["source_import"]
        with patch.object(sys, "path", [str(fixture.ROOT / "scripts"), *sys.path]):
            spec = importlib.util.spec_from_file_location("source_exchange", fixture.ROOT / "scripts/repository-exchange.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        original = module.git
        def lose_response(repository: Path, *arguments: str, **kwargs):
            result = original(repository, *arguments, **kwargs)
            if arguments[0] == "push" and not result.returncode:
                return subprocess.CompletedProcess(result.args, 1, "", "Lost response")
            return result
        source_mirror = self.workspace / ".workspace-state/repositories/changeswork-copy.git"
        with patch.object(module, "git", side_effect=lose_response):
            result = module.prepare_source_import(self.workspace, source_mirror, self.documents, "documents", False)
        self.assertEqual(result["status"], "awaiting-merge")
        self.assertEqual(result["request_commit"], prepared["request_commit"])

    def test_conflict_is_resolved_only_in_isolated_candidate(self) -> None:
        (self.documents / "context/shared.txt").write_text("Accepted documents\n")
        self.git(self.documents, "add", "--", "context/shared.txt")
        self.git(self.documents, "commit", "-m", "Accept analytical text")
        self.git(self.documents, "push", "origin", "main")
        initial = self.git(self.documents, "rev-parse", "HEAD")
        self.change_source("Incoming conflict\n")
        result = fixture.run("python3", str(fixture.ROOT / "scripts/repository-exchange.py"), "--root", str(self.workspace), "sync", env=self.environment)
        self.assertNotEqual(result.returncode, 0)
        state = json.loads((self.workspace / ".workspace-state/source-import.json").read_text())
        checkout = Path(state["checkout"])
        self.assertTrue((checkout / ".git/MERGE_HEAD").is_file())
        self.assertFalse((self.documents / ".git/MERGE_HEAD").exists())
        self.assertEqual(self.git(self.documents, "rev-parse", "HEAD"), initial)
        (checkout / "context/shared.txt").write_text("Reviewed resolution\n")
        self.git(checkout, "add", "--", "context/shared.txt")
        self.git(checkout, "-c", "user.name=Reviewer", "-c", "user.email=reviewer@example.test", "commit", "-m", "Resolve incoming conflict")
        request = self.sync()["analytics_exchange"]["source_import"]
        self.assertEqual(request["status"], "awaiting-merge")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), initial)
        self.accept(request["request_branch"])
        self.sync()
        self.assertEqual((self.documents / "context/shared.txt").read_text(), "Reviewed resolution\n")

    def test_deferred_import_waits_for_new_source_without_republishing(self) -> None:
        self.change_source("Rejected incoming\n")
        first = self.sync()["analytics_exchange"]["source_import"]
        self.git(self.documents, "push", "origin", "--delete", first["request_branch"])
        command = ("python3", str(fixture.ROOT / "scripts/repository-exchange.py"), "--root", str(self.workspace), "defer-source-import")
        self.assertNotEqual(fixture.run(*command, env=self.environment).returncode, 0)
        self.assertEqual(fixture.run(*command, "--analyst-confirmed", env=self.environment).returncode, 0)
        self.assertEqual(self.sync()["analytics_exchange"]["source_import"]["status"], "deferred")
        self.assertEqual(self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/codex/source-import/"), "")
        self.change_source("Revised incoming\n")
        next_request = self.sync()["analytics_exchange"]["source_import"]
        self.assertNotEqual(next_request["request_branch"], first["request_branch"])


if __name__ == "__main__":
    unittest.main()
