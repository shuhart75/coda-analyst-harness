import time
import unittest
from unittest.mock import patch

from operator_stages import stages, timestamp
from run_status import dashboard, health_summary, observation_lines
from session_activity import SessionActivity


class StageTests(unittest.TestCase):
    def test_accepted_tasks_still_require_final_acceptance(self):
        value = {'status': 'running', 'tasks': [{'id': 'a', 'status': 'done'}],
                 'integration': 'blocked', 'operations': [{'kind': 'llm', 'task_id': 'integration',
                 'elapsed_seconds': 50, 'model': 'GLM', 'phase': 'разработка'}], 'ready': False}
        phases = {p['id']: p for p in stages(value)}
        self.assertEqual(phases['tasks']['status'], 'done')
        self.assertEqual(phases['integration']['status'], 'blocked')
        self.assertEqual(phases['readiness']['status'], 'pending')
        self.assertTrue(dashboard(value).startswith(health_summary(value)))
        self.assertIn('Есть блокировка', health_summary(value))
        self.assertIn('Итоговая приёмка всего решения', dashboard(value))

    def test_dates_explain_yesterday_future_and_previous_controller(self):
        now = 1800000000
        with patch('time.time', return_value=now):
            yesterday = now-86400
            self.assertIn('24 ч', timestamp(yesterday))
            self.assertIn(time.strftime('%d.%m.%Y', time.localtime(yesterday)), timestamp(yesterday))
            self.assertIn('в будущем', timestamp(now+600))
            lines = observation_lines({'status': 'running', 'observation': {'snapshot_at': now, 'status': 'unknown'},
                'observer_health': {'scope': {'model': 'GLM', 'phase': 'waiting',
                    'last_success_at': yesterday, 'controller_started_at': now-100}}})
            self.assertIn('успех предыдущего запуска контроллера', '\n'.join(lines))

    def test_tool_detail_is_bounded_redacted_and_not_completion_proof(self):
        reader = SessionActivity('/unused')
        reader.observe({'message': {'content': [{'type': 'tool_use', 'id': 't', 'name': 'Bash',
            'input': {'command': 'curl --token TOPSECRET https://local.test', 'secret': 'UNRELATED_SECRET'}}]}})
        summary = reader.summary()
        self.assertEqual(summary['pending_calls'], 1)
        self.assertIn('curl', summary['pending_actions'][0])
        self.assertNotIn('TOPSECRET', str(summary))
        self.assertNotIn('UNRELATED_SECRET', str(summary))
        reader.observe({'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't', 'is_error': True,
                                                 'content': 'PRIVATE_RESULT'}]}})
        self.assertEqual(reader.summary()['pending_actions'], [])
        self.assertEqual(reader.summary()['recent_events'][-1]['outcome'], 'ошибка инструмента')
        self.assertNotIn('PRIVATE_RESULT', str(reader.summary()))
        for _ in range(30):
            reader.observe({'message': {'content': [{'type': 'text', 'text': 'x'*10000}]}})
        self.assertEqual(len(reader.summary()['recent_events']), 8)
        self.assertLessEqual(len(reader.summary()['recent_events'][0]['action']), 500)
        self.assertFalse(reader.summary()['terminal_result'])
