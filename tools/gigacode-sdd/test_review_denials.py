"""Completed reviews survive an explicitly unexecuted diagnostic probe."""
import json
import os
import shutil
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
        shutil.rmtree(self.work)
        clone_snapshot(main,self.work)
        self.record['base'] = git(main,'rev-parse','HEAD')
        os.utime(self.work/'frontend/.npmrc',ns=(1000000000,1000000000))

    def review_session(self, *args, **kwargs):
        result = self.denied_session(*args, **kwargs)
        report = self.review(args[0], *args[1:3], self.task['requirements'], args[4], **kwargs)
        report.update(getattr(self, 'report_override', {}))
        events = Path(result['events_path'])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        rows[-1]['result'] = json.dumps(report)
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

    def test_changed_review_workspace_remains_blocked(self):
        self.task['resources'] = ['database']
        with self.assertRaises(Blocked): self.run_task(review=self.old_review)
        receipt = read(self.saved_events.parent/'session-receipt.json')
        (Path(receipt['workspace'])/'backend/value.txt').write_text('tampered')
        with self.assertRaisesRegex(Blocked, 'receipt changed'): self.run_task()

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
