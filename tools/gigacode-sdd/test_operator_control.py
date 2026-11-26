import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from controller import read, save
from platform_support import FileLock
from operator_control import ControllerControl, status, request_stop
from operator_actions import Actions, revision
from operator_worker import WebUI


def until(predicate, timeout=3):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Condition not reached')


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'config.json'
        save(self.config, {'state_dir': str(self.root)})
        self.actions = Actions(self.root/'operator-control')

    def payload(self, action='resume', key='a'*32):
        return {'id': key, 'action': action, 'revision': revision(self.config)}

    def test_request_is_bound_to_exact_instance_and_does_not_signal(self):
        stop = threading.Event()
        with FileLock(self.root/'run.lock'):
            control = ControllerControl(self.root, stop)
            control.start()
            try:
                self.assertTrue(status(self.root)['controllable'])
                with self.assertRaises(ValueError):
                    request_stop(self.root, 'old-instance', 'a'*32)
                self.assertFalse(stop.is_set())
                request_stop(self.root, control.instance, 'b'*32)
                until(stop.is_set)
                until(lambda: (self.root/'controller-stop-receipt.json').exists())
                self.assertTrue(read(self.root/'controller-stop-receipt.json')['accepted'])
            finally:
                control.close()
        self.assertFalse(status(self.root)['active'])
        other = ControllerControl(self.root, threading.Event())
        with FileLock(self.root/'run.lock'):
            other.start()
            try:
                time.sleep(.3)
                self.assertFalse(other.stop.is_set())
            finally:
                other.close()

    def test_legacy_owner_cannot_be_stopped_or_resumed_by_guessing_pid(self):
        with FileLock(self.root/'run.lock'):
            self.assertFalse(status(self.root)['controllable'])
            for action in ('resume', 'new', 'stop', 'restart'):
                with self.assertRaises(ValueError):
                    self.actions.submit(self.config, self.payload(action))

    def test_duplicate_request_starts_one_detached_fixed_worker(self):
        child = Mock()
        child.poll.return_value = None
        payload = self.payload()
        with patch('operator_actions.subprocess.Popen', return_value=child) as launch:
            receipt = self.actions.submit(self.config, payload)
            self.assertEqual(self.actions.submit(self.config, payload), receipt)
            launch.assert_called_once()
            args, kwargs = launch.call_args
            self.assertTrue(args[0][1].endswith('operator_worker.py'))
            self.assertNotIn('shell', kwargs)
            self.assertEqual(args[0][2], '--operation')
            with self.assertRaises(ValueError):
                self.actions.submit(self.config, {**payload, 'action': 'new'})
            with self.assertRaises(ValueError):
                self.actions.submit(self.config, self.payload(key='b'*32))

    def test_stale_revision_cannot_launch_changed_config(self):
        payload = self.payload()
        save(self.config, {'different': True})
        with patch('operator_actions.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                self.actions.submit(self.config, payload)
            launch.assert_not_called()

    def test_wizard_reply_is_durable_idempotent_and_bound_to_question(self):
        folder = self.actions.path('a'*32)
        folder.mkdir()
        ui = WebUI(folder)
        result = []
        thread = threading.Thread(target=lambda: result.append(ui.choose('Выберите', ['Первый', 'Второй'])))
        thread.start()
        until(lambda: read(folder/'state.json').get('status') == 'waiting')
        q = read(folder/'state.json')['question']
        payload = {'operation': folder.name, 'question': q['id'], 'value': 1}
        self.actions.reply(payload)
        self.actions.reply(payload)
        thread.join(3)
        self.assertEqual(result, [1])
        with self.assertRaises(ValueError):
            self.actions.reply({**payload, 'value': 0})
        self.assertEqual(len(list(folder.glob('reply-*.json'))), 1)

    def test_cancel_cannot_reverse_a_submitted_confirmation(self):
        folder = self.actions.path('a'*32)
        folder.mkdir()
        save(folder/'state.json', {'status': 'waiting', 'question': {'id': 'b'*32}})
        save(folder/('reply-'+'b'*32+'.json'), {'value': 0})
        with self.assertRaises(ValueError):
            self.actions.cancel(folder.name)
        self.assertFalse((folder/'cancel.json').exists())

    def test_cancelled_wizard_cannot_acknowledge_a_new_answer(self):
        folder = self.actions.path('a'*32)
        folder.mkdir()
        save(folder/'state.json', {'status': 'waiting', 'question': {
            'id': 'b'*32, 'kind': 'confirm', 'options': ['Go', 'Cancel']}})
        self.actions.cancel(folder.name)
        with self.assertRaises(ValueError):
            self.actions.reply({'operation': folder.name, 'question': 'b'*32, 'value': 0})
        self.assertFalse((folder/('reply-'+'b'*32+'.json')).exists())

    def test_download_requires_unchanged_receipted_artifact(self):
        from controller import digest
        folder = self.actions.path('a'*32)
        folder.mkdir()
        output = folder/'diagnostics.txt'
        output.write_text('result')
        save(folder/'state.json', {'status': 'succeeded', 'artifact': str(output), 'artifact_sha256': digest(output)})
        self.assertEqual(self.actions.artifact(folder.name), output)
        output.write_text('changed')
        with self.assertRaises(ValueError):
            self.actions.artifact(folder.name)

    def test_detached_diagnostics_worker_returns_downloadable_receipt(self):
        save(self.root/'state.json', {'status': 'interrupted', 'tasks': {}})
        save(self.root/'plan.json', {'tasks': []})
        receipt = self.actions.submit(self.config, self.payload('diagnostics'))
        child = self.actions.children[0]
        try:
            self.assertEqual(child.wait(timeout=20), 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        output = self.actions.artifact(receipt['id'])
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(self.actions.operations()[0]['status'], 'succeeded')

    def operation(self, action):
        from controller import digest
        folder = self.actions.path('a'*32)
        folder.mkdir()
        save(folder/'request.json', {'action': action, 'config': str(self.config),
             'config_sha256': digest(self.config), 'instance': 'b'*32})
        return folder

    def test_restart_waits_for_release_before_using_guarded_resume(self):
        from operator_worker import perform
        folder = self.operation('restart')
        with patch('operator_worker.operator_control.request_stop') as stop, \
             patch('operator_worker.operator_control.active', side_effect=[True, False]) as active, \
             patch('operator_worker.bootstrap.resume', return_value=0) as resume:
            self.assertEqual(perform(folder), 0)
            stop.assert_called_once_with(self.root, 'b'*32, folder.name)
            self.assertEqual(active.call_count, 2)
            resume.assert_called_once()
            self.assertEqual(resume.call_args.args[1], self.config)
        self.assertEqual(read(folder/'state.json')['status'], 'succeeded')

    def test_restart_timeout_does_not_start_another_controller(self):
        from operator_worker import perform
        folder = self.operation('restart')
        with patch('operator_worker.operator_control.request_stop'), \
             patch('operator_worker.operator_control.active', return_value=True), \
             patch('operator_worker.time.monotonic', side_effect=[0, 121]), \
             patch('operator_worker.bootstrap.resume') as resume:
            self.assertEqual(perform(folder), 2)
            resume.assert_not_called()
        self.assertEqual(read(folder/'state.json')['status'], 'failed')

    def test_restart_rechecks_config_after_stopping(self):
        from operator_worker import perform
        folder = self.operation('restart')
        def stop(*args):
            save(self.config, {'changed': True})
        with patch('operator_worker.operator_control.request_stop', side_effect=stop), \
             patch('operator_worker.operator_control.active', return_value=False), \
             patch('operator_worker.bootstrap.resume') as resume:
            self.assertEqual(perform(folder), 2)
            resume.assert_not_called()

    def test_new_work_uses_existing_wizard_and_callback(self):
        from operator_worker import perform
        folder = self.operation('new')
        def launch(**kwargs):
            self.assertTrue(kwargs['new'])
            kwargs['on_config'](self.config)
            return 130
        with patch('operator_worker.bootstrap.launch', side_effect=launch):
            perform(folder)
        state = read(folder/'state.json')
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['config'], str(self.config))


if __name__ == '__main__':
    unittest.main()
