"""Completed reviews survive an explicitly unexecuted diagnostic probe."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from controller import Runner, Blocked, OperationalBlocked, RetryableSessionError, digest, read, save
from execution_cycle import MODELS
from transport import parse_events, review_denial_errors
from workspace import git, clone_snapshot
import test_execution_denials as fixtures


class ReviewDenialTests(unittest.TestCase):
    setUp = fixtures.ExecutionDenialTests.setUp
    result = fixtures.ExecutionDenialTests.result
    session = fixtures.ExecutionDenialTests.session
    checks = fixtures.ExecutionDenialTests.checks
    review = fixtures.ExecutionDenialTests.review
    run_task = fixtures.ExecutionDenialTests.run_task
    records = fixtures.ExecutionDenialTests.records
    audited_session_result = fixtures.ExecutionDenialTests.audited_session_result
    denied_session = fixtures.ExecutionDenialTests.denied_session

    def seed_private_clone_fixture(self):
        main = self.runner.main
        (main/'frontend/.npmrc').write_text('registry=https://nexus.test/npm/\n')
        git(main, 'add', '--', 'frontend/.npmrc')
        git(main, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            'commit', '-qm', 'tracked registry fixture')
        # Keep the old temporary clone for TemporaryDirectory's portable cleanup.
        self.work = self.work.with_name('private-fixture')
        clone_snapshot(main,self.work)
        self.record['workspace'] = str(self.work)
        self.record['base'] = git(main,'rev-parse','HEAD')
        os.utime(self.work/'frontend/.npmrc',ns=(1000000000,1000000000))

    def review_session(self, *args, **kwargs):
        result = self.denied_session(*args, **kwargs)
        report = self.review(args[0], *args[1:3], self.task['requirements'], args[4], **kwargs)
        report.update(getattr(self, 'report_override', {}))
        events = Path(result['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        if hasattr(self, 'denial_message'):
            rows[2]['message']['content'][0]['content'] = self.denial_message
        rows[-1]['result'] = getattr(self, 'report_text', json.dumps(report))
        events.write_text('\n'.join(map(json.dumps, rows)) + '\n')
        result = parse_events(events)
        result.update(returncode=0, cleanup={'preserved': False})
        receipt = read(events.parent/'session-receipt.json')
        receipt.update(result=result, events_sha256=digest(events))
        save(events.parent/'session-receipt.json', receipt)
        self.saved_events = events
        return result

    def old_review(self, *args, **kwargs):
        def denied(*a, **kw):
            result = self.review_session(*a, **kw)
            raise Blocked('Permission preflight/runtime denial; inspect ' + result['events_path'])
        with patch.object(self.runner, 'session', side_effect=denied):
            return Runner.review(self.runner, *args, **kwargs)

    def test_saved_review_recovers_without_repeating_any_completed_stage(self):
        self.seed_private_clone_fixture()
        self.task['resources'] = ['database']
        with self.assertRaisesRegex(Blocked, 'Permission preflight'):
            self.run_task(review=self.old_review)
        counts = (len(self.calls), self.check_count, self.review_count)
        def forbidden(*a, **kw):
            raise AssertionError('completed stage repeated')
        accepted = self.run_task(session=forbidden, checks=forbidden, review=forbidden)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), counts)
        self.assertEqual(accepted['review']['events_path'], str(self.saved_events))
        self.assertTrue(list((self.runner.root/'execution-cycles').glob('*/recovered-fatal-*.json')))

    def test_successful_historical_error_read_recovers_old_receipt(self):
        self.task['resources'] = ['database']
        def legacy_review(*args, **kwargs):
            def completed(*a, **kw):
                result = self.review_session(*a, **kw)
                events = Path(result['events_path'])
                rows = [json.loads(line) for line in events.read_text().splitlines()]
                rows[-1].pop('permission_denials', None)
                for row in rows:
                    for item in row.get('message', {}).get('content', []):
                        if item.get('type') == 'tool_result':
                            item.update(is_error=False, content='Historical error: permission denied')
                events.write_text('\n'.join(map(json.dumps, rows)) + '\n')
                result = parse_events(events, _legacy_denial_text=True)
                result['returncode'] = 0
                receipt = read(events.parent/'session-receipt.json')
                receipt.update(result=result, events_sha256=digest(events))
                save(events.parent/'session-receipt.json', receipt)
                raise Blocked('Permission preflight/runtime denial; inspect ' + str(events))
            with patch.object(self.runner, 'session', side_effect=completed):
                return Runner.review(self.runner, *args, **kwargs)
        with self.assertRaisesRegex(Blocked, 'Permission preflight'):
            self.run_task(review=legacy_review)
        original = self.saved_events.read_bytes()
        receipt_path = self.saved_events.parent/'session-receipt.json'
        original_receipt = receipt_path.read_bytes()
        def forbidden(*a, **kw):
            raise AssertionError('completed work repeated')
        accepted = self.run_task(session=forbidden, checks=forbidden, review=forbidden)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(self.saved_events.read_bytes(), original)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)

    def test_changed_review_workspace_remains_blocked(self):
        self.task['resources'] = ['database']
        with self.assertRaises(Blocked): self.run_task(review=self.old_review)
        receipt = read(self.saved_events.parent/'session-receipt.json')
        (Path(receipt['workspace'])/'backend/value.txt').write_text('tampered')
        with self.assertRaisesRegex(Blocked, 'receipt changed'): self.run_task()

    def test_old_unclassified_tool_refusal_repeats_only_review(self):
        self.denial_message = 'Permission denied; outcome unknown'
        with self.assertRaisesRegex(Blocked, 'Permission preflight'):
            self.run_task(review=self.old_review)
        counts = len(self.calls), self.check_count
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count), counts)
        self.assertEqual(accepted['history'][-1]['kind'], 'review-protocol')

    def test_plan_mode_refusal_does_not_discard_completed_review(self):
        self.denial_message = ('Plan mode blocked this shell command because it was classified as '
                               'state-modifying. Do not retry it through wrappers or obfuscation; '
                               'continue read-only investigation and include the action in the plan.')
        result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        self.assertIsNotNone(review_denial_errors(result))
        report = self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])
        self.assertEqual(report['verdict'], 'pass')

    def test_read_error_and_unexecuted_denial_allow_result_validation(self):
        result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        events = Path(result['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        rows[-1:-1] = [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'missing',
                'name': 'read_file', 'input': {'file_path': 'wrong/tasks.md'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'missing',
                'is_error': True, 'content': 'File not found: wrong/tasks.md'}]}}]
        events.write_text('\n'.join(map(json.dumps, rows)))
        result = parse_events(events)
        result.update(returncode=0, cleanup={'preserved': False})
        report = self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])
        self.assertEqual(report['verdict'], 'pass')
        self.assertIn('Tool read_file (missing) returned an error', report['tool_warnings'])
        result['errors'].append('Unknown shell failure')
        with self.assertRaises(RetryableSessionError):
            self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])

    def test_completed_shell_error_preserves_negative_feedback_only(self):
        for verdict in ('changes', 'pass', 'blocked'):
            self.report_override = {'verdict': verdict, 'findings': ['Required evidence missing'] if verdict != 'pass' else []}
            result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
            events = Path(result['events_path'])
            rows = [json.loads(line) for line in events.read_text().splitlines()]
            rows[-1:-1] = [
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'shell',
                    'name': 'run_shell_command', 'input': {'command': 'ls --time-style=full-iso'}}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'shell',
                    'is_error': True, 'content': "ls: unrecognized option '--time-style=full-iso'"}]}}]
            events.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
            result = parse_events(events)
            result.update(returncode=0, cleanup={'preserved': False})
            if verdict == 'changes':
                report = self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])
                self.assertEqual(report['verdict'], 'changes')
                self.assertIn('Tool run_shell_command (shell) returned an error', report['tool_warnings'])
                result['terminal_result'] = False
            with self.assertRaises(RetryableSessionError) as caught:
                self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])
            if verdict != 'changes':
                self.assertEqual(caught.exception.cause, 'tool-errors')

    def test_object_findings_keep_text_without_protocol_retry(self):
        self.report_override = {'verdict': 'changes', 'findings': [{'id': 'issue-1', 'text': 'Fix **значние**'}]}
        result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        report = self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])
        self.assertEqual(report['findings'], ['Fix **значние**'])
        self.assertEqual(report['reported_findings'][0]['id'], 'issue-1')

    def test_saved_context_failure_retries_only_review_with_focused_instruction(self):
        self.task['resources'] = ['database']
        self.report_text = '[API Error: Context is too large to send safely; COMPRESSION_FAILED_API_ERROR.]'
        self.denial_message = 'The command was not run; Plan mode remains active.'
        with self.assertRaisesRegex(Blocked, 'Permission preflight'):
            self.run_task(review=self.old_review)
        # Reproduce a receipt from the old parser, which missed the API failure.
        receipt_path = self.saved_events.parent/'session-receipt.json'
        receipt = read(receipt_path)
        receipt['result']['errors'] = [e for e in receipt['result']['errors'] if 'context exhausted' not in e]
        save(receipt_path, receipt)
        counts = len(self.calls), self.check_count
        del self.report_text
        def focused(*args, **kwargs):
            self.assertIn('120 lines', args[2])
            self.assertIn('EVERY', args[2])
            return self.review(*args, **kwargs)
        accepted = self.run_task(review=focused)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count), counts)
        self.assertEqual(accepted['history'][-1]['kind'], 'review-context')
        self.assertTrue(receipt_path.is_file())

    def test_denied_probe_does_not_mask_retryable_transport_failure(self):
        result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        result.update(timed_out=True, ok=False)
        result['errors'].append('Session process timeout; partial events preserved')
        with patch('controller.run_session', return_value=result):
            returned = self.runner.session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        with self.assertRaises(RetryableSessionError):
            self.runner.validate_review_result(self.work, returned, self.task['requirements'], 'be-review', MODELS[1])

    def test_private_file_changed_after_receipt_stays_blocked(self):
        self.seed_private_clone_fixture()
        self.task['resources'] = ['database']
        with self.assertRaises(Blocked): self.run_task(review=self.old_review)
        path = self.work/'frontend/.npmrc'
        stamp = path.stat()
        os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1000000000))
        with self.assertRaisesRegex(Blocked, 'candidate changed'):
            self.run_task()

    def test_malformed_saved_verdict_enters_bounded_protocol_repair(self):
        self.task['resources'] = ['database']
        self.report_override = {'verdict': 'invalid'}
        with self.assertRaises(Blocked): self.run_task(review=self.old_review)
        calls, checks = len(self.calls), self.check_count
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count), (calls, checks))
        self.assertTrue(any(h['kind'] == 'review-protocol' for h in accepted['history']))

    def test_changes_survive_unverified_optional_suggestion(self):
        self.report_override = {'verdict': 'changes', 'findings': ['Required contract contradicts implementation'],
            'optional_findings': [{'status':'optional', 'requirements':[], 'outside_required_scope':True,
                'text':'Optional cleanup', 'reason':'Readability',
                'evidence':[{'path':'backend/value.txt','quote':'Paraphrase with typo'}]}]}
        result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        report = self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])
        self.assertEqual(report['verdict'], 'changes')
        self.assertEqual(len(report['findings']), 1)
        self.assertNotIn('optional_findings', report)
        self.assertIn('unverified_optional_findings', report)

    def test_unknown_denial_and_timeout_do_not_allow_review(self):
        result = self.review_session(self.work, '.', 'task', 'review', 'be-review', model=MODELS[1])
        self.assertIsNotNone(review_denial_errors(result))
        self.assertIsNone(review_denial_errors({**result, 'timed_out': True}))
        events = Path(result['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        rows[2]['message']['content'][0]['content'] = 'Permission denied; outcome unknown'
        events.write_text('\n'.join(map(json.dumps, rows)))
        result = parse_events(events); result['returncode'] = 0
        self.assertIsNone(review_denial_errors(result))
        with self.assertRaises(RetryableSessionError):
            self.runner.validate_review_result(self.work, result, self.task['requirements'], 'be-review', MODELS[1])

    def save_operational_failure(self):
        self.task['resources'] = ['database']
        def failed_review(*args, **kwargs):
            def completed(*a, **kw):
                result = self.review_session(*a, **kw)
                events = Path(result['events_path'])
                rows = [json.loads(line) for line in events.read_text().splitlines()]
                rows = [rows[0], rows[-1]]
                rows[-1]['permission_denials'] = []
                events.write_text('\n'.join(map(json.dumps, rows)))
                result = parse_events(events); result['returncode'] = 0
                receipt = read(events.parent/'session-receipt.json')
                receipt.update(result=result, events_sha256=digest(events))
                save(events.parent/'session-receipt.json', receipt)
                raise PermissionError(1, 'Operation not permitted')
            with patch.object(self.runner, 'session', side_effect=completed):
                return Runner.review(self.runner, *args, **kwargs)
        with self.assertRaises(PermissionError): self.run_task(review=failed_review)
        return next((self.runner.root/'execution-cycles').glob('*/fatal.json'))

    def test_os_failure_recovers_audited_review_and_quarantines_unfinished_peer(self):
        self.seed_private_clone_fixture()
        fatal = self.save_operational_failure()
        # Reproduce the legacy marker that had lost operation and traceback.
        data = read(fatal)
        save(fatal, {k:data[k] for k in ('error','type','scope')})
        counts = len(self.calls), self.check_count, self.review_count
        def forbidden(*a, **kw): raise AssertionError('completed work repeated')
        with patch.object(self.runner, 'check_preserved_processes') as guard:
            accepted = self.run_task(session=forbidden, checks=forbidden, review=forbidden)
        guard.assert_called_once()
        scopes = guard.call_args.kwargs['roots']
        self.assertNotIn(self.runner.root, scopes)
        self.assertIn(self.work, scopes)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), counts)
        self.assertFalse(fatal.exists())
        self.assertEqual(next(v for v in self.records() if v['model']==MODELS[1])['status'], 'quarantined')

    def test_os_failure_with_live_process_is_not_recovered(self):
        fatal = self.save_operational_failure()
        with patch.object(self.runner, 'check_preserved_processes', side_effect=Blocked('live agent')):
            with self.assertRaisesRegex(Blocked, 'live agent'): self.run_task()
        self.assertTrue(fatal.exists())

    def test_os_failure_with_peer_changes_outside_ownership_is_not_recovered(self):
        fatal = self.save_operational_failure()
        peer = next(v for v in self.records() if v['model']==MODELS[1])
        (Path(peer['workspace'])/'unexpected.txt').write_text('unowned mutation')
        with patch.object(self.runner, 'check_preserved_processes'):
            with self.assertRaisesRegex(RuntimeError, 'outside task ownership'): self.run_task()
        self.assertTrue(fatal.exists())

    def test_os_failure_keeps_trace_and_does_not_resume_without_audited_review(self):
        fatal = self.save_operational_failure()
        data = read(fatal)
        self.assertEqual(data['category'], 'operational')
        self.assertEqual(data['errno'], 1)
        self.assertEqual(data['frames'][-1]['function'], 'completed')
        (self.saved_events.parent/'session-receipt.json').unlink()
        with patch.object(self.runner, 'check_preserved_processes'):
            with self.assertRaisesRegex(Blocked, 'no unchanged candidate'): self.run_task()
        self.assertTrue(fatal.exists())

    def test_cleanup_block_is_rechecked_on_resume_without_permanent_fatal(self):
        self.task['resources'] = ['database']
        def cleanup(*a, **kw): raise OperationalBlocked('Process cleanup incomplete')
        with self.assertRaises(OperationalBlocked): self.run_task(review=cleanup)
        self.assertFalse(list((self.runner.root/'execution-cycles').glob('*/fatal.json')))
        self.assertTrue(list((self.runner.root/'execution-cycles').glob('*/operational.json')))
        counts = len(self.calls), self.check_count
        self.assertEqual(self.run_task()['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count), counts)

    def test_cleanup_record_keeps_guard_membership(self):
        path = self.runner.root/'preserved-processes/123.json'
        save(path, {'process_group':123, 'member_pids':[123,456], 'services':[]})
        self.runner.preserve_cleanup({'process_group':123, 'preserved':True,
                                      'diagnostic':{'reason':'stop failed'}})
        self.assertEqual(read(path)['member_pids'], [123,456])
        self.assertTrue(self.runner.stop.is_set())
