"""Receiver-only import: integrity, dry run, collisions, recovery and repeat."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sdd_bundle

spec = importlib.util.spec_from_file_location("sdd_receive", ROOT / "scripts/sdd-receive.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class SddReceiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.code = self.root / "code"
        self.code.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "receiver@example.invalid")
        self.git("config", "user.name", "Receiver tests")
        self.git("remote", "add", "origin", "https://example.invalid/code.git")
        for contour in ("backend", "frontend"):
            root = self.code / contour / "openspec"
            root.mkdir(parents=True)
            (root / "config.yaml").write_text("schema: spec-driven\n")
        (self.code / "service.py").write_text("VALUE = 1\n")
        self.git("add", "backend/openspec/config.yaml", "frontend/openspec/config.yaml", "service.py")
        self.git("commit", "-q", "-m", "Create receiver fixture")
        self.commit = self.git("rev-parse", "HEAD")
        self.packet = self.root / "requirements-exchange" / "demo"
        self.bundle = self.packet / "revisions/001/sdd"
        self.bundle.mkdir(parents=True)
        self.requirements = "# Требования\n\n### REQ-DEMO. Результат\nПоказать результат.\n"
        (self.bundle.parent / "requirements.md").write_text(self.requirements)
        self.package = {"schema_version": 1, "profile": sdd_bundle.PROFILE,
                        "requirements_sha256": receiver.digest(self.requirements.encode()), "changes": []}
        self.add_change("backend")
        self.save_packet()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.code), *args], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def add_change(self, contour):
        change_id = "show-result"
        change_root = self.bundle / contour / change_id
        (change_root / "specs/results").mkdir(parents=True)
        (change_root / ".openspec.yaml").write_text("schema: spec-driven\ncreated: 2026-10-08\n")
        (change_root / "proposal.md").write_text(
            "## Why\nПоказать результат.\n## What Changes\nРезультат доступен.\n"
            "## Capabilities\n### New Capabilities\n- `results`: Результат\n"
            "### Modified Capabilities\n\n## Impact\nAPI.\n")
        (change_root / "specs/results/spec.md").write_text(
            "## ADDED Requirements\n### Requirement: Show result\nThe system SHALL show result.\n"
            "#### Scenario: Available result\n- **WHEN** result exists\n- **THEN** user sees result\n")
        context = {f"{contour}/openspec/config.yaml": receiver.digest((self.code / contour / "openspec/config.yaml").read_bytes()),
                   "service.py": receiver.digest((self.code / "service.py").read_bytes())}
        self.package["changes"].append({"contour": contour, "change_id": change_id,
            "target_root": contour + "/openspec", "repository": "https://example.invalid/code.git",
            "code_commit": self.commit, "context_files": context, "base_specs": {"results": None},
            "coverage": [{"req_id": "REQ-DEMO", "capability": "results", "requirement": "Show result", "scenarios": ["Available result"]}]})

    def save_packet(self):
        (self.bundle / "package.json").write_text(json.dumps(self.package))
        self.descriptor = sdd_bundle.inspect(self.bundle, self.requirements)
        stage = {"stage_id": "stage-1", "number": 1, "title": "Результат", "goal": "Показать результат"}
        entry = {"revision": 1, "state": "sent", "requirements_path": "revisions/001/requirements.md",
                 "sha256": receiver.digest(self.requirements.encode()), "returns_contract_version": 2,
                 "stage_id": "stage-1", "stage_revision": 1, "stage": stage,
                 "stage_sha256": receiver.digest(json.dumps(stage, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
                 "sdd_input": self.descriptor}
        self.manifest = {"schema_version": 5, "exchange_kind": "feature-requirements", "feature": "demo",
                         "sdd_revision_floor": 1, "active_revision": 1, "revisions": [entry]}
        self.write_manifest()

    def write_manifest(self):
        (self.packet / "manifest.json").write_text(json.dumps(self.manifest))

    def target(self, contour="backend"):
        return self.code / contour / "openspec/changes/show-result"

    def receive(self, apply=False, revision=1):
        return receiver.receive(self.packet, revision, self.code, apply=apply)

    def test_dry_run_is_default_and_does_not_write_code(self):
        result = self.receive()
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["applied"])
        self.assertFalse(self.target().parent.exists())
        self.assertFalse(self.git("status", "--porcelain"))
        self.assertFalse(result["receipt_created"])

    def test_cli_default_never_imports(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/sdd-receive.py"), "--packet", str(self.packet),
                                 "--revision", "1", "--code-root", str(self.code)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        self.assertFalse(self.target().exists())

    def test_deployed_cli_dry_run_keeps_code_checkout_clean(self):
        exchange = self.code / "requirements-exchange"
        shutil.copytree(self.packet, exchange / "demo")
        for name in ("sdd-receive.py", "sdd_bundle.py"):
            shutil.copyfile(ROOT / "scripts" / name, exchange / name)
        self.git("add", "--", "requirements-exchange")
        self.git("commit", "-q", "-m", "Accept receiver input and helpers")
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run([sys.executable, str(exchange / "sdd-receive.py"),
                                 "--packet", str(exchange / "demo"), "--revision", "1",
                                 "--code-root", str(self.code)], text=True, capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        self.assertFalse((exchange / "__pycache__").exists())
        self.assertFalse(self.target().exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_inactive_states_block_import(self):
        for state in ("paused", "completed", "draft"):
            with self.subTest(state=state):
                self.manifest["revisions"][0]["state"] = state
                self.write_manifest()
                with self.assertRaises(ValueError):
                    self.receive(True)
                self.assertFalse(self.target().exists())

    def test_exact_copy_repeat_preserves_developer_design_and_tasks(self):
        self.assertEqual(self.receive(True)["status"], "imported")
        target = self.target()
        for relative in ("proposal.md", ".openspec.yaml", "specs/results/spec.md"):
            self.assertEqual((target / relative).read_bytes(), (self.bundle / "backend/show-result" / relative).read_bytes())
        (target / "design.md").write_text("Developer design\n")
        (target / "tasks.md").write_text("- [ ] 1.1 Implement\n")
        result = self.receive(True)
        self.assertEqual(result["status"], "already-current")
        self.assertEqual((target / "design.md").read_text(), "Developer design\n")
        self.assertFalse((self.bundle.parent / "returns/receipt.json").exists())

    def test_tampered_bundle_blocks_without_writes(self):
        (self.bundle / "backend/show-result/proposal.md").write_text("tampered")
        with self.assertRaises(ValueError):
            self.receive(True)
        self.assertFalse(self.target().parent.exists())

    def test_repeat_rejects_extra_developer_specification(self):
        self.receive(True)
        extra = self.target() / "specs/unreviewed/spec.md"
        extra.parent.mkdir()
        extra.write_text("Additional behavior\n")
        with self.assertRaisesRegex(ValueError, "дополнительные"):
            self.receive(True)
        self.assertTrue(extra.is_file())

    def test_manifest_delivery_binding_cannot_name_another_packet(self):
        binding = {"feature_id": "demo", "delivery_id": "stage", "quarter": "2026-Q4", "delivery_key": "other"}
        self.manifest["delivery_binding"] = binding
        self.manifest["revisions"][0]["delivery_binding"] = binding
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "идентичность"):
            self.receive(True)
        self.assertFalse(self.target().exists())

    def test_unrelated_existing_change_is_never_adopted(self):
        self.target().mkdir(parents=True)
        (self.target() / "proposal.md").write_text("Existing work\n")
        with self.assertRaisesRegex(ValueError, "точным входом"):
            self.receive(True)
        self.assertEqual((self.target() / "proposal.md").read_text(), "Existing work\n")

    def test_all_targets_are_preflighted_before_any_write(self):
        self.add_change("frontend")
        self.save_packet()
        self.target("frontend").mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.receive(True)
        self.assertFalse(self.target().parent.exists())

    def test_partial_write_rolls_back_only_new_targets(self):
        self.add_change("frontend")
        self.save_packet()
        original_open = Path.open
        def injected(path, mode="r", *args, **kwargs):
            if mode == "xb" and path == self.target("frontend") / "proposal.md":
                raise OSError("Injected copy failure")
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", injected):
            with self.assertRaisesRegex(OSError, "Injected"):
                self.receive(True)
        for contour in ("backend", "frontend"):
            self.assertFalse(self.target(contour).parent.exists())
            self.assertTrue((self.code / contour / "openspec/config.yaml").is_file())

    def test_changed_source_and_wrong_remote_block_import(self):
        (self.code / "service.py").write_text("VALUE = 2\n")
        self.git("add", "service.py")
        self.git("commit", "-q", "-m", "Change source fixture")
        with self.assertRaisesRegex(ValueError, "stale context"):
            self.receive(True)
        self.assertFalse(self.target().exists())
        self.git("remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(ValueError, "origin"):
            self.receive(True)

    def test_unrelated_dirty_worktree_blocks_but_own_developer_files_do_not(self):
        (self.code / "service.py").write_text("VALUE = 2\n")
        with self.assertRaises(ValueError):
            self.receive(True)
        self.assertFalse(self.target().exists())

    def test_target_symlink_is_rejected_without_outside_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.code / "backend/openspec/changes").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Символические"):
            self.receive(True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_revision_does_not_overwrite_old_change(self):
        self.receive(True)
        shutil.copytree(self.bundle.parent, self.packet / "revisions/002")
        second = dict(self.manifest["revisions"][0], revision=2, stage_revision=2, requirements_path="revisions/002/requirements.md")
        self.manifest["revisions"].append(second)
        self.manifest["active_revision"] = 2
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "точным входом"):
            self.receive(True, revision=2)
        binding = json.loads((self.target() / ".analyst-input.json").read_text())
        self.assertEqual(binding["revision"], 1)

    def test_wrong_stage_binding_and_old_revision_are_rejected(self):
        self.manifest["revisions"][0]["stage_sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "этапу"):
            self.receive(True)
        with self.assertRaisesRegex(ValueError, "активную"):
            self.receive(True, revision=2)


if __name__ == "__main__":
    unittest.main()
