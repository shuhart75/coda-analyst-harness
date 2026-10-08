"""Transport, traceability, and pinned-source safety for analyst SDD bundles."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sdd_bundle as bundle

REQUIREMENTS = "# Requirements\n\n### REQ-ONE. Правило\nПоказать результат.\n"
SPEC = """## ADDED Requirements

### Requirement: Show result
The system SHALL show the result.

#### Scenario: Existing result
- **WHEN** a result exists
- **THEN** the user sees the result
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def build_bundle(root: Path, requirements_text: str, code_root: Path) -> dict:
    """Write a new-capability bundle for an existing clean integration Git fixture.

    Caller creates backend/openspec/config.yaml and at least one tracked code file.
    Every authored REQ maps to the fixture's one requirement and scenario.
    """
    root, code_root = Path(root), Path(code_root)
    change = root / "backend/show-result"
    spec = change / "specs/results/spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(SPEC, encoding="utf-8")
    (change / ".openspec.yaml").write_text("schema: spec-driven\ncreated: 2026-10-08\n")
    (change / "proposal.md").write_text("## Why\nNeed results.\n## What Changes\nShow results.\n## Capabilities\n### New Capabilities\n- `results`: Show results\n### Modified Capabilities\n\n## Impact\nBackend API.\n")
    tracked = git(code_root, "ls-files").splitlines()
    context = {path: digest((code_root / path).read_bytes()) for path in tracked
               if not path.startswith("requirements-exchange/") and not path.startswith("backend/openspec/specs/")}
    package = {"schema_version": 1, "profile": bundle.PROFILE,
               "requirements_sha256": digest(requirements_text.encode()), "changes": [{
                   "contour": "backend", "change_id": "show-result", "target_root": "backend/openspec",
                   "repository": git(code_root, "remote", "get-url", "origin"), "code_commit": git(code_root, "rev-parse", "HEAD"),
                   "context_files": context, "base_specs": {"results": None},
                   "coverage": [{"req_id": req_id, "capability": "results", "requirement": "Show result", "scenarios": ["Existing result"]}
                                for req_id in sorted(set(bundle.REQS.findall(requirements_text)))]}]}
    (root / "package.json").write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    return package


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "sdd"
        self.change = self.root / "backend/show-result"
        self.spec = self.change / "specs/results/spec.md"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text(SPEC)
        (self.change / ".openspec.yaml").write_text("schema: spec-driven\ncreated: 2026-10-08\n")
        self.proposal = self.change / "proposal.md"
        self.proposal.write_text("## Why\nNeed results.\n## What Changes\nShow results.\n## Capabilities\n### New Capabilities\n- `results`: Show results\n### Modified Capabilities\n\n## Impact\nBackend API.\n")
        self.code = self.home / "code"
        self.code.mkdir()
        git(self.code, "init", "-b", "main")
        git(self.code, "config", "user.name", "SDD test")
        git(self.code, "config", "user.email", "sdd@example.invalid")
        git(self.code, "remote", "add", "origin", "https://example.invalid/code.git")
        self.config = self.code / "backend/openspec/config.yaml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("schema: spec-driven\n")
        (self.code / "source.py").write_text("print('result')\n")
        self.commit()
        self.package = {"schema_version": 1, "profile": bundle.PROFILE,
                        "requirements_sha256": digest(REQUIREMENTS.encode()), "changes": [{
                            "contour": "backend", "change_id": "show-result", "target_root": "backend/openspec",
                            "repository": "https://example.invalid/code.git", "code_commit": git(self.code, "rev-parse", "HEAD"),
                            "context_files": {"backend/openspec/config.yaml": digest(self.config.read_bytes()),
                                              "source.py": digest((self.code / "source.py").read_bytes())},
                            "base_specs": {"results": None},
                            "coverage": [{"req_id": "REQ-ONE", "capability": "results", "requirement": "Show result", "scenarios": ["Existing result"]}]}]}
        self.save()

    def save(self):
        (self.root / "package.json").write_text(json.dumps(self.package, ensure_ascii=False))

    def commit(self):
        git(self.code, "add", "--all")
        git(self.code, "commit", "-m", "Source fixture")

    def verify(self):
        descriptor = bundle.inspect(self.root, REQUIREMENTS)
        return bundle.verify_sources(self.root, descriptor, self.code)

    def modified(self):
        source = self.code / "backend/openspec/specs/results/spec.md"
        source.parent.mkdir(parents=True)
        source.write_text(SPEC.replace("## ADDED Requirements", "## Requirements"))
        self.commit()
        item = self.package["changes"][0]
        item["code_commit"] = git(self.code, "rev-parse", "HEAD")
        item["base_specs"]["results"] = digest(source.read_bytes())
        self.proposal.write_text(self.proposal.read_text().replace("### New Capabilities\n- `results`: Show results\n### Modified Capabilities", "### New Capabilities\n\n### Modified Capabilities\n- `results`: Show results"))
        self.spec.write_text(SPEC.replace("ADDED", "MODIFIED"))
        self.save()
        return item

    def test_valid_snapshot_and_source_checks_do_not_change_code(self):
        before = git(self.code, "status", "--porcelain"), git(self.code, "rev-parse", "HEAD")
        self.assertEqual(self.verify()["runtime_validation"], "not-performed")
        descriptor = bundle.inspect(self.root, REQUIREMENTS)
        target = self.home / "snapshot"
        bundle.copy_snapshot(self.root, target, descriptor, REQUIREMENTS)
        self.assertEqual(bundle.verify_descriptor(target, REQUIREMENTS, descriptor), descriptor)
        self.assertEqual(before, (git(self.code, "status", "--porcelain"), git(self.code, "rev-parse", "HEAD")))

    def test_requirements_or_bundle_change_invalidates_descriptor(self):
        descriptor = bundle.inspect(self.root, REQUIREMENTS)
        with self.assertRaisesRegex(ValueError, "requirements checksum"):
            bundle.inspect(self.root, REQUIREMENTS + "Changed\n")
        self.spec.write_text(self.spec.read_text().replace("show the result", "display the result"))
        with self.assertRaisesRegex(ValueError, "descriptor mismatch"):
            bundle.verify_descriptor(self.root, REQUIREMENTS, descriptor)

    def test_unexpected_design_tasks_and_empty_directory_rejected(self):
        for name in ("design.md", "tasks.md"):
            with self.subTest(name=name):
                path = self.change / name
                path.write_text("not analyst owned")
                with self.assertRaisesRegex(ValueError, "unexpected bundle files"):
                    bundle.inspect(self.root, REQUIREMENTS)
                path.unlink()
        (self.change / "tasks").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected bundle directory"):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_paths_symlinks_and_credentials_rejected(self):
        item = self.package["changes"][0]
        for bad in ("../backend", "/backend", "backend/../openspec", "backend//openspec", "backend\\openspec"):
            item["target_root"] = bad
            self.save()
            with self.subTest(path=bad), self.assertRaises(ValueError):
                bundle.inspect(self.root, REQUIREMENTS)
        item["target_root"] = "backend/openspec"
        item["repository"] = "https://token@example.invalid/code.git"
        self.save()
        with self.assertRaisesRegex(ValueError, "credentials"):
            bundle.inspect(self.root, REQUIREMENTS)
        item["repository"] = "https://example.invalid/code.git"
        self.save()
        self.spec.unlink()
        self.spec.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, "non-regular"):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_all_requirements_and_scenarios_must_be_mapped(self):
        item = self.package["changes"][0]
        item["coverage"][0]["scenarios"] = ["Invented scenario"]
        self.save()
        with self.assertRaisesRegex(ValueError, "unknown requirement/scenario"):
            bundle.inspect(self.root, REQUIREMENTS)
        item["coverage"][0]["scenarios"] = ["Existing result"]
        self.spec.write_text(SPEC + "\n#### Scenario: No result\n- **WHEN** nothing exists\n- **THEN** no result appears\n")
        self.save()
        with self.assertRaisesRegex(ValueError, "every requirement and scenario"):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_scenario_format_and_normative_requirement_are_checked(self):
        for malformed in (SPEC.replace("#### Scenario", "### Scenario"), SPEC.replace("SHALL", "should"), SPEC.replace("- **THEN**", "THEN")):
            self.spec.write_text(malformed)
            with self.subTest(text=malformed), self.assertRaises(ValueError):
                bundle.inspect(self.root, REQUIREMENTS)

    def test_missing_config_pin_rejected(self):
        del self.package["changes"][0]["context_files"]["backend/openspec/config.yaml"]
        self.save()
        with self.assertRaisesRegex(ValueError, "config.yaml"):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_config_without_code_context_rejected(self):
        del self.package["changes"][0]["context_files"]["source.py"]
        self.save()
        with self.assertRaisesRegex(ValueError, "outside OpenSpec"):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_new_capability_cannot_use_modified(self):
        self.spec.write_text(SPEC.replace("ADDED", "MODIFIED"))
        with self.assertRaisesRegex(ValueError, "only support ADDED"):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_advancing_head_is_allowed_only_with_unchanged_sources(self):
        (self.code / "unrelated.txt").write_text("unrelated")
        self.commit()
        self.assertEqual(self.verify()["status"], "source-verified")
        (self.code / "source.py").write_text("print('changed')")
        self.commit()
        with self.assertRaisesRegex(ValueError, "stale context"):
            self.verify()

    def test_new_capability_appearing_at_head_blocks(self):
        spec = self.code / "backend/openspec/specs/results/spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text(SPEC)
        self.commit()
        with self.assertRaisesRegex(ValueError, "stale base spec"):
            self.verify()

    def test_wrong_origin_and_dirty_checkout_block(self):
        git(self.code, "remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(ValueError, "origin differs"):
            self.verify()
        git(self.code, "remote", "set-url", "origin", "https://example.invalid/code.git")
        self.config.write_text("schema: unknown\n")
        with self.assertRaisesRegex(ValueError, "must be clean"):
            self.verify()

    def test_modified_scenarios_preserved_or_explicitly_removed(self):
        item = self.modified()
        self.assertEqual(self.verify()["status"], "source-verified")
        self.spec.write_text(self.spec.read_text().replace("Existing result", "New result"))
        item["coverage"][0]["scenarios"] = ["New result"]
        self.save()
        with self.assertRaisesRegex(ValueError, "preserve old scenarios"):
            self.verify()
        item["removed_scenarios"] = {"results": {"Show result": {"Existing result": "Explicitly replaced by approved new behavior"}}}
        self.save()
        self.assertEqual(self.verify()["status"], "source-verified")
        item["removed_scenarios"]["results"]["Show result"]["Not present"] = "Invented reason"
        self.save()
        with self.assertRaisesRegex(ValueError, "preserve old scenarios"):
            self.verify()

    def test_modified_or_removed_unknown_requirement_blocks(self):
        item = self.modified()
        self.spec.write_text(self.spec.read_text().replace("Show result", "Unknown result"))
        item["coverage"][0]["requirement"] = "Unknown result"
        self.save()
        with self.assertRaisesRegex(ValueError, "does not exist in base"):
            self.verify()

    def test_malformed_mapping_types_fail_with_value_error(self):
        item = self.package["changes"][0]
        for removals in (["invalid"], {"results": []}, {"results": {"Show result": []}}, {"results": {"Show result": {"scenario": None}}}):
            item["removed_scenarios"] = removals
            self.save()
            with self.subTest(removals=removals), self.assertRaises(ValueError):
                bundle.inspect(self.root, REQUIREMENTS)
        del item["removed_scenarios"]
        item["coverage"][0]["requirement"] = []
        self.save()
        with self.assertRaises(ValueError):
            bundle.inspect(self.root, REQUIREMENTS)

    def test_removal_and_rename_reference_existing_requirements(self):
        item = self.modified()
        self.spec.write_text("## REMOVED Requirements\n\n### Requirement: Show result\n**Reason**: No longer supported\n**Migration**: Use the replacement\n")
        item["coverage"][0]["scenarios"] = []
        self.save()
        self.assertEqual(self.verify()["status"], "source-verified")
        self.spec.write_text("## RENAMED Requirements\n\n- FROM: `### Requirement: Show result`\n- TO: `### Requirement: Display result`\n")
        item["coverage"][0]["requirement"] = "Display result"
        self.save()
        self.assertEqual(self.verify()["status"], "source-verified")

    def test_receiver_retry_may_only_allow_exact_change_root(self):
        path = self.code / "backend/openspec/changes/show-result/design.md"
        path.parent.mkdir(parents=True)
        path.write_text("Developer design")
        descriptor = bundle.inspect(self.root, REQUIREMENTS)
        with self.assertRaisesRegex(ValueError, "must be clean"):
            bundle.verify_sources(self.root, descriptor, self.code)
        result = bundle.verify_sources(self.root, descriptor, self.code, allowed_receiver_paths=["backend/openspec/changes/show-result"])
        self.assertEqual(result["status"], "source-verified")
        with self.assertRaisesRegex(ValueError, "exact receiver"):
            bundle.verify_sources(self.root, descriptor, self.code, allowed_receiver_paths=["backend"])

    def test_receiver_dirty_exception_never_covers_context_files(self):
        relpath = "backend/openspec/changes/show-result/context.md"
        path = self.code / relpath
        path.parent.mkdir(parents=True)
        path.write_text("source context")
        self.commit()
        self.package["changes"][0]["context_files"][relpath] = digest(path.read_bytes())
        self.package["changes"][0]["code_commit"] = git(self.code, "rev-parse", "HEAD")
        self.save()
        descriptor = bundle.inspect(self.root, REQUIREMENTS)
        with self.assertRaisesRegex(ValueError, "must not cover pinned"):
            bundle.verify_sources(self.root, descriptor, self.code, allowed_receiver_paths=["backend/openspec/changes/show-result"])


if __name__ == "__main__":
    unittest.main()
