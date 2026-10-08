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
sys.path.insert(0, str(ROOT / "scripts"))
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

    def test_server_length_limit_accepts_short_branch_for_long_delivery_key(self) -> None:
        key = "pv-approval-history-" + "a" * 60
        self.assertEqual(len(key), 80)
        feature = self.project / "features" / key
        feature.mkdir()
        (feature / "requirements.md").write_text(requirements().replace("`demo`", f"`{key}`"))
        exchange_fixture.register_stage(self.project, key)
        self.fixture.authorize(self.project, key)
        hook = self.remote / "hooks/pre-receive"
        hook.write_text('''#!/bin/sh
while read old new ref; do
  name=${ref#refs/heads/}
  case "$name" in requirements/*) ;; *) echo "review branch required" >&2; exit 1 ;; esac
  if [ "${#name}" -gt 48 ]; then echo "branch name too long" >&2; exit 1; fi
done
''')
        hook.chmod(0o755)
        initial = self.fixture.git(self.remote, "rev-parse", "main")
        before_index = (self.code / ".git/index").read_bytes()
        first = self.fixture.command("prepare", str(self.project), key, "--code-root", str(self.code), "--analyst", "ivan")
        self.assertEqual(first["destination_role"], "code")
        self.assertEqual(first["status"], "awaiting-merge")
        self.assertFalse(first["publication_confirmed"])
        self.assertFalse(first["target_branch_push_allowed"])
        self.assertLessEqual(len(first["request_branch"].encode()), 48)
        self.assertEqual(self.fixture.git(self.remote, "rev-parse", "main"), initial)
        self.assertEqual((self.code / ".git/index").read_bytes(), before_index)
        self.assertFalse((self.project / "requirements-exchange").exists())
        cached = json.loads(Path(first["manifest"]).read_text())
        self.assertEqual(cached["publication_request"]["feature"], key)
        self.assertEqual(cached["publication_request"]["input_sha256"], cached["revisions"][0]["sha256"])
        self.assertEqual(len(cached["publication_request"]["identity_sha256"]), 64)
        repeated = self.fixture.command("prepare", str(self.project), key, "--code-root", str(self.code), "--analyst", "ivan")
        self.assertEqual(first["request_commit"], repeated["request_commit"])

    def test_shared_short_prefix_keeps_full_delivery_identity(self) -> None:
        names = []
        for key in ("pv-approval-history-alpha", "pv-approval-history-beta"):
            feature = self.project / "features" / key
            feature.mkdir()
            (feature / "requirements.md").write_text(requirements().replace("`demo`", f"`{key}`"))
            exchange_fixture.register_stage(self.project, key)
            self.fixture.authorize(self.project, key)
            result = self.fixture.command("prepare", str(self.project), key, "--code-root", str(self.code), "--analyst", "ivan")
            names.append(result["request_branch"])
            self.assertEqual(json.loads(Path(result["manifest"]).read_text())["feature"], key)
        self.assertEqual(names[0].rsplit("/", 1)[0], names[1].rsplit("/", 1)[0])
        self.assertNotEqual(names[0], names[1])

    def test_short_alias_collision_never_reuses_another_input(self) -> None:
        first = self.prepare()
        project, feature = self.fixture.prepare_project(self.root / "another-machine")
        (feature / "requirements.md").write_text(requirements("Система должна показать другой результат."))
        self.fixture.authorize(project)
        module = self.load_module()
        with patch.object(module, "request_branch_name", return_value=first["request_branch"]):
            with self.assertRaisesRegex(ValueError, "не совпадает с подтверждённым входом"):
                module.publish_to_code(project, self.code, "demo", (feature / "requirements.md").read_bytes(), (feature / "requirements.md").read_text(), "ivan")
        self.assertEqual(self.fixture.git(self.remote, "rev-parse", first["request_branch"]), first["request_commit"])

    def test_shared_prefix_branch_with_inherited_manifest_does_not_block_next_revision(self) -> None:
        keys = ("pv-approval-history-alpha", "pv-approval-history-beta")
        for key in keys:
            feature = self.project / "features" / key
            feature.mkdir()
            (feature / "requirements.md").write_text(requirements().replace("`demo`", f"`{key}`"))
            exchange_fixture.register_stage(self.project, key)
            self.fixture.authorize(self.project, key)
        first = self.fixture.command("prepare", str(self.project), keys[0], "--code-root", str(self.code), "--analyst", "ivan")
        self.merge(first["request_branch"])
        accepted = self.fixture.command("prepare", str(self.project), keys[0], "--code-root", str(self.code), "--analyst", "ivan")
        self.fixture.command("prepare", str(self.project), keys[1], "--code-root", str(self.code), "--analyst", "ivan")
        feature = self.project / "features" / keys[0]
        (feature / "requirements.md").write_text(requirements("Система должна показать другой результат.").replace("`demo`", f"`{keys[0]}`"))
        self.fixture.authorize(self.project, keys[0])
        following = self.fixture.command("prepare", str(self.project), keys[0], "--code-root", str(self.code), "--analyst", "ivan")
        self.assertEqual(following["revision"], accepted["revision"] + 1)
        self.assertEqual(following["status"], "awaiting-merge")
        self.assertNotEqual(first["request_branch"], following["request_branch"])

    def legacy_request(self, *, keep_short=False):
        first = self.prepare()
        module = self.load_module()
        target_key = module.hashlib.sha256(b"main").hexdigest()[:12]
        entry = json.loads(Path(first["manifest"]).read_text())["revisions"][0]
        name = f"requirements/demo/{target_key}-{entry['sha256']}"
        checkout = self.root / "legacy-code"
        self.fixture.git(self.root, "clone", "--quiet", str(self.remote), str(checkout))
        self.fixture.git(checkout, "config", "user.name", "Legacy receiver")
        self.fixture.git(checkout, "config", "user.email", "legacy@example.invalid")
        self.fixture.git(checkout, "switch", "--detach", "origin/" + first["request_branch"])
        path = checkout / "requirements-exchange/demo/manifest.json"
        value = json.loads(path.read_text())
        value.pop("publication_request")
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        self.fixture.git(checkout, "add", "--", "requirements-exchange/demo/manifest.json")
        self.fixture.git(checkout, "commit", "-m", "Preserve legacy publication format")
        self.fixture.git(checkout, "push", "origin", "HEAD:refs/heads/" + name)
        if not keep_short:
            self.fixture.git(checkout, "push", "origin", "--delete", first["request_branch"])
        return first, name, self.fixture.git(checkout, "rev-parse", "HEAD")

    def test_existing_long_branch_is_resumed_and_accepted_without_duplicate(self) -> None:
        first, name, commit = self.legacy_request()
        repeated = self.prepare()
        self.assertEqual(repeated["request_branch"], name)
        self.assertEqual(repeated["request_commit"], commit)
        self.assertEqual(repeated["revision"], first["revision"])
        self.merge(name)
        self.assertTrue(self.prepare()["publication_confirmed"])
        branches = self.fixture.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/requirements/").splitlines()
        self.assertEqual(branches, ["refs/heads/" + name])

    def test_duplicate_legacy_and_short_pending_requests_require_review(self) -> None:
        self.legacy_request(keep_short=True)
        result = run(sys.executable, str(SCRIPT), "prepare", str(self.project), "demo", "--code-root", str(self.code), "--analyst", "ivan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("несколько незавершённых веток", result.stdout + result.stderr)

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
