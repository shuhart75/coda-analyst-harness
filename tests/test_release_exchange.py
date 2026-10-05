"""Release exchange retention and cleanup against accepted temporary Git history."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_requirements_exchange as fixtures

sys.path.insert(0, str(fixtures.ROOT / "scripts"))
import baseline_releases as baseline
import release_exchange as release


class ReleaseExchangeTests(unittest.TestCase):
    prepare_project = fixtures.RequirementsExchangeTests.prepare_project
    command = fixtures.RequirementsExchangeTests.command
    authorize = fixtures.RequirementsExchangeTests.authorize
    prepare = fixtures.RequirementsExchangeTests.prepare
    write_receipt = fixtures.RequirementsExchangeTests.write_receipt
    result_review_fixture = fixtures.RequirementsExchangeTests.result_review_fixture
    record_review = fixtures.RequirementsExchangeTests.record_review
    git = fixtures.RequirementsExchangeTests.git

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.project, self.prepared, review_path, self.result_review = self.result_review_fixture(self.parent)
        self.state_command("mark-published", "--manifest", self.prepared["manifest"],
                           "--revision", "1", "--destination-role", "analytics")
        recorded = self.record_review(self.project, review_path, self.result_review)
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.state_command("close-stage", "--return-id", self.result_review["return_id"],
                           "--note", "Reviewed residual scope is retained in backlog", "--analyst-confirmed")
        self.packet = Path(self.prepared["manifest"]).parent
        self.current = self.project / "baseline/current"
        self.candidate = self.project / "releases/release-one/baseline-candidate"
        for root, value in ((self.current, "before"), (self.candidate, "after")):
            for section in baseline.SECTIONS:
                (root / section).mkdir(parents=True)
                (root / section / "README.md").write_text(value)
        self.git(self.project, "init", "-b", "feature/release-exchange")
        self.git(self.project, "config", "user.name", "Release tests")
        self.git(self.project, "config", "user.email", "release@example.invalid")
        self.git(self.project, "add", "--all")
        self.git(self.project, "commit", "-m", "Prepare accepted fixture inputs")
        self.remote = self.parent / "analytics.git"
        self.git(self.parent, "init", "--bare", str(self.remote))
        self.git(self.project, "remote", "add", "origin", str(self.remote))
        self.git(self.project, "push", "origin", "HEAD:main")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        observation = {"release_id": "release-one", "membership_complete": True,
                       "membership_evidence": "Full reviewed membership",
                       "tasks": [{"id": "task-one", "closed": True, "evidence": "Confirmed closure"}]}
        entry = baseline.observe(self.project, observation)
        domain_review = {aspect: {"status": "updated", "evidence": f"Reviewed {aspect}"}
                         for aspect in baseline.DOMAIN_ASPECTS}
        baseline.prepare(self.project, "release-one", self.candidate, {
            "scope_hash": entry["scope_hash"], "base_hash": baseline.tree_hash(self.current),
            "sections": baseline.review_hashes(self.candidate), "domain_review": domain_review,
            "consistency_evidence": "Reviewed all baseline sections against deployed release"})
        self.deployment = {"version": "one", "environment": "production", "evidence": "Analyst confirmed deployment"}
        self.promoted = baseline.promote(self.project, "release-one", entry["scope_hash"],
                                         self.deployment, analyst_confirmed=True)
        self.review = {"scope_hash": entry["scope_hash"], "deliveries": [
            {"delivery_key": "demo", "disposition": "complete", "task_ids": ["task-one"],
             "packet_hash": baseline.tree_hash(self.packet),
             "evidence": "Closed delivery with reviewed summary and explicit residual decision"}],
            "other_tasks": []}
        self.index = self.project / "releases/release-one/exchange-archive-index.json"
        self.archive = self.project / "releases/release-one/exchange-archive/demo"

    def state_command(self, action, *arguments):
        result = fixtures.run(sys.executable, str(fixtures.STATE_SCRIPT), action,
                              str(self.project), "demo", *arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def files(root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def archive_exchange(self):
        return release.review_exchange(self.project, "release-one", self.review)

    def accept_archive(self):
        self.git(self.project, "add", "--all")
        self.git(self.project, "commit", "-m", "Accept release baseline and exchange archive")
        self.git(self.project, "push", "origin", "HEAD:main")

    def test_complete_archive_preserves_exact_packet_and_return_identity(self):
        original = self.files(self.packet)
        result = self.archive_exchange()
        self.assertEqual(result["state"], "cleanup-pending")
        self.assertEqual(self.files(self.archive), original)
        self.assertEqual(self.files(self.packet), original)
        record = release.find_archive(self.project, "demo")
        self.assertEqual(record["closure"]["return_id"], self.result_review["return_id"])
        self.assertEqual(record["baseline_hash"], self.promoted["preparation"]["candidate_hash"])
        self.assertEqual(release.archived_root(self.project, "demo"), self.archive)
        shared = {path.name: path.read_bytes() for path in self.packet.parent.iterdir() if path.is_file()}
        self.assertEqual(self.files(self.project / record["contracts_path"]), shared)
        self.assertEqual(self.archive_exchange(), result)
        self.assertEqual(self.files(self.archive), original)

    def test_partial_delivery_retains_packet_without_archive(self):
        original = self.files(self.packet)
        self.review["deliveries"][0]["disposition"] = "partial"
        result = self.archive_exchange()
        self.assertEqual(result["deliveries"]["demo"]["state"], "retained")
        self.assertEqual(self.files(self.packet), original)
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.index.exists())

    def test_complete_archive_requires_review_of_exact_packet_bytes(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                review = copy.deepcopy(self.review)
                if missing:
                    del review["deliveries"][0]["packet_hash"]
                else:
                    review["deliveries"][0]["packet_hash"] = "0" * 64
                with self.assertRaises(ValueError):
                    release.review_exchange(self.project, "release-one", review)
                self.assertFalse(self.archive.exists())
                self.assertFalse(self.index.exists())
        (self.packet / "revisions/001/returns/tasks.md").write_text("Changed after packet review")
        with self.assertRaises(ValueError):
            self.archive_exchange()
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.index.exists())

    def test_mapping_must_cover_full_release_and_not_claim_published_packet_absent(self):
        for change in ({"deliveries": []}, {"scope_hash": "wrong"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.review_exchange(self.project, "release-one", {**self.review, **change})
        no_transfer = copy.deepcopy(self.review)
        no_transfer["deliveries"][0]["disposition"] = "no-transfer"
        with self.assertRaises(ValueError):
            release.review_exchange(self.project, "release-one", no_transfer)
        self.assertFalse(self.index.exists())
        self.assertFalse(self.archive.exists())

    def test_no_transfer_is_valid_only_for_unpublished_delivery(self):
        (self.project / "features/unpublished").mkdir()
        self.review["deliveries"][0].update(delivery_key="unpublished", disposition="no-transfer")
        result = self.archive_exchange()
        self.assertEqual(result["deliveries"]["unpublished"]["state"], "not-required")
        self.assertFalse(self.index.exists())
        self.assertTrue(self.packet.is_dir())

    def test_closed_delivery_without_promoted_baseline_cannot_be_archived(self):
        path = self.project / "releases/baseline-status.json"
        state = json.loads(path.read_text())
        state["releases"]["release-one"]["status"] = "prepared"
        path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            self.archive_exchange()
        self.assertFalse(self.index.exists())
        self.assertFalse(self.archive.exists())

    def test_unclosed_stage_blocks_archive(self):
        path = self.project / "features/demo/requirements-state.json"
        state = json.loads(path.read_text())
        state["delivery_stages"]["stages"][-1]["state"] = "open"
        path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            self.archive_exchange()
        self.assertFalse(self.index.exists())
        self.assertFalse(self.archive.exists())

    def test_cleanup_requires_archive_and_baseline_in_real_remote_main(self):
        self.archive_exchange()
        with self.assertRaises(ValueError):
            release.require_accepted_archive(self.project, "release-one", "demo")
        with self.assertRaises(ValueError):
            release.cleanup(self.project, "release-one", "demo")
        self.assertTrue(self.packet.is_dir())
        self.accept_archive()
        accepted = release.require_accepted_archive(self.project, "release-one", "demo")
        self.assertEqual(accepted, release.find_archive(self.project, "demo"))

    def test_accepted_cleanup_preserves_shared_files_archive_and_review_lookup(self):
        self.archive_exchange()
        self.accept_archive()
        original_archive = self.files(self.archive)
        shared = {p.name: p.read_bytes() for p in self.packet.parent.iterdir() if p.is_file()}
        sibling = self.packet.parent / "unrelated-packet"
        sibling.mkdir()
        (sibling / "keep.md").write_text("Another delivery")
        before_baseline = self.files(self.project / "baseline")
        with patch.object(baseline, "_install_snapshot", side_effect=AssertionError("Must not reinstall baseline")):
            first = release.cleanup(self.project, "release-one", "demo")
            self.assertEqual(first["status"], "awaiting-merge")
            self.assertEqual(release.cleanup(self.project, "release-one", "demo"), first)
            self.assertEqual(self.archive_exchange()["state"], "cleanup-pending")
            self.assertFalse(self.packet.exists())
            self.accept_archive()
            completed = release.cleanup(self.project, "release-one", "demo")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(release.cleanup(self.project, "release-one", "demo"), completed)
            self.assertEqual(self.archive_exchange()["state"], "completed")
        self.assertFalse(self.packet.exists())
        self.assertEqual(self.files(self.archive), original_archive)
        self.assertEqual({p.name: p.read_bytes() for p in sibling.parent.iterdir() if p.is_file()}, shared)
        self.assertEqual((sibling / "keep.md").read_text(), "Another delivery")
        self.assertEqual(self.files(self.project / "baseline"), before_baseline)
        self.assertEqual(release.archived_root(self.project, "demo"), self.archive)
        release.exchange().validate_result_review(self.project, "demo", self.result_review["return_id"], self.result_review)
        self.assertEqual(baseline.scan(self.project), [])

    def test_accepted_archive_without_accepted_baseline_still_blocks_cleanup(self):
        self.archive_exchange()
        self.git(self.project, "add", "--", "releases/release-one/exchange-archive",
                 "releases/release-one/exchange-contracts", "releases/release-one/exchange-archive-index.json")
        self.git(self.project, "commit", "-m", "Accept archive before baseline review")
        self.git(self.project, "push", "origin", "HEAD:main")
        with self.assertRaises(ValueError):
            release.require_accepted_archive(self.project, "release-one", "demo")
        self.assertTrue(self.packet.is_dir())
        self.accept_archive()
        self.assertEqual(release.require_accepted_archive(self.project, "release-one", "demo"),
                         release.find_archive(self.project, "demo"))

    def test_changed_returns_after_archive_block_cleanup(self):
        self.archive_exchange()
        self.accept_archive()
        returns = self.packet / "revisions/001/returns"
        (returns / "tasks.md").write_text("New developer result after archive")
        with self.assertRaisesRegex(ValueError, "новые требования или возвраты"):
            release.cleanup(self.project, "release-one", "demo")
        self.assertTrue(self.packet.is_dir())

    def test_tampered_archive_is_refused(self):
        self.archive_exchange()
        (self.archive / "revisions/001/requirements.md").write_text("Tampered immutable input")
        for operation in (lambda: release.archived_root(self.project, "demo"),
                          self.archive_exchange,
                          lambda: release.cleanup(self.project, "release-one", "demo")):
            with self.assertRaises(ValueError):
                operation()
        self.assertTrue(self.packet.is_dir())

    def test_source_symlink_fails_before_archive_writes(self):
        target = self.parent / "linked.md"
        target.write_text("External untrusted result")
        source = self.packet / "revisions/001/returns/tasks.md"
        source.unlink()
        source.symlink_to(target)
        with self.assertRaises(ValueError):
            self.archive_exchange()
        self.assertFalse(self.index.exists())
        self.assertFalse(self.archive.exists())

    def test_archive_or_contract_symlink_fails_before_any_archive_writes(self):
        for name in ("exchange-archive", "exchange-contracts"):
            with self.subTest(name=name):
                outside = self.parent / name
                outside.mkdir()
                linked = self.project / "releases/release-one" / name
                linked.symlink_to(outside, target_is_directory=True)
                try:
                    with self.assertRaises(ValueError):
                        self.archive_exchange()
                    self.assertFalse(self.index.exists())
                    self.assertFalse(self.archive.exists())
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    linked.unlink()

    def test_promoted_release_stays_visible_until_cleanup_completed(self):
        self.archive_exchange()
        pending = baseline.scan(self.project)
        self.assertEqual([entry["release_id"] for entry in pending], ["release-one"])
        self.assertEqual(pending[0]["exchange_cleanup"]["state"], "cleanup-pending")
        self.accept_archive()
        self.assertEqual(release.cleanup(self.project, "release-one", "demo")["status"], "awaiting-merge")
        self.assertEqual(baseline.scan(self.project)[0]["exchange_cleanup"]["state"], "cleanup-pending")
        self.accept_archive()
        self.assertEqual(release.cleanup(self.project, "release-one", "demo")["status"], "completed")
        self.assertEqual(baseline.scan(self.project), [])

    def test_archived_scan_keeps_original_return_ids_and_filters_owner(self):
        original_ids = {
            f"demo:001:{path.relative_to(self.packet).as_posix()}:{release.exchange().sha256(path)}"
            for path in (self.packet / "revisions/001/returns").rglob("*") if path.is_file()}
        self.archive_exchange()
        own = self.command("scan", str(self.project), "--archived", "--analyst", "ivan")
        self.assertEqual(own["scope"], "archived")
        self.assertEqual(len(own["items"]), 1)
        item = own["items"][0]
        self.assertEqual(item["feature"], "demo")
        self.assertEqual(item["owner"], "ivan")
        self.assertEqual(item["revisions"][0]["revision"], 1)
        self.assertEqual(set(item["revisions"][0]["return_ids"]), original_ids)
        self.assertIn(self.result_review["return_id"], original_ids)
        foreign = self.command("scan", str(self.project), "--archived", "--analyst", "other")
        self.assertEqual(foreign["items"], [])
        all_owners = self.command("scan", str(self.project), "--archived", "--analyst", "other", "--all")
        self.assertEqual(all_owners["items"], own["items"])

    def test_code_archive_to_accepted_cleanup_roundtrip_preserves_ordinary_clone(self):
        # Simulate publication metadata for the already reviewed/closed common packet.
        # Publication itself is covered by test_requirements_exchange; archive and cleanup
        # below use real code and analytics remotes without mocking acceptance checks.
        seed = self.parent / "code-seed"
        seed.mkdir()
        shutil.copytree(self.packet.parent, seed / "requirements-exchange")
        (seed / "product.txt").write_text("Unrelated product code")
        self.git(seed, "init", "-b", "main")
        self.git(seed, "config", "user.name", "Developer")
        self.git(seed, "config", "user.email", "developer@example.invalid")
        self.git(seed, "add", "--all")
        self.git(seed, "commit", "-m", "Accept reviewed development results")
        code_remote = self.parent / "code.git"
        self.git(self.parent, "init", "--bare", str(code_remote))
        self.git(seed, "remote", "add", "origin", str(code_remote))
        self.git(seed, "push", "origin", "main")
        self.git(code_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        published_commit = self.git(seed, "rev-parse", "HEAD")
        ordinary = self.parent / "ordinary-code"
        self.git(self.parent, "clone", "--quiet", str(code_remote), str(ordinary))
        (ordinary / "developer-local.txt").write_text("Unsaved developer-owned file")
        before = {key: self.git(ordinary, *args) for key, args in {
            "head": ("rev-parse", "HEAD"), "refs": ("show-ref",),
            "status": ("status", "--porcelain=v1")}.items()}
        cached = self.parent / "cached-code-manifest.json"
        manifest = json.loads(Path(self.prepared["manifest"]).read_text())
        manifest["publication"] = {
            "state": "merged", "repository_url": str(code_remote),
            "target_branch": "main", "target_commit": published_commit}
        cached.write_text(json.dumps(manifest))
        state_path = self.project / "features/demo/requirements-state.json"
        state = json.loads(state_path.read_text())
        state["delivery_stages"]["revisions"][-1].update(destination_role="code", manifest_path=str(cached))
        state["last_published"].update(destination_role="code", manifest_path=str(cached))
        state_path.write_text(json.dumps(state))

        self.archive_exchange()
        record = release.find_archive(self.project, "demo")
        self.assertEqual(record["source"]["role"], "code")
        self.assertEqual(record["source"]["commit"], published_commit)
        self.assertEqual(self.files(self.archive), self.files(seed / "requirements-exchange/demo"))
        self.accept_archive()
        requested = release.cleanup(self.project, "release-one", "demo")
        self.assertEqual(requested["status"], "awaiting-merge")
        self.assertEqual(self.git(code_remote, "rev-parse", "refs/heads/main"), published_commit)
        self.assertEqual(release.cleanup(self.project, "release-one", "demo"), requested)
        human = self.parent / "code-human-review"
        self.git(self.parent, "clone", "--quiet", str(code_remote), str(human))
        self.git(human, "config", "user.name", "Human reviewer")
        self.git(human, "config", "user.email", "reviewer@example.invalid")
        self.git(human, "merge", "--no-ff", "origin/" + requested["request_branch"],
                 "-m", "Accept archived exchange cleanup")
        self.git(human, "push", "origin", "main")
        completed = release.cleanup(self.project, "release-one", "demo")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["request_commit"], requested["request_commit"])
        self.assertFalse((human / "requirements-exchange/demo").exists())
        self.assertTrue((human / "requirements-exchange/AGENTS.md").is_file())
        self.assertEqual((human / "product.txt").read_text(), "Unrelated product code")
        after = {key: self.git(ordinary, *args) for key, args in {
            "head": ("rev-parse", "HEAD"), "refs": ("show-ref",),
            "status": ("status", "--porcelain=v1")}.items()}
        self.assertEqual(after, before)
        self.assertEqual((ordinary / "developer-local.txt").read_text(), "Unsaved developer-owned file")
        self.assertEqual(baseline.scan(self.project), [])


if __name__ == "__main__":
    unittest.main()
