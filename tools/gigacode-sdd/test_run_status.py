import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import run_status
from controller import save


class ProgressTests(unittest.TestCase):
    def value(self):
        def row(name, status):
            return {'id': name, 'status': status, 'waiting_for': [], 'error': None,
                    'goal': ['NEVER PRINT MODEL INSTRUCTIONS'], 'deferred_evidence': [], 'candidates': []}
        return {'status': 'running', 'done': 1, 'total': 3,
                'tasks': [row('prepare', 'done'), row('code', 'running'), row('test', 'pending')],
                'services': [], 'questions': [], 'answer_command': 'python /absolute/bootstrap.py --answers-config /run/config.json',
                'integration': None, 'ready': False}

    def test_full_map_never_prints_instructions_or_deliverable_prose(self):
        value = self.value()
        value['tasks'][2]['waiting_for'] = ['code']
        output = run_status.render(value)
        self.assertNotIn('NEVER PRINT', output)
        self.assertIn('prepare: принято', output)
        self.assertIn('test: ожидает запуска; после code', output)
        self.assertIn('1/3', output)

    def test_stage_change_only_prints_changed_task_and_ignores_heartbeat(self):
        before = self.value()
        before['tasks'][1]['candidates'] = [{'model': 'GLM', 'status': 'running', 'stage': 'execute', 'updated_at': 1}]
        after = copy.deepcopy(before)
        after['tasks'][1]['candidates'][0]['updated_at'] = 2
        self.assertEqual(run_status.render(after, before), '')
        after['tasks'][1]['candidates'][0]['stage'] = 'review'
        output = run_status.render(after, before)
        self.assertIn('Вариант GLM: независимое ревью', output)
        self.assertNotIn('prepare', output)
        self.assertNotIn('test:', output)

    def test_acceptance_releases_dependency_without_repeating_completed_tasks(self):
        before = self.value()
        before['tasks'][2]['waiting_for'] = ['code']
        after = copy.deepcopy(before)
        after['tasks'][1]['status'] = 'done'
        after['tasks'][2]['waiting_for'] = []
        after['done'] = 2
        output = run_status.render(after, before)
        self.assertIn('2/3', output)
        self.assertIn('code: принято', output)
        self.assertIn('test: ожидает запуска; готова к запуску', output)
        self.assertNotIn('prepare', output)

    def test_questions_and_services_remain_visible_when_no_task_changes(self):
        before = self.value()
        after = copy.deepcopy(before)
        after['questions'] = [{'task': 'code', 'question': 'Разрешить запуск?'}]
        after['services'] = [{'id': 'backend', 'status': 'ready'}]
        output = run_status.render(after, before)
        self.assertIn(after['answer_command'], output)
        self.assertIn('Разрешить запуск?', output)
        self.assertIn('Стенд backend: готов', output)
        self.assertNotIn('prepare', output)
        resolved = copy.deepcopy(after)
        resolved['questions'] = []
        self.assertIn('Открытых вопросов пользователю больше нет.', run_status.render(resolved, after))
        self.assertNotIn('--answers-config', run_status.render(resolved, after))

    def test_blocker_and_final_readiness_are_not_hidden(self):
        before = self.value()
        after = copy.deepcopy(before)
        after['tasks'][1].update(status='blocked', error='Нет доступа к браузеру')
        self.assertIn('Причина: Нет доступа к браузеру', run_status.render(after, before))
        final = copy.deepcopy(before)
        final.update(status='complete', ready=True)
        self.assertIn('итоговая приёмка: пройдена', run_status.render(final, before))

    def test_publish_full_on_start_and_request_but_silent_when_unchanged(self):
        with TemporaryDirectory() as tmp:
            messages = []
            runner = SimpleNamespace(root=Path(tmp), state={}, progress=messages.append)
            value = self.value()
            with patch.object(run_status, 'snapshot', return_value=value):
                run_status.publish(runner)
                run_status.publish(runner)
                self.assertEqual(len(messages), 1)
                run_status.publish(runner, force=True)
                self.assertEqual(messages[0], messages[1])
            self.assertTrue((runner.root/'progress.json').is_file())

    def test_legacy_plan_prompt_does_not_enter_progress_projection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            save(root/'config.json', {'requirements_sha256': 'fixed'})
            save(root/'plan.json', {'tasks': [{'id': 'code', 'prompt': 'SECRET INSTRUCTIONS', 'depends_on': []}]})
            save(root/'state.json', {'status': 'running', 'tasks': {}})
            value = run_status.snapshot(root)
            self.assertEqual(value['tasks'][0]['goal'], [])
            self.assertNotIn('SECRET', str(value))


if __name__ == '__main__':
    unittest.main()
