"""Reuse decisions bind one live listener, not a PID or a readiness claim."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from controller import Blocked, read, save
import inventory_recovery
import operator_questions as oq
import service_handoff as handoff


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save(self.root/'config.json', {'requirements_sha256': 'requirements'})
        self.work = self.root/'workers/candidate'
        self.work.mkdir(parents=True)
        self.runner = SimpleNamespace(root=self.root, state={}, guard=Mock())
        self.service = {'pid': 123, 'pgid': 120, 'uid': 501, 'started': 'start-time',
                        'kind': 'python', 'cwd': str(self.work), 'ports': [3002],
                        'command_sha256': 'abc', 'agent_pids': []}

    def approve(self, service=None):
        service = service or self.service
        self.assertFalse(handoff.request(self.runner, service))
        question = oq.questions(self.root, pending_only=True)[0]
        oq.answer(self.root, question['id'], handoff.ACCEPT)

    def test_approval_is_reusable_but_never_readiness_or_process_ownership(self):
        self.approve()
        self.assertTrue(handoff.accepted(self.runner, self.service))
        handoff.reconcile(self.runner, [self.service])
        with patch('service_inventory.inspect_services', return_value={'complete': True, 'services': [self.service]}):
            context = handoff.context(self.runner)
        self.assertEqual(context[0]['readiness'], 'not-verified')
        self.assertIn('externally-owned', context[0]['lifecycle'])
        self.assertFalse((self.root/'managed-services').exists())

    def test_identity_changes_require_new_decision(self):
        self.approve()
        for field, value in [('pid', 124), ('started', 'new'), ('pgid', 125), ('uid', 502),
                             ('cwd', str(self.root/'workers/other')), ('ports', [3003]),
                             ('command_sha256', 'changed')]:
            with self.subTest(field=field):
                changed = dict(self.service, **{field: value})
                self.assertFalse(handoff.accepted(self.runner, changed))

    def test_plan_changes_invalidate_approval(self):
        self.approve()
        save(self.root/'plan.json', {'tasks': ['new-plan']})
        self.assertFalse(handoff.accepted(self.runner, self.service))

    def test_all_questions_are_saved_and_exact_command_is_shown(self):
        other = dict(self.service, pid=124, ports=[8082])
        with self.assertRaises(oq.WaitingForOperator) as raised:
            handoff.reconcile(self.runner, [self.service, other])
        self.assertEqual(len(oq.questions(self.root, pending_only=True)), 2)
        self.assertIn('--answers-config', str(raised.exception))
        self.assertEqual(self.runner.state['status'], 'waiting-user')

    def test_refusal_is_not_reprompted_or_treated_as_approval(self):
        handoff.request(self.runner, self.service)
        question = oq.questions(self.root)[0]
        oq.answer(self.root, question['id'], handoff.DECLINE)
        with self.assertRaisesRegex(Blocked, 'не подтверждено'):
            handoff.reconcile(self.runner, [self.service])
        self.assertEqual(len(oq.questions(self.root)), 1)
        self.assertFalse(handoff.accepted(self.runner, self.service))

    def test_incomplete_identity_agent_or_external_workspace_is_not_adopted(self):
        for item in [dict(self.service, started=None), dict(self.service, command_sha256=None),
                     dict(self.service, agent_pids=[50]), dict(self.service, cwd=str(self.root.parent))]:
            with self.subTest(item=item), self.assertRaises(Blocked):
                handoff.request(self.runner, item)
        self.assertEqual(oq.questions(self.root), [])

    def test_windows_identity_requires_confirmed_owner(self):
        item = dict(self.service, pgid=None, uid=None, started=123456, owner_known=True, same_user=True)
        self.assertIsNotNone(handoff.identity(item))
        self.assertIsNone(handoff.identity(dict(item, same_user=False)))

    def test_complete_inventory_is_required_for_context(self):
        self.approve()
        with patch('service_inventory.inspect_services', return_value={'complete': False, 'services': [self.service]}):
            self.assertEqual(handoff.context(self.runner), [])

    def test_answer_for_another_service_cannot_be_substituted(self):
        self.approve()
        other = dict(self.service, pid=124)
        handoff.request(self.runner, other)
        records = list((self.root/'service-handoffs').glob('*.json'))
        first = next(read(p) for p in records if read(p)['identity']['pid'] == 123)
        second = next(p for p in records if read(p)['identity']['pid'] == 124)
        value = read(second)
        value['question_id'] = first['question_id']
        save(second, value)
        self.assertFalse(handoff.accepted(self.runner, other))

    def test_handoff_dashboard_keeps_answer_command_visible(self):
        from run_status import dashboard
        with self.assertRaises(oq.WaitingForOperator):
            handoff.reconcile(self.runner, [self.service])
        rendered = dashboard({'status': 'waiting-user', 'questions': oq.questions(self.root),
                              'service_stop': {'reason': 'old stop', 'report': 'old report'},
                              'answer_command': oq.command(self.root)})
        self.assertTrue(rendered.startswith('Нужно решение по работающим сервисам'))
        self.assertIn('--answers-config', rendered)

    def test_disappeared_process_retires_question_only_on_complete_inventory(self):
        handoff.request(self.runner, self.service)
        handoff.refresh(self.runner, {'complete': False, 'processes': []})
        self.assertEqual(len(oq.questions(self.root, pending_only=True)), 1)
        handoff.refresh(self.runner, {'complete': True, 'processes': [self.service]})
        self.assertEqual(len(oq.questions(self.root, pending_only=True)), 1)
        handoff.refresh(self.runner, {'complete': True, 'processes': []})
        self.assertEqual(oq.questions(self.root, pending_only=True), [])
        self.assertFalse(handoff.accepted(self.runner, self.service))
        handoff.request(self.runner, self.service)
        self.assertEqual(len(oq.questions(self.root, pending_only=True)), 1)
        self.assertEqual(len(oq.questions(self.root)), 2)
        handoff.request(self.runner, self.service)
        self.assertEqual(len(oq.questions(self.root)), 2)

    def test_new_service_fatal_can_resume_after_identity_bound_handoff(self):
        self.approve()
        report = self.root/'logs/task-execute-services-123456abcdef.json'
        save(report, {'inventory': {'complete': True}, 'new_workspace_services': [self.service]})
        folder = self.root/'execution-cycles/scope'
        fatal = {'type': 'Blocked', 'scope': 'scope',
                 'error': 'Service inspection requires attention; no automatic service shutdown. See ' + str(report)}
        save(folder/'fatal.json', fatal)
        save(self.root/'service-stop.json', {'report': str(report)})
        self.runner.check_preserved_processes = Mock()
        with patch('service_inventory.inspect_services', return_value={'complete': True}):
            self.assertTrue(inventory_recovery.recover(self.runner, folder, 'scope', {'workspace': str(self.work)}))
        self.runner.check_preserved_processes.assert_called_once()
        self.assertFalse((folder/'fatal.json').exists())
        self.assertFalse((self.root/'service-stop.json').exists())
        self.assertEqual(read(next(folder.glob('fatal-inventory-recovered-*'))), fatal)


if __name__ == '__main__':
    unittest.main()
