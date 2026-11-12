"""Service preservation and repeat-run safety, independent of host applications."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import controller
import test_controller as fixtures
from transport import run_session


@unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
class TransportPreservationTests(unittest.TestCase):
    def test_group_survives_completed_cli_when_cleanup_is_unsafe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / 'alive'
            child = "import time,pathlib; time.sleep(.3); pathlib.Path(%r).write_text('alive'); time.sleep(30)" % str(marker)
            cli = root / 'cli.py'
            cli.write_text('import sys,subprocess,json\n'
                           + 'subprocess.Popen([sys.executable,"-c",' + repr(child) + '])\n'
                           + 'print(json.dumps({"type":"result","is_error":False}),flush=True)\n')
            result = run_session([sys.executable, str(cli)], root, 'task', root / 'logs',
                                 cleanup_guard=lambda group: {'reason': 'Service present'})
            group = result['cleanup']['process_group']
            try:
                self.assertFalse(result['ok'])
                self.assertTrue(result['cleanup']['preserved'])
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(marker.read_text(), 'alive')
            finally:
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_timeout_returns_without_killing_preserved_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli = root / 'cli.py'
            cli.write_text('import time\ntime.sleep(30)\n')
            result = run_session([sys.executable, str(cli)], root, 'task', root / 'logs',
                                 timeout_seconds=.15,
                                 cleanup_guard=lambda group: {'reason': 'Inventory unavailable'})
            group = result['cleanup']['process_group']
            try:
                self.assertTrue(result['timed_out'])
                self.assertTrue(result['cleanup']['preserved'])
                self.assertIsNone(result['returncode'])
                os.killpg(group, 0)
            finally:
                os.killpg(group, signal.SIGTERM)
                try:
                    os.waitpid(group, 0)
                except ChildProcessError:
                    pass


class ControllerServiceGuardTests(unittest.TestCase):
    setUp = fixtures.ControllerE2E.setUp
    task = fixtures.ControllerE2E.task

    def service(self, pid=54321, group=54320):
        return {'pid': pid, 'pgid': group, 'ppid': 1, 'kind': 'postgres',
                'cwd': str(self.runner.root / 'workers/demo'), 'ports': [5433],
                'started': 'known-process-identity'}

    def inventory(self, services=(), complete=True):
        return {'complete': complete, 'limitations': [] if complete else ['lsof-unavailable'],
                'agents': [], 'services': list(services)}

    def test_group_with_database_is_preserved_and_blocks_resume(self):
        with patch('controller.os.killpg') as signal_probe, \
                patch('controller.inspect_services', return_value=self.inventory([self.service()])):
            report = self.runner.cleanup_guard(54320)
            self.assertIsNotNone(report)
            self.assertTrue(self.runner.stop.is_set())
            self.assertTrue(Path(report['report']).is_file())
            with self.assertRaisesRegex(controller.Blocked, 'still present'):
                self.runner.check_preserved_processes()
            self.assertTrue(all(call.args[1] == 0 for call in signal_probe.call_args_list))
        with patch('controller.os.killpg', side_effect=ProcessLookupError):
            self.runner.check_preserved_processes()

    def test_incomplete_inventory_never_authorizes_group_termination(self):
        with patch('controller.os.killpg'), \
                patch('controller.inspect_services', return_value=self.inventory(complete=False)):
            self.assertIsNotNone(self.runner.cleanup_guard(54320))

    def test_absent_group_needs_no_inventory(self):
        with patch('controller.os.killpg', side_effect=ProcessLookupError), \
                patch('controller.inspect_services') as inspect:
            self.assertIsNone(self.runner.cleanup_guard(54320))
            inspect.assert_not_called()

    def test_detached_service_remains_blocker_on_next_run(self):
        service = self.service()
        with patch('controller.inspect_services', return_value=self.inventory([service])):
            with self.assertRaisesRegex(controller.Blocked, 'no automatic service shutdown'):
                self.runner.service_audit(self.inventory(), self.runner.root, 'fake')
        resumed = controller.Runner(self.config)
        with patch('controller.os.kill') as probe:
            with self.assertRaisesRegex(controller.Blocked, 'detached service'):
                resumed.check_preserved_processes()
            probe.assert_called_once_with(service['pid'], 0)
        with patch('controller.os.kill', side_effect=ProcessLookupError):
            resumed.check_preserved_processes()

    def test_resume_finds_late_listener_without_a_preservation_record(self):
        service = self.service()
        self.assertFalse((self.runner.root / 'preserved-services').exists())
        with patch('controller.inspect_services', return_value=self.inventory([service])), \
                patch('controller.os.kill') as kill, patch('controller.os.killpg') as killpg:
            with self.assertRaisesRegex(controller.Blocked, 'Existing managed agent or service'):
                self.runner.check_preserved_processes()
            kill.assert_not_called()
            killpg.assert_not_called()
        self.assertTrue((self.runner.root / 'preserved-services' / (str(service['pid']) + '.json')).is_file())

    def test_resume_finds_surviving_agent_after_unjournaled_controller_crash(self):
        inventory = self.inventory()
        inventory['agents'] = [{'pid': 60001, 'cwd': str(self.runner.root / 'workers/demo'), 'kind': 'gigacode'}]
        with patch('controller.inspect_services', return_value=inventory), \
                patch('controller.os.kill') as kill, patch('controller.os.killpg') as killpg:
            with self.assertRaisesRegex(controller.Blocked, 'Existing managed agent or service'):
                self.runner.check_preserved_processes()
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_worker_existing_listener_guard_does_not_block_other_workers(self):
        worker = self.runner.root / 'workers/one'
        other_service = self.service()
        other_service['cwd'] = str(self.runner.root / 'workers/two')
        inventory = self.inventory([other_service])
        self.runner.check_existing_workspace_processes(inventory, worker, 'test')
        self.assertFalse(self.runner.stop.is_set())
        inventory['services'][0]['cwd'] = str(worker / 'backend')
        with self.assertRaisesRegex(controller.Blocked, 'Existing managed agent or service'):
            self.runner.check_existing_workspace_processes(inventory, worker, 'test')

    def test_resume_requires_an_actual_complete_process_snapshot(self):
        with patch('controller.inspect_services', return_value=self.inventory(complete=False)):
            with self.assertRaisesRegex(controller.Blocked, 'Cannot inspect managed processes'):
                self.runner.check_preserved_processes()

    def test_history_is_initial_reference_and_pinned_after_import(self):
        from prior_context import prepare_prior_context
        history = self.root / 'history.jsonl'
        history.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(self.source)}}) + '\n'
                           + json.dumps({'type': 'assistant', 'message': {'role': 'assistant',
                                         'content': 'Old claim: everything is ready'}}) + '\n')
        meta = prepare_prior_context(history, self.source, self.runner.root / 'prior-context')
        cfg = controller.read(self.config)
        cfg['prior_context'] = meta
        controller.save(self.config, cfg)
        resumed = controller.Runner(self.config)
        self.assertIn(meta['evidence_path'], resumed.context('review', include_history=True))
        self.assertNotIn(meta['evidence_path'], resumed.context('execute'))
        resumed.guard()
        Path(meta['evidence_path']).write_text('Replacement old instructions')
        with self.assertRaises(ValueError):
            resumed.guard()


if __name__ == '__main__':
    unittest.main()
