"""Completed reviews survive an explicitly unexecuted diagnostic probe."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from controller import Runner, Blocked, RetryableSessionError, digest, read, save
from execution_cycle import MODELS
from transport import parse_events, review_denial_errors
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
