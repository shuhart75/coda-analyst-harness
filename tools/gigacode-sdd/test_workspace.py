import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import workspace as ws


class WorkspacesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        ws.git(self.repo, "init", "-q")
        ws.git(self.repo, "config", "user.name", "Tester")
        ws.git(self.repo, "config", "user.email", "tester@localhost")
        (self.repo / "backend").mkdir()
        (self.repo / "backend/a.txt").write_text("original\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("node_modules/\n.gigacode/\n", encoding="utf-8")
        ws.git(self.repo, "add", "--", ".gitignore", "backend/a.txt")
        ws.git(self.repo, "commit", "-qm", "initial")
        self.base = ws.git(self.repo, "rev-parse", "HEAD")

    def test_clone_preserves_source_and_has_no_remote(self):
        (self.repo / "backend/a.txt").write_text("staged\n", encoding="utf-8")
        ws.git(self.repo, "add", "--", "backend/a.txt")
        (self.repo / "backend/a.txt").write_text("working\n", encoding="utf-8")
        (self.repo / "backend/new.txt").write_text("new\n", encoding="utf-8")
        (self.repo / ".env").write_text("SECRET=never-copy", encoding="utf-8")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/cache").write_text("big", encoding="utf-8")
        before = ws.snapshot(self.repo)
        index = (self.repo / ".git/index").read_bytes()
        dest = self.root / "clone"
        result = ws.clone_snapshot(self.repo, dest, include_dirty=True)
        self.assertEqual((dest / "backend/a.txt").read_text(encoding="utf-8"), "working\n")
        self.assertEqual((dest / "backend/new.txt").read_text(encoding="utf-8"), "new\n")
        self.assertFalse((dest / ".env").exists())
        self.assertIn(".env", result["skipped"])
        self.assertFalse((dest / "node_modules").exists())
        self.assertEqual(ws.git(dest, "remote"), "")
        self.assertEqual(ws.snapshot(self.repo), before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_local_sdd_context_copied_without_settings(self):
        ctx = self.repo / "backend/.gigacode/commands"
        ctx.mkdir(parents=True)
        (ctx / "opsx.md").write_text("SDD instructions", encoding="utf-8")
        (ctx.parent / "settings.json").write_text('{"token":"secret"}', encoding="utf-8")
        (self.repo / "backend/GIGACODE.md").write_text("Read me", encoding="utf-8")
        dest = self.root / "clone"
        ws.clone_snapshot(self.repo, dest)
        self.assertEqual((dest / "backend/GIGACODE.md").read_text(encoding="utf-8"), "Read me")
        self.assertTrue((dest / "backend/.gigacode/commands/opsx.md").exists())
        self.assertFalse((dest / "backend/.gigacode/settings.json").exists())
        self.assertEqual(ws.git(dest, "status", "--porcelain"), "")

    def test_permissions_survive_clone_without_auth(self):
        directory = self.repo / "backend/.gigacode"
        directory.mkdir(parents=True)
        original = {"permissions": {"deny": ["Bash(curl:*)"], "ask": ["Skill"],
                                    "allow": ["Read"], "futureConstraint": "kept"},
                    "mcpServers": {"x": {"token": "SECRET"}}}
        (directory / "settings.json").write_text(json.dumps(original), encoding="utf-8")
        dest = self.root / "clone"
        result = ws.clone_snapshot(self.repo, dest)
        saved = json.loads((dest / "backend/.gigacode/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"permissions": original["permissions"]})
        self.assertEqual(result["permission_settings"], ["backend/.gigacode/settings.json"])
        self.assertNotIn("SECRET", repr(result))
        self.assertEqual(ws.git(dest, "status", "--porcelain"), "")

    def test_custom_hooks_keep_relative_directory_semantics(self):
        hooks = self.repo / ".hooks"
        hooks.mkdir()
        helper = self.repo / "hook-marker"
        helper.write_text("present", encoding="utf-8")
        ws.git(self.repo, "add", "--", "hook-marker")
        ws.git(self.repo, "commit", "-qm", "hook helper")
        hook = hooks / "commit-msg"
        hook.write_text('#!/bin/sh\ntest -f "$(dirname "$0")/../hook-marker" || exit 17\nexit 19\n', encoding="utf-8")
        hook.chmod(0o755)
        ws.git(self.repo, "config", "core.hooksPath", ".hooks")
        dest = self.root / "clone"
        ws.clone_snapshot(self.repo, dest)
        self.assertEqual(ws.git(dest, "config", "core.hooksPath"), ".hooks")
        (dest / "backend/a.txt").write_text("modified", encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            ws.commit_changes(dest, "test rejected by hook")

    def test_binary_and_new_files_patch_without_touching_index(self):
        (self.repo / "backend/blob").write_bytes(b"\x00\xff\x01")
        (self.repo / "backend/a.txt").write_text("changed\n", encoding="utf-8")
        index = (self.repo / ".git/index").read_bytes()
        patch = self.root / "patch"
        result = ws.export_patch(self.repo, self.base, ["backend"], patch)
        self.assertEqual(result["paths"], ["backend/a.txt", "backend/blob"])
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        dest = self.root / "clone"
        ws.clone_snapshot(self.repo, dest)
        ws.apply_patch(dest, patch)
        self.assertEqual((dest / "backend/blob").read_bytes(), b"\x00\xff\x01")
        self.assertEqual((dest / "backend/a.txt").read_text(encoding="utf-8"), "changed\n")

    def test_conflict_does_not_change_target(self):
        dest = self.root / "clone"
        ws.clone_snapshot(self.repo, dest)
        (self.repo / "backend/a.txt").write_text("worker\n", encoding="utf-8")
        patch = self.root / "patch"
        ws.export_patch(self.repo, self.base, ["backend"], patch)
        (dest / "backend/a.txt").write_text("other work\n", encoding="utf-8")
        before = ws.snapshot(dest)
        with self.assertRaises(ws.WorkspaceError):
            ws.apply_patch(dest, patch)
        self.assertEqual(ws.snapshot(dest), before)

    def test_ownership_rejected(self):
        (self.repo / "frontend").mkdir()
        (self.repo / "frontend/a.txt").write_text("wrong owner", encoding="utf-8")
        with self.assertRaisesRegex(ws.WorkspaceError, "ownership"):
            ws.export_patch(self.repo, self.base, ["backend"], self.root / "patch")

    def test_protected_packet_rejected(self):
        p = self.repo / "requirements-exchange/feature/revisions/003/requirements.md"
        p.parent.mkdir(parents=True)
        p.write_text("rewrite contract", encoding="utf-8")
        with self.assertRaisesRegex(ws.WorkspaceError, "Protected"):
            ws.export_patch(self.repo, self.base, ["requirements-exchange"], self.root / "patch")

    def test_returns_allowed(self):
        p = self.repo / "requirements-exchange/feature/revisions/003/returns/summary.md"
        p.parent.mkdir(parents=True)
        p.write_text("result", encoding="utf-8")
        ws.export_patch(self.repo, self.base, ["requirements-exchange"], self.root / "patch")

    def test_worker_commit_rejected(self):
        (self.repo / "backend/a.txt").write_text("changed", encoding="utf-8")
        ws.commit_changes(self.repo, "RSCON-123: implementation")
        with self.assertRaisesRegex(ws.WorkspaceError, "HEAD"):
            ws.export_patch(self.repo, self.base, ["backend"], self.root / "patch")

    def test_snapshot_detects_uncommitted_reviewer_changes(self):
        before = ws.snapshot(self.repo)
        (self.repo / "backend/a.txt").write_text("reviewer edit", encoding="utf-8")
        after = ws.snapshot(self.repo)
        self.assertEqual(before["head"], after["head"])
        self.assertNotEqual(before, after)

    def test_secret_snapshot_does_not_contain_contents(self):
        (self.repo / ".env").write_text("SECRET=never-read", encoding="utf-8")
        value = ws.snapshot(self.repo)
        self.assertTrue(value["files"][".env"]["redacted"])
        self.assertNotIn("never-read", repr(value))

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_escaping_symlink_rejected(self):
        try:
            (self.repo / "backend/escape").symlink_to("../../outside")
        except OSError:
            if os.name != 'nt':
                raise
            self.skipTest('Symlink creation requires Windows developer mode or privilege')
        with self.assertRaisesRegex(ws.WorkspaceError, "escapes"):
            ws.snapshot(self.repo)

    def test_embedded_repository_rejected(self):
        sub = self.repo / "backend/nested"
        sub.mkdir()
        ws.git(sub, "init", "-q")
        (sub / "x").write_text("x", encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            ws.snapshot(self.repo)

    def test_commit_runs_hook(self):
        hook = self.repo / ".git/hooks/commit-msg"
        hook.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.repo / "backend/a.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            ws.commit_changes(self.repo, "RSCON-123: rejected")
        self.assertEqual(ws.git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_commit_hook_receives_complete_nexus_environment(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text('#!/bin/sh\ntest "$SDD_NEXUS_TEST" = yes || exit 27\ntest -z "$SDD_PUBLIC_FALLBACK" || exit 28\n', encoding="utf-8")
        hook.chmod(0o755)
        (self.repo / "backend/a.txt").write_text("changed", encoding="utf-8")
        os.environ["SDD_PUBLIC_FALLBACK"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "SDD_PUBLIC_FALLBACK", None)
        environment = {k: v for k, v in os.environ.items() if k != "SDD_PUBLIC_FALLBACK"}
        environment["SDD_NEXUS_TEST"] = "yes"
        ws.commit_changes(self.repo, "TEST-1 Nexus-aware hook", env=environment)
        self.assertNotEqual(ws.git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_destination_must_not_exist(self):
        dest = self.root / "existing"
        dest.mkdir()
        (dest / "keep").write_text("keep", encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            ws.clone_snapshot(self.repo, dest)
        self.assertEqual((dest / "keep").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
