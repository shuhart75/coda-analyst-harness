import unittest
from step_recovery import record_failure, TOTAL_FAILURES
from retry_budget import renew


class StepRecoveryTests(unittest.TestCase):
    def test_context_and_schema_share_budget_but_adjudication_is_distinct(self):
        state = {}
        for kind in ('review-protocol', 'review-context') * 3:
            retry = record_failure(state, 'review', kind, 1)
        self.assertTrue(retry['exhausted'])
        retry = record_failure(state, 'adjudicate', 'review-protocol', 1)
        self.assertFalse(retry['exhausted'])
        self.assertEqual(retry['strategy'], 0)

    def test_total_ceiling_cannot_be_evaded_by_alternating_steps(self):
        state = {}
        steps = [('execute', 'transport'), ('execute', 'model'),
                 ('execute', 'checks'), ('review', 'review-protocol'),
                 ('adjudicate', 'review-context')]
        for n in range(TOTAL_FAILURES):
            stage, kind = steps[n % len(steps)]
            retry = record_failure(state, stage, kind, 1)
            self.assertEqual(retry['exhausted'], n == TOTAL_FAILURES-1)

    def test_runtime_update_renews_step_budgets_but_preserves_draft(self):
        state = {'runtime_revision': 'old', 'review_draft': {'text': 'evidence'}}
        record_failure(state, 'review', 'review-context', 1)
        old = state['retry_steps']
        renew(state, {'failures': 0, 'strategy': 0, 'stagnation': 0, 'retry_steps': {}}, 'new')
        self.assertEqual(state['retry_steps'], {})
        self.assertEqual(state['budget_history'][0]['counters']['retry_steps'], old)
        self.assertEqual(state['review_draft']['text'], 'evidence')
