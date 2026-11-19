import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import task_route
from run_status import dashboard, snapshot


class RouteTests(unittest.TestCase):
    def test_chain_is_topological_and_hides_completed_tasks(self):
        tasks = [{'id': 'backend-regression-compat', 'status': 'pending', 'depends_on': ['backend-modular-its']},
                 {'id': 'backend-modular-its', 'status': 'running', 'depends_on': ['prepare']},
                 {'id': 'prepare', 'status': 'done', 'depends_on': []}]
        text = '\n'.join(task_route.route(tasks))
        self.assertIn('▶ Интеграционные тесты backend [сейчас]', text)
        self.assertIn('→ ○ Проверка обратной совместимости', text)
        self.assertNotIn('prepare', text)
        self.assertLess(text.index('Интеграционные'), text.index('Проверка обратной'))

    def test_branches_are_not_a_serial_chain(self):
        tasks = [{'id': 'a', 'status': 'running', 'depends_on': []},
                 {'id': 'b', 'status': 'running', 'depends_on': []},
                 {'id': 'c', 'status': 'pending', 'depends_on': ['a', 'b']}]
        text = '\n'.join(task_route.route(tasks))
        self.assertNotIn('→', text)
        self.assertIn('после: a, b', text)
        self.assertEqual(text.count('[сейчас]'), 2)
        self.assertNotIn('[сейчас]', '\n'.join(task_route.route(tasks, stopped=True)))

    def test_title_never_uses_prompt_or_terminal_controls(self):
        self.assertEqual(task_route.title({'id': 'x', 'prompt': 'secret'}), 'x')
        self.assertEqual(task_route.title({'id': 'x', 'title': 'Короткое название'}), 'Короткое название')
        self.assertEqual(task_route.title({'id': 'x', 'title': 'bad\x1b[1A'}), 'x')

    def test_sessions_count_operations_not_models_or_checks(self):
        ops = [{'title': 'session', 'kind': 'llm', 'model': m, 'elapsed_seconds': 3}
               for m in ['GLM', 'GLM', 'DeepSeek']]
        ops.append({'title': 'test', 'kind': 'check', 'elapsed_seconds': 2})
        value = {'status': 'running', 'operations': ops}
        text = dashboard(value)
        self.assertIn('LLM-сессии сейчас: 3 (DeepSeek: 1, GLM: 2)', text)
        self.assertIn('число неизвестно', dashboard(dict(value, telemetry_stale=True)))
        self.assertIn('LLM-сессии сейчас: 0.', dashboard(dict(value, status='interrupted')))
        del ops[0]['kind']
        self.assertIn('число неизвестно', dashboard(value))

    def test_snapshot_counts_saved_attempts_and_preserves_plan(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = json.dumps({'tasks': [{'id': 'code', 'depends_on': [], 'prompt': 'secret'}]})
            (root/'plan.json').write_text(plan)
            (root/'state.json').write_text(json.dumps({'status': 'running', 'tasks': {}}))
            (root/'config.json').write_text(json.dumps({'execution_models': None}))
            (root/'console-status.json').write_text(json.dumps({'observed_at': time.time(), 'operations': []}))
            for name in ['execute-1', 'execute-2', 'check']:
                folder = root/'logs'/name
                folder.mkdir(parents=True)
                (folder/'stdout.jsonl').write_text('not parsed')
                if name != 'check':
                    (folder/'prompt.txt').write_bytes(b'\xff')
            value = snapshot(root)
            self.assertEqual(value['session_attempts'], 2)
            self.assertEqual(len(value['tasks'][0]['candidates']), 2)
            self.assertEqual((root/'plan.json').read_text(), plan)
            self.assertNotIn('secret', str(value))


if __name__ == '__main__':
    unittest.main()
