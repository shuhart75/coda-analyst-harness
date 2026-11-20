import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from console_progress import ConsoleProgress
from run_status import dashboard, snapshot
import run_status
from progress_io import read_projection, write_projection
from types import SimpleNamespace
from controller import save
from test_progress import Terminal


class ConsoleTests(unittest.TestCase):
    def test_route_remains_on_each_frame_and_resize_does_not_append_history(self):
        stream = Terminal()
        size = [100, 30]
        with patch.dict(os.environ, {'TERM': 'xterm'}):
            display = ConsoleProgress(self.root/'logs/screen.log', stream=stream,
                clock=lambda: self.now, dimensions=lambda: size)
            value = dict(self.value, tasks=[{'id': 'backend-modular-its', 'status': 'running'},
                {'id': 'backend-regression-compat', 'status': 'pending', 'depends_on': ['backend-modular-its']}])
            display.summary(value)
            for self.now in range(1, 4):
                start = len(stream.getvalue())
                display.tick()
                frame = stream.getvalue()[start:]
                self.assertTrue(frame.startswith('\x1b[H\x1b[J'))
                self.assertIn('▶ Интеграционные тесты backend', frame)
                self.assertIn('Проверка обратной совместимости', frame)
                self.assertTrue(frame.endswith('\x1b[J'))
                self.assertNotIn('\x1b[?1049l', frame)
            size[:] = [30, 8]
            display.tick()
            size[:] = [100, 30]
            display.tick()
            self.assertIn('▶ Интеграционные тесты backend', stream.getvalue().rsplit('\x1b[H', 1)[1])
            self.assertEqual(stream.getvalue().count('\x1b[?1049h'), 1)
            display.close(status='interrupted')
            self.assertIn('\x1b[?25h\x1b[?1049l', stream.getvalue())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = 0
        self.stream = io.StringIO()
        self.display = ConsoleProgress(self.root/'logs/controller.log', stream=self.stream,
                                       clock=lambda: self.now)
        self.value = {'status': 'running', 'tasks': [], 'questions': [], 'done': 4, 'total': 9,
                      'ready': False, 'answer_command': 'python /absolute/path/bootstrap.py --answers-config /run/config.json'}

    def test_technical_messages_go_to_file_only_and_timer_has_bounded_output(self):
        self.display.summary(self.value)
        token = self.display.start('RAW SESSION DETAILS', title='backend · разработка · GLM', timeout=3600)
        try:
            self.display.message('RAW RETRY REASON')
            self.display.telemetry(token, {'idle_seconds': 0})
            initial = self.stream.getvalue()
            for self.now in range(1, 60):
                self.display.tick()
            self.assertEqual(self.stream.getvalue(), initial)
            self.now = 60
            self.display.tick()
            self.assertIn('00:01:00', self.stream.getvalue())
            self.assertNotIn('RAW', self.stream.getvalue())
            self.assertIn('RAW RETRY REASON', (self.root/'logs/controller.log').read_text())
            self.assertNotIn('\x1b', self.stream.getvalue())
        finally:
            self.display.finish(token)
        self.assertFalse(self.display.thread.is_alive())

    def test_interactive_updates_in_place_and_command_is_not_clipped(self):
        stream = Terminal()
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}):
            p = ConsoleProgress(self.root/'logs/tty.log', stream=stream,
                                clock=lambda: self.now, dimensions=lambda: (80, 24))
            p.summary(self.value)
            token = p.start('raw details', title='backend-modular-its · разработка · DeepSeek-V4.1-Flash-1017', timeout=3600)
            try:
                self.now = 18*60+19
                p.tick()
                self.assertIn('Сейчас: 00:18:19 · backend-modular-its', stream.getvalue())
                self.assertIn('\x1b[?1049h', stream.getvalue())
                self.assertIn('\x1b[H', stream.getvalue())
                command = 'python /' + 'long-path/'*30 + 'bootstrap.py --answers-config /run/config.json'
                p.notice(command)
                self.assertIn(command, stream.getvalue())
                self.assertLessEqual(p.rows, 7)
            finally:
                p.finish(token)
            p.close(status='interrupted')
            self.assertEqual(p.rows, 0)
            self.assertIn('Работа остановлена', stream.getvalue())

    def test_question_command_is_complete_in_panel_or_full_status_file(self):
        command = "python3 '/path with spaces/" + 'folder/'*30 + "bootstrap.py' --answers-config '/run space/config.json'"
        value = dict(self.value, questions=[{'question': 'Разрешить?'}], answer_command=command)
        for stream, size in ((io.StringIO(), (40, 12)), (Terminal(), (100, 40)), (Terminal(), (40, 12))):
            with patch.dict(os.environ, {'TERM': 'xterm-256color'}):
                display = ConsoleProgress(self.root/'logs/command.log', stream=stream,
                    clock=lambda: self.now, dimensions=lambda: size)
                display.summary(self.value)
                display.summary(value)
                full = (self.root/'operator-status.txt').read_text()
                self.assertIn(command, full)
                self.assertIn('Откройте отдельное окно терминала', full)
                if size == (100, 40) or not stream.isatty():
                    self.assertIn(command, stream.getvalue())
                else:
                    self.assertIn('Увеличьте окно', stream.getvalue())
                self.assertLess(display.rows, size[1])
                display.close()

    def test_active_events_do_not_imply_success_and_remaining_is_not_a_time_estimate(self):
        result = dashboard(self.value)
        self.assertIn('Запросов на вмешательство нет', result)
        self.assertIn('4 из 9', result)
        self.assertIn('5 задач и итоговая проверка', result)
        self.assertIn('Оценки времени пока нет', result)
        self.assertNotIn('всё хорошо', result.lower())
        self.assertNotIn('пройдена', result)

    def test_timeout_warning_identifies_real_operation_and_not_user_cancel(self):
        value = dict(self.value, operations=[{'title': 'backend · GLM', 'elapsed_seconds': 3500,
                                             'timeout_seconds': 3600, 'idle_seconds': 1}])
        result = dashboard(value)
        self.assertIn('близок лимит', result)
        self.assertIn('00:58:20', result)
        self.assertIn('лимит 01:00:00', result)
        self.assertNotIn('отмен', result)
        value['operations'][0]['elapsed_seconds'] = 100
        value['operations'][0]['idle_seconds'] = 350
        self.assertIn('Давно нет новых событий', dashboard(value))

    def test_questions_and_blockers_override_reassuring_status(self):
        value = dict(self.value, questions=[{'question': 'Разрешить?'}])
        self.assertIn('Нужен ваш ответ', dashboard(value))
        self.assertIn(self.value['answer_command'], dashboard(value))
        self.assertNotIn(self.value['answer_command'], dashboard(value, include_command=False))
        value = dict(self.value, tasks=[{'id': 'backend', 'status': 'blocked'}])
        self.assertIn('Требуется вмешательство', dashboard(value))
        self.assertIn('backend', dashboard(value))
        self.assertIn('Требуется вмешательство', dashboard(dict(self.value, integration='blocked')))

    def test_all_tasks_done_still_requires_final_acceptance(self):
        value = dict(self.value, done=9)
        self.assertIn('Осталась итоговая проверка', dashboard(value))
        value.update(ready=True, status='complete')
        self.assertIn('Итоговая проверка пройдена', dashboard(value))
        self.assertIn('Осталось: 0', dashboard(value))
        self.assertNotIn('Оценки времени пока нет', dashboard(value))

    def test_runtime_is_saved_and_stale_observation_is_not_presented_as_live(self):
        save(self.root/'config.json', {'requirements_sha256': 'fixed'})
        save(self.root/'state.json', {'status': 'running', 'tasks': {}})
        save(self.root/'plan.json', {'tasks': []})
        token = self.display.start('backend', timeout=3600)
        try:
            runtime = json.loads((self.root/'console-status.json').read_text())
            self.assertEqual(runtime['operations'][0]['title'], 'backend')
            runtime['observed_at'] = 1
            save(self.root/'console-status.json', runtime)
            value = snapshot(self.root)
            self.assertIn('Сводка устарела', dashboard(value))
            self.assertIn('нет свежих данных', dashboard(value))
        finally:
            self.display.finish(token)
        self.assertEqual(json.loads((self.root/'console-status.json').read_text())['operations'], [])

    def test_windows_sharing_conflict_retries_and_does_not_fail_execution(self):
        path = self.root/'progress/candidate.json'
        problems = []
        with patch.object(Path, 'read_text', side_effect=[PermissionError('sharing'), '{"task":"backend"}']):
            self.assertEqual(read_projection(path, problems), {'task': 'backend'})
        self.assertEqual(problems, [])
        with patch.object(Path, 'read_text', side_effect=PermissionError('sharing')):
            self.assertEqual(read_projection(path, problems), {})
        self.assertEqual(problems, [str(path)])
        with patch('progress_io.os.replace', side_effect=PermissionError('sharing')):
            self.assertFalse(write_projection(path, {'task': 'backend'}))
        self.assertEqual(list(path.parent.iterdir()), [])
        runner = SimpleNamespace(root=self.root, state={'status': 'running'},
                                 progress=lambda message: None, _display=self.display)
        with patch.object(run_status, 'snapshot', side_effect=PermissionError('sharing')):
            result = run_status.publish(runner)
        self.assertTrue(result['telemetry_incomplete'])
        self.assertIn('Сводка временно неполна', self.stream.getvalue())

    def test_llm_metadata_survives_runtime_projection(self):
        token = self.display.start('session', metadata={'kind': 'llm', 'model': 'GLM', 'task_id': 'code'})
        try:
            runtime = json.loads((self.root/'console-status.json').read_text())
            self.assertEqual(runtime['operations'][0]['kind'], 'llm')
            self.assertEqual(runtime['operations'][0]['model'], 'GLM')
            self.assertIn('LLM-сессии сейчас: 1', self.stream.getvalue())
        finally:
            self.display.finish(token)

    def test_route_is_not_clipped_in_small_terminal(self):
        stream = Terminal()
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}):
            display = ConsoleProgress(self.root/'logs/route.log', stream=stream,
                clock=lambda: self.now, dimensions=lambda: (30, 8))
            value = dict(self.value, tasks=[{'id': 'backend-modular-its', 'status': 'running'}])
            display.summary(value)
            self.assertIn('Увеличьте окно', stream.getvalue())
            self.assertIn('▶ Интеграционные тесты backend [сейчас]', (self.root/'operator-status.txt').read_text())
            self.assertLess(display.rows, 8)
            display.close()

    def test_runtime_write_failure_does_not_abort_operation(self):
        with patch('console_progress.write_projection', return_value=False):
            token = self.display.start('backend')
            try:
                self.assertIn('Сводка временно неполна', self.stream.getvalue())
            finally:
                self.display.finish(token)
        self.assertFalse(self.display.thread.is_alive())

    def test_failure_before_first_summary_is_visible(self):
        self.display.close(status='blocked', failed=True)
        self.assertIn('Запуск остановлен с ошибкой', self.stream.getvalue())
        self.assertNotIn('Запросов на вмешательство нет', self.stream.getvalue())

    def test_failed_close_does_not_claim_success(self):
        self.display.summary(dict(self.value, ready=True, done=9))
        self.display.close(status='blocked', failed=True)
        self.assertIn('Запуск остановлен с ошибкой', self.stream.getvalue())
        self.assertNotIn('Обязательные проверки завершены', self.stream.getvalue().split('\n\n')[-2])

    def test_overview_returns_on_acceptance_but_not_on_periodic_ticks(self):
        value = dict(self.value, tasks=[
            {'id': 'sdd-backend-prepare', 'status': 'done'},
            {'id': 'backend-modular-its', 'status': 'running'},
            {'id': 'backend-regression-compat', 'status': 'pending', 'depends_on': ['backend-modular-its']}])
        self.display.summary(value)
        self.assertIn('✓ Подготовка backend', self.stream.getvalue())
        first = len(self.stream.getvalue())
        self.now = 60
        self.display.tick()
        periodic = self.stream.getvalue()[first:]
        self.assertNotIn('Маршрут', periodic)
        self.assertNotIn('✓', periodic)
        self.assertIn('Принято:', periodic)
        first = len(self.stream.getvalue())
        value['tasks'][1]['status'] = 'done'
        value['done'] += 1
        self.display.summary(value)
        accepted = self.stream.getvalue()[first:]
        self.assertIn('✓ Подготовка backend · Интеграционные тесты backend', accepted)
        self.assertIn('Проверка обратной совместимости', accepted)


if __name__ == '__main__':
    unittest.main()
