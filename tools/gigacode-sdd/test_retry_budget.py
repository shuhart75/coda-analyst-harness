import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import retry_budget


class RetryBudgetTests(unittest.TestCase):
    def test_revision_uses_runtime_bytes_not_tests_or_documentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'runner.py').write_text('runtime v1')
            with patch.object(retry_budget, '__file__', str(root/'retry_budget.py')):
                first = retry_budget.runtime_revision()
                (root/'README.md').write_text('new documentation')
                (root/'test_runner.py').write_text('new test')
                self.assertEqual(retry_budget.runtime_revision(), first)
                (root/'runner.py').write_text('runtime v2')
                self.assertNotEqual(retry_budget.runtime_revision(), first)

    def test_renew_archives_counters_and_never_erases_work_or_external_gate(self):
        state = {'failures': 4, 'strategy': 2, 'runtime_revision': 'old',
                 'history': ['evidence'], 'workspace': 'work', 'external_blocker': 'needs analyst'}
        self.assertTrue(retry_budget.renew(state, {'failures': 0, 'strategy': 0}, 'new'))
        self.assertEqual(state['budget_history'][0]['counters'], {'failures': 4, 'strategy': 2})
        self.assertEqual(state['history'], ['evidence'])
        self.assertEqual(state['workspace'], 'work')
        self.assertEqual(state['external_blocker'], 'needs analyst')
        state['failures'] = 1
        self.assertFalse(retry_budget.renew(state, {'failures': 0, 'strategy': 0}, 'new'))
        self.assertEqual(state['failures'], 1)
