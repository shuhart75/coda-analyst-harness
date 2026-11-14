import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from controller import Blocked, Interrupted, RetryableSessionError, digest, read, save
from execution_cycle import execute_task, MODELS, fingerprint, adjudicated_report, _review_copy
import test_controller as fixtures
from workspace import clone_snapshot, git, snapshot


class ExecutionPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.task = self.fixture.plan['tasks'][0]
        self.work = self.runner.root / 'workers' / 'original'
        clone_snapshot(self.runner.main, self.work)
        self.record = {'workspace': str(self.work), 'base': git(self.work, 'rev-parse', 'HEAD'),
                       'attempts': 0, 'status': 'running'}
        self.calls = []
        self.check_count = 0
        self.review_count = 0
        self.lock = threading.Lock()
        self.audit = patch.object(self.runner, 'audit')
        self.audit.start()
        self.addCleanup(self.audit.stop)
        self.runner.progress = lambda message: None

    def result(self, root, model, reported=None):
        folder = self.runner.logdir('portfolio-test')
        folder.mkdir(parents=True)
        events = folder / 'stdout.jsonl'
        events.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'model': reported or model}) + '\n')
        return {'ok': True, 'cancelled': False, 'events_path': str(events), 'errors': [],
                'returncode': 0, 'terminal_result': True}

    def session(self, root, cwd, prompt, role, name, model=None, stop_event=None):
        with self.lock:
            self.calls.append((str(root), model, prompt))
        (Path(root) / 'backend/value.txt').write_text('2')
        return self.result(root, model)

    def checks(self, root, checks, name, stop_event=None):
        with self.lock:
            self.check_count += 1
        folder = self.runner.logdir('checks-test')
        folder.mkdir(parents=True)
        output = folder / 'output.log'
        output.write_text('pass')
        return [{'ok': True, 'log': str(output)}]

    def review(self, root, cwd, detail, requirements, name, model=None, stop_event=None):
        with self.lock:
            self.review_count += 1
        self.assertEqual((Path(root) / 'backend/value.txt').read_text(), '2')
        result = self.result(root, model)
        return {'verdict': 'pass', 'findings': [], 'coverage': {'REQ-BE': 'actual file and check'},
                'coverage_checks': {'REQ-BE': {'status': 'covered', 'reason': 'file implements pinned requirement',
                    'evidence': [{'path':'backend/value.txt','quote':'2'},
                                 {'path':'.review-input/packet/requirements.md','quote':'REQ-BE: value 2'}]}},
                'model_reported': [model], 'events_path': result['events_path'],
                'events_sha256': digest(result['events_path'])}

    def run_task(self, session=None, checks=None, review=None):
        with patch.object(self.runner, 'session', side_effect=session or self.session), \
             patch.object(self.runner, 'checks', side_effect=checks or self.checks), \
             patch.object(self.runner, 'review', side_effect=review or self.review):
            return execute_task(self.runner, self.task, self.record.copy())

    def records(self):
        return [read(p) for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')]

    def test_parallel_first_verified_winner_cancels_only_other_variant(self):
        barrier = threading.Barrier(2)
        def concurrent(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.session(root, cwd, prompt, role, name, model, stop_event)
            barrier.wait(timeout=5)
            if model == MODELS[1]:
                self.assertTrue(stop_event.wait(5))
                raise Interrupted('local cancellation')
            return result
        accepted = self.run_task(session=concurrent)
        self.assertEqual(accepted['model'], MODELS[0])
        self.assertEqual(accepted['reviewer_model'], MODELS[1])
        self.assertFalse(self.runner.stop.is_set())
        self.assertEqual(len({c[0] for c in self.calls}), 2)
        loser = next(v for v in self.records() if v['model'] == MODELS[1])
        self.assertEqual(loser['status'], 'interrupted')
        self.assertEqual(loser['attempts'], 0)
        self.assertEqual((Path(loser['workspace'])/'backend/value.txt').read_text(), '2')
        checkpoint = self.work.parent / (self.work.name+'-checkpoint.json')
        self.assertEqual(read(checkpoint)['patch']['sha256'], accepted['patch']['sha256'])

    def test_interrupted_variants_resume_wip_without_charging_attempts(self):
        barrier = threading.Barrier(2)
        def cancel(root, cwd, prompt, role, name, model=None, stop_event=None):
            self.session(root,cwd,prompt,role,name,model,stop_event)
            barrier.wait(timeout=5)
            self.runner.stop.set()
            raise Interrupted('user pause')
        with self.assertRaises(Interrupted):
            self.run_task(session=cancel)
        self.assertEqual([v['attempts'] for v in self.records()], [0,0])
        self.assertEqual([v['failures'] for v in self.records()], [0,0])
        self.runner.stop.clear()
        def resume(root,cwd,prompt,role,name,model=None,stop_event=None):
            self.assertEqual((Path(root)/'backend/value.txt').read_text(), '2')
            return self.session(root,cwd,prompt,role,name,model,stop_event)
        self.assertEqual(self.run_task(session=resume)['status'], 'accepted')

    def test_resume_review_does_not_repeat_execution_or_passed_checks(self):
        self.task['resources'] = ['database']
        def cancel(*args, **kwargs):
            self.runner.stop.set()
            raise Interrupted('paused in review')
        with self.assertRaises(Interrupted):
            self.run_task(review=cancel)
        self.runner.stop.clear()
        self.assertEqual(next(v for v in self.records() if v['model'] == MODELS[0])['stage'], 'review')
        def forbidden(*args, **kwargs):
            raise AssertionError('completed stage repeated')
        self.assertEqual(self.run_task(session=forbidden, checks=forbidden)['status'], 'accepted')
        self.assertEqual(self.check_count, 1)

    def test_transient_review_retry_preserves_successful_execution_and_checks(self):
        self.task['resources'] = ['database']
        count = 0
        def transient(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise RetryableSessionError('malformed review')
            return self.review(*args, **kwargs)
        accepted = self.run_task(review=transient)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.check_count, 1)
        self.assertEqual(count, 2)
        self.assertEqual(accepted['history'][0]['kind'], 'review-protocol')
        self.assertEqual(accepted['history'][0]['result'], 'verified')

    def test_shared_resources_serialize_and_no_progress_switches_model(self):
        self.task['resources'] = ['database']
        active = set()
        current_model = {}
        def session(*args, **kwargs):
            self.assertFalse(active)
            active.add(kwargs['model'])
            current_model[str(args[0])] = kwargs['model']
            return self.session(*args, **kwargs)
        def checks(root, *args, **kwargs):
            results = self.checks(root, *args, **kwargs)
            model = current_model[str(root)]
            active.remove(model)
            if model == MODELS[0]:
                results[0]['ok'] = False
            return results
        accepted = self.run_task(session=session, checks=checks)
        self.assertEqual(accepted['model'], MODELS[1])
        first = next(v for v in self.records() if v['model'] == MODELS[0])
        self.assertEqual(first['status'], 'stalled')
        self.assertEqual(first['failures'], 6)
        self.assertEqual({v['strategy'] for v in first['history']}, {0,1,2})

    def test_cache_and_review_evidence_integrity(self):
        self.task['resources'] = ['database']
        accepted = self.run_task()
        def forbidden(*args, **kwargs):
            raise AssertionError('cached work repeated')
        self.assertEqual(self.run_task(session=forbidden, checks=forbidden, review=forbidden)['patch'], accepted['patch'])
        events = Path(accepted['review']['events_path'])
        events.write_text(events.read_text()+'tamper')
        with self.assertRaisesRegex(Blocked, 'evidence changed'):
            self.run_task(session=forbidden)

    def test_safety_failure_is_not_hidden_by_successful_peer(self):
        barrier = threading.Barrier(2)
        release = threading.Event()
        def session(*args, **kwargs):
            result = self.session(*args, **kwargs)
            barrier.wait(timeout=5)
            if kwargs['model'] == MODELS[1]:
                self.assertTrue(release.wait(5))
                raise Blocked('Agent changed permission settings')
            return result
        def review(*args, **kwargs):
            result = self.review(*args, **kwargs)
            release.set()
            return result
        with self.assertRaisesRegex(Blocked, 'permission settings'):
            self.run_task(session=session, review=review)
        self.assertFalse((self.work.parent/(self.work.name+'-checkpoint.json')).exists())
        with self.assertRaisesRegex(Blocked, 'Preserved safety failure'):
            self.run_task()

    def test_wrong_model_cannot_win(self):
        self.task['resources'] = ['database']
        def wrong(*args, **kwargs):
            result = self.session(*args, **kwargs)
            if kwargs['model'] == MODELS[0]:
                return self.result(args[0], MODELS[0], reported='wrong-model')
            return result
        self.assertEqual(self.run_task(session=wrong)['model'], MODELS[1])
        first = next(v for v in self.records() if v['model'] == MODELS[0])
        self.assertTrue(all(h['kind'] == 'model' for h in first['history']))

    def test_false_review_finding_is_refuted_without_repeating_executor(self):
        self.task['resources'] = ['database']
        issue = 'Required backend behavior is absent'
        def review(root,cwd,detail,requirements,name,model=None,stop_event=None):
            report = self.review(root,cwd,detail,requirements,name,model,stop_event)
            if name.endswith('-adjudicate'):
                self.assertEqual(model, MODELS[0])
                report['issue_dispositions'] = {fingerprint(issue)[:16]: {
                    'status': 'refuted', 'requirements': ['REQ-BE'],
                    'reason': 'The actual implementation and passing check already cover it.',
                    'evidence': [{'path': 'backend/value.txt', 'quote': '2'},
                                 {'path':'.review-input/packet/requirements.md','quote':'REQ-BE: value 2'}]}}
            else:
                report.update(verdict='changes', findings=[issue])
            return report
        accepted = self.run_task(review=review)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.check_count, 1)
        self.assertEqual(self.review_count, 2)
        self.assertEqual(accepted['original_review']['verdict'], 'changes')
        self.assertEqual(accepted['review']['verdict'], 'pass')

    def test_bad_adjudication_evidence_retries_only_adjudication(self):
        self.task['resources'] = ['database']
        issue = 'Required backend behavior is absent'
        count = 0
        def review(root,cwd,detail,requirements,name,model=None,stop_event=None):
            nonlocal count
            report = self.review(root,cwd,detail,requirements,name,model,stop_event)
            if name.endswith('-adjudicate'):
                count += 1
                report['issue_dispositions'] = {fingerprint(issue)[:16]: {
                    'status': 'refuted', 'requirements': ['REQ-BE'], 'reason': 'Existing implementation covers it.',
                    'evidence': [{'path': 'backend/value.txt', 'quote': 'invented' if count == 1 else '2'},
                                 {'path':'.review-input/packet/requirements.md','quote':'REQ-BE: value 2'}]}}
                if count == 2:
                    self.assertIn('Previous protocol error', detail)
            else:
                report.update(verdict='changes', findings=[issue])
            return report
        accepted = self.run_task(review=review)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.check_count, 1)
        self.assertEqual(count, 2)
        self.assertEqual(accepted['history'][0]['kind'], 'review-protocol')

    def test_new_instruction_invalidates_accepted_candidate_cache(self):
        self.task['resources'] = ['database']
        first = self.run_task()
        (self.runner.root/'decision.md').write_text('New execution constraint')
        second = self.run_task()
        self.assertNotEqual(first['portfolio_scope'], second['portfolio_scope'])
        self.assertEqual(len(self.calls), 2)


    def test_mandatory_gap_cannot_be_downgraded_to_optional(self):
        root = _review_copy(self.runner, self.work, self.task)
        (root/'backend/value.txt').write_text('2')
        report = self.review(root, '.', '', ['REQ-BE'], 'be-review', model=MODELS[1])
        report['coverage_checks']['REQ-BE']['status'] = 'gap'
        issue = 'Required backend behavior is absent'
        report['issue_dispositions'] = {fingerprint(issue)[:16]: {
            'status':'optional', 'requirements':[], 'outside_required_scope':True,
            'reason':'Incorrectly treated as cosmetic',
            'evidence':[{'path':'backend/value.txt','quote':'2'},
                        {'path':'.review-input/packet/requirements.md','quote':'REQ-BE: value 2'}]}}
        with self.assertRaisesRegex(RetryableSessionError, 'Mandatory coverage gap'):
            adjudicated_report(root, self.task, {'findings':[issue]}, report)

    def test_refutation_without_pinned_requirement_evidence_is_rejected(self):
        root = _review_copy(self.runner, self.work, self.task)
        (root/'backend/value.txt').write_text('2')
        report = self.review(root, '.', '', ['REQ-BE'], 'be-review', model=MODELS[1])
        issue = 'Required backend behavior is absent'
        report['issue_dispositions'] = {fingerprint(issue)[:16]: {
            'status':'refuted', 'requirements':['REQ-BE'], 'reason':'Code alone says so',
            'evidence':[{'path':'backend/value.txt','quote':'2'}]}}
        with self.assertRaisesRegex(RetryableSessionError, 'pinned requirements'):
            adjudicated_report(root, self.task, {'findings':[issue]}, report)

    def audited_session_result(self, root, cwd, prompt, role, name, model=None, stop_event=None):
        result = self.session(root,cwd,prompt,role,name,model,stop_event)
        original = Path(result['events_path']).parent
        folder = self.runner.logdir(name)
        original.rename(folder)
        result['events_path'] = str(folder/'stdout.jsonl')
        full_prompt = 'Fixture controller context\n' + prompt
        (folder/'prompt.txt').write_text(full_prompt, encoding='utf-8', newline='\n')
        save(folder/'session-receipt.json', {
            'result':result, 'events_sha256':digest(result['events_path']),
            'workspace_snapshot':snapshot(root,normalize_settings=True),
            'workspace':str(Path(root).resolve()), 'model':model,
            'prompt_sha256':hashlib.sha256(full_prompt.encode()).hexdigest(), 'decision_sha256':None})
        return result

    def test_crash_after_audited_execution_resumes_without_duplicate_edits(self):
        self.task['resources'] = ['database']
        class Crash(BaseException):
            pass
        def crash(*args, **kwargs):
            self.audited_session_result(*args, **kwargs)
            raise Crash('process stopped before caller saved checks stage')
        with self.assertRaises(Crash):
            self.run_task(session=crash)
        current = next(v for v in self.records() if v['model'] == MODELS[0])
        self.assertEqual(current['stage'], 'execute')
        self.assertEqual(current['attempts'], 0)
        self.assertIn('pending_session', current)
        def forbidden(*args, **kwargs):
            raise AssertionError('successful edits executed twice')
        accepted = self.run_task(session=forbidden)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['attempts'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('recovered_receipt', accepted)

    def test_old_successful_receipt_is_not_reused_for_new_repair_attempt(self):
        self.task['resources'] = ['database']
        class Crash(BaseException):
            pass
        count = 0
        def session(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise Crash('repair invocation stopped before auditing')
            return self.audited_session_result(*args, **kwargs)
        def first_check_fails(*args, **kwargs):
            result = self.checks(*args, **kwargs)
            result[0]['ok'] = False
            return result
        with self.assertRaises(Crash):
            self.run_task(session=session, checks=first_check_fails)
        current = next(v for v in self.records() if v['model'] == MODELS[0])
        self.assertEqual(current['attempts'], 1)
        self.assertEqual(current['failures'], 1)
        accepted = self.run_task()
        self.assertEqual(accepted['attempts'], 2)
        self.assertNotIn('recovered_receipt', accepted)
        self.assertEqual(len(self.calls), 2)

    def test_audited_receipt_with_changed_events_is_not_replayed_or_reexecuted(self):
        self.task['resources'] = ['database']
        class Crash(BaseException):
            pass
        events = []
        def crash(*args, **kwargs):
            result = self.audited_session_result(*args, **kwargs)
            events.append(Path(result['events_path']))
            raise Crash()
        with self.assertRaises(Crash):
            self.run_task(session=crash)
        events[0].write_text(events[0].read_text()+'tampered')
        def forbidden(*args, **kwargs):
            raise AssertionError('corrupt receipt silently reexecuted')
        with self.assertRaisesRegex(Blocked, 'session receipt changed'):
            self.run_task(session=forbidden)

    def new_gap_report(self):
        root = _review_copy(self.runner, self.work, self.task)
        (root/'backend/value.txt').write_text('2')
        report = self.review(root, '.', '', ['REQ-BE'], 'be-review', model=MODELS[1])
        old = 'Entire backend implementation is absent'
        evidence = [{'path':'backend/value.txt','quote':'2'},
                    {'path':'.review-input/packet/requirements.md','quote':'REQ-BE: value 2'}]
        report['issue_dispositions'] = {fingerprint(old)[:16]: {
            'status':'refuted', 'requirements':['REQ-BE'], 'reason':'Implementation exists', 'evidence':evidence}}
        report['coverage_checks']['REQ-BE']['status'] = 'gap'
        new = 'Required boundary scenario remains uncovered'
        report['new_findings'] = [{'id':'new-boundary', 'text':new, 'status':'confirmed',
                                  'requirements':['REQ-BE'], 'reason':'New required coverage gap',
                                  'evidence':evidence}]
        report.update(verdict='changes', findings=[new])
        return root, {'findings':[old]}, report

    def test_new_required_gap_is_a_substantive_defect_not_protocol_failure(self):
        root, original, report = self.new_gap_report()
        result = adjudicated_report(root, self.task, original, report)
        self.assertEqual(result['verdict'], 'changes')
        self.assertEqual(result['findings'], ['Required boundary scenario remains uncovered'])
        self.assertEqual(result['new_findings'][0]['model_id'], 'new-boundary')
        self.assertEqual(result['new_findings'][0]['id'], fingerprint(result['findings'][0])[:16])

    def test_new_gap_without_pinned_evidence_is_rejected(self):
        root, original, report = self.new_gap_report()
        report['new_findings'][0]['evidence'] = [{'path':'backend/value.txt','quote':'2'}]
        with self.assertRaisesRegex(RetryableSessionError, 'packet and actual code evidence'):
            adjudicated_report(root, self.task, original, report)

    def test_new_gap_cannot_reuse_original_issue_identifier(self):
        root, original, report = self.new_gap_report()
        report['new_findings'][0]['id'] = fingerprint(original['findings'][0])[:16]
        with self.assertRaisesRegex(RetryableSessionError, 'unique required defect'):
            adjudicated_report(root, self.task, original, report)


if __name__ == '__main__':
    unittest.main()
