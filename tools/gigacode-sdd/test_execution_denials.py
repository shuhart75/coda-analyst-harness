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
    setUp = fixtures.ExecutionPortfolioTests.setUp
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

    def old_gate(self, *args, **kwargs):
        result = self.denied_session(*args, **kwargs)
        raise Blocked('Permission preflight/runtime denial; inspect ' + result['events_path'])

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

    def test_controller_exception_applies_only_to_completed_execution(self):
        result = self.denied_session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
        with patch('controller.run_session', return_value=result):
            returned = self.runner.session(self.work, '.', 'task', 'execute', 'be-execute', model=MODELS[0])
            self.assertEqual(returned['permission_denials'], result['permission_denials'])
            for role, name in [('review', 'be-review'), ('execute', 'preflight')]:
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
