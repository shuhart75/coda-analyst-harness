from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_collaboration_recovery as recovery_tests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collaboration as MODULE


class WorktreeRecoveryTests(unittest.TestCase):
    setUp = recovery_tests.CollaborationRecoveryTests.setUp
    git = recovery_tests.CollaborationRecoveryTests.git
    identity = recovery_tests.CollaborationRecoveryTests.identity
    commit = recovery_tests.CollaborationRecoveryTests.commit
    command = recovery_tests.CollaborationRecoveryTests.command

    def dirty(self) -> dict[str, str]:
        self.document.write_text("unsaved progress\n", encoding="utf-8")
        return {self.relative: hashlib.sha256(self.document.read_bytes()).hexdigest()}

    def arguments(self, expected=None) -> argparse.Namespace:
        return argparse.Namespace(root=str(self.workspace), feature="cohorts", expected_head=self.base,
                                  expected_file=list((expected or self.dirty()).items()), analyst_confirmed=True)

    def recover(self, expected=None, success=True):
        arguments = ["recover-worktree", "--feature", "cohorts", "--expected-head", self.base, "--analyst-confirmed"]
        for relative, checksum in (expected or self.dirty()).items():
            arguments.extend(["--expected-file", relative, checksum])
        return self.command(*arguments, success=success)

    def test_preserves_files_main_index_and_completed_sessions_without_commit_or_push(self):
        expected = self.dirty()
        index = self.git(self.analytics, "ls-files", "--stage")
        content = self.document.read_bytes()
        self.assertFalse(self.command("status")["feature_work_allowed"])
        recovered = self.recover(expected)
        self.assertEqual(self.recover(expected)["branch"], recovered["branch"])
        self.assertEqual(self.document.read_bytes(), content)
        self.assertEqual(self.git(self.analytics, "ls-files", "--stage"), index)
        for repository, revision in ((self.analytics, "main"), (self.analytics, "HEAD"), (self.remote, "main")):
            self.assertEqual(self.git(repository, "rev-parse", revision), self.base)
        self.assertEqual(self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")
        current = json.loads(self.state_file.read_text())
        self.assertEqual(current["completed_work"], self.state["completed_work"])
        self.assertEqual(current["active_work"]["status"], "active")
        self.assertTrue(Path(recovered["snapshot"]).is_file())
        self.assertFalse(recovered["automatic_commit_created"])
        self.assertFalse(recovered["automatic_push_performed"])

    def test_wrong_head_checksum_or_path_list_preserves_main(self):
        expected = self.dirty()
        for change in ("head", "checksum", "extra", "duplicate", "confirmation"):
            with self.subTest(change=change):
                args = self.arguments(expected)
                if change == "head":
                    args.expected_head = "0" * 40
                elif change == "checksum":
                    args.expected_file = [(self.relative, "0" * 64)]
                elif change == "extra":
                    args.expected_file.append(("README.md", "0" * 64))
                elif change == "duplicate":
                    args.expected_file *= 2
                else:
                    args.analyst_confirmed = False
                with self.assertRaises(ValueError):
                    MODULE.recover_worktree_command(args)
                self.assertEqual(self.git(self.analytics, "branch", "--show-current"), "main")
                self.assertIsNone(json.loads(self.state_file.read_text())["active_work"])

    def test_index_changes_and_new_files_are_not_silently_transferred(self):
        expected = self.dirty()
        self.git(self.analytics, "add", "--", self.relative)
        self.assertIn("Индекс", self.recover(expected, success=False))
        self.assertIsNone(json.loads(self.state_file.read_text())["active_work"])

    def test_untracked_file_blocks_even_when_listed(self):
        expected = self.dirty()
        new = self.analytics / "new.md"
        new.write_text("new\n")
        expected["new.md"] = hashlib.sha256(new.read_bytes()).hexdigest()
        self.assertIn("отслеживаемые", self.recover(expected, success=False))
        self.assertTrue(new.is_file())

    def test_deletion_and_symlink_are_blocked(self):
        expected = self.dirty()
        self.document.unlink()
        self.assertIn("Удалённый", self.recover(expected, success=False))
        outside = self.root / "outside.md"
        outside.write_text("unsaved progress\n")
        self.document.symlink_to(outside)
        self.assertIn("символическая", self.recover(expected, success=False))
        self.assertTrue(self.document.is_symlink())

    def test_active_work_and_git_operation_block(self):
        expected = self.dirty()
        self.state["active_work"] = {"feature": "other", "branch": "feature/other/ivan", "status": "active"}
        MODULE.write_state(self.workspace, self.state)
        self.assertIn("другая активная", self.recover(expected, success=False))
        self.state["active_work"] = None
        MODULE.write_state(self.workspace, self.state)
        marker = self.analytics / ".git" / "CHERRY_PICK_HEAD"
        marker.write_text(self.base + "\n")
        self.assertIn("Git-операция", self.recover(expected, success=False))
        self.assertTrue(marker.exists())

    def test_unaccepted_commits_and_network_error_do_not_create_session(self):
        self.document.write_text("committed\n")
        self.base = self.commit("Local unaccepted progress")
        expected = self.dirty()
        self.assertIn("непринятые", self.recover(expected, success=False))
        self.assertIsNone(json.loads(self.state_file.read_text())["active_work"])
        with patch.object(MODULE, "fetch_main", side_effect=ValueError("offline")):
            with self.assertRaisesRegex(ValueError, "offline"):
                MODULE.recover_worktree_command(self.arguments(expected))

    def test_interrupted_switch_can_only_resume_same_files_and_feature(self):
        expected = self.dirty()
        real_git = MODULE.git
        def failing_git(repository, *arguments):
            if arguments[0] == "switch":
                return subprocess.CompletedProcess(arguments, 1, "", "switch failed")
            return real_git(repository, *arguments)
        with patch.object(MODULE, "git", side_effect=failing_git):
            with self.assertRaisesRegex(ValueError, "повтори ту же"):
                MODULE.recover_worktree_command(self.arguments(expected))
        pending = json.loads(self.state_file.read_text())["active_work"]
        self.assertEqual(pending["status"], "worktree-recovery-pending")
        self.assertFalse(self.command("status")["feature_work_allowed"])
        self.assertIn("recover-worktree", self.command("finish", success=False))
        self.assertIn("recover-worktree", self.command("update", success=False))
        self.document.write_text("changed after interruption\n")
        self.assertIn("Содержимое", self.recover(expected, success=False))
        self.document.write_text("unsaved progress\n")
        self.assertEqual(self.recover(expected)["branch"], pending["branch"])

    def test_state_write_failure_after_switch_resumes_without_new_branch(self):
        expected = self.dirty()
        real_write = MODULE.write_state
        def failing_write(root, state):
            if state["active_work"]["status"] == "active":
                raise OSError("write failed")
            return real_write(root, state)
        with patch.object(MODULE, "write_state", side_effect=failing_write):
            with self.assertRaisesRegex(OSError, "write failed"):
                MODULE.recover_worktree_command(self.arguments(expected))
        branch = self.git(self.analytics, "branch", "--show-current")
        self.assertNotEqual(branch, "main")
        self.assertEqual(self.recover(expected)["branch"], branch)

    def test_snapshot_tampering_does_not_allow_retry(self):
        expected = self.dirty()
        result = self.recover(expected)
        Path(result["snapshot"]).write_text("{}\n")
        self.assertIn("Контрольная сумма", self.recover(expected, success=False))
        self.assertEqual(self.document.read_text(), "unsaved progress\n")

    def test_existing_branch_is_not_reused(self):
        expected = self.dirty()
        self.git(self.analytics, "branch", "feature/cohorts/ivan", self.base)
        self.assertEqual(self.recover(expected)["branch"], "feature/cohorts/ivan-2")

    def test_changes_during_preflight_do_not_register_recovery(self):
        expected = self.dirty()
        real_next = MODULE.next_feature_branch
        def changing_branch(repository, feature, analyst):
            target = real_next(repository, feature, analyst)
            self.document.write_text("changed during fetch\n")
            return target
        with patch.object(MODULE, "next_feature_branch", side_effect=changing_branch):
            with self.assertRaisesRegex(ValueError, "Содержимое"):
                MODULE.recover_worktree_command(self.arguments(expected))
        self.assertIsNone(json.loads(self.state_file.read_text())["active_work"])


if __name__ == "__main__":
    unittest.main()
