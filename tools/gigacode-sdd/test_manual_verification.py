import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from controller import read, save
import operator_questions as oq
from operator_display import render_question
import desktop_notifications as notifications
from transport import finish_process


CHECK = {'url': 'http://127.0.0.1:3002/modular-simulations/8',
         'artifact': 'manifest sha256:1234', 'requirements': ['REQ-UI'],
         'preconditions': 'Откройте тестовую запись с ролью редактора.',
         'steps': [{'action': 'Откройте вкладку параметров.', 'expected': 'Поле comment отображается.'}]}


class ManualCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        save(self.root/'config.json', {'requirements_sha256': 'fixed'})
        save(self.root/'plan.json', {'tasks': []})
        self.delivery = patch('desktop_notifications.deliver', return_value={'status': 'submitted'}).start()
        self.addCleanup(patch.stopall)

    def request(self, check=None):
        return oq.request(self.root, 'ui', 'manual-ui', 'Совпали ли результаты?',
                          manual_check=check or CHECK, options=['Да', 'Нет'])

    def test_render_and_answer_keep_reproducible_instructions(self):
        key = self.request()
        q = oq.questions(self.root)[0]
        text = render_question(self.root, q)
        for value in (CHECK['url'], CHECK['artifact'], CHECK['preconditions'],
                      CHECK['steps'][0]['action'], CHECK['steps'][0]['expected']):
            self.assertIn(value, text)
        oq.answer(self.root, key, 'Нет', 'Шаг 1: поля нет')
        reply = oq.replies(self.root, None)[0]
        self.assertEqual(reply['manual_check'], CHECK)
        self.assertEqual(reply['answer']['text'], 'Нет')
        self.assertEqual(reply['answer']['explanation'], 'Шаг 1: поля нет')

    def test_peer_acceptance_cannot_resolve_unanswered_manual_check(self):
        key = self.request()
        ordinary = oq.request(self.root, 'ui', 'other', 'Обычный вопрос?')
        oq.resolve_task(self.root, 'ui')
        self.assertEqual([q['id'] for q in oq.questions(self.root, pending_only=True)], [key])
        self.assertTrue(next(q for q in oq.questions(self.root) if q['id'] == ordinary)['resolved'])

    def test_new_build_never_inherits_old_answer(self):
        old = self.request()
        oq.answer(self.root, old, 'Да')
        new = self.request({**CHECK, 'artifact': 'manifest sha256:changed'})
        self.assertNotEqual(old, new)
        self.assertIsNone(oq.response(self.root, new))

    def test_incomplete_manual_request_returns_to_model_not_operator(self):
        for check in ({}, {**CHECK, 'url': 'file:///tmp/page'}, {**CHECK, 'steps': []},
                      {**CHECK, 'url': 'https://user:secret@example.com'}):
            result = oq.execution_control(json.dumps({'operator_question': {
                'question': 'Проверите?', 'manual_check': check}}))
            self.assertEqual(result[0], 'request_repair')
        self.assertEqual(oq.questions(self.root), [])

    def test_delivery_is_once_across_repeat_and_reopen(self):
        key = self.request()
        self.request()
        notifications.notify_question(self.root, key)
        self.delivery.assert_called_once()
        state = read(self.root/'operator-questions'/'notifications'/(key+'.json'))
        self.assertEqual(state['status'], 'submitted')

    def test_final_gate_waits_for_all_manual_answers(self):
        one = self.request()
        two = oq.request(self.root, 'other', 'manual', 'Проверка?', manual_check=CHECK)
        with self.assertRaises(oq.WaitingForOperator) as error:
            oq.require_manual_answers(self.root)
        self.assertEqual(set(error.exception.question_ids), {one, two})
        oq.answer(self.root, one, 'Нет', 'Не совпало')
        with self.assertRaises(oq.WaitingForOperator):
            oq.require_manual_answers(self.root)
        oq.answer(self.root, two, 'Да')
        oq.require_manual_answers(self.root)
        self.assertEqual(len(oq.replies(self.root, None)), 2)

    def test_failed_delivery_keeps_answerable_question(self):
        self.delivery.return_value = {'status': 'failed'}
        key = self.request()
        oq.answer(self.root, key, 'Нет')
        self.assertEqual(oq.response(self.root, key)['text'], 'Нет')


class DeliveryTests(unittest.TestCase):
    def test_macos_uses_fixed_script_and_argv_not_interpolated_source(self):
        with patch.dict('os.environ', {'SDD_DESKTOP_NOTIFICATIONS': '1'}), \
             patch.object(notifications.sys, 'platform', 'darwin'), \
             patch.object(notifications.shutil, 'which', return_value='/usr/bin/osascript'), \
             patch.object(notifications.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(notifications.deliver()['status'], 'submitted')
            self.assertEqual(run.call_args.args[0][2], notifications.APPLE_SCRIPT)
            self.assertEqual(run.call_args.kwargs['timeout'], 3)
            run.side_effect = subprocess.TimeoutExpired('osascript', 3)
            self.assertEqual(notifications.deliver()['status'], 'failed')


class CleanupRaceTests(unittest.TestCase):
    def test_permission_error_reconciles_only_confirmed_exited_scope(self):
        for code, exists, preserved in ((0, False, False), (None, False, True), (0, True, True)):
            process = Mock(pid=123)
            process.poll.return_value = code
            with patch('transport.stop_process_scope', side_effect=PermissionError(1, 'denied')), \
                 patch('transport.process_scope_exists', return_value=exists):
                result = finish_process(process)
            self.assertEqual(result['preserved'], preserved)
            self.assertEqual(result['observation']['leader_returncode'], code)
            process.kill.assert_not_called()

    def test_guard_race_does_not_signal_completed_scope(self):
        process = Mock(pid=123)
        process.poll.return_value = 0
        with patch('transport.process_scope_exists', return_value=False), patch('transport.stop_process_scope') as stop:
            result = finish_process(process, lambda _: {'reason': 'inventory incomplete'})
        self.assertFalse(result['preserved'])
        stop.assert_not_called()

    def test_unknown_scope_remains_preserved(self):
        process = Mock(pid=123)
        process.poll.return_value = 0
        with patch('transport.process_scope_exists', side_effect=PermissionError(1, 'denied')):
            result = finish_process(process, lambda _: {'reason': 'inventory incomplete'})
        self.assertTrue(result['preserved'])
        self.assertEqual(result['observation']['scope_state'], 'unknown')
