"""Recovery display must distinguish a decision from a verified postcondition."""
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from controller import save
from console_progress import ConsoleProgress
import recovery_incidents as incidents
import run_status


class RecoveryVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        save(self.root/'state.json', {'status': 'running', 'tasks': {}})
        save(self.root/'console-status.json', {'observed_at': __import__('time').time()})
        self.identity = incidents.begin(self.root, scope='integration', task_id='integration', model='GLM',
            stage='checks', cause='checks', action='repair-implementation', occurrence='first', attempt=2,
            evidence={'failure': {'error': 'Failed check'}, 'candidate': '/internal/candidate.json'})

    def test_pending_never_claims_verified_or_hides_existing_blocker(self):
        value = run_status.snapshot(self.root)
        output = run_status.dashboard(value)
        self.assertIn('попытка 2', output)
        self.assertIn('Результат восстановления ещё не подтверждён', output)
        self.assertNotIn('/internal/candidate.json', output)
        self.assertIn('Обязательные проверки не пройдены: Failed check', output)
        value['status'] = 'blocked'
        output = run_status.dashboard(value)
        self.assertIn('Есть блокировка', output)
        self.assertIn('контроллер остановлен', output)
        self.assertNotIn('Запросов на вмешательство нет', output)

    def test_verified_recovery_does_not_accept_task_or_run(self):
        incidents.transition(self.root, self.identity, 'verified',
                             {'kind': 'checks-passed', 'task_accepted': False})
        value = run_status.snapshot(self.root)
        self.assertFalse(value['ready'])
        self.assertEqual(value['done'], 0)
        self.assertIn('восстановление подтверждено', run_status.dashboard(value))
        self.assertNotIn('Завершено. Итоговая проверка пройдена', run_status.dashboard(value))

    def test_partial_history_keeps_valid_incident_visible(self):
        (self.root/'recovery/incidents'/'broken.json').write_text('{')
        value = run_status.snapshot(self.root)
        output = run_status.dashboard(value)
        self.assertIn('История восстановления неполна', output)
        self.assertIn('попытка 2', output)
        self.assertFalse(value['telemetry_incomplete'])

    def test_live_console_refreshes_without_task_transition(self):
        display = ConsoleProgress(self.root/'logs/controller.log', stream=io.StringIO())
        display.value = run_status.snapshot(self.root)
        incidents.transition(self.root, self.identity, 'verified',
                             {'kind': 'checks-passed', 'task_accepted': False})
        current = display._value()
        self.assertEqual(current['recovery']['incidents'][0]['status'], 'verified')
        self.assertEqual(display.value['recovery']['incidents'][0]['status'], 'pending')

    def test_stale_observation_does_not_claim_current_recovery(self):
        value = run_status.snapshot(self.root)
        value['telemetry_stale'] = True
        self.assertIn('текущее выполнение не подтверждено', run_status.dashboard(value))

    def test_saved_gate_labels_are_readable(self):
        incidents.begin(self.root, scope='task', task_id='task', model=None, stage='recovery',
            cause='saved-gate', action='revalidate-saved-gate', occurrence='gate',
            evidence={'failure': {'error': 'Receipt changed'}, 'fatal': '/internal/fatal.json'})
        value = run_status.snapshot(self.root)
        output = run_status.dashboard(value)
        self.assertIn('перепроверить сохранённую блокировку', output)
        self.assertIn('Сохранённая блокировка требует повторной проверки: Receipt changed', output)
        self.assertNotIn('/internal/fatal.json', output)


if __name__ == '__main__':
    unittest.main()
