import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from controller import Blocked, OperationalBlocked, save
import inventory_recovery
from run_status import dashboard
from compact_diagnostics import collect_compact


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root/'execution-cycles/scope'
        self.report = self.root/'logs/backend-execute-services-123456abcdef.json'
        save(self.report, {'inventory': {'complete': False, 'limitations': ['listener-process-changed-or-unavailable'],
                                        'processes': [{'pid': 999}]}, 'new_workspace_services': []})
        self.fatal = {'type': 'Blocked', 'scope': 'scope',
                      'error': 'Service inspection requires attention; no automatic service shutdown. See ' + str(self.report)}
        save(self.folder/'fatal.json', self.fatal)
        self.runner = SimpleNamespace(root=self.root, guard=Mock(), check_preserved_processes=Mock())
        self.record = {'workspace': str(self.root/'workers/backend')}

    def recover(self):
        return inventory_recovery.recover(self.runner, self.folder, 'scope', self.record)

    def test_only_verified_inventory_failure_is_archived(self):
        with patch('service_inventory.inspect_services', return_value={'complete': True}):
            self.assertTrue(self.recover())
        self.runner.guard.assert_called_once()
        self.runner.check_preserved_processes.assert_called_once()
        self.assertFalse((self.folder/'fatal.json').exists())
        archived = list(self.folder.glob('fatal-inventory-recovered-*.json'))
        self.assertEqual(json.loads(archived[0].read_text()), self.fatal)

    def test_incomplete_fresh_inventory_or_live_service_preserves_fatal(self):
        with patch('service_inventory.inspect_services', return_value={'complete': False}):
            with self.assertRaises(Blocked): self.recover()
        self.assertTrue((self.folder/'fatal.json').exists())
        self.runner.check_preserved_processes.side_effect = Blocked('live managed service')
        with patch('service_inventory.inspect_services', return_value={'complete': True}):
            with self.assertRaises(Blocked): self.recover()
        self.assertTrue((self.folder/'fatal.json').exists())

    def test_actual_new_service_and_other_safety_failures_are_not_recovered(self):
        save(self.report, {'inventory': {'complete': False, 'limitations': ['race']},
                           'new_workspace_services': [{'pid': 42}]})
        self.assertFalse(self.recover())
        save(self.report, {'inventory': {'complete': False, 'limitations': ['race']}, 'new_workspace_services': []})
        self.fatal['related_failures'] = [{'type': 'Blocked', 'scope': 'scope', 'error': 'Changed Git controls'}]
        save(self.folder/'fatal.json', self.fatal)
        self.assertFalse(self.recover())
        self.runner.guard.assert_not_called()

    def test_compact_report_includes_service_stop_without_full_process_table(self):
        save(self.root/'config.json', {})
        save(self.root/'state.json', {'status': 'interrupted', 'tasks': {}})
        stop = {'reason': 'Автоматическая остановка: снимок неполон', 'report': str(self.report)}
        save(self.root/'service-stop.json', stop)
        path = collect_compact(self.root/'config.json', self.root/'report.txt')
        text = Path(path).read_text()
        self.assertIn('compact/logs/' + self.report.name, text)
        self.assertIn('listener-process-changed-or-unavailable', text)
        self.assertIn('process_count', text)
        result = dashboard({'status': 'interrupted', 'service_stop': stop})
        self.assertIn(stop['reason'], result)
        self.assertIn(str(self.report), result)
        self.assertNotIn('Для продолжения запустите мастер', result)


if __name__ == '__main__':
    unittest.main()
