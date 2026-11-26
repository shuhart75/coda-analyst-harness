import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from execution_inputs import accepted_tasks, missing_prerequisites, prompt_value
from controller import Runner


class ExecutionInputTests(unittest.TestCase):
    def test_recursive_state_is_not_a_prompt_but_obligations_survive(self):
        records = {'task': {'status': 'done', 'integrated_head': 'abc',
            'history': ['x' * 9000000], 'workspace_snapshot': {'files': {'big': 'data'}},
            'session_result': {'text': 'y' * 1000000},
            'review': {'verdict': 'pass', 'deferred_evidence': [{'requirement': 'REQ-X', 'owner': 'integration'}]},
            'checks': [{'ok': True, 'log': 'check.log', 'output': 'z' * 1000000}]}}
        result = accepted_tasks(records)
        self.assertLess(len(json.dumps(result)), 1000)
        self.assertEqual(result[0]['deferred_evidence'][0]['requirement'], 'REQ-X')
        self.assertEqual(result[0]['checks'], [{'ok': True, 'log': 'check.log'}])
        self.assertEqual(len(records['task']['history'][0]), 9000000)

    def test_large_input_is_preserved_instead_of_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = {'requirements': 'REQ-X ' * 10000, 'decision': 'last decision'}
            reference = prompt_value(tmp, value)
            self.assertLess(len(json.dumps(reference)), 1000)
            self.assertEqual(json.loads(Path(reference['full_input_file']).read_text()), value)
            self.assertEqual(prompt_value(tmp, value), reference)
            self.assertEqual(prompt_value(tmp, {'small': 1}), {'small': 1})
            Path(reference['full_input_file']).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                prompt_value(tmp, value)

    def test_dependency_entry_is_checked_in_each_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, patch('execution_inputs.shutil.which', return_value='/bin/node'):
            root = Path(tmp)
            checks = [{'cwd': 'frontend', 'argv': ['node', 'node_modules/typescript/bin/tsc', '--noEmit']}]
            self.assertEqual(missing_prerequisites(root, checks, {})[0]['kind'], 'missing-dependency-entry')
            entry = root/'frontend/node_modules/typescript/bin/tsc'
            entry.parent.mkdir(parents=True)
            entry.write_text('tool')
            self.assertEqual(missing_prerequisites(root, checks, {}), [])
            self.assertEqual(len(missing_prerequisites(root/'peer', checks, {})), 1)

    def test_missing_source_and_unknown_command_are_not_dependency_failures(self):
        with tempfile.TemporaryDirectory() as tmp, patch('execution_inputs.shutil.which', return_value='/bin/node'):
            for argv in (['node', 'tools/check.js'], ['node', '-e', 'hello'], ['npm', 'test']):
                self.assertEqual(missing_prerequisites(tmp, [{'argv': argv}], {}), [])

    def test_missing_executable_is_not_a_product_defect(self):
        with tempfile.TemporaryDirectory() as tmp, patch('execution_inputs.shutil.which', return_value=None):
            self.assertEqual(missing_prerequisites(tmp, [{'argv': ['not-installed']}], {})[0]['kind'], 'missing-executable')

    def test_check_batch_is_not_launched_before_environment_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp, patch('execution_inputs.shutil.which', return_value='/bin/node'), \
                patch('controller.launch_process') as launch:
            root = Path(tmp)
            runner = SimpleNamespace(stop=None, env=dict(os.environ), guard=lambda: None,
                                     logdir=lambda name: root/'logs'/name)
            checks = [{'argv': ['node', 'node_modules/test/run.js']}, {'argv': ['node', 'tools/expensive.js']}]
            reports = Runner.checks(runner, root, checks, 'check')
            launch.assert_not_called()
            self.assertEqual(reports[0]['category'], 'environment')
            self.assertFalse(reports[0]['ok'])
            self.assertIn('missing-dependency-entry', Path(reports[0]['log']).read_text())


if __name__ == '__main__':
    unittest.main()
