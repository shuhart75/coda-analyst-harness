from __future__ import annotations

import json
import hashlib
from unittest.mock import patch
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import baseline_releases as baseline


class BaselineReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        subprocess.run(["git", "init", "-b", "feature/baseline", str(self.project)], check=True, capture_output=True)
        self.current = self.project / "baseline" / "current"
        self.candidate = self.project / "releases" / "release-one" / "baseline-candidate"
        for root, content in ((self.current, "before"), (self.candidate, "after")):
            for section in baseline.SECTIONS:
                (root / section).mkdir(parents=True)
                (root / section / "README.md").write_text(content)
        self.observation = {"release_id": "release-one", "membership_complete": True,
                            "membership_evidence": "reviewed run membership", "tasks": [
                                {"id": "task-one", "closed": True, "evidence": "reviewed history"}]}
        self.deployment = {"version": "one", "environment": "production", "evidence": "analyst deployment confirmation"}

    def prepare(self):
        entry = baseline.observe(self.project, self.observation)
        return baseline.prepare(self.project, "release-one", self.candidate, self.release_review(entry))

    def release_review(self, entry):
        return {
            "scope_hash": entry["scope_hash"], "sections": baseline.review_hashes(self.candidate),
            "base_hash": baseline.tree_hash(self.current),
            "domain_review": {aspect: {"status": "updated", "evidence": f"Reviewed {aspect} against release results"}
                              for aspect in baseline.DOMAIN_ASPECTS},
            "consistency_evidence": "Domain impact, backlog, decisions and auxiliary results reviewed"}

    def promote(self, entry, **kwargs):
        return baseline.promote(self.project, "release-one", entry["scope_hash"], self.deployment, **kwargs)

    def test_closed_requires_nonempty_complete_explicit_evidence(self):
        cases = [{"tasks": []}, {"membership_complete": False}, {"membership_evidence": ""},
                 {"tasks": [{"id": "one", "closed": None, "evidence": "history"}]},
                 {"tasks": [{"id": "one", "closed": True, "evidence": ""}]},
                 {"tasks": [{"id": "one", "closed": False, "evidence": "history"}]}]
        for change in cases:
            with self.subTest(change=change):
                entry = baseline.observe(self.project, {**self.observation, **change})
                self.assertEqual(entry["status"], "blocked")
                self.assertEqual(baseline.scan(self.project), [])
        self.observation["tasks"][0]["closed"] = "done"
        with self.assertRaises(ValueError):
            baseline.observe(self.project, self.observation)

    def test_scan_preserves_deferred_and_observe_is_idempotent(self):
        baseline.observe(self.project, self.observation)
        baseline.defer(self.project, "release-one", "waiting for deployment")
        baseline.observe(self.project, self.observation)
        self.assertEqual(baseline.scan(self.project)[0]["status"], "deferred")

    def test_membership_change_invalidates_preparation(self):
        self.prepare()
        self.observation["tasks"].append({"id": "two", "closed": True, "evidence": "history"})
        entry = baseline.observe(self.project, self.observation)
        self.assertNotIn("preparation", entry)
        with self.assertRaises(ValueError):
            self.promote(entry, analyst_confirmed=True)
        self.assertEqual(len(entry["history"]), 1)

    def test_reopened_release_stays_blocked(self):
        self.prepare()
        self.observation["tasks"][0]["closed"] = False
        entry = baseline.observe(self.project, self.observation)
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(baseline.scan(self.project), [])

    def test_confirmation_and_deployment_evidence_required(self):
        entry = self.prepare()
        with self.assertRaises(ValueError):
            self.promote(entry)
        for key in self.deployment:
            with self.subTest(key=key), self.assertRaises(ValueError):
                baseline.promote(self.project, "release-one", entry["scope_hash"], {**self.deployment, key: ""}, True)
        with self.assertRaises(ValueError):
            baseline.promote(self.project, "release-one", "stale", self.deployment, True)

    def test_immutable_snapshot_and_repeat(self):
        entry = self.prepare()
        result = self.promote(entry, analyst_confirmed=True)
        self.assertEqual(result, self.promote(entry, analyst_confirmed=True))
        self.assertEqual(baseline.scan(self.project)[0]["exchange_cleanup"]["state"], "pending-review")
        snapshot = self.project / result["snapshot"]
        self.assertEqual(baseline.tree_hash(self.current), baseline.tree_hash(snapshot))
        prior = self.project / "baseline" / "versions" / ("before-" + entry["preparation"]["base_hash"])
        self.assertEqual((prior / "domain" / "README.md").read_text(), "before")
        (snapshot / "domain" / "README.md").write_text("tampered")
        with self.assertRaises(ValueError):
            self.promote(entry, analyst_confirmed=True)

    def test_existing_snapshot_not_overwritten(self):
        entry = self.prepare()
        snapshot = self.project / "baseline" / "versions" / "release-one"
        snapshot.mkdir(parents=True)
        (snapshot / "README.md").write_text("previous immutable snapshot")
        with self.assertRaises(ValueError):
            self.promote(entry, analyst_confirmed=True)
        self.assertEqual((snapshot / "README.md").read_text(), "previous immutable snapshot")

    def test_edits_after_review_block_promotion(self):
        entry = self.prepare()
        (self.candidate / "domain" / "README.md").write_text("changed after review")
        with self.assertRaises(ValueError):
            self.promote(entry, analyst_confirmed=True)

    def test_changed_current_blocks_promotion(self):
        entry = self.prepare()
        (self.current / "domain" / "README.md").write_text("another release")
        with self.assertRaises(ValueError):
            self.promote(entry, analyst_confirmed=True)

    def test_domain_review_is_required_before_preparation(self):
        entry = baseline.observe(self.project, self.observation)
        review = self.release_review(entry)
        del review["domain_review"]
        before = baseline._path(self.project).read_bytes()
        with self.assertRaisesRegex(ValueError, "domain_review"):
            baseline.prepare(self.project, "release-one", self.candidate, review)
        self.assertEqual(baseline._path(self.project).read_bytes(), before)

    def test_every_domain_aspect_requires_explicit_status_and_evidence(self):
        entry = baseline.observe(self.project, self.observation)
        for aspect in baseline.DOMAIN_ASPECTS:
            for invalid in (None, {"status": "unknown", "evidence": "Reviewed"},
                            {"status": "unchanged", "evidence": " "}):
                with self.subTest(aspect=aspect, invalid=invalid):
                    review = self.release_review(entry)
                    if invalid is None:
                        del review["domain_review"][aspect]
                    else:
                        review["domain_review"][aspect] = invalid
                    with self.assertRaisesRegex(ValueError, "domain_review"):
                        baseline.prepare(self.project, "release-one", self.candidate, review)

    def test_unchanged_domain_review_requires_identical_domain_tree(self):
        entry = baseline.observe(self.project, self.observation)
        review = self.release_review(entry)
        for item in review["domain_review"].values():
            item["status"] = "unchanged"
        with self.assertRaisesRegex(ValueError, "unchanged"):
            baseline.prepare(self.project, "release-one", self.candidate, review)
        (self.candidate / "domain/README.md").write_bytes((self.current / "domain/README.md").read_bytes())
        review["sections"] = baseline.review_hashes(self.candidate)
        prepared = baseline.prepare(self.project, "release-one", self.candidate, review)
        self.assertEqual(prepared["preparation"]["review_hash"], baseline.digest(review))
        self.assertEqual(self.promote(prepared, analyst_confirmed=True)["status"], "promoted")
        self.assertEqual((self.current / "domain/README.md").read_text(), "before")

    def test_changed_domain_review_allows_explicitly_updated_aspect(self):
        entry = baseline.observe(self.project, self.observation)
        review = self.release_review(entry)
        for aspect, item in review["domain_review"].items():
            item["status"] = "updated" if aspect == "rules" else "unchanged"
        prepared = baseline.prepare(self.project, "release-one", self.candidate, review)
        self.assertEqual(self.promote(prepared, analyst_confirmed=True)["status"], "promoted")
        self.assertEqual((self.current / "domain/README.md").read_text(), "after")

    def test_review_binds_base_and_domain_before_preparation(self):
        entry = baseline.observe(self.project, self.observation)
        review = self.release_review(entry)
        review["base_hash"] = "stale"
        with self.assertRaisesRegex(ValueError, "base_hash"):
            baseline.prepare(self.project, "release-one", self.candidate, review)
        review["base_hash"] = baseline.tree_hash(self.current)
        (self.candidate / "domain/README.md").write_text("changed after domain review")
        with self.assertRaisesRegex(ValueError, "актуальная проверка"):
            baseline.prepare(self.project, "release-one", self.candidate, review)

    def test_saved_domain_review_tampering_blocks_first_and_repeat_promotion(self):
        for already_promoted in (False, True):
            with self.subTest(already_promoted=already_promoted):
                entry = self.prepare()
                if already_promoted:
                    self.promote(entry, analyst_confirmed=True)
                path = baseline._path(self.project)
                original = path.read_bytes()
                state = json.loads(original)
                state["releases"]["release-one"]["preparation"]["review"]["domain_review"]["rules"]["evidence"] = "Substituted review"
                path.write_text(json.dumps(state))
                current_hash = baseline.tree_hash(self.current)
                with self.assertRaisesRegex(ValueError, "Проверка baseline изменилась"):
                    self.promote(entry, analyst_confirmed=True)
                self.assertEqual(baseline.tree_hash(self.current), current_hash)
                path.write_bytes(original)

    def test_legacy_prepared_requires_review_but_promoted_repeat_remains_readable(self):
        entry = self.prepare()
        path = baseline._path(self.project)
        current_hash = baseline.tree_hash(self.current)
        original = path.read_bytes()
        state = json.loads(original)
        preparation = state["releases"]["release-one"]["preparation"]
        for field in ("review_hash", "base_domain_hash"):
            del preparation[field]
        for field in ("domain_review", "base_hash"):
            del preparation["review"][field]
        path.write_text(json.dumps(state))
        self.assertEqual(len(baseline.scan(self.project)), 1)
        with self.assertRaisesRegex(ValueError, "новая подготовка"):
            self.promote(entry, analyst_confirmed=True)
        self.assertEqual(baseline.tree_hash(self.current), current_hash)
        path.write_bytes(original)
        self.promote(entry, analyst_confirmed=True)
        promoted = json.loads(path.read_text())
        preparation = promoted["releases"]["release-one"]["preparation"]
        for field in ("review_hash", "base_domain_hash"):
            del preparation[field]
        for field in ("domain_review", "base_hash"):
            del preparation["review"][field]
        path.write_text(json.dumps(promoted))
        self.assertEqual(self.promote(entry, analyst_confirmed=True), promoted["releases"]["release-one"])

    def test_review_requires_every_section(self):
        entry = baseline.observe(self.project, self.observation)
        hashes = baseline.review_hashes(self.candidate)
        del hashes["decisions"]
        with self.assertRaises(ValueError):
            baseline.prepare(self.project, "release-one", self.candidate, {
                "scope_hash": entry["scope_hash"], "sections": hashes, "consistency_evidence": "reviewed"})

    def test_main_writes_blocked_but_scan_allowed(self):
        subprocess.run(["git", "-C", str(self.project), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        with self.assertRaises(ValueError):
            baseline.observe(self.project, self.observation)
        self.assertEqual(baseline.scan(self.project), [])

    def test_symlink_candidate_blocked(self):
        (self.candidate / "domain" / "external.md").symlink_to(self.current / "domain" / "README.md")
        with self.assertRaises(ValueError):
            baseline.review_hashes(self.candidate)


class BaselineRunObservationTests(unittest.TestCase):
    def setUp(self):
        BaselineReleaseTests.setUp(self)
        import tracker_workflow
        self.run_id = "reviewed-release-run"
        self.run_root = self.project / "run-evidence"
        self.run_root.mkdir()
        self.result = {"scope": {"kind": "release", "intent": "update-planning", "provider": "jira", "ids": ["release-one"]},
                       "issues": [{"jira_key": "BE-ONE", "development": {"state": "completed"}}],
                       "skipped": [{"jira_key": "DOC-ONE"}], "excluded": [{"jira_key": "REMOVED-ONE"}]}
        self.completion = {"reconciled_sha256": "verified-result-hash"}
        self.history = {"run_id": self.run_id, "reconciled_sha256": self.completion["reconciled_sha256"],
                        "status": "history-review-ready", "project_root": str(self.project),
                        "release_membership_evidence": {"release": "release-one", "complete": True,
                                                        "keys": ["BE-ONE", "DOC-ONE", "REMOVED-ONE"]}}
        source = self.project / "analyst-closure.txt"
        source.write_text("All three tasks explicitly confirmed closed, including removed and document tasks")
        self.review = {"run_id": self.run_id, "reconciled_sha256": self.completion["reconciled_sha256"],
                       "tasks": [{"id": key, "closed": True, "analyst_confirmed": True,
                                  "source": {"file": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                             "quote": source.read_text()}}
                                 for key in ["BE-ONE", "DOC-ONE", "REMOVED-ONE"]]}
        self.review_path = self.project / "closure-review.json"
        self.addCleanup(patch.stopall)
        patch.object(tracker_workflow, "verified_result", return_value=(self.completion, self.result)).start()
        patch.object(tracker_workflow, "run_root", return_value=self.run_root).start()
        self.write_reviews()

    def write_reviews(self):
        from tracker_workflow import digest_object
        path = self.run_root / "history" / (digest_object(self.history) + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.history))
        self.review["history_review"] = str(path)
        self.review_path.write_text(json.dumps(self.review))

    def observed(self):
        self.write_reviews()
        return baseline.observe_run(self.project, self.run_id, self.review_path)

    def test_run_closed_membership_includes_skipped_and_excluded(self):
        entry = self.observed()
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(len(entry["observation"]["tasks"]), 3)

    def test_development_completed_does_not_imply_closed(self):
        self.review["tasks"] = []
        entry = self.observed()
        self.assertEqual(entry["status"], "blocked")
        self.assertTrue(all(task["closed"] is None for task in entry["observation"]["tasks"]))

    def test_missing_skipped_closure_blocks_candidate(self):
        self.review["tasks"] = [task for task in self.review["tasks"] if task["id"] != "DOC-ONE"]
        self.assertEqual(self.observed()["status"], "blocked")

    def test_membership_incomplete_or_different_blocks_candidate(self):
        self.history["release_membership_evidence"]["complete"] = False
        self.assertEqual(self.observed()["status"], "blocked")
        self.history["release_membership_evidence"]["complete"] = True
        self.history["release_membership_evidence"]["keys"].remove("REMOVED-ONE")
        self.assertEqual(self.observed()["status"], "blocked")

    def test_stale_review_result_or_history_not_ready_rejected(self):
        self.review["reconciled_sha256"] = "old-result"
        with self.assertRaises(ValueError):
            self.observed()
        self.review["reconciled_sha256"] = self.completion["reconciled_sha256"]
        self.history["status"] = "history-collection-incomplete"
        with self.assertRaises(ValueError):
            self.observed()

    def test_unconfirmed_or_missing_closure_evidence_stays_unknown(self):
        self.review["tasks"][0]["analyst_confirmed"] = False
        self.assertEqual(self.observed()["status"], "blocked")
        self.review["tasks"][0]["analyst_confirmed"] = True
        self.review["tasks"][0]["source"] = {}
        self.assertEqual(self.observed()["status"], "blocked")

    def test_changed_closure_evidence_rejected(self):
        Path(self.review["tasks"][0]["source"]["file"]).write_text("changed")
        with self.assertRaises(ValueError):
            self.observed()

    def test_verified_result_failure_not_bypassed(self):
        with patch("tracker_workflow.verified_result", side_effect=ValueError("not verified")):
            with self.assertRaises(ValueError):
                baseline.observe_run(self.project, self.run_id, self.review_path)

    def test_review_from_other_project_rejected(self):
        self.history["project_root"] = "/other/project"
        with self.assertRaises(ValueError):
            self.observed()


class HistoricalReconciliationTests(unittest.TestCase):
    def setUp(self):
        BaselineReleaseTests.setUp(self)
        destination = self.project / 'releases/baseline-reconciliations/docs-one/candidate'
        destination.parent.mkdir(parents=True)
        self.candidate.rename(destination)
        self.candidate = destination
        delivery_path = 'quarters/2026-Q3/features/demo/deliveries/mvp'
        (self.project / delivery_path).mkdir(parents=True)
        (self.project / 'delivery-index.json').write_text(json.dumps({'schema_version': 1, 'deliveries': {
            'demo-mvp': {'feature_id': 'demo', 'delivery_id': 'mvp', 'quarter': '2026-Q3', 'path': delivery_path}}}))
        evidence = self.project / 'releases/baseline-reconciliations/docs-one/evidence'
        evidence.mkdir()
        (evidence / 'gantt.puml').write_text('Confirmed completed quarterly work')
        (evidence / 'decision.md').write_text('Внедрение подтверждаю. Остальные направления определяй по завершённым квартальным Гантам.')
        def source(name):
            path = evidence / name
            return {'path': path.relative_to(self.project).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        scope = [{'feature_id': 'demo', 'delivery_key': 'demo-mvp', 'quarter': '2026-Q3',
                  'deployment_basis': 'completed-quarter-gantt', 'evidence': [{'kind': 'gantt', **source('gantt.puml')}]}]
        self.review = {'kind': 'historical-baseline-reconciliation', 'environment': 'ПРОМ',
                       'product_release_version': None,
                       'analyst_decision': {**source('decision.md'), 'quote': (evidence / 'decision.md').read_text()},
                       'scope': scope, 'scope_hash': baseline.digest(scope),
                       'sections': baseline.review_hashes(self.candidate), 'candidate_hash': baseline.tree_hash(self.candidate),
                       'base_hash': baseline.tree_hash(self.current), 'consistency_evidence': 'All sections and limitations reviewed'}

    def prepare(self):
        return baseline.prepare_reconciliation(self.project, 'docs-one', self.candidate, self.review)

    def promote(self, entry, confirmed=True):
        return baseline.promote_reconciliation(self.project, 'docs-one', entry['review_hash'], confirmed)

    def test_historical_promotion_does_not_invent_or_process_release(self):
        release = baseline.observe(self.project, self.observation)
        entry = self.prepare()
        promoted = self.promote(entry)
        state = json.loads((self.project / 'releases/baseline-status.json').read_text())
        self.assertEqual(state['releases'], {'release-one': release})
        self.assertIsNone(promoted['product_release_version'])
        self.assertNotIn('release_id', promoted)
        self.assertEqual(promoted['environment'], 'ПРОМ')
        self.assertEqual(promoted['snapshot'], 'baseline/versions/documentation/docs-one')
        self.assertEqual(baseline.tree_hash(self.current), entry['preparation']['candidate_hash'])
        self.assertEqual(len(baseline.scan(self.project)), 1)

    def test_repeated_prepare_and_promote_are_immutable(self):
        entry = self.prepare()
        self.assertEqual(self.prepare(), entry)
        promoted = self.promote(entry)
        before = (self.project / 'releases/baseline-status.json').read_bytes()
        self.assertEqual(self.promote(entry), promoted)
        self.assertEqual(self.prepare(), promoted)
        self.assertEqual((self.project / 'releases/baseline-status.json').read_bytes(), before)
        self.review['consistency_evidence'] = 'Different decision'
        with self.assertRaises(ValueError):
            self.prepare()

    def test_prepared_review_correction_preserves_audit_and_invalidates_old_hash(self):
        original = self.prepare()
        self.review['consistency_evidence'] = 'Corrected consistency reference'
        updated = self.prepare()
        self.assertEqual(updated['history'], [original])
        self.assertNotEqual(updated['review_hash'], original['review_hash'])
        self.assertEqual(self.prepare(), updated)
        with self.assertRaises(ValueError):
            self.promote(original)
        (self.candidate / 'domain/README.md').write_text('Corrected reviewed candidate')
        self.review['candidate_hash'] = baseline.tree_hash(self.candidate)
        self.review['sections'] = baseline.review_hashes(self.candidate)
        latest = self.prepare()
        self.assertEqual(latest['history'][0], original)
        self.assertEqual(latest['history'][1]['review'], updated['review'])
        self.assertEqual(latest['history'][1]['preparation'], updated['preparation'])
        with self.assertRaises(ValueError):
            self.promote(updated)
        self.assertEqual(self.promote(latest)['status'], 'promoted')

    def test_reprepare_cannot_adopt_different_base(self):
        original = self.prepare()
        (self.current / 'domain/README.md').write_text('Different baseline')
        self.review['base_hash'] = baseline.tree_hash(self.current)
        self.review['consistency_evidence'] = 'Rechecked against a new baseline'
        with self.assertRaises(ValueError):
            self.prepare()
        state = json.loads((self.project / 'releases/baseline-status.json').read_text())
        self.assertEqual(state['reconciliations']['docs-one'], original)

    def test_exact_confirmation_and_review_hash_required(self):
        entry = self.prepare()
        with self.assertRaises(ValueError):
            self.promote(entry, False)
        with self.assertRaises(ValueError):
            baseline.promote_reconciliation(self.project, 'docs-one', 'wrong-review', True)
        self.assertEqual((self.current / 'domain/README.md').read_text(), 'before')

    def test_changed_source_invalidates_preparation_and_promotion(self):
        entry = self.prepare()
        path = self.project / self.review['scope'][0]['evidence'][0]['path']
        path.write_text('Source changed after review')
        with self.assertRaises(ValueError):
            self.promote(entry)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_wrong_identity_or_quarter_rejected(self):
        for key, value in (('feature_id', 'other'), ('quarter', '2026-Q4'), ('delivery_key', 'other-delivery')):
            original = self.review['scope'][0][key]
            self.review['scope'][0][key] = value
            self.review['scope_hash'] = baseline.digest(self.review['scope'])
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.prepare()
            self.review['scope'][0][key] = original

    def test_missing_analyst_quote_or_section_review_blocks(self):
        quote = self.review['analyst_decision'].pop('quote')
        with self.assertRaises(ValueError):
            self.prepare()
        self.review['analyst_decision']['quote'] = quote
        del self.review['sections']['decisions']
        with self.assertRaises(ValueError):
            self.prepare()

    def test_gantt_basis_requires_gantt_evidence(self):
        self.review['scope'][0]['evidence'][0]['kind'] = 'tasks'
        self.review['scope_hash'] = baseline.digest(self.review['scope'])
        with self.assertRaises(ValueError):
            self.prepare()

    def test_changed_baseline_and_candidate_block_promotion(self):
        entry = self.prepare()
        (self.current / 'domain/README.md').write_text('Other baseline')
        with self.assertRaises(ValueError):
            self.promote(entry)
        (self.current / 'domain/README.md').write_text('before')
        (self.candidate / 'domain/README.md').write_text('Unreviewed candidate')
        with self.assertRaises(ValueError):
            self.promote(entry)

    def test_existing_documentation_snapshot_never_overwritten(self):
        entry = self.prepare()
        target = self.project / 'baseline/versions/documentation/docs-one'
        target.mkdir(parents=True)
        (target / 'README.md').write_text('Immutable existing version')
        with self.assertRaises(ValueError):
            self.promote(entry)
        self.assertEqual((target / 'README.md').read_text(), 'Immutable existing version')

    def test_reconciliation_cli_prepare_and_confirmed_promotion(self):
        review_path = self.candidate.parent / 'review.json'
        review_path.write_text(json.dumps(self.review))
        script = Path(__file__).resolve().parents[1] / 'scripts/baselinectl.py'
        command = [sys.executable, str(script), '--project', str(self.project)]
        prepared = subprocess.run([*command, 'prepare-reconciliation', '--documentation-version', 'docs-one',
                                   '--candidate', str(self.candidate), '--review', str(review_path)], capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        entry = json.loads(prepared.stdout)
        promoted = subprocess.run([*command, 'promote-reconciliation', '--documentation-version', 'docs-one',
                                  '--review-hash', entry['review_hash'], '--analyst-confirmed'], capture_output=True, text=True)
        self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        self.assertEqual(json.loads(promoted.stdout)['status'], 'promoted')

    def test_main_cannot_prepare_or_promote_reconciliation(self):
        entry = self.prepare()
        subprocess.run(['git', '-C', str(self.project), 'symbolic-ref', 'HEAD', 'refs/heads/main'], check=True)
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            self.promote(entry)


if __name__ == "__main__":
    unittest.main()
