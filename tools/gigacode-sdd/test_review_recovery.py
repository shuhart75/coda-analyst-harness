"""Recovery and evidence-based dispute resolution, using isolated fake sessions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from controller import Blocked, Runner, save
import plan_review
import review_cycle
import test_review_cycle as fixtures


class ReviewRecoveryTests(unittest.TestCase):
    setUp = fixtures.ReviewCycleTests.setUp
    result = fixtures.ReviewCycleTests.result
    fake = fixtures.ReviewCycleTests.fake
    pair = fixtures.ReviewCycleTests.pair

    def test_three_interruptions_do_not_exhaust_budget_or_repeat_peer(self):
        class Interrupted(Exception):
            pass
        victim = review_cycle.MODELS[0]
        def interrupt(root, cwd, prompt, role, name, model=None):
            if model == victim:
                raise Interrupted('Stopped by user')
            return self.fake(root, cwd, prompt, role, name, model)
        for _ in range(3):
            with patch.object(self.runner, 'session', side_effect=interrupt):
                with self.assertRaisesRegex(Blocked, 'Stopped by user'):
                    self.pair()
            self.runner = Runner(self.fixture.config)
        with patch.object(self.runner, 'session', side_effect=self.fake):
            self.assertEqual(self.pair()['verdict'], 'pass')
        self.assertEqual(self.counts, {model: 1 for model in review_cycle.MODELS})
        records = [json.loads(p.read_text()) for p in (self.runner.root/'review-cycles').glob('*/*/attempt-*.json')]
        self.assertEqual(sum(r['status'] == 'interrupted' for r in records), 3)

    def test_audited_result_survives_crash_before_complete_record(self):
        with patch.object(self.runner, 'session', side_effect=self.fake):
            expected = self.pair()
        path = next((self.runner.root/'review-cycles').glob('*/*/attempt-1.json'))
        record = json.loads(path.read_text())
        record['status'] = 'running'
        record.pop('review')
        record.pop('evidence')
        save(path, record)
        self.runner = Runner(self.fixture.config)
        with patch.object(self.runner, 'session', side_effect=AssertionError('must reuse result')):
            self.assertEqual(self.pair(), expected)

    def test_controller_receipt_recovers_crash_before_result_record(self):
        with patch.object(self.runner, 'session', side_effect=self.fake):
            expected = self.pair()
        path = next((self.runner.root/'review-cycles').glob('*/*/attempt-1.json'))
        record = json.loads(path.read_text())
        receipt = {key: record[key] for key in ('workspace', 'model', 'result', 'events_sha256', 'workspace_snapshot')}
        save(Path(record['result']['events_path']).parent/'session-receipt.json', receipt)
        record['status'] = 'running'
        for key in ('review', 'evidence', 'result', 'events_sha256', 'workspace_snapshot'):
            record.pop(key)
        save(path, record)
        self.runner = Runner(self.fixture.config)
        with patch.object(self.runner, 'session', side_effect=AssertionError('must recover audited receipt')):
            self.assertEqual(self.pair(), expected)

    def test_transient_exception_retries_without_repeating_successful_peer(self):
        class RetryableSessionError(Exception):
            retryable = True
        attempts = {}
        def flaky(root, cwd, prompt, role, name, model=None):
            attempts[model] = attempts.get(model, 0) + 1
            if model == review_cycle.MODELS[0] and attempts[model] == 1:
                raise RetryableSessionError('Session transport unavailable')
            return self.result(root, model)
        with patch.object(self.runner, 'session', side_effect=flaky):
            self.assertEqual(self.pair()['verdict'], 'pass')
        self.assertEqual(attempts, {review_cycle.MODELS[0]: 2, review_cycle.MODELS[1]: 1})

    def test_repeated_schema_failure_escalates_strategy_and_persists_patch(self):
        prompts = []
        def flaky(root, cwd, prompt, role, name, model=None):
            result = self.fake(root, cwd, prompt, role, name, model)
            if model == review_cycle.MODELS[0]:
                prompts.append(prompt)
                if len(prompts) < 3:
                    result['text'] = 'not JSON'
            return result
        with patch.object(self.runner, 'session', side_effect=flaky):
            self.assertEqual(self.pair()['verdict'], 'pass')
        self.assertIn('two internal steps', prompts[2])
        records = [json.loads(p.read_text()) for p in (self.runner.root/'review-cycles').glob('*/*/attempt-3.json')]
        self.assertEqual(records[0]['correction']['scope'], 'technical-protocol-only')
        self.assertEqual(records[0]['correction_outcome'], 'validated')

    def adjudicated(self, root, model, status, issue):
        required = [] if status in ('optional', 'refuted') else ['REQ-BE']
        findings = [issue['text']] if status in ('confirmed', 'external') else []
        return {'verdict': 'blocked' if status == 'external' else 'changes' if findings else 'pass',
                'findings': findings, 'coverage': {r: 'verified pinned evidence' for r in self.runner.cfg['requirements']},
                'coverage_checks': {r: {'status': 'covered', 'reason': 'Candidate owns this requirement',
                    'evidence': [{'path': '.review-input/plan.json', 'quote': r},
                                 {'path': '.review-input/packet/requirements.md', 'quote': r}]}
                    for r in self.runner.cfg['requirements']},
                'decisions': {issue['id']: {'status': status, 'reason': 'Specific scope comparison',
                    'requirements': required, 'evidence': [
                        {'path': '.review-input/plan.json', 'quote': '"tasks"'},
                        {'path': '.review-input/packet/requirements.md', 'quote': 'REQ-BE: value 2'}]}}}

    def dispute(self, decision):
        origin = review_cycle.MODELS[0]
        seen = []
        def answer(root, cwd, prompt, role, name, model=None):
            if 'TARGETED_ADJUDICATION' in prompt:
                self.assertEqual(model, review_cycle.MODELS[1])
                issues = json.loads((root/'.review-input/disputed-findings.json').read_text())
                seen.append(issues)
                return self.result(root, model, report=self.adjudicated(root, model, decision, issues[0]))
            report = {'verdict': 'changes' if model == origin else 'pass',
                      'findings': ['task be allegedly lacks a check'] if model == origin else [],
                      'coverage': {r: 'candidate check evidence' for r in self.runner.cfg['requirements']}}
            return self.result(root, model, report=report)
        with patch.object(self.runner, 'session', side_effect=answer):
            result = self.pair()
        self.assertEqual(len(seen), 1)
        with patch.object(self.runner, 'session', side_effect=AssertionError('both reviews and adjudication cached')):
            self.assertEqual(self.pair(), result)
        return result

    def test_other_model_refutes_false_finding_without_rewriting_plan(self):
        self.assertEqual(self.dispute('refuted')['verdict'], 'pass')

    def test_confirmed_defect_goes_to_plan_repair(self):
        self.assertEqual(self.dispute('confirmed')['verdict'], 'changes')

    def test_optional_issue_keeps_pass_and_explicit_scope_evidence(self):
        report = self.dispute('optional')
        self.assertEqual(report['verdict'], 'pass')
        self.assertTrue(report['optional_findings'][0]['outside_required_scope'])

    def test_optional_cannot_hide_required_issue_or_invent_file_quote(self):
        root = self.fixture.root/'adjudication'
        root.mkdir()
        sha = plan_review.prepare(root, self.runner.root/'input', self.candidate, root/'missing')
        issue = {'id': 'issue', 'text': 'Mandatory issue'}
        report = self.adjudicated(root, review_cycle.MODELS[0], 'optional', issue)
        report['decisions']['issue']['requirements'] = ['REQ-BE']
        with self.assertRaisesRegex(plan_review.ProtocolError, 'outside required scope'):
            review_cycle.validate_adjudication(root, self.result(root, review_cycle.MODELS[0], report=report),
                                              sha, self.runner.cfg['requirements'], review_cycle.MODELS[0], [issue])
        report['decisions']['issue']['requirements'] = []
        report['decisions']['issue']['evidence'][0]['quote'] = 'fabricated quotation'
        with self.assertRaisesRegex(plan_review.ProtocolError, 'does not match'):
            review_cycle.validate_adjudication(root, self.result(root, review_cycle.MODELS[0], report=report),
                                              sha, self.runner.cfg['requirements'], review_cycle.MODELS[0], [issue])

    def test_refuted_findings_cannot_pass_unresolved_coverage_gap(self):
        root = self.fixture.root/'adjudication'
        root.mkdir()
        sha = plan_review.prepare(root, self.runner.root/'input', self.candidate, root/'missing')
        issue = {'id': 'issue', 'text': 'Alleged defect'}
        report = self.adjudicated(root, review_cycle.MODELS[0], 'refuted', issue)
        report['coverage_checks']['REQ-BE']['status'] = 'gap'
        with self.assertRaisesRegex(plan_review.ProtocolError, 'coverage gaps'):
            review_cycle.validate_adjudication(root, self.result(root, review_cycle.MODELS[0], report=report),
                                              sha, self.runner.cfg['requirements'], review_cycle.MODELS[0], [issue])

    def test_concurrent_candidate_reviews_preserve_both_ledger_rounds(self):
        second = self.runner.root/'plan-candidate-second.json'
        plan = json.loads(self.candidate.read_text())
        plan['tasks'][0]['prompt'] += ' Alternative.'
        save(second, plan)
        barrier = threading.Barrier(4)
        def simultaneous(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.fake(*args, **kwargs)
        with patch.object(self.runner, 'session', side_effect=simultaneous), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(review_cycle.review_pair, self.runner, candidate, 'test', self.runner.cfg['requirements'])
                       for candidate in (self.candidate, second)]
            self.assertTrue(all(f.result()['verdict'] == 'pass' for f in futures))
        ledger = json.loads((self.runner.root/'review-ledger.json').read_text())
        self.assertEqual(len(ledger['rounds']), 2)

    def test_identical_parallel_candidates_share_review_without_attempt_races(self):
        second = self.runner.root/'plan-candidate-identical.json'
        save(second, json.loads(self.candidate.read_text()))
        barrier = threading.Barrier(2)
        def simultaneous(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.fake(*args, **kwargs)
        with patch.object(self.runner, 'session', side_effect=simultaneous), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(review_cycle.review_pair, self.runner, candidate, 'test', self.runner.cfg['requirements'])
                       for candidate in (self.candidate, second)]
            self.assertTrue(all(f.result()['verdict'] == 'pass' for f in futures))
        self.assertEqual(self.counts, {model: 1 for model in review_cycle.MODELS})
        ledger = json.loads((self.runner.root/'review-ledger.json').read_text())
        self.assertEqual(len(ledger['rounds']), 1)

    def with_new_gap(self, root, issue):
        report = self.adjudicated(root, review_cycle.MODELS[1], 'refuted', issue)
        report['coverage_checks']['REQ-BE']['status'] = 'gap'
        text = 'task be omits a newly discovered mandatory negative check'
        report.update(verdict='changes', findings=[text], new_findings=[{
            'id': 'new-negative-check', 'text': text, 'status': 'confirmed',
            'requirements': ['REQ-BE'], 'reason': 'The candidate omits required negative behavior evidence',
            'evidence': [{'path': '.review-input/plan.json', 'quote': 'REQ-BE'},
                         {'path': '.review-input/packet/requirements.md', 'quote': 'REQ-BE: value 2'}]}])
        return report

    def test_new_required_gap_is_a_plan_finding_with_canonical_ledger_identity(self):
        def answer(root, cwd, prompt, role, name, model=None):
            if 'TARGETED_ADJUDICATION' in prompt:
                self.assertIn('new_findings', prompt)
                issue = json.loads((root/'.review-input/disputed-findings.json').read_text())[0]
                return self.result(root, model, report=self.with_new_gap(root, issue))
            changes = model == review_cycle.MODELS[0]
            report = {'verdict': 'changes' if changes else 'pass',
                      'findings': ['Original suspected issue'] if changes else [],
                      'coverage': {r: 'Candidate evidence' for r in self.runner.cfg['requirements']}}
            return self.result(root, model, report=report)
        with patch.object(self.runner, 'session', side_effect=answer):
            report = self.pair()
        self.assertEqual(report['verdict'], 'changes')
        self.assertEqual(len(report['findings']), 1)
        canonical = report['new_findings'][0]
        self.assertEqual(canonical['id'], review_cycle.fingerprint(report['findings'][0])[:16])
        self.assertEqual(canonical['model_id'], 'new-negative-check')
        ledger = json.loads((self.runner.root/'review-ledger.json').read_text())
        self.assertEqual(ledger['rounds'][-1]['issues'], [canonical])
        with patch.object(self.runner, 'session', side_effect=AssertionError('validated new finding should be cached')):
            self.assertEqual(self.pair(), report)

    def test_new_defects_require_unique_ids_required_scope_and_real_packet_evidence(self):
        root = self.fixture.root/'adjudication'
        root.mkdir()
        sha = plan_review.prepare(root, self.runner.root/'input', self.candidate, root/'missing')
        issue = {'id': 'original', 'text': 'Original suspected issue'}
        original = self.with_new_gap(root, issue)
        def duplicate(report):
            report['new_findings'].append(dict(report['new_findings'][0]))
        def missing_packet(report):
            report['new_findings'][0]['evidence'] = report['new_findings'][0]['evidence'][:1]
        def invented(report):
            report['new_findings'][0]['evidence'][0]['quote'] = 'Invented candidate quotation'
        def optional(report):
            report['new_findings'][0]['status'] = 'optional'
        def unknown_requirement(report):
            report['new_findings'][0]['requirements'] = ['REQ-NOT-PINNED']
        def reused_id(report):
            report['new_findings'][0]['id'] = issue['id']
        for change in (duplicate, missing_packet, invented, optional, unknown_requirement, reused_id):
            with self.subTest(change=change.__name__):
                report = json.loads(json.dumps(original))
                change(report)
                with self.assertRaises(plan_review.ProtocolError):
                    review_cycle.validate_adjudication(root, self.result(root, review_cycle.MODELS[1], report=report),
                        sha, self.runner.cfg['requirements'], review_cycle.MODELS[1], [issue])
