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

    def test_active_actor_and_candidate_author_are_not_conflated(self):
        value = self.value()
        value['tasks'][1]['title'] = 'Интеграционные тесты backend'
        value['tasks'][1]['candidates'] = [
            {'model': 'DeepSeek-V4.1-Flash-1017', 'status': 'running', 'stage': 'execute'},
            {'model': 'GLM-5.3-Flash', 'status': 'running', 'stage': 'review'}]
        value['operations'] = [
            {'kind': 'llm', 'task_id': 'code', 'model': 'DeepSeek-V4.1-Flash-1017',
             'candidate_model': 'DeepSeek-V4.1-Flash-1017', 'phase': 'разработка', 'elapsed_seconds': 352},
            {'kind': 'llm', 'task_id': 'code', 'model': 'DeepSeek-V4.1-Flash-1017',
             'candidate_model': 'GLM-5.3-Flash', 'phase': 'независимое ревью', 'elapsed_seconds': 313}]
        output = run_status.dashboard(value)
        self.assertIn('00:05:52 · Интеграционные тесты backend · DeepSeek разрабатывает свой вариант', output)
        self.assertIn('00:05:13 · Интеграционные тесты backend · DeepSeek проверяет вариант GLM', output)
        self.assertNotIn('Вариант ', output)
        self.assertIn('LLM-сессии сейчас: 2 (DeepSeek-V4.1-Flash-1017: 2)', output)
        value['tasks'][1]['candidates'][1]['resource_wait'] = {'state': 'waiting', 'holder': 'DeepSeek'}
        value['operations'].pop()
        output = run_status.dashboard(value)
        self.assertIn('GLM-5.3-Flash: ждёт ресурс', output)
        self.assertIn('В очереди: 1 вариант решения ждёт ресурс.', output)
        self.assertIn('Ответ пользователя не требуется.', output)
        self.assertNotIn('Вариант DeepSeek', output)
        value['status'] = 'interrupted'
        self.assertIn('Вариант сохранён: DeepSeek', run_status.dashboard(value))

    def test_missing_author_is_not_inferred_from_actor_or_candidates(self):
        value = self.value()
        value['operations'] = [{'kind': 'llm', 'task_id': 'code', 'model': 'GLM',
                                'phase': 'review', 'elapsed_seconds': 3}]
        self.assertIn('GLM проверяет вариант (автор не указан)', run_status.dashboard(value))
        self.assertNotIn('свой вариант', run_status.dashboard(value))

    def test_completed_list_has_no_implied_execution_order_and_compact_status_omits_route(self):
        value = self.value()
        value['tasks'][0]['title'] = 'Подготовка backend'
        value['tasks'].insert(1, {'id': 'prepare-fe', 'title': 'Подготовка frontend', 'status': 'done'})
        value['done'] = 2
        self.assertIn('✓ Подготовка backend · Подготовка frontend', run_status.dashboard(value))
        compact = run_status.dashboard(value, include_overview=False)
        self.assertNotIn('✓', compact)
        self.assertNotIn('Оставшиеся задачи и зависимости', compact)
        self.assertIn('Принято: 2 из 3', compact)
        value['observation'] = {'summary': 'Проверка ещё выполняется.'}
        self.assertIn('Основание: Проверка ещё выполняется.', run_status.dashboard(value))

    def test_observer_projection_is_advisory_and_stale_report_is_labelled(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            save(root/'state.json', {'status': 'interrupted', 'tasks': {}})
            save(root/'observer/latest.json', {'summary': 'Есть продвижение.', 'snapshot_at': 1})
            value = run_status.snapshot(root)
            self.assertTrue(value['observation']['stale'])
            self.assertFalse(value['telemetry_incomplete'])
            output = run_status.dashboard(value)
            self.assertTrue(output.startswith('Работа остановлена.'))
            self.assertIn('Предыдущее наблюдение аудиторов', output)
            self.assertIn('не описывает текущее исполнение', output)
            (root/'observer/latest.json').write_text('{broken')
            value = run_status.snapshot(root)
            self.assertFalse(value['telemetry_incomplete'])
            self.assertEqual(value['observation'], {})

    def test_observer_health_shows_attempt_and_retry_but_not_after_stop(self):
        value = self.value()
        value['observation'] = {'snapshot_at': 1000, 'status': 'unavailable'}
        value['observer_health'] = {'scope': {'model': 'GLM', 'attempt': 3, 'phase': 'waiting',
            'last_success_at': 800, 'failure': 'превышено время ожидания', 'next_attempt_at': 1200}}
        text = '\n'.join(run_status.observation_lines(value))
        self.assertIn('попытка 3', text)
        self.assertIn('последний успех', text)
        self.assertIn('повтор не раньше', text)
        value['status'] = 'interrupted'
        self.assertNotIn('повтор не раньше', '\n'.join(run_status.observation_lines(value)))

    def test_candidate_history_is_bounded_with_explicit_truncation(self):
        with TemporaryDirectory() as tmp:
            runner = SimpleNamespace(root=Path(tmp))
            value = {'model': 'GLM', 'stage': 'review', 'status': 'running', 'history': [
                {'kind': 'old', 'detail': 'not retained'},
                {'kind': 'checks', 'detail': {'ok': False}},
                {'kind': 'review', 'detail': 'x'*1600},
                {'kind': 'retry', 'detail': 'Retried'}]}
            run_status.candidate(runner, {'id': 'code'}, value)
            from controller import read
            projection = read(next((runner.root/'progress').glob('*.json')))
            self.assertEqual([h['kind'] for h in projection['history']], ['checks', 'review', 'retry'])
            self.assertEqual(len(projection['history'][1]['detail']), 1500)
            self.assertTrue(projection['history'][1]['detail_truncated'])
            self.assertFalse(projection['history'][0]['detail_truncated'])

    def test_auditor_assessment_is_near_top_short_and_not_execution_health(self):
        value = self.value()
        value['operations'] = [{'kind': 'llm', 'purpose': 'observation', 'model': 'GLM',
            'title': 'Аудитор прогресса', 'elapsed_seconds': 170, 'timeout_seconds': 180, 'idle_seconds': 350}]
        value['observation'] = {'snapshot_at': 1000, 'status': 'progress',
            'summary': 'Продвижение есть.', 'results': {'progress': {'status': 'progress', 'summary': '25 тестов прошли.'},
            'scope': {'status': 'progress', 'summary': 'Границы соблюдены.'}}, 'report': '/run/report.json'}
        with patch.object(run_status.time, 'time', return_value=1100):
            output = run_status.dashboard(value)
            self.assertTrue(output.startswith('В работе. Запросов на вмешательство нет.'))
            self.assertLess(output.index('Аудиторы, снимок'), output.index('Принято:'))
            self.assertIn('Основание: 25 тестов прошли.', output)
            self.assertNotIn('близок лимит', output)
            self.assertNotIn('Давно нет новых событий', output)
            self.assertIn('LLM-сессии сейчас: 1 (GLM: 1)', output)
            self.assertIn('Отчёт аудиторов: /run/report.json', output)
            compact = run_status.dashboard(value, include_overview=False)
            self.assertNotIn('/run/report.json', compact)
            value['questions'] = [{'question': 'Ответ?'}]
            self.assertNotIn('Независимая работа продолжается', run_status.dashboard(value))

    def test_auditor_disagreement_and_long_text_do_not_hide_factual_stop(self):
        value = self.value()
        value['status'] = 'interrupted'
        value['observation'] = {'snapshot_at': 1000, 'status': 'disagreement', 'results': {
            'progress': {'status': 'progress', 'summary': 'Проверки идут.'},
            'scope': {'status': 'concern', 'summary': 'INSTRUCTION '*100}}}
        with patch.object(run_status.time, 'time', return_value=1100):
            output = run_status.dashboard(value)
        self.assertTrue(output.startswith('Работа остановлена.'))
        self.assertIn('оценки расходятся', output)
        self.assertIn('не описывает текущее исполнение', output)
        self.assertIn('Прогресс: Проверки идут.', output)
        self.assertIn('Соответствие задаче: Подробности в отчёте.', output)
        self.assertNotIn('INSTRUCTION', output)
        value['status'] = 'running'
        with patch.object(run_status.time, 'time', return_value=2000):
            self.assertIn('(устарело)', run_status.dashboard(value))
        value['observation'] = {'status': 'unavailable', 'summary': 'Режим без инструментов не подтверждён.'}
        self.assertIn(': недоступны.', run_status.dashboard(value))


if __name__ == '__main__':
    unittest.main()
