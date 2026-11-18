import json
import threading
import unittest
from recovery_policy import decide, retry_at, wait_until, transport_cause
from step_recovery import record_failure


class RecoveryPolicyTests(unittest.TestCase):
    def test_orthogonal_actions(self):
        cases = [('review', 'timeout', 'retry-later', False),
                 ('adjudicate', 'transport', 'retry-later', False),
                 ('review', 'protocol', 'repair-response', True),
                 ('execute', 'checks', 'repair-implementation', True),
                 ('execute', 'resource-busy', 'wait-resource', False),
                 ('review', 'cleanup', 'block', False),
                 ('execute', 'cancelled', 'pause', False)]
        for stage, cause, action, amendment in cases:
            with self.subTest(stage=stage, cause=cause):
                d = decide(stage, cause)
                self.assertEqual((d['action'], d['instruction_change']), (action, amendment))

    def test_stage_transport_budget_does_not_spend_protocol_budget(self):
        state = {}
        for _ in range(6):
            decision = record_failure(state, 'review', 'transport', 1)
        self.assertTrue(decision['exhausted'])
        self.assertEqual(record_failure(state, 'review', 'review-protocol', 1)['step_failures'], 1)
        self.assertEqual(record_failure(state, 'execute', 'transport', 1)['step_failures'], 1)

    def test_deadline_survives_restart_and_cancel(self):
        saved = json.loads(json.dumps({'next_attempt_at': retry_at(100, 2)}))
        clock = [103]
        class Stop:
            def is_set(self): return False
            def wait(self, seconds): clock[0] += seconds; return False
        self.assertTrue(wait_until(saved['next_attempt_at'], Stop(), lambda: clock[0]))
        self.assertEqual(clock[0], 110)
        stop = threading.Event(); stop.set()
        self.assertFalse(wait_until(120, stop, lambda: 110))
        self.assertEqual(saved['next_attempt_at'], 110)
        self.assertEqual(retry_at(100, 200), 160)

    def test_metadata_not_payload_determines_transport(self):
        self.assertIsNone(transport_cause({'returncode': 0, 'terminal_result': True,
                                         'text': 'timeout permission denied'}))
        self.assertEqual(transport_cause({'timed_out': True}), 'timeout')
        self.assertEqual(transport_cause({'returncode': -15}), 'transport')
        for value in (-1, float('nan'), float('inf'), True, 61):
            with self.assertRaises(ValueError): retry_at(0, 1, value)
