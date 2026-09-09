from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_requirements_exchange as exchange_fixture


ROOT = exchange_fixture.ROOT
SCRIPT = exchange_fixture.SCRIPT
STATE_SCRIPT = exchange_fixture.STATE_SCRIPT
requirements = exchange_fixture.requirements
run = exchange_fixture.run


class ReviewedPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "ANALYST_HARNESS_STATE_ROOT": str(self.root / "state"),
            "CODA_ANALYST_STATE_ROOT": str(self.root / "state"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.fixture = exchange_fixture.RequirementsExchangeTests()
        self.project, self.feature = self.fixture.prepare_project(self.root)
        self.remote, self.code = self.fixture.prepare_code_repository(self.root)

    def prepare(self, *extra: str) -> dict:
        return self.fixture.prepare(self.project, "--code-root", str(self.code), *extra)

    def merge(self, branch: str, *, squash: bool = False) -> None:
        checkout = self.root / "receiver"
        result = run("git", "clone", "--quiet", str(self.remote), str(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.fixture.git(checkout, "config", "user.name", "Receiver")
        self.fixture.git(checkout, "config", "user.email", "receiver@example.test")
        if squash:
            self.fixture.git(checkout, "merge", "--squash", f"origin/{branch}")
            self.fixture.git(checkout, "commit", "-m", "Accept requirements")
        else:
            self.fixture.git(checkout, "merge", "--no-ff", f"origin/{branch}", "-m", "Accept requirements")
        self.fixture.git(checkout, "push", "origin", "main")

    def test_repeat_reuses_remote_branch_and_acceptance_requires_merge(self) -> None:
        initial = self.fixture.git(self.remote, "rev-parse", "main")
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first["request_branch"], second["request_branch"])
        self.assertEqual(first["request_commit"], second["request_commit"])
        self.assertEqual(self.fixture.git(self.remote, "rev-parse", "main"), initial)
        mark = (sys.executable, str(STATE_SCRIPT), "mark-published", str(self.project), "demo", "--revision", "1", "--destination-role", "code", "--manifest", first["manifest"])
        blocked = run(*mark)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("PR/MR", blocked.stdout)
        self.merge(first["request_branch"])
        merged = self.prepare()
        self.assertTrue(merged["publication_confirmed"])
        self.assertEqual(merged["repository_branch"], "main")
        self.assertEqual(run(*mark).returncode, 0)
        self.assertEqual(self.fixture.git(self.code, "rev-parse", "HEAD"), initial)
        self.assertFalse((self.code / "requirements-exchange/demo").exists())

    def test_squash_merge_recognizes_exact_revision(self) -> None:
        first = self.prepare()
        self.merge(first["request_branch"], squash=True)
        self.assertTrue(self.prepare()["publication_confirmed"])
        (self.feature / "requirements.md").write_text(requirements("Система должна показать другой результат."), encoding="utf-8")
        next_revision = self.prepare()
        self.assertEqual(next_revision["revision"], 2)
        self.assertEqual(next_revision["status"], "awaiting-merge")

    def load_module(self):
        with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
            spec = importlib.util.spec_from_file_location("review_publication", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_lost_push_response_reuses_published_request(self) -> None:
        self.fixture.authorize(self.project)
        module = self.load_module()
        original = module.git
        def lose_response(repository: Path, *arguments: str):
            result = original(repository, *arguments)
            if arguments[0] == "push" and result.returncode == 0:
                return subprocess.CompletedProcess(result.args, 1, "", "Connection closed after push")
            return result
        with patch.object(module, "git", side_effect=lose_response):
            first = module.publish_to_code(self.project, self.code, "demo", (self.feature / "requirements.md").read_bytes(), requirements(), "ivan")
        second = self.prepare()
        self.assertEqual(first["request_commit"], second["request_commit"])
        self.assertFalse(second["publication_confirmed"])
        self.assertFalse((self.project / "requirements-exchange").exists())

    def test_rejected_push_can_be_retried_without_target_change(self) -> None:
        self.fixture.authorize(self.project)
        module = self.load_module()
        original = module.git
        initial = self.fixture.git(self.remote, "rev-parse", "main")
        def reject_push(repository: Path, *arguments: str):
            if arguments[0] == "push":
                return subprocess.CompletedProcess(arguments, 1, "", "Push rejected")
            return original(repository, *arguments)
        with patch.object(module, "git", side_effect=reject_push), self.assertRaises(module.CodeDestinationUnavailable):
            module.publish_to_code(self.project, self.code, "demo", (self.feature / "requirements.md").read_bytes(), requirements(), "ivan")
        self.assertEqual(self.fixture.git(self.remote, "rev-parse", "main"), initial)
        self.assertEqual(self.prepare()["status"], "awaiting-merge")

    def test_unknown_push_outcome_blocks_fallback(self) -> None:
        module = self.load_module()
        original = module.git
        reads = module.remote_heads
        def reject_push(repository: Path, *arguments: str):
            if arguments[0] == "push":
                return subprocess.CompletedProcess(arguments, 1, "", "Connection lost")
            return original(repository, *arguments)
        def unavailable_after_push(remote: str, pattern: str):
            if not pattern.endswith("*"):
                raise ValueError("Remote outcome unknown")
            return reads(remote, pattern)
        self.fixture.authorize(self.project)
        args = module.parser().parse_args(["prepare", str(self.project), "demo", "--code-root", str(self.code), "--analyst", "ivan"])
        with patch.object(module, "git", side_effect=reject_push), patch.object(module, "remote_heads", side_effect=unavailable_after_push), self.assertRaisesRegex(ValueError, "outcome unknown"):
            module.prepare_command(args)
        self.assertFalse((self.project / "requirements-exchange").exists())

    def test_target_is_not_inferred_from_local_checkout_branch(self) -> None:
        self.fixture.git(self.code, "switch", "-c", "local-inspection")
        self.assertEqual(self.prepare()["target_branch"], "main")
        self.assertEqual(self.fixture.git(self.code, "branch", "--show-current"), "local-inspection")

    def test_exchange_symlink_cannot_write_outside_temporary_clone(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        seed = self.root / "code-seed"
        (seed / "requirements-exchange/demo").symlink_to(outside, target_is_directory=True)
        self.fixture.git(seed, "add", "--", "requirements-exchange/demo")
        self.fixture.git(seed, "commit", "-m", "Add unsafe exchange link")
        self.fixture.git(seed, "push", "origin", "main")
        self.fixture.authorize(self.project)
        result = run(sys.executable, str(SCRIPT), "prepare", str(self.project), "demo", "--code-root", str(self.code), "--analyst", "ivan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("символические ссылки", result.stdout)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.project / "requirements-exchange").exists())

    def test_explicit_target_and_unrelated_target_update_preserve_pending_request(self) -> None:
        self.fixture.git(self.code, "push", str(self.remote), "HEAD:refs/heads/develop")
        first = self.prepare("--target-branch", "develop")
        self.assertEqual(first["target_branch"], "develop")
        seed = self.root / "code-seed"
        (seed / "README.md").write_text("# Unrelated code documentation\n", encoding="utf-8")
        self.fixture.git(seed, "add", "--", "README.md")
        self.fixture.git(seed, "commit", "-m", "Add code documentation")
        self.fixture.git(seed, "push", "origin", "HEAD:develop")
        repeated = self.prepare("--target-branch", "develop")
        self.assertEqual(first["request_commit"], repeated["request_commit"])
        self.assertFalse(repeated["publication_confirmed"])

    def test_new_input_does_not_overwrite_pending_request(self) -> None:
        first = self.prepare()
        (self.feature / "requirements.md").write_text(requirements("Система должна показать другой результат."), encoding="utf-8")
        self.fixture.authorize(self.project)
        result = run(sys.executable, str(SCRIPT), "prepare", str(self.project), "demo", "--code-root", str(self.code), "--analyst", "ivan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("незавершённая передача", result.stdout)
        self.assertFalse((self.project / "requirements-exchange").exists())
        self.assertEqual(self.fixture.git(self.remote, "rev-parse", first["request_branch"]), first["request_commit"])

    def test_changed_input_during_guard_is_not_published(self) -> None:
        self.fixture.authorize(self.project)
        with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
            spec = importlib.util.spec_from_file_location("review_publication", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        original = module.run_requirement_guards
        def change_after_checks(project: Path, feature: str) -> None:
            original(project, feature)
            (self.feature / "requirements.md").write_text(requirements("Система должна показать изменённый результат."), encoding="utf-8")
        args = module.parser().parse_args(["prepare", str(self.project), "demo", "--code-root", str(self.code), "--analyst", "ivan"])
        with patch.object(module, "run_requirement_guards", side_effect=change_after_checks), self.assertRaisesRegex(ValueError, "изменились после проверки"), contextlib.redirect_stdout(io.StringIO()):
            module.prepare_command(args)
        self.assertEqual(self.fixture.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/requirements/"), "")


if __name__ == "__main__":
    unittest.main()
