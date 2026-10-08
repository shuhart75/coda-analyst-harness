"""SDD audit, immutable exchange revisions, and isolated publication integration."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_requirements_exchange as legacy
import test_sdd_bundle as fixtures
import test_release_exchange as release_fixtures

sys.path.insert(0, str(legacy.ROOT / "scripts"))
import layoutctl
import project_layout
import sdd_bundle

_SPEC = importlib.util.spec_from_file_location("sdd_exchange_integration", legacy.SCRIPT)
exchange_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(exchange_module)


class SddExchangeTests(unittest.TestCase):
    prepare_project = legacy.RequirementsExchangeTests.prepare_project
    prepare_code_repository = legacy.RequirementsExchangeTests.prepare_code_repository
    git = legacy.RequirementsExchangeTests.git
    write_receipt = legacy.RequirementsExchangeTests.write_receipt

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.project, self.feature = self.prepare_project(self.parent)
        self.remote, self.code = self.prepare_code_repository(self.parent, with_exchange=False)
        self.git(self.code, "config", "user.name", "SDD tests")
        self.git(self.code, "config", "user.email", "sdd@example.invalid")
        config = self.code / "backend/openspec/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("schema: spec-driven\n")
        (self.code / "source.py").write_text("print('result')\n")
        self.git(self.code, "add", "--all")
        self.git(self.code, "commit", "-m", "Add SDD source fixture")
        self.git(self.code, "push", "origin", "main")
        self.runtime = self.parent / "state"
        self.runtime.mkdir()
        self.registry = self.runtime / "code-repos.json"
        self.registry.write_text(json.dumps({"repositories": [{"id": "code", "location": {"environment": "SDD_TEST_CODE_ROOT"},
                                                             "requirements_exchange": {"target_branch": "main"}}]}))
        self.environment = {**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(self.runtime),
                            "CODA_ANALYST_STATE_ROOT": str(self.runtime), "SDD_TEST_CODE_ROOT": str(self.code)}
        self.bundle()

    def bundle(self):
        self.sdd = self.feature / "sdd"
        if self.sdd.exists():
            shutil.rmtree(self.sdd)
        self.package = fixtures.build_bundle(self.sdd, (self.feature / "requirements.md").read_text(), self.code)

    def cli(self, script, *args, success=True):
        result = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, env=self.environment)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def state(self, action, *args, success=True):
        return self.cli(legacy.STATE_SCRIPT, action, self.project, "demo", *args, success=success)

    def begin(self):
        return self.state("begin-preparation", "--input-profile", sdd_bundle.PROFILE)

    def audit(self, success=True):
        return self.state("record-audit", "--finding-count", "0", "--blocking-finding-count", "0", "--summary", "Вход проверен", success=success)

    def authorize(self):
        self.begin()
        self.audit()
        self.state("confirm-audit")

    def prepare(self, success=True):
        return self.cli(legacy.SCRIPT, "prepare", self.project, "demo", "--analyst", "ivan", success=success)

    def manifest(self, result):
        return json.loads(Path(result["manifest"]).read_text())

    def validate(self, success=True):
        return self.cli(legacy.SCRIPT, "validate", self.project / "requirements-exchange", success=success)

    def change_proposal(self):
        path = self.sdd / "backend/show-result/proposal.md"
        path.write_text(path.read_text().replace("Need results.", "Need traceable results."))

    def enable_code_exchange(self):
        root = self.code / "requirements-exchange"
        root.mkdir()
        (root / "README.md").write_text("# Каталог обмена\n")
        self.git(self.code, "add", "--", "requirements-exchange/README.md")
        self.git(self.code, "commit", "-m", "Add exchange catalog")
        self.git(self.code, "push", "origin", "main")

    def code_snapshot(self):
        return self.git(self.code, "rev-parse", "HEAD"), self.git(self.code, "status", "--porcelain"), self.git(self.code, "branch", "--show-current")

    def accept(self, result):
        clone = self.parent / ("merge-" + str(result["revision"]))
        subprocess.run(["git", "clone", "--quiet", str(self.remote), str(clone)], check=True, capture_output=True)
        self.git(clone, "merge", "--ff-only", "origin/" + result["request_branch"])
        self.git(clone, "push", "origin", "main")

    def migrate_delivery(self):
        self.git(self.project, "init", "-b", "feature/sdd-fixture")
        self.git(self.project, "config", "user.name", "SDD tests")
        self.git(self.project, "config", "user.email", "sdd@example.invalid")
        self.git(self.project, "add", "--all")
        self.git(self.project, "commit", "-m", "Preserve delivery fixture")
        assignments = {"demo": {"feature_id": "demo", "delivery_id": "first", "quarter": "2026-Q4"}}
        layoutctl.apply(self.project, layoutctl.plan(self.project, assignments), True)
        self.feature = project_layout.feature_root(self.project, "demo")
        self.sdd = self.feature / "sdd"

    def test_schema_five_snapshot_and_checksum_bound_receipt(self):
        self.authorize()
        result = self.prepare()
        manifest = self.manifest(result)
        self.assertEqual(manifest["schema_version"], 5)
        self.assertEqual(manifest["sdd_revision_floor"], 1)
        entry = manifest["revisions"][0]
        self.assertEqual(entry["returns_contract_version"], 2)
        self.assertEqual(entry["sdd_input"], sdd_bundle.inspect(self.sdd, (self.feature / "requirements.md").read_text()))
        snapshot = Path(result["requirements"]).parent / "sdd"
        self.assertEqual((snapshot / "package.json").read_bytes(), (self.sdd / "package.json").read_bytes())
        receipt_path = self.write_receipt(result)
        self.assertIn("schema_version", self.validate(success=False))
        receipt = json.loads(receipt_path.read_text())
        receipt.update(schema_version=2, sdd_sha256=entry["sdd_input"]["sha256"])
        receipt_path.write_text(json.dumps(receipt))
        self.validate()
        receipt["sdd_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt))
        self.assertIn("sdd_sha256", self.validate(success=False))

    def test_sdd_only_change_creates_new_revision_and_preserves_history(self):
        self.authorize()
        first = self.prepare()
        root = Path(first["requirements"]).parent
        before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.change_proposal()
        self.authorize()
        second = self.prepare()
        self.assertEqual(second["revision"], 2)
        self.assertEqual(Path(second["requirements"]).read_bytes(), Path(first["requirements"]).read_bytes())
        self.assertEqual(before, {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()})
        entries = self.manifest(second)["revisions"]
        self.assertNotEqual(entries[0]["sdd_input"], entries[1]["sdd_input"])
        self.validate()

    def test_change_after_audit_blocks_confirmation(self):
        self.begin()
        self.audit()
        self.change_proposal()
        self.assertIn("changed after audit", self.state("confirm-audit", success=False))

    def test_removal_after_confirmation_blocks_prepare(self):
        self.authorize()
        shutil.rmtree(self.sdd)
        self.assertIn("absent", self.prepare(success=False))

    def test_additional_file_after_confirmation_blocks_prepare(self):
        self.authorize()
        (self.sdd / "backend/show-result/tasks.md").write_text("unapproved tasks")
        self.assertIn("unexpected bundle files", self.prepare(success=False))

    def test_missing_code_and_stale_sources_block_audit(self):
        self.begin()
        self.registry.write_text(json.dumps({"repositories": []}))
        self.assertIn("registered code repository", self.audit(success=False))
        self.registry.write_text(json.dumps({"repositories": [{"id": "code", "location": {"environment": "SDD_TEST_CODE_ROOT"}}]}))
        (self.code / "source.py").write_text("print('new behavior')\n")
        self.git(self.code, "add", "--", "source.py")
        self.git(self.code, "commit", "-m", "Change source fixture")
        self.assertIn("stale context", self.audit(success=False))

    def test_legacy_then_sdd_upgrade_preserves_old_receipt_and_entry(self):
        shutil.rmtree(self.sdd)
        self.state("begin-preparation")
        self.audit()
        self.state("confirm-audit")
        first = self.prepare()
        old_receipt = self.write_receipt(first)
        receipt_bytes = old_receipt.read_bytes()
        old_entry = self.manifest(first)["revisions"][0]
        self.bundle()
        self.authorize()
        second = self.prepare()
        manifest = self.manifest(second)
        self.assertEqual(manifest["schema_version"], 5)
        self.assertEqual(manifest["sdd_revision_floor"], 2)
        self.assertEqual(old_receipt.read_bytes(), receipt_bytes)
        # Publication may mark the prior state superseded; its identity is immutable.
        preserved = manifest["revisions"][0]
        self.assertEqual({k: v for k, v in old_entry.items() if k != "state"}, {k: v for k, v in preserved.items() if k != "state"})
        self.assertNotIn("sdd_input", preserved)
        self.assertEqual(preserved["returns_contract_version"], 1)
        self.validate()

    def test_profile_downgrade_is_blocked(self):
        self.begin()
        self.assertIn("cannot downgrade", self.state("begin-preparation", "--input-profile", "requirements-only-v1", success=False))

    def test_quarter_delivery_binding_survives_schema_five(self):
        self.migrate_delivery()
        self.authorize()
        result = self.prepare()
        manifest = self.manifest(result)
        self.assertEqual(manifest["schema_version"], 5)
        expected = {"feature_id": "demo", "delivery_id": "first", "quarter": "2026-Q4", "delivery_key": "demo"}
        self.assertEqual(manifest["delivery_binding"], expected)
        self.assertEqual(manifest["revisions"][0]["delivery_binding"], expected)
        self.validate()

    def test_schema_four_upgrade_preserves_delivery_and_historical_receipt(self):
        shutil.rmtree(self.sdd)
        self.migrate_delivery()
        self.state("begin-preparation")
        self.audit()
        self.state("confirm-audit")
        first = self.prepare()
        manifest = self.manifest(first)
        self.assertEqual(manifest["schema_version"], 4)
        receipt = self.write_receipt(first)
        original_receipt = receipt.read_bytes()
        binding = manifest["delivery_binding"]
        self.bundle()
        self.authorize()
        second = self.prepare()
        upgraded = self.manifest(second)
        self.assertEqual(upgraded["schema_version"], 5)
        self.assertEqual(upgraded["sdd_revision_floor"], 2)
        self.assertEqual(upgraded["delivery_binding"], binding)
        self.assertEqual(upgraded["revisions"][0]["delivery_binding"], binding)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertNotIn("sdd_input", upgraded["revisions"][0])
        self.validate()

    def test_failed_snapshot_copy_leaves_no_partial_revision_and_retry_succeeds(self):
        self.authorize()
        first = self.prepare()
        original_manifest = Path(first["manifest"]).read_bytes()
        self.change_proposal()
        self.authorize()
        text = (self.feature / "requirements.md").read_text()
        with patch.dict(os.environ, self.environment), patch.object(exchange_module.sdd_bundle, "copy_snapshot", side_effect=ValueError("fixture copy failure")):
            with self.assertRaisesRegex(ValueError, "fixture copy failure"):
                exchange_module.prepare_in_exchange(self.project / "requirements-exchange", self.project, "demo", text.encode(), text, "ivan")
        self.assertEqual(Path(first["manifest"]).read_bytes(), original_manifest)
        self.assertFalse((self.project / "requirements-exchange/demo/revisions/002").exists())
        self.assertEqual(self.prepare()["revision"], 2)

    def test_mark_published_rejects_tampered_sdd_snapshot(self):
        self.authorize()
        prepared = self.prepare()
        proposal = Path(prepared["requirements"]).parent / "sdd/backend/show-result/proposal.md"
        proposal.write_text(proposal.read_text().replace("Need results.", "Unreviewed result."))
        error = self.state("mark-published", "--manifest", prepared["manifest"], "--revision", "1", "--destination-role", "analytics", success=False)
        self.assertIn("SDD", error)

    def test_status_requires_origin_after_sdd_only_change(self):
        self.authorize()
        prepared = self.prepare()
        self.state("mark-published", "--manifest", prepared["manifest"], "--revision", "1", "--destination-role", "analytics")
        self.change_proposal()
        response = self.state("status", success=False)
        self.assertEqual(json.loads(response)["next_action"], "record-requirements-change")
        self.state("record-change", "--origin", "analyst")
        self.assertEqual(self.state("status")["next_action"], "offer-new-revision-once")

    def test_sdd_return_release_archive_and_accepted_cleanup_roundtrip(self):
        self.authorize()
        cycle = release_fixtures.ReleaseExchangeTests()
        self.addCleanup(cycle.doCleanups)
        cycle.prepare_project = lambda root: (self.project, self.feature)
        cycle.prepare = lambda project: self.prepare()

        def receipt(prepared):
            path = self.write_receipt(prepared)
            value = json.loads(path.read_text())
            value.update(schema_version=2, sdd_sha256=self.manifest(prepared)["revisions"][0]["sdd_input"]["sha256"])
            path.write_text(json.dumps(value))
            return path

        cycle.write_receipt = receipt
        with patch.dict(os.environ, self.environment):
            cycle.setUp()
            original = cycle.files(cycle.packet)
            cycle.test_accepted_cleanup_preserves_shared_files_archive_and_review_lookup()
            self.assertEqual(cycle.files(cycle.archive), original)
            self.assertIn("revisions/001/sdd/backend/show-result/proposal.md", original)
            self.assertEqual(json.loads(original["revisions/001/returns/receipt.json"])["schema_version"], 2)
            self.assertFalse(cycle.packet.exists())

    def test_close_stage_rejects_unrecorded_sdd_only_change(self):
        self.authorize()
        prepared = self.prepare()
        self.state("mark-published", "--manifest", prepared["manifest"], "--revision", "1", "--destination-role", "analytics")
        before = (self.feature / "requirements-state.json").read_bytes()
        self.change_proposal()
        error = self.state("close-stage", "--return-id", "demo:001:revisions/001/returns/summary.md:fixture",
                           "--note", "Завершить этап", "--analyst-confirmed", success=False)
        self.assertIn("незарегистрированного изменения SDD", error)
        self.assertEqual((self.feature / "requirements-state.json").read_bytes(), before)

    def test_code_publication_retry_merge_and_sdd_only_revision(self):
        self.enable_code_exchange()
        before = self.code_snapshot()
        self.authorize()
        first = self.prepare()
        self.assertEqual(first["status"], "awaiting-merge")
        self.assertFalse(first["publication_confirmed"])
        remote_file = self.git(self.remote, "show", first["request_commit"] + ":requirements-exchange/demo/revisions/001/sdd/backend/show-result/proposal.md")
        self.assertEqual(remote_file, (self.sdd / "backend/show-result/proposal.md").read_text().strip())
        retry = self.prepare()
        self.assertEqual(retry["request_commit"], first["request_commit"])
        self.assertEqual(self.code_snapshot(), before)
        self.accept(first)
        accepted = self.prepare()
        self.assertTrue(accepted["publication_confirmed"])
        self.state("mark-published", "--manifest", accepted["manifest"], "--revision", str(accepted["revision"]), "--destination-role", "code")
        self.change_proposal()
        self.authorize()
        second = self.prepare()
        self.assertEqual(second["revision"], 2)
        self.assertNotEqual(second["request_branch"], first["request_branch"])
        self.assertEqual(self.code_snapshot(), before)

    def test_pending_code_publication_blocks_changed_sdd(self):
        self.enable_code_exchange()
        self.authorize()
        first = self.prepare()
        self.change_proposal()
        self.authorize()
        error = self.prepare(success=False)
        self.assertTrue("неподтвержд" in error or "незавершён" in error, error)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/" + first["request_branch"]), first["request_commit"])


if __name__ == "__main__":
    unittest.main()
