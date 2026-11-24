import hashlib
import json
from pathlib import Path
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import patch

from controller import Blocked, Interrupted, RetryableSessionError, digest, read, save
from execution_cycle import execute_task, MODELS, fingerprint, adjudicated_report, _review_copy, CandidateStalled
import test_controller as fixtures
from workspace import clone_snapshot, git, snapshot


class ExecutionPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.runner.cfg['execution_comparison_seconds'] = .05
        self.task = self.fixture.plan['tasks'][0]
        save(self.runner.root/'plan.json', self.fixture.plan)
        self.runner.state['tasks'] = {t['id']: {'status': 'pending'} for t in self.fixture.plan['tasks']}
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
        # Recovery/protocol unit cases may explicitly model an unavailable peer.
        # Scheduling tests exercise both real candidate workers below.
        class UnavailablePeerPool(ThreadPoolExecutor):
            def submit(pool, fn, index):
                def unavailable():
                    raise CandidateStalled('fixture peer unavailable')
                return super().submit(unavailable) if index else super().submit(fn, index)
        pool = patch('execution_cycle.ThreadPoolExecutor', UnavailablePeerPool) if getattr(self, 'primary_only', False) else nullcontext()
        with patch.object(self.runner, 'session', side_effect=session or self.session), \
             patch.object(self.runner, 'checks', side_effect=checks or self.checks), \
             patch.object(self.runner, 'review', side_effect=review or self.review), pool:
            return execute_task(self.runner, self.task, self.record.copy())

    def records(self):
        return [read(p) for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')]

    def test_review_copy_race_freezes_candidate_without_fatal(self):
        from workspace import SnapshotChanged
        self.primary_only = True
        calls = []
        def copying(runner, work, task):
            calls.append(work)
            if len(calls) == 1:
                report = runner.root / 'copy-conflict.json'
                save(report, {'usable': False, 'difference': {'paths': ['backend/value.txt']}})
                raise SnapshotChanged(str(report))
            return _review_copy(runner, work, task)
        with patch('execution_cycle._review_copy', side_effect=copying):
            accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(self.check_count, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(accepted['workspace'], str(self.work))
        self.assertTrue(accepted['verification_checkpoint'])
        self.assertEqual(list((self.runner.root/'execution-cycles').glob('*/fatal.json')), [])
        self.assertTrue(Path(accepted['snapshot_conflicts'][0]).is_file())

    def test_settled_review_copy_rechecks_without_reexecuting_model(self):
        from workspace import SnapshotChanged
        self.primary_only = True
        calls = []
        def copying(runner, work, task):
            calls.append(work)
            if len(calls) == 1:
                report = runner.root / 'copy-conflict.json'
                save(report, {'usable': False, 'difference': {'paths': ['backend/value.txt']}})
                error = SnapshotChanged(str(report))
                error.stabilized = True
                raise error
            return _review_copy(runner, work, task)
        with patch('execution_cycle._review_copy', side_effect=copying):
            accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(self.check_count, 2)
        self.assertEqual(len(self.calls), 1)

    def test_pending_snapshot_question_resumes_through_frozen_checks_without_operator(self):
        import operator_questions as questions
        import snapshot_recovery
        import workspace as ws
        self.primary_only = True
        accepted = self.run_task()
        path = next(p for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')
                    if read(p)['model'] == accepted['model'])
        value = read(path)
        source = Path(value['workspace'])
        log = source/'backend/runtime-evidence.data'
        log.write_text('observed experiment\n')
        value.update(stage='execute', status='waiting-user', workspace_snapshot=snapshot(source))
        value['snapshot_recovery'] = {'previous_candidate': str(path.parent/'old-candidate.json')}
        value['feedback'] = snapshot_recovery.repair_instruction(value['snapshot_recovery']['previous_candidate'])
        key = questions.request(self.runner.root, self.task['id'], 'runtime-placement',
                                'Who redirects the runtime output?', model=value['model'])
        value['question_id'] = key
        save(path,value)
        original_copy = ws._copy_permissions
        def append_live(source_root, dest):
            if Path(source_root) == source:
                with log.open('a') as stream:
                    stream.write('periodic metrics\n')
            return original_copy(source_root,dest)
        initial_sessions, initial_checks = len(self.calls), self.check_count
        with patch.object(ws,'_copy_permissions',side_effect=append_live):
            result = self.run_task()
        self.assertEqual(result['status'],'accepted')
        self.assertNotEqual(result['workspace'],str(source))
        self.assertEqual(len(self.calls),initial_sessions)
        self.assertEqual(self.check_count,initial_checks+1)
        self.assertEqual((Path(result['workspace'])/'backend/runtime-evidence.data').read_text(),
                         'observed experiment\n')
        self.assertIn('periodic metrics',log.read_text())
        self.assertIsNone(questions.response(self.runner.root,key))
        self.assertFalse(questions.questions(self.runner.root,pending_only=True))
        self.assertTrue(read(self.runner.root/'operator-questions'/(key+'-resolved.json'))['operator_action_performed'] is False)

    def test_unrelated_question_is_not_bypassed_by_old_snapshot_recovery(self):
        import verification_checkpoint
        self.primary_only=True
        value=self.run_task()
        value.update(stage='execute',status='waiting-user',feedback='Need a product decision',
                     snapshot_recovery={'previous_candidate':'historical'})
        self.assertFalse(verification_checkpoint.prepare(self.runner,value,self.runner.root/'candidate.json'))
        self.assertEqual(value['status'],'waiting-user')

    def test_resume_execution_without_snapshot_preserves_work_and_rechecks(self):
        self.resume_incomplete_result(json.dumps({'operator_question': {
            'topic': 'stand', 'question': 'Start the stand?'}}))

    def test_resume_legacy_conflicting_controls_without_snapshot(self):
        self.resume_incomplete_result(json.dumps({
            'service_request': [{'kind': 'postgres', 'bind': '127.0.0.1:5434'}],
            'scope_request': {'paths': ['frontend/node_modules']}}))

    def resume_incomplete_result(self, text):
        self.primary_only = True
        accepted = self.run_task()
        path = next(p for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')
                    if read(p)['model'] == accepted['model'])
        value = read(path)
        value.update(stage='execute', status='running')
        value.pop('workspace_snapshot')
        value['session_result']['text'] = text
        old_result = value['session_result'].copy()
        save(path, value)
        before = self.check_count
        def resumed(root, cwd, prompt, role, name, model=None, stop_event=None):
            self.assertEqual((Path(root)/'backend/value.txt').read_text(), '2')
            self.assertIn('previous session ended before workspace audit/export', prompt)
            self.assertNotIn('workspace_snapshot', read(path))
            self.assertNotIn('question_id', read(path))
            return self.session(root, cwd, prompt, role, name, model, stop_event)
        result = self.run_task(session=resumed)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['incomplete_execution_results'], [old_result])
        self.assertGreater(self.check_count, before)

    def test_review_without_snapshot_stays_blocked(self):
        from execution_cycle import _receipt_valid
        with self.assertRaisesRegex(Blocked, 'no completed workspace receipt'):
            _receipt_valid({'workspace': str(self.work), 'stage': 'review'})

    def test_verified_winner_cancels_only_peer_after_comparison_window(self):
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
        self.primary_only = True
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
        self.primary_only = True
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

    def test_check_failures_do_not_exhaust_reviewer_budget(self):
        self.primary_only = True
        reviews = 0
        def checks(*args, **kwargs):
            results = self.checks(*args, **kwargs)
            if self.check_count <= 4:
                results[0]['ok'] = False
            return results
        def review(*args, **kwargs):
            nonlocal reviews
            reviews += 1
            if reviews <= 3:
                raise RetryableSessionError('Missing coverage: REQ-' + str(reviews))
            return self.review(*args, **kwargs)
        accepted = self.run_task(checks=checks, review=review)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['retry_steps']['implementation:repair']['failures'], 4)
        self.assertEqual(accepted['retry_steps']['review:protocol']['failures'], 3)
        self.assertEqual(self.check_count, 5)
        self.assertEqual(len(self.calls), 5)

    def test_review_changes_strategy_and_reuses_draft_across_interruption(self):
        self.primary_only = True
        details = []
        def review(root, cwd, detail, *args, **kwargs):
            details.append(detail)
            if len(details) <= 2:
                raise RetryableSessionError('malformed review', {'text': 'partial observations'})
            draft = read(Path(root)/'.review-input/previous-review-draft.json')
            self.assertEqual(draft['text'], 'partial observations')
            self.assertIn('checklist', detail)
            self.runner.stop.set()
            raise Interrupted('pause review')
        with self.assertRaises(Interrupted):
            self.run_task(review=review)
        self.assertNotEqual(details[1], details[2])
        self.runner.stop.clear()
        def resumed(root, cwd, detail, *args, **kwargs):
            self.assertEqual(read(Path(root)/'.review-input/previous-review-draft.json')['text'], 'partial observations')
            self.assertIn('checklist', detail)
            return self.review(root, cwd, detail, *args, **kwargs)
        accepted = self.run_task(review=resumed)
        self.assertEqual(accepted['retry_steps']['review:protocol']['failures'], 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.check_count, 1)

    def test_review_budget_exhaustion_does_not_repeat_on_plain_resume(self):
        self.primary_only = True
        details = []
        def review(root, cwd, detail, *args, **kwargs):
            details.append(detail)
            raise RetryableSessionError('malformed review')
        exhausted_writes = []
        def journal(path, value):
            if (Path(path).name == 'candidate.json' and value.get('history')
                    and value['history'][-1].get('exhausted')):
                exhausted_writes.append(value['status'])
            save(path, value)
        with patch('controller.save', side_effect=journal), self.assertRaises(Blocked):
            self.run_task(review=review)
        self.assertTrue(exhausted_writes)
        self.assertEqual(set(exhausted_writes), {'stalled'})
        self.assertEqual(len(details), 3)
        self.assertIn('checklist', details[-1])
        with self.assertRaises(Blocked):
            self.run_task(review=review)
        self.assertEqual(len(details), 3)
        self.assertEqual(self.check_count, 1)

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
        self.assertIn(first['status'], ('interrupted', 'stalled'))
        self.assertGreaterEqual(first['failures'], 1)
        self.assertLessEqual(first['failures'], 6)

    def test_shared_resource_review_overlaps_peer_execution_and_compares_results(self):
        self.task['resources'] = ['database']
        self.runner.cfg['execution_comparison_seconds'] = 30
        second_executed = threading.Event()
        reviewing = threading.Event()
        active = set()
        def session(*args, **kwargs):
            self.assertFalse(active)
            active.add(kwargs['model'])
            result = self.session(*args, **kwargs)
            if kwargs['model'] == MODELS[1]:
                second_executed.set()
            return result
        def checks(*args, **kwargs):
            result = self.checks(*args, **kwargs)
            active.clear()
            return result
        def review(*args, **kwargs):
            report = self.review(*args, **kwargs)
            if kwargs['model'] == MODELS[1]:
                reviewing.set()
                self.assertTrue(second_executed.wait(30))
                report['optional_findings'] = [{'status': 'optional', 'text': 'fixture validated optional gap'}]
            else:
                self.assertTrue(reviewing.wait(30))
            return report
        accepted = self.run_task(session=session, checks=checks, review=review)
        self.assertEqual(accepted['model'], MODELS[1])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(v['status'] == 'accepted' for v in self.records()))
        selection = read(next((self.runner.root/'execution-cycles').glob('*/selection.json')))
        self.assertEqual(len(selection['compared']), 2)

    def test_no_progress_budget_stays_bounded_without_usable_peer(self):
        self.primary_only = True
        def failed(*args, **kwargs):
            result = self.checks(*args, **kwargs)
            result[0]['ok'] = False
            return result
        with self.assertRaisesRegex(Blocked, 'No verified execution candidate'):
            self.run_task(checks=failed)
        first = next(v for v in self.records() if v['model'] == MODELS[0])
        self.assertEqual(first['failures'], 6)
        self.assertEqual(first['status'], 'stalled')

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
            barrier.wait(timeout=30)
            if kwargs['model'] == MODELS[1]:
                # The peer creates a real Git review copy before releasing us.
                # Its Windows filesystem latency is not part of this assertion.
                self.assertTrue(release.wait(30))
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

    def test_cleanup_failure_cannot_replace_peer_safety_failure(self):
        from controller import OperationalBlocked
        barrier = threading.Barrier(2)
        def session(*args, **kwargs):
            barrier.wait(timeout=30)
            if kwargs['model'] == MODELS[0]:
                raise Blocked('Agent changed Git configuration or hooks')
            self.assertTrue(kwargs['stop_event'].wait(30))
            raise OperationalBlocked('Process cleanup incomplete')
        with self.assertRaisesRegex(Blocked, 'Git configuration'):
            self.run_task(session=session)
        fatal = read(next((self.runner.root/'execution-cycles').glob('*/fatal.json')))
        self.assertEqual(fatal['type'], 'Blocked')
        self.assertEqual({v['type'] for v in fatal['related_failures']}, {'Blocked','OperationalBlocked'})

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

    def test_stalled_execution_renews_budget_after_update_preserving_work(self):
        self.task['resources'] = ['database']
        def failed_checks(*args, **kwargs):
            results = self.checks(*args, **kwargs)
            results[0]['ok'] = False
            return results
        with patch('execution_cycle.runtime_revision', return_value='old'):
            with self.assertRaisesRegex(Blocked, 'No verified execution candidate'):
                self.run_task(checks=failed_checks)
            with self.assertRaisesRegex(Blocked, 'No verified execution candidate'):
                self.run_task(session=lambda *a, **k: self.fail('must not execute again'))
        before = {r['model']: r['workspace'] for r in self.records()}
        with patch('execution_cycle.runtime_revision', return_value='new'):
            accepted = self.run_task()
        self.assertEqual(accepted['workspace'], before[accepted['model']])
        self.assertEqual(accepted['budget_history'][0]['counters']['failures'], 6)
        self.assertGreaterEqual(len(accepted['history']), 6)

    def test_agreed_review_finding_goes_directly_to_repair(self):
        self.primary_only = True
        def review(*args, **kwargs):
            report = self.review(*args, **kwargs)
            if self.review_count == 1:
                report.update(verdict='changes', findings=['Fix backend behavior'])
            return report
        with patch.object(self.runner, 'resolve_review_issue', side_effect=AssertionError('no dispute')):
            accepted = self.run_task(review=review)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.check_count, 2)
        self.assertEqual(self.review_count, 2)

    def test_adjudication_accepts_markdown_typos_and_paraphrases(self):
        root = _review_copy(self.runner, self.work, self.task)
        (root/'backend/value.txt').write_text('2')
        report = self.review(root, '.', '', ['REQ-BE'], 'be-review', model=MODELS[1])
        issue = 'Required backend behavior is absent'
        references = [{'path': 'backend/value.txt', 'quote': 'Реализовано значние **2**'},
                      {'path': '.review-input/packet/requirements.md', 'quote': '**REQ-BE** требует значние 2'}]
        report['issue_dispositions'] = {fingerprint(issue)[:16]: {
            'status': 'refuted', 'requirements': ['REQ-BE'], 'reason': 'Existing behavior covers the requirement.',
            'evidence': references}}
        report['coverage_checks']['REQ-BE']['evidence'] = [dict(item) for item in references]
        reviewed = adjudicated_report(root, self.task, {'findings': [issue]}, report)
        self.assertEqual(reviewed['verdict'], 'pass')
        self.assertEqual(reviewed['issue_dispositions'][fingerprint(issue)[:16]]['evidence'][0]['quote'],
                         'Реализовано значние **2**')

    def test_adjudication_uses_issue_ids_not_repeated_wording(self):
        root = _review_copy(self.runner, self.work, self.task)
        (root/'backend/value.txt').write_text('2')
        report = self.review(root, '.', '', ['REQ-BE'], 'be-review', model=MODELS[1])
        issue = 'Required backend behavior is absent'
        report.update(verdict='changes', findings=['Missing **поведние** in backend'])
        report['issue_dispositions'] = {fingerprint(issue)[:16]: {
            'status': 'confirmed', 'requirements': ['REQ-BE'], 'reason': 'Required behavior is missing.',
            'evidence': [{'path': 'backend/value.txt', 'quote': '2'},
                         {'path': '.review-input/packet/requirements.md', 'quote': 'REQ-BE'}]}}
        reviewed = adjudicated_report(root, self.task, {'findings': [issue]}, report)
        self.assertEqual(reviewed['findings'], [issue])
        self.assertEqual(reviewed['reported_findings'], ['Missing **поведние** in backend'])

    def test_legacy_adjudication_returns_findings_without_discarding_code(self):
        self.primary_only = True
        accepted = self.run_task()
        path = next(p for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')
                    if read(p)['model'] == accepted['model'])
        value = read(path)
        original = dict(value['review'], verdict='changes', findings=['Check backend behavior'])
        value.update(stage='adjudicate', status='interrupted', original_review=original)
        value.pop('review_resolution', None)
        checks = value['checks']
        save(path, value)
        def resumed(root, cwd, prompt, role, name, model=None, stop_event=None):
            self.assertEqual((Path(root)/'backend/value.txt').read_text(), '2')
            self.assertIn('Check backend behavior', prompt)
            self.assertIn('REVIEW_FEEDBACK', prompt)
            self.assertEqual(read(path)['checks'], checks)
            return self.session(root, cwd, prompt, role, name, model, stop_event)
        self.assertEqual(self.run_task(session=resumed)['status'], 'accepted')

    def test_new_instruction_invalidates_accepted_candidate_cache(self):
        self.primary_only = True
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
        # An audited receipt includes the real terminal event, not just a mock flag.
        events = folder/'stdout.jsonl'
        events.write_text(events.read_text() + json.dumps({
            'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'}) + '\n')
        from transport import parse_events
        result.update(parse_events(events))
        result['returncode'] = 0
        full_prompt = 'Fixture controller context\n' + prompt
        (folder/'prompt.txt').write_text(full_prompt, encoding='utf-8', newline='\n')
        save(folder/'session-receipt.json', {
            'result':result, 'events_sha256':digest(result['events_path']),
            'workspace_snapshot':snapshot(root,normalize_settings=True),
            'workspace':str(Path(root).resolve()), 'model':model,
            'prompt_sha256':hashlib.sha256(full_prompt.encode()).hexdigest(), 'decision_sha256':None})
        return result

    def test_review_transport_wait_preserves_prompt_and_completed_work(self):
        self.primary_only = True
        self.runner.cfg['recovery_retry_seconds'] = 0
        prompts = []
        def review(*args, **kwargs):
            prompts.append(args[2])
            if len(prompts) == 1:
                raise RetryableSessionError('Network unavailable', cause='transport')
            return self.review(*args, **kwargs)
        accepted = self.run_task(review=review)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.check_count, 1)
        self.assertEqual(accepted['retry_steps']['review:transport']['failures'], 1)
        self.assertNotIn('review:protocol', accepted['retry_steps'])
        self.assertFalse(accepted['history'][-1]['decision']['instruction_change'])

    def test_transport_draft_is_reused_without_protocol_amendment(self):
        self.primary_only = True
        self.runner.cfg['recovery_retry_seconds'] = 0
        calls = []
        def review(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 1:
                raise RetryableSessionError('Interrupted transport', {'text': 'Observed implementation evidence'}, cause='timeout')
            draft = read(Path(args[0])/'.review-input/previous-review-draft.json')
            self.assertEqual(draft['text'], 'Observed implementation evidence')
            self.assertNotIn('Controller technical correction:', args[2])
            self.assertIn('untrusted diagnostic data', args[2])
            return self.review(*args, **kwargs)
        self.assertEqual(self.run_task(review=review)['status'], 'accepted')
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_retry_wait_resumes_without_spending_failure(self):
        self.primary_only = True
        self.runner.cfg['recovery_retry_seconds'] = 0
        def broken(*args, **kwargs):
            raise RetryableSessionError('Temporary transport', cause='transport')
        def cancel(*args):
            self.runner.stop.set()
            return False
        with patch('execution_cycle.wait_until', side_effect=cancel):
            with self.assertRaises(Interrupted): self.run_task(review=broken)
        self.runner.stop.clear()
        state = next(v for v in self.records() if v['model'] == MODELS[0])
        self.assertEqual(state['retry_steps']['review:transport']['failures'], 1)
        self.assertIn('recovery_wait', state)
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['retry_steps']['review:transport']['failures'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_crash_after_audited_execution_resumes_without_duplicate_edits(self):
        self.primary_only = True
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
        self.primary_only = True
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
        self.primary_only = True
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
