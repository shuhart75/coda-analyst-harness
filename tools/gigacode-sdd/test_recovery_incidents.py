"""Recovery receipts are evidence, never task acceptance or a second retry policy."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import recovery_incidents as incidents
from controller import Blocked, read, save
import execution_cycle as cycle
import test_execution_cycle as portfolio


class RecoveryIncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def begin(self, **overrides):
        return incidents.begin(self.root, **dict(dict(scope='scope', task_id='task', model='model',
            stage='execute', cause='checks', action='repair-implementation', occurrence='failure1',
            evidence={'log': 'actual.log'}, attempt=1), **overrides))

    def test_pending_is_not_success_and_restart_does_not_spend_retry(self):
        identity = self.begin()
        candidate = {'recovery_incidents': {'implementation:repair': identity}, 'failures': 2}
        incidents.pending(self.root, candidate, running=True)
        self.assertEqual(incidents.context(self.root)['active'][0]['status'], 'running')
        self.assertEqual(self.begin(), identity)
        self.assertEqual(candidate['failures'], 2)
        self.assertEqual(incidents.context(self.root)['incidents'][0]['attempt'], 1)
        incidents.finish(self.root, candidate, 'execution-validated')
        self.assertEqual(incidents.context(self.root)['incidents'][0]['status'], 'running')
        incidents.finish(self.root, candidate, 'checks-passed', {'logs': ['actual.log']})
        receipt = incidents.context(self.root)['incidents'][0]
        self.assertEqual(receipt['status'], 'verified')
        self.assertFalse(receipt['verification']['task_accepted'])

    def test_repeat_records_failed_attempt_and_preserves_supplied_budget(self):
        identity = self.begin()
        self.assertEqual(self.begin(occurrence='failure2', attempt=6, exhausted=True), identity)
        receipt = incidents.context(self.root)['incidents'][0]
        self.assertEqual(receipt['status'], 'exhausted')
        self.assertEqual(receipt['attempt'], 6)
        self.assertEqual(receipt['history'][0]['status'], 'failed')
        incidents.pending(self.root, {'recovery_incidents': {'step': identity}})
        self.assertEqual(incidents.context(self.root)['incidents'][0]['status'], 'exhausted')

    def test_guarded_action_counts_distinct_occurrences_without_resetting_on_resume(self):
        identity = self.begin(attempt=None)
        self.assertEqual(self.begin(attempt=None), identity)
        self.assertEqual(incidents.context(self.root)['incidents'][0]['attempt'], 1)
        self.begin(attempt=None, occurrence='new-failure')
        self.assertEqual(incidents.context(self.root)['incidents'][0]['attempt'], 2)

    def test_action_postcondition_failure_never_becomes_success(self):
        with self.assertRaisesRegex(ValueError, 'postcondition'):
            incidents.run_verified(self.root, scope='scope', task_id='task', model=None,
                stage='recovery', cause='saved-gate', action='revalidate', occurrence='a', evidence={},
                operation=lambda: None, verify=lambda result: None)
        receipt = incidents.context(self.root)['incidents'][0]
        self.assertEqual(receipt['status'], 'failed')
        self.assertFalse(receipt['verification']['task_accepted'])

    def test_corrupt_telemetry_does_not_prevent_guarded_action(self):
        identity = self.begin(stage='recovery', cause='saved-gate', model=None)
        path = self.root/'recovery'/'incidents'/(identity+'.json')
        path.write_text('{broken', encoding='utf-8')
        self.assertTrue(incidents.context(self.root)['unavailable'])
        operations = []
        with self.assertLogs('recovery_incidents', level='WARNING'):
            incidents.run_verified(self.root, scope='scope', task_id='task', model=None,
                stage='recovery', cause='saved-gate', action='revalidate', occurrence='a', evidence={},
                operation=lambda: operations.append('guarded'), verify=lambda result: {'kind': 'checked'})
        self.assertEqual(operations, ['guarded'])
        self.assertEqual(path.read_text(), '{broken')

    def test_candidate_lanes_do_not_overwrite_each_other(self):
        self.begin(model='a')
        self.begin(model='b')
        self.assertEqual({i['model'] for i in incidents.context(self.root)['incidents']}, {'a', 'b'})

    def test_invalid_receipt_shapes_never_block_begin_finish_or_guarded_operation(self):
        identity = self.begin()
        path = self.root/'recovery'/'incidents'/(identity+'.json')
        valid = read(path)
        bad_values = [[], None, {**valid, 'history': None}, {**valid, 'history': ['bad']},
                      {**valid, 'history': [{}]}, {**valid, 'updated_at': []},
                      {**valid, 'created_at': 'yesterday'}, {**valid, 'updated_at': '2026-01-01'},
                      {**valid, 'attempt': True}, {**valid, 'verification': []},
                      {**valid, 'cause': []}, {**valid, 'schema': True}]
        for bad in bad_values:
            with self.subTest(value=bad):
                save(path, bad)
                before = path.read_bytes()
                candidate = {'recovery_incidents': {'step': identity}}
                with self.assertLogs('recovery_incidents', level='WARNING'):
                    self.assertIsNone(self.begin(occurrence='new'))
                    incidents.finish(self.root, candidate, 'accepted')
                current = incidents.context(self.root)
                self.assertTrue(current['unavailable'])
                self.assertEqual(current['incidents'], [])
                self.assertEqual(before, path.read_bytes())
        for bad in ([], None):
            save(path, bad)
            operations = []
            with self.assertLogs('recovery_incidents', level='WARNING'):
                incidents.run_verified(self.root, scope='scope', task_id='task', model='model',
                    stage='execute', cause='checks', action='repair-implementation', occurrence='new',
                    evidence={}, operation=lambda: operations.append('guarded'),
                    verify=lambda result: {'kind': 'checked'})
            self.assertEqual(operations, ['guarded'])

    def test_bad_candidate_references_are_optional_telemetry(self):
        for references in (None, [], 'bad'):
            with self.subTest(references=references), self.assertLogs('recovery_incidents', level='WARNING'):
                candidate = {'recovery_incidents': references}
                incidents.pending(self.root, candidate)
                incidents.finish(self.root, candidate, 'accepted')
                incidents.suspend(self.root, candidate, 'failed', 'failure')

    def test_oversized_receipt_read_is_bounded_and_original_is_preserved(self):
        identity = self.begin()
        path = self.root/'recovery'/'incidents'/(identity+'.json')
        original = b' ' * (incidents.MAX_RECEIPT_BYTES + 100)
        path.write_bytes(original)
        with self.assertLogs('recovery_incidents', level='WARNING'):
            self.assertIsNone(self.begin())
        current = incidents.context(self.root)
        self.assertTrue(current['unavailable'])
        self.assertIn('Oversized', current['error'])
        self.assertEqual(path.read_bytes(), original)

    def test_scan_cap_is_explicit_and_does_not_read_remaining_receipts(self):
        for model in ('a', 'b', 'c'):
            self.begin(model=model)
        with patch.object(incidents, 'MAX_SCAN_ENTRIES', 2), patch.object(
                incidents, '_read', wraps=incidents._read) as reads:
            current = incidents.context(self.root)
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(len(current['incidents']), 2)
        self.assertTrue(current['truncated'])
        self.assertTrue(current['unavailable'])

    def test_new_cause_does_not_erase_verified_checks_proof(self):
        identity = self.begin()
        candidate = {'recovery_incidents': {'implementation:repair': identity}}
        incidents.finish(self.root, candidate, 'checks-passed', {'logs': ['actual.log']})
        path = self.root/'recovery'/'incidents'/(identity+'.json')
        before = path.read_bytes()
        next_identity = self.begin(cause='finding', occurrence='review-finding', attempt=2)
        incidents.supersede(self.root, identity, next_identity)
        self.assertEqual(path.read_bytes(), before)
        prior = next(i for i in incidents.context(self.root)['incidents'] if i['id'] == identity)
        self.assertEqual(prior['status'], 'verified')
        self.assertEqual(prior['verification']['kind'], 'checks-passed')
        for state in ('pending', 'running', 'interrupted'):
            with self.subTest(state=state):
                active = self.begin(model=state)
                incidents.transition(self.root, active, state)
                incidents.supersede(self.root, active, next_identity)
                self.assertEqual(read(self.root/'recovery'/'incidents'/(active+'.json'))['status'], 'failed')

    def test_newest_receipts_selected_after_old_directory_entries(self):
        for index, model in enumerate(('old', 'middle', 'newest')):
            identity = self.begin(model=model)
            path = self.root/'recovery'/'incidents'/(identity+'.json')
            os.utime(path, ns=(index + 1, index + 1))
        with patch.object(incidents, 'MAX_SCAN_ENTRIES', 1):
            current = incidents.context(self.root)
        self.assertEqual([i['model'] for i in current['incidents']], ['newest'])
        self.assertTrue(current['truncated'])
        self.assertTrue(current['unavailable'])


class RecoveryPortfolioTests(unittest.TestCase):
    setUp = portfolio.ExecutionPortfolioTests.setUp
    result = portfolio.ExecutionPortfolioTests.result
    session = portfolio.ExecutionPortfolioTests.session
    checks = portfolio.ExecutionPortfolioTests.checks
    review = portfolio.ExecutionPortfolioTests.review
    run_task = portfolio.ExecutionPortfolioTests.run_task
    records = portfolio.ExecutionPortfolioTests.records

    def test_failed_checks_repair_and_verify_without_operator(self):
        self.primary_only = True
        def checks(*args, **kwargs):
            result = self.checks(*args, **kwargs)
            if self.check_count == 1:
                result[0]['ok'] = False
            return result
        accepted = self.run_task(checks=checks)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 2, 1))
        receipt = incidents.context(self.runner.root)['incidents'][0]
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(receipt['verification']['kind'], 'checks-passed')
        self.assertEqual(accepted['retry_steps']['implementation:repair']['failures'], 1)

    def test_repeated_protocol_failure_exhausts_original_budget(self):
        self.primary_only = True
        def review(*args, **kwargs):
            from controller import RetryableSessionError
            raise RetryableSessionError('Malformed reviewer verdict: missing coverage')
        with self.assertRaises(Blocked):
            self.run_task(review=review)
        receipt = incidents.context(self.runner.root)['incidents'][0]
        self.assertEqual((receipt['status'], receipt['attempt']), ('exhausted', 3))
        self.assertEqual(len(receipt['history']), 2)

    def test_automatic_fatal_guard_blocks_same_evidence_and_foreign_failure(self):
        self.primary_only = True
        folder = self.runner.root/'execution-cycles'/'scope'
        folder.mkdir(parents=True)
        fatal = {'scope': 'scope', 'type': 'PermissionError', 'category': 'operational',
                 'error': 'fixture error', 'related_failures': []}
        path = folder/'fatal.json'
        save(path, fatal)
        handler = lambda *args: path.unlink()
        with patch.object(cycle, '_recover_permission_fatal', side_effect=handler) as recover:
            self.assertTrue(cycle._automatic_fatal_recovery(self.runner, self.task, [], folder, 'scope', None))
            save(path, fatal)
            self.assertFalse(cycle._automatic_fatal_recovery(self.runner, self.task, [], folder, 'scope', None))
            self.assertEqual(recover.call_count, 1)
        receipt = incidents.context(self.runner.root)['incidents'][0]
        self.assertEqual((receipt['status'], receipt['attempt']), ('exhausted', 1))
        self.assertIn('этот же сбой', receipt['evidence']['reason'])
        fatal['related_failures'] = [{'scope': 'scope', 'type': 'Blocked', 'error': 'Protected inputs changed'}]
        save(path, fatal)
        with patch.object(cycle, '_recover_permission_fatal') as recover:
            self.assertFalse(cycle._automatic_fatal_recovery(self.runner, self.task, [], folder, 'scope', None))
            recover.assert_not_called()

    def test_fatal_total_budget_exhaustion_is_visible_and_keeps_original_failure(self):
        folder = self.runner.root/'execution-cycles'/'scope'
        folder.mkdir(parents=True)
        fatal = {'scope': 'scope', 'type': 'PermissionError', 'category': 'operational',
                 'error': 'fixture error', 'related_failures': []}
        path = folder/'fatal.json'
        save(path, fatal)
        before = path.read_bytes()
        save(folder/'automatic-recovery.json', {'scope': 'scope', 'attempts': [
            {'fatal_sha256': str(i), 'status': 'failed'} for i in range(3)]})
        messages = []
        with patch.object(cycle, '_recover_permission_fatal') as recover, patch.object(
                self.runner, 'progress', side_effect=messages.append):
            self.assertFalse(cycle._automatic_fatal_recovery(self.runner, self.task, [], folder, 'scope', None))
            recover.assert_not_called()
        receipt = incidents.context(self.runner.root)['incidents'][0]
        self.assertEqual((receipt['status'], receipt['attempt']), ('exhausted', 3))
        self.assertIn('исчерпаны три', receipt['evidence']['reason'])
        self.assertIn('автоматическое восстановление остановлено', messages[0])
        self.assertEqual(path.read_bytes(), before)

    def test_verified_fatal_recovery_continues_in_same_call(self):
        self.primary_only = True
        first = [True]
        original = cycle._recover_permission_fatal
        def session(*args, **kwargs):
            if first[0]:
                first[0] = False
                raise PermissionError('fixture transient failure')
            return self.session(*args, **kwargs)
        def guarded_recovery(runner, task, candidates, folder, scope):
            # Simulate the existing independently guarded handler only for the
            # injected transient OS failure. The continuation/checks are real.
            if (folder/'fatal.json').exists():
                (folder/'fatal.json').replace(folder/'verified-fixture-fatal.json')
            else:
                return original(runner, task, candidates, folder, scope)
        with patch.object(cycle, '_recover_permission_fatal', side_effect=guarded_recovery):
            accepted = self.run_task(session=session)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((self.check_count, self.review_count), (1, 1))
        guard = read(next((self.runner.root/'execution-cycles').glob('*/automatic-recovery.json')))
        self.assertEqual(len(guard['attempts']), 1)
        self.assertEqual(guard['attempts'][0]['status'], 'verified')


class CancelledAutomaticRecoveryTests(unittest.TestCase):
    from test_cancelled_receipts import CancelledReceiptTests as _helpers
    setUp = _helpers.setUp
    result = _helpers.result
    session = _helpers.session
    checks = _helpers.checks
    review = _helpers.review
    run_task = _helpers.run_task
    records = _helpers.records
    audited_session_result = _helpers.audited_session_result
    prepare_receipt = _helpers.prepare_receipt
    del _helpers

    def test_cancelled_receipt_gate_recovers_in_same_call_with_real_handler(self):
        self.prepare_receipt()
        original = cycle._recover_execution_session
        first = [True]
        def legacy_once(*args, **kwargs):
            if first[0]:
                first[0] = False
                raise Blocked(cycle.RECEIPT_CHANGED + str(self.receipt_path))
            return original(*args, **kwargs)
        with patch.object(cycle, '_recover_execution_session', side_effect=legacy_once):
            accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual((len(self.calls), self.check_count, self.review_count), (2, 1, 1))
        self.assertEqual(self.receipt_before, self.receipt_path.read_bytes())
        guard = read(self.portfolio/'automatic-recovery.json')
        self.assertEqual(guard['attempts'][0]['status'], 'verified')
        receipt = incidents.context(self.runner.root)['incidents'][0]
        self.assertEqual(receipt['verification']['kind'], 'gate-revalidated')
        self.assertFalse(receipt['verification']['task_accepted'])
