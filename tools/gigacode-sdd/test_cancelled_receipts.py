"""Interrupted invocations retain WIP without promoting cancelled proof to success."""
from pathlib import Path
import unittest

from controller import Blocked, digest, git_controls, read, save
import test_execution_cycle as portfolio
from workspace import git


class SimulatedControllerCrash(BaseException):
    pass


class CancelledReceiptTests(unittest.TestCase):
    # Reuse fixture helpers only, without inheriting or rediscovering its tests.
    setUp = portfolio.ExecutionPortfolioTests.setUp
    result = portfolio.ExecutionPortfolioTests.result
    session = portfolio.ExecutionPortfolioTests.session
    checks = portfolio.ExecutionPortfolioTests.checks
    review = portfolio.ExecutionPortfolioTests.review
    run_task = portfolio.ExecutionPortfolioTests.run_task
    records = portfolio.ExecutionPortfolioTests.records
    audited_session_result = portfolio.ExecutionPortfolioTests.audited_session_result

    def prepare_receipt(self, *, cancelled=True, cleanup_denied=False):
        self.primary_only = True
        self.task['writes'] = ['backend']
        self.logs = [self.work/'backend/runtime.log', self.work/'backend/evidence.txt']
        for path in self.logs:
            path.write_text('before cancellation\n', encoding='utf-8')

        def interrupted(*args, **kwargs):
            result = self.audited_session_result(*args, **kwargs)
            self.receipt_path = Path(result['events_path']).parent/'session-receipt.json'
            receipt = read(self.receipt_path)
            receipt['git_controls'] = git_controls(args[0])
            if cancelled:
                events = Path(result['events_path'])
                first = events.read_text(encoding='utf-8').splitlines()[0]
                events.write_text(first+'\n', encoding='utf-8', newline='\n')
                receipt['events_sha256'] = digest(events)
                receipt['result'].update(cancelled=True, ok=False, terminal_result=False, returncode=130)
            if cleanup_denied:
                receipt['result']['cleanup'] = {'preserved': True, 'reason': 'process ownership unconfirmed'}
            save(self.receipt_path, receipt)
            raise SimulatedControllerCrash()

        with self.assertRaises(SimulatedControllerCrash):
            self.run_task(session=interrupted)
        self.runner.stop.clear()
        self.portfolio = next((self.runner.root/'execution-cycles').iterdir())
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (1, 0, 0))
        self.receipt_before = self.receipt_path.read_bytes()
        for path in self.logs:
            path.write_text(path.read_text(encoding='utf-8')+'later service output\n', encoding='utf-8')

    def forbidden_execution(self, *args, **kwargs):
        raise AssertionError('Unvalidated immutable evidence must not start another executor')

    def saved_failure(self, *, other_error=None):
        error = 'Saved execution session receipt changed; preserve and inspect ' + str(self.receipt_path)
        fatal = {'scope': self.portfolio.name, 'type': 'Blocked', 'error': error,
                 'related_failures': [{'scope': self.portfolio.name, 'type': 'Blocked',
                                       'error': error, 'model': portfolio.MODELS[0]}]}
        if other_error:
            fatal['related_failures'].append({'scope': self.portfolio.name, 'type': 'Blocked',
                                             'error': other_error, 'model': portfolio.MODELS[1]})
        path = self.portfolio/'fatal.json'
        save(path, fatal)
        return path, path.read_bytes()

    def test_cancelled_log_drift_runs_new_execution_checks_and_review(self):
        self.prepare_receipt()
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 1, 1))
        self.assertNotIn('recovered_receipt', accepted)
        self.assertEqual(self.receipt_path.read_bytes(), self.receipt_before)
        for path in self.logs:
            self.assertIn('later service output', path.read_text(encoding='utf-8'))

    def test_cancelled_ordinary_log_rotation_runs_new_validation(self):
        self.prepare_receipt()
        rotated = self.logs[0].with_suffix('.log.1')
        self.logs[0].rename(rotated)
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 1, 1))
        self.assertTrue(rotated.is_file())
        self.assertFalse(self.logs[0].exists())

    def test_cancelled_drift_without_git_control_proof_remains_blocked(self):
        self.prepare_receipt()
        receipt = read(self.receipt_path)
        receipt.pop('git_controls')
        save(self.receipt_path, receipt)
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_cancelled_event_tamper_remains_blocked(self):
        self.prepare_receipt()
        events = self.receipt_path.parent/'stdout.jsonl'
        events.write_text(events.read_text(encoding='utf-8')+'tampered\n', encoding='utf-8')
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_cancelled_prompt_tamper_remains_blocked(self):
        self.prepare_receipt()
        prompt = self.receipt_path.parent/'prompt.txt'
        prompt.write_text('tampered preamble\n'+prompt.read_text(encoding='utf-8'), encoding='utf-8')
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_cancelled_git_controls_tamper_remains_blocked(self):
        self.prepare_receipt()
        git(self.work, 'config', 'core.editor', 'fixture-editor')
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_cancelled_head_change_remains_blocked(self):
        self.prepare_receipt()
        git(self.work, 'commit', '--allow-empty', '-m', 'Unrecorded commit after cancellation')
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_cancelled_protected_context_change_remains_blocked(self):
        self.prepare_receipt()
        (self.work/'backend/AGENTS.md').write_text('Unrecorded agent instructions\n', encoding='utf-8')
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_successful_receipt_drift_is_not_treated_as_cancelled_wip(self):
        self.prepare_receipt(cancelled=False)
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_cancelled_cleanup_denial_remains_blocked(self):
        self.prepare_receipt(cleanup_denied=True)
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)

    def test_existing_receipt_failure_is_archived_after_safe_revalidation(self):
        self.prepare_receipt()
        fatal, original = self.saved_failure()
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 1, 1))
        self.assertFalse(fatal.exists())
        archives = [p for p in self.portfolio.glob('*.json') if p.read_bytes() == original]
        self.assertEqual(len(archives), 1)
        self.assertEqual(self.receipt_path.read_bytes(), self.receipt_before)

    def test_unrelated_sibling_failure_is_never_cleared_with_cancelled_receipt(self):
        self.prepare_receipt()
        fatal, original = self.saved_failure(other_error='Protected project instructions changed')
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)
        self.assertEqual(fatal.read_bytes(), original)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_sibling_receipt_is_not_assumed_safe(self):
        self.prepare_receipt()
        other = self.runner.root/'logs/another/session-receipt.json'
        fatal, original = self.saved_failure(
            other_error='Saved execution session receipt changed; preserve and inspect '+str(other))
        with self.assertRaises(Blocked):
            self.run_task(session=self.forbidden_execution)
        self.assertEqual(fatal.read_bytes(), original)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
