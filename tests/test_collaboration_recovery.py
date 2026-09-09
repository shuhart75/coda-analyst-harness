from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/collaboration.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("recovery_collaboration", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CollaborationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "harness"
        self.analytics = self.workspace / "documents"
        self.remote = self.root / "analytics.git"
        self.analytics.mkdir(parents=True)
        self.git(self.analytics, "init", "-b", "main")
        self.identity(self.analytics)
        self.relative = "features/cohorts/execution/actual-progress.md"
        self.document = self.analytics / self.relative
        self.document.parent.mkdir(parents=True)
        self.document.write_text("base\n", encoding="utf-8")
        self.commit("Initial progress")
        self.git(self.root, "clone", "--bare", str(self.analytics), str(self.remote))
        self.git(self.analytics, "remote", "add", "origin", str(self.remote))
        self.git(self.analytics, "fetch", "origin")
        self.base = self.git(self.analytics, "rev-parse", "HEAD")
        runtime = self.workspace / ".workspace-state"
        runtime.mkdir()
        (runtime / "workspace.json").write_text(json.dumps({
            "roles": {"analytics": {"repository": "documents", "path": str(self.analytics)}},
        }), encoding="utf-8")
        self.state_file = runtime / "collaboration.json"
        self.state = MODULE.new_state("ivan")
        self.state["completed_work"] = [{"feature": "registry", "status": "merged"}]
        MODULE.write_state(self.workspace, self.state)

    def git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments], text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def identity(self, repository: Path) -> None:
        self.git(repository, "config", "user.name", "Harness Test")
        self.git(repository, "config", "user.email", "harness@example.test")

    def commit(self, message: str) -> str:
        self.git(self.analytics, "add", "--", self.relative)
        self.git(self.analytics, "commit", "-m", message)
        return self.git(self.analytics, "rev-parse", "HEAD")

    def local_work(self) -> str:
        self.document.write_text("local progress\n", encoding="utf-8")
        return self.commit("Update local progress")

    def command(self, *arguments: str, success: bool = True) -> dict | str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.workspace), *arguments],
            text=True, capture_output=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def recover(self, commit: str, **kwargs) -> dict | str:
        return self.command(
            "recover-main", "--feature", "cohorts", "--expected-head", commit,
            "--analyst-confirmed", **kwargs,
        )

    def accept(self, branch: str) -> None:
        reviewer = self.root / "reviewer"
        self.git(self.root, "clone", str(self.remote), str(reviewer))
        self.identity(reviewer)
        self.git(reviewer, "merge", "--no-ff", f"origin/{branch}", "-m", "Accept reviewed progress")
        self.git(reviewer, "push", "origin", "main")

    def test_recovery_submit_and_human_merge_preserve_history(self) -> None:
        local = self.local_work()
        before = self.document.read_bytes()
        self.assertFalse(self.command("status")["feature_work_allowed"])
        blocked = self.command("start", "--feature", "cohorts", success=False)
        self.assertIn("recover-main", blocked)
        self.assertEqual(self.git(self.analytics, "branch", "--show-current"), "main")
        recovered = self.recover(local)
        branch = recovered["branch"]
        self.assertEqual(self.recover(local)["branch"], branch)
        self.assertEqual(self.git(self.analytics, "rev-parse", "main"), local)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.base)
        self.assertEqual(self.document.read_bytes(), before)
        self.assertFalse(recovered["automatic_push_performed"])
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["completed_work"], self.state["completed_work"])
        self.assertEqual(state["configured_at"], self.state["configured_at"])
        self.assertEqual(self.git(self.analytics, "status", "--porcelain"), "")
        self.command("update")
        self.command("submit")
        self.assertEqual(self.command("finish", success=False).count("feature-branch-not-merged"), 1)
        self.accept(branch)
        self.assertEqual(self.command("finish")["status"], "feature-work-finished")
        self.assertIsNone(self.command("status")["collaboration"]["active_work"])
        self.assertEqual(self.git(self.analytics, "rev-parse", "main"), self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(self.document.read_bytes(), before)

    def test_recovery_of_diverged_history_requires_update(self) -> None:
        local = self.local_work()
        colleague = self.root / "colleague"
        self.git(self.root, "clone", str(self.remote), str(colleague))
        self.identity(colleague)
        (colleague / "README.md").write_text("remote\n", encoding="utf-8")
        self.git(colleague, "add", "--", "README.md")
        self.git(colleague, "commit", "-m", "Add project introduction")
        self.git(colleague, "push", "origin", "main")
        self.recover(local)
        self.assertIn("обнови", self.command("submit", success=False))
        self.command("update")
        self.assertEqual(self.git(self.analytics, "rev-parse", "main"), local)
        self.assertEqual(self.document.read_text(), "local progress\n")
        self.assertEqual((self.analytics / "README.md").read_text(), "remote\n")
        self.command("submit")

    def test_confirmation_and_exact_head_are_required(self) -> None:
        local = self.local_work()
        self.command("recover-main", "--feature", "cohorts", "--expected-head", local, success=False)
        self.assertIn("изменились", self.recover(self.base, success=False))
        self.assertIn("хеш коммита", self.recover("HEAD", success=False))
        self.assertEqual(json.loads(self.state_file.read_text()), self.state)
        self.assertEqual(self.git(self.analytics, "branch", "--show-current"), "main")

    def test_dirty_work_and_existing_session_are_not_adopted(self) -> None:
        local = self.local_work()
        self.document.write_text("unsaved\n", encoding="utf-8")
        self.assertIn("несохранённые", self.recover(local, success=False))
        self.assertEqual(self.document.read_text(), "unsaved\n")
        local = self.commit("Preserve pending progress")
        self.state["active_work"] = {"feature": "registry", "branch": "feature/registry/ivan"}
        MODULE.write_state(self.workspace, self.state)
        self.assertIn("другая активная", self.recover(local, success=False))
        self.assertEqual(json.loads(self.state_file.read_text()), self.state)

    def test_current_main_and_active_merge_are_blocked(self) -> None:
        self.assertIn("нет непринятых", self.recover(self.base, success=False))
        local = self.local_work()
        merge_head = self.analytics / ".git/MERGE_HEAD"
        merge_head.write_text(self.base + "\n", encoding="ascii")
        self.assertIn("слияние", self.recover(local, success=False))
        self.assertEqual(merge_head.read_text(), self.base + "\n")
        self.assertIsNone(json.loads(self.state_file.read_text())["active_work"])

    def test_branch_collision_gets_new_name_and_remote_failure_stops(self) -> None:
        local = self.local_work()
        self.git(self.analytics, "branch", "feature/cohorts/ivan", self.base)
        recovered = self.recover(local)
        self.assertEqual(recovered["branch"], "feature/cohorts/ivan-2")
        self.assertEqual(self.git(self.analytics, "rev-parse", "feature/cohorts/ivan"), self.base)
        real_git = MODULE.git
        def unavailable(repository, *arguments):
            if arguments[0] == "ls-remote":
                return subprocess.CompletedProcess(arguments, 128, "", "offline")
            return real_git(repository, *arguments)
        with patch.object(MODULE, "git", side_effect=unavailable):
            with self.assertRaisesRegex(ValueError, "удалённые ветки"):
                MODULE.next_feature_branch(self.analytics, "other", "ivan")

    def test_retry_after_switch_failure_uses_registered_recovery(self) -> None:
        local = self.local_work()
        real_git = MODULE.git
        def fail_switch(repository, *arguments):
            if arguments[0] == "switch":
                return subprocess.CompletedProcess(arguments, 1, "", "injected switch failure")
            return real_git(repository, *arguments)
        args = argparse.Namespace(root=str(self.workspace), feature="cohorts", expected_head=local)
        with patch.object(MODULE, "git", side_effect=fail_switch):
            with self.assertRaisesRegex(ValueError, "повтори ту же"):
                MODULE.recover_main_command(args)
        pending = json.loads(self.state_file.read_text())["active_work"]
        self.assertEqual(pending["status"], "recovery-pending")
        self.assertFalse(self.command("status")["feature_work_allowed"])
        self.assertIn("recover-main", self.command("finish", success=False))
        self.assertEqual(self.recover(local)["branch"], pending["branch"])

    def test_retry_after_state_write_failure_preserves_both_branches(self) -> None:
        local = self.local_work()
        real_write = MODULE.write_state
        def fail_activation(root, state):
            if state["active_work"]["status"] == "active":
                raise OSError("injected write failure")
            real_write(root, state)
        args = argparse.Namespace(root=str(self.workspace), feature="cohorts", expected_head=local)
        with patch.object(MODULE, "write_state", side_effect=fail_activation):
            with self.assertRaisesRegex(OSError, "injected"):
                MODULE.recover_main_command(args)
        branch = self.git(self.analytics, "branch", "--show-current")
        self.assertNotEqual(branch, "main")
        self.assertEqual(self.git(self.analytics, "rev-parse", "main"), local)
        self.assertEqual(self.recover(local)["branch"], branch)

    def test_missing_configuration_is_not_replaced(self) -> None:
        local = self.local_work()
        self.state_file.unlink()
        self.assertIn("миграция", self.recover(local, success=False))
        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
