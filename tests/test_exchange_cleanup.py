from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import exchange_cleanup as cleanup
from baseline_releases import tree_hash


class ExchangeCleanupTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.code = self.root / "ordinary-code"
        self.remote = self.root / "remote.git"
        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.root, "init", "-b", "main", str(self.code))
        self.git(self.code, "config", "user.name", "Test")
        self.git(self.code, "config", "user.email", "test@example.invalid")
        self.package = self.code / "requirements-exchange" / "delivery-one"
        self.package.mkdir(parents=True)
        (self.package / "manifest.json").write_text('{}\n')
        returns = self.package / "revisions" / "001" / "returns"
        returns.mkdir(parents=True)
        (returns / "summary.md").write_text("Result\n")
        (self.code / "requirements-exchange" / "GIGACODE.md").write_text("Shared instructions\n")
        (self.code / "unrelated.txt").write_text("Preserve\n")
        self.git(self.code, "add", ".")
        self.git(self.code, "commit", "-m", "Initialize fixture")
        self.git(self.code, "remote", "add", "origin", str(self.remote))
        self.git(self.code, "push", "origin", "main")
        self.original = self.git(self.code, "rev-parse", "HEAD")
        self.record = {"release_id": "release-one", "delivery_key": "delivery-one",
                       "archive_path": "releases/release-one/exchange-archive/delivery-one",
                       "archive_hash": tree_hash(self.package), "source": {
                           "role": "code", "repository_url": str(self.remote),
                           "target_branch": "main", "commit": self.original}}
        self.gate = patch.dict(sys.modules, {"release_exchange": types.SimpleNamespace(
            require_accepted_archive=lambda *args: copy.deepcopy(self.record))})
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def publish(self):
        return cleanup.publish_cleanup(self.root, "release-one", "delivery-one")

    def test_scoped_publication_retry_and_merged_completion(self):
        (self.code / "unrelated.txt").write_text("Local work\n")
        before = self.git(self.code, "status", "--porcelain")
        result = self.publish()
        self.assertEqual(result["status"], "awaiting-merge")
        self.assertFalse(result["merge_request_created"])
        self.assertEqual(self.git(self.code, "rev-parse", "HEAD"), self.original)
        self.assertEqual(self.git(self.code, "status", "--porcelain"), before)
        self.assertTrue(self.package.is_dir())
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.original)
        commit = result["request_commit"]
        diff = self.git(self.remote, "diff", "--name-status", "main", commit).splitlines()
        self.assertTrue(diff)
        self.assertTrue(all(row.startswith("D\trequirements-exchange/delivery-one/") for row in diff))
        self.assertEqual(self.publish(), result)
        self.git(self.remote, "update-ref", "refs/heads/main", commit)
        self.assertEqual(self.publish()["status"], "completed")
        self.assertTrue(self.package.exists())

    def test_changed_return_blocks_new_and_existing_branch(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.publish()
                new = self.package / "new-return.md"
                new.write_text("New result\n")
                self.git(self.code, "add", str(new))
                self.git(self.code, "commit", "-m", "Add result")
                self.git(self.code, "push", "origin", "main")
                with self.assertRaisesRegex(ValueError, "изменилась после архивирования"):
                    self.publish()
                self.git(self.remote, "update-ref", "refs/heads/main", self.original)
                self.git(self.code, "reset", "--hard", self.original)

    def test_rejected_gate_creates_no_branch(self):
        with patch.object(sys.modules["release_exchange"], "require_accepted_archive", side_effect=ValueError("not accepted")):
            with self.assertRaisesRegex(ValueError, "not accepted"):
                self.publish()
        self.assertEqual(self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/"), "refs/heads/main")

    def test_second_gate_rejection_does_not_push(self):
        with patch.object(sys.modules["release_exchange"], "require_accepted_archive", side_effect=[copy.deepcopy(self.record), ValueError("changed gate")]):
            with self.assertRaisesRegex(ValueError, "changed gate"):
                self.publish()
        self.assertEqual(self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/"), "refs/heads/main")

    def test_absent_package_without_proven_cleanup_is_blocked(self):
        self.git(self.code, "rm", "-r", "requirements-exchange/delivery-one")
        self.git(self.code, "commit", "-m", "Remove package independently")
        self.git(self.code, "push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "принятие точного коммита"):
            self.publish()

    def test_foreign_branch_with_unrelated_changes_is_blocked(self):
        branch = "codex/cleanup-exchange-" + cleanup.digest(self.record)[:24]
        self.git(self.code, "switch", "-c", branch)
        self.git(self.code, "rm", "-r", "requirements-exchange/delivery-one", "unrelated.txt")
        self.git(self.code, "-c", "user.name=Analyst Requirements Exchange", "-c", "user.email=analyst-harness@local.invalid", "commit", "-m", cleanup.MESSAGE)
        self.git(self.code, "push", "origin", branch)
        with self.assertRaisesRegex(ValueError, "за границами"):
            self.publish()

    def test_cleanup_with_unaccepted_parent_is_blocked(self):
        branch = "codex/cleanup-exchange-" + cleanup.digest(self.record)[:24]
        self.git(self.code, "switch", "-c", branch)
        (self.code / "unrelated.txt").write_text("Unaccepted change\n")
        self.git(self.code, "add", "unrelated.txt")
        self.git(self.code, "commit", "-m", "Change unrelated behavior")
        self.git(self.code, "rm", "-r", "requirements-exchange/delivery-one")
        self.git(self.code, "-c", "user.name=Analyst Requirements Exchange", "-c", "user.email=analyst-harness@local.invalid", "commit", "-m", cleanup.MESSAGE)
        self.git(self.code, "push", "origin", branch)
        with self.assertRaisesRegex(ValueError, "не принят в целевую ветку"):
            self.publish()
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.original)

    def test_retry_accepts_parent_when_target_has_advanced(self):
        result = self.publish()
        (self.code / "unrelated.txt").write_text("Accepted later change\n")
        self.git(self.code, "add", "unrelated.txt")
        self.git(self.code, "commit", "-m", "Accept unrelated behavior")
        self.git(self.code, "push", "origin", "main")
        self.assertEqual(self.publish(), result)

    def test_reintroduced_package_after_merge_is_blocked(self):
        result = self.publish()
        self.git(self.code, "fetch", "origin", result["request_branch"])
        self.git(self.code, "merge", "--ff-only", "FETCH_HEAD")
        self.git(self.code, "restore", "--source=" + self.original, "--staged", "--worktree", "requirements-exchange/delivery-one")
        self.git(self.code, "commit", "-m", "Restore package")
        self.git(self.code, "push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "восстановлен"):
            self.publish()

    def test_push_reports_failure_after_success_is_reconciled(self):
        original_git = cleanup._git

        def transport(root, *args, **kwargs):
            result = original_git(root, *args, **kwargs)
            if args[0] == "push":
                return subprocess.CompletedProcess(result.args, 1, result.stdout, b"connection lost")
            return result

        with patch.object(cleanup, "_git", side_effect=transport):
            result = self.publish()
        self.assertEqual(result["status"], "awaiting-merge")
        self.assertEqual(self.git(self.remote, "rev-parse", result["request_branch"]), result["request_commit"])

    def test_rejected_push_never_falls_back(self):
        original_git = cleanup._git

        def transport(root, *args, **kwargs):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, b"", b"rejected")
            return original_git(root, *args, **kwargs)

        with patch.object(cleanup, "_git", side_effect=transport):
            with self.assertRaisesRegex(ValueError, "Публикация очистки не подтверждена"):
                self.publish()
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.original)
        self.assertTrue(self.package.exists())

    def test_symlink_package_rejected(self):
        self.git(self.code, "rm", "-r", "requirements-exchange/delivery-one")
        self.package.symlink_to("../unrelated.txt")
        self.git(self.code, "add", "requirements-exchange/delivery-one")
        self.git(self.code, "commit", "-m", "Replace package by link")
        self.git(self.code, "push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "без ссылок"):
            self.publish()


if __name__ == "__main__":
    unittest.main()
