"""Failed tool attempts are not successful evidence, nor automatic task rejection."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from controller import Blocked, digest, read, save
from execution_cycle import MODELS
from transport import parse_events, execution_result_usable
import test_execution_cycle as fixtures


class ExecutionDenialTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExecutionPortfolioTests.setUp(self)
        self.primary_only = True
    result = fixtures.ExecutionPortfolioTests.result
    session = fixtures.ExecutionPortfolioTests.session
    checks = fixtures.ExecutionPortfolioTests.checks
    review = fixtures.ExecutionPortfolioTests.review
    run_task = fixtures.ExecutionPortfolioTests.run_task
    records = fixtures.ExecutionPortfolioTests.records
    audited_session_result = fixtures.ExecutionPortfolioTests.audited_session_result

    def denied_session(self, *args, **kwargs):
        result = self.audited_session_result(*args, **kwargs)
        events = Path(result['events_path'])
        rows = [{'type': 'system', 'subtype': 'init', 'model': kwargs.get('model', MODELS[0])},
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'probe', 'name': 'run_shell_command',
                     'input': {'command': 'git config --get core.hooksPath'}}]}},
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'probe', 'is_error': True,
                     'content': 'Shell security policy denied this command: policy restriction'}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Ready for checks',
                 'permission_denials': [{'tool_name': 'run_shell_command', 'tool_use_id': 'probe'}]}]
        events.write_text('\n'.join(map(json.dumps, rows))+'\n')
        parsed = parse_events(events); parsed.update(returncode=0, cleanup={'preserved': False})
        receipt_path = events.parent/'session-receipt.json'; receipt = read(receipt_path)
        receipt.update(result=parsed, events_sha256=digest(events)); save(receipt_path, receipt)
        return parsed

    def service_session(self, *args, **kwargs):
        result = self.denied_session(*args, **kwargs)
        events = Path(result['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        rows[-1]['result'] = json.dumps({'service_request': {'id': 'stand'}})
        events.write_text('\n'.join(map(json.dumps, rows)) + '\n')
        result = parse_events(events)
        result.update(returncode=0, cleanup={'preserved': False})
        receipt_path = events.parent/'session-receipt.json'
        receipt = read(receipt_path)
        receipt.update(result=result, events_sha256=digest(events))
        save(receipt_path, receipt)
        return result

    def test_completed_tool_failures_do_not_hide_service_prerequisite(self):
        from operator_questions import WaitingForOperator
        with patch.object(self.runner.services, 'prepare', return_value={'identity': 'service-id', 'question_id': 'q'}) as prepare:
            with self.assertRaises(WaitingForOperator):
                self.run_task(session=self.service_session)
        prepare.assert_called_once()
        self.assertEqual((self.check_count, self.review_count), (0, 0))
        self.assertTrue(any(v.get('pending_control', [None])[0] == 'service_request' for v in self.records()))

    def test_resume_returns_previously_ignored_prerequisite_to_executor(self):
        self.run_task(session=self.denied_session)
        path = next(p for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')
                    if read(p)['model'] == MODELS[0])
        value = read(path)
        events = Path(value['session_result']['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        rows[-1]['result'] = json.dumps({'service_request': {'id': 'stand'}})
        events.write_text('\n'.join(map(json.dumps, rows))+'\n')
        restored = parse_events(events)
        restored.update(returncode=0, cleanup={'preserved': False})
        value.update(status='stalled', stage='review', session_result=restored)
        value['evidence_files'][str(events)] = digest(events)
        save(path, value)
        previous = (self.check_count, self.review_count)
        from operator_questions import WaitingForOperator
        with patch.object(self.runner.services, 'prepare', return_value={'identity': 'service-id', 'question_id': 'q'}) as prepare:
            with self.assertRaises(WaitingForOperator):
                self.run_task(session=self.service_session)
        prepare.assert_called_once()
        self.assertEqual((self.check_count, self.review_count), previous)
        self.assertTrue(read(path)['control_recovery'])

    def test_transport_failure_cannot_launch_service_or_skip_to_checks(self):
        self.runner.cfg['recovery_retry_seconds'] = .001
        def broken(*args, **kwargs):
            result = self.service_session(*args, **kwargs)
            result['errors'].append('Unaccounted transport failure')
            return result
        with patch.object(self.runner.services, 'prepare') as prepare:
            with self.assertRaises(Blocked):
                self.run_task(session=broken)
        prepare.assert_not_called()
        self.assertEqual((self.check_count, self.review_count), (0, 0))

    def old_gate(self, *args, **kwargs):
        result = self.denied_session(*args, **kwargs)
        raise Blocked('Permission preflight/runtime denial; inspect ' + result['events_path'])

    def timed_out_session(self, *args, **kwargs):
        result = self.denied_session(*args, **kwargs)
        events = Path(result['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()][:-1]
        rows[2]['message']['content'][0]['content'] = 'Shell security policy denied this command: shell.python3.wrapper'
        events.write_text('\n'.join(map(json.dumps, rows))+'\n')
        result = parse_events(events)
        result.update(returncode=-15, timed_out=True, cleanup={'preserved':False})
        result['errors'].append('Session process timeout; partial events preserved')
        receipt = read(events.parent/'session-receipt.json')
        receipt.update(result=result,events_sha256=digest(events))
        save(events.parent/'session-receipt.json',receipt)
        return result

    def test_old_timeout_and_denial_recovers_work_through_checks_and_review(self):
        self.task['resources'] = ['database']
        def old(*args, **kwargs):
            result = self.timed_out_session(*args, **kwargs)
            raise Blocked('Permission preflight/runtime denial; inspect ' + result['events_path'])
        with self.assertRaisesRegex(Blocked, 'Permission preflight'):
            self.run_task(session=old)
        def forbidden(*args, **kwargs): raise AssertionError('preserved implementation repeated')
        accepted = self.run_task(session=forbidden)
        self.assertTrue(accepted['session_result']['timed_out'])
        self.assertFalse(accepted['session_result']['terminal_result'])
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (1, 1, 1))
        self.assertEqual(accepted['status'], 'accepted')
        self.assertFalse(list((self.runner.root/'execution-cycles').glob('*/fatal.json')))

    def test_timeout_is_not_a_fatal_that_cancels_the_other_model(self):
        self.primary_only = False
        self.task['resources'] = ['database']
        def execute(*args, **kwargs):
            if kwargs['model'] == MODELS[0]: return self.timed_out_session(*args, **kwargs)
            return self.session(*args, **kwargs)
        accepted = self.run_task(session=execute)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual({call[1] for call in self.calls}, set(MODELS))
        self.assertFalse(list((self.runner.root/'execution-cycles').glob('*/fatal.json')))

    def test_partial_candidate_cannot_bypass_failed_checks(self):
        def checks(*args, **kwargs):
            reports = self.checks(*args, **kwargs)
            reports[0]['ok'] = False
            return reports
        with self.assertRaisesRegex(Blocked, 'No verified execution candidate'):
            self.run_task(session=self.timed_out_session, checks=checks)
        self.assertEqual(self.review_count, 0)
        self.assertFalse(any(v['status']=='accepted' for v in self.records()))

    def test_controller_returns_audited_incomplete_sessions_for_stage_recovery(self):
        result = self.timed_out_session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
        for role in ('execute', 'review'):
            with self.subTest(role=role), patch('controller.run_session',return_value=result):
                actual = self.runner.session(self.work, '.', 'task', role, 'be-'+role, model=MODELS[0])
                self.assertEqual(actual['disposition']['action'], 'check' if role=='execute' else 'retry')

    def test_partial_candidate_with_wrong_model_never_reaches_checks(self):
        def execute(*args, **kwargs):
            result = self.timed_out_session(*args, **kwargs)
            events = Path(result['events_path'])
            events.write_text(events.read_text().replace(kwargs['model'], 'unrequested-model'))
            return result
        with self.assertRaisesRegex(Blocked, 'No verified execution candidate'):
            self.run_task(session=execute)
        self.assertEqual((self.check_count, self.review_count), (0, 0))

    def test_partial_session_safeguards_and_role_boundary(self):
        from session_outcome import classify
        result = self.timed_out_session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
        self.assertEqual(classify(result, 'execute')['action'], 'check')
        self.assertEqual(classify({**result, 'text': '{"verdict":"pass"}'}, 'review')['action'], 'retry')
        for changes, action in [({'cleanup': {'preserved': True}}, 'stop-cleanup'),
                                ({'cancelled': True}, 'cancel'),
                                ({'event_count': 0}, 'retry'),
                                ({'returncode': None}, 'retry'),
                                ({'errors': result['errors'] + ['Malformed stream event at line 2']}, 'retry')]:
            with self.subTest(changes=changes):
                self.assertEqual(classify({**result, **changes}, 'execute')['action'], action)

    def test_saved_denial_resumes_checks_without_repeating_execution(self):
        self.task['resources'] = ['database']
        with self.assertRaisesRegex(Blocked, 'Permission preflight'):
            self.run_task(session=self.old_gate)
        fatal = next((self.runner.root/'execution-cycles').glob('*/fatal.json')); original = fatal.read_bytes()
        def forbidden(*args, **kwargs):
            raise AssertionError('completed execution repeated')
        def review(root, *args, **kwargs):
            data = read(root/'.review-input/execution-limitations.json')
            self.assertTrue(data['permission_denials'])
            self.assertIn('NOT successful', data['rule'])
            return self.review(root, *args, **kwargs)
        result = self.run_task(session=forbidden, review=review)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual((self.check_count, self.review_count, len(self.calls)), (1,1,1))
        self.assertFalse(fatal.exists())
        self.assertEqual(next(fatal.parent.glob('recovered-fatal-*.json')).read_bytes(), original)
        self.assertTrue(result['session_result']['permission_denials'])

    def test_new_denial_requires_checks_and_other_model_review(self):
        self.task['resources'] = ['database']
        result = self.run_task(session=self.denied_session)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual((self.check_count, self.review_count), (1,1))
        self.assertNotEqual(result['model'], result['reviewer_model'])
        self.assertFalse(result['session_result']['ok'])

    def test_failed_required_check_prevents_acceptance(self):
        self.task['resources'] = ['database']
        def failing(*args, **kwargs):
            results = self.checks(*args, **kwargs)
            results[0]['ok'] = False
            return results
        with self.assertRaisesRegex(Blocked, 'No verified execution candidate'):
            self.run_task(session=self.denied_session, checks=failing)
        self.assertGreater(self.check_count, 0)
        self.assertEqual(self.review_count, 0)

    def test_controller_allows_completed_review_but_not_preflight(self):
        result = self.denied_session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
        with patch('controller.run_session', return_value=result):
            returned = self.runner.session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
            self.assertEqual(returned['permission_denials'], result['permission_denials'])
            self.runner.session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[0])
            for role, name in [('review', 'preflight'), ('execute', 'preflight')]:
                with self.assertRaisesRegex(Blocked, 'Permission preflight/runtime denial'):
                    self.runner.session(self.work, '.', 'task', role, name, model=MODELS[0])

    def test_saved_denial_with_changed_workspace_stays_blocked(self):
        self.task['resources'] = ['database']
        with self.assertRaises(Blocked): self.run_task(session=self.old_gate)
        (self.work/'backend/value.txt').write_text('changed after receipt')
        with self.assertRaisesRegex(Blocked, 'receipt changed'): self.run_task()
        self.assertTrue(list((self.runner.root/'execution-cycles').glob('*/fatal.json')))
        self.assertEqual(self.check_count, 0)

    def test_unrelated_fatal_never_reopens(self):
        self.task['resources'] = ['database']
        with self.assertRaises(Blocked): self.run_task(session=self.old_gate)
        fatal = next((self.runner.root/'execution-cycles').glob('*/fatal.json'))
        record = read(fatal); record['error'] = 'Agent changed Git configuration or hooks'; save(fatal,record)
        with self.assertRaisesRegex(Blocked, 'Preserved safety failure'): self.run_task()
        self.assertEqual(self.check_count, 0)

    def test_transport_failure_and_unattributed_denials_are_not_usable(self):
        result = self.denied_session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
        self.assertTrue(execution_result_usable(result))
        for field, value in [('returncode', -15), ('timed_out', True), ('cancelled', True),
                             ('cleanup', {'preserved': True})]:
            self.assertFalse(execution_result_usable({**result, field: value}), field)
        events = Path(result['events_path']); rows = [json.loads(line) for line in events.read_text().splitlines()]
        rows[-1]['permission_denials'] = [{'tool_name': 'unknown'}]
        events.write_text('\n'.join(map(json.dumps,rows)))
        parsed = parse_events(events); parsed['returncode'] = 0
        self.assertFalse(execution_result_usable(parsed))
        rows[-1]['permission_denials'] = []; rows[-1]['is_error'] = True
        events.write_text('\n'.join(map(json.dumps,rows)))
        parsed = parse_events(events); parsed['returncode'] = 0
        self.assertFalse(execution_result_usable(parsed))
