import io
import json
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import patch

from controller import save, read, digest, Interrupted
from platform_support import FileLock
import bootstrap
import operator_questions as oq
import run_status
import task_acceptance as ta
import managed_services as ms
import test_execution_cycle as portfolio
import test_controller as fixtures


class MailboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='question space ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        save(self.root/'config.json', {'requirements_sha256': 'pinned'})
        save(self.root/'plan.json', {'tasks': []})
        save(self.root/'state.json', {'status': 'running', 'tasks': {}})

    def test_concurrent_models_share_question_and_second_terminal_avoids_run_lock(self):
        with FileLock(self.root/'run.lock'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                keys = list(pool.map(lambda model: oq.request(self.root, 'frontend', 'browser',
                    'Как снять страницу?', details=model, model=model), ['one', 'two']))
            self.assertEqual(keys[0], keys[1])
            self.assertEqual(len(oq.questions(self.root)[0]['requests']), 2)
            oq.answer(self.root, keys[0], 'Снимок сделаю вручную', 'Корпоративная политика')
            self.assertEqual(oq.response(self.root, keys[0])['text'], 'Снимок сделаю вручную')
            self.assertEqual(oq.questions(self.root, pending_only=True), [])
            with self.assertRaises(ValueError):
                oq.answer(self.root, keys[0], 'overwrite')
        self.assertEqual(read(self.root/'state.json')['status'], 'running')

    def test_stale_answer_cannot_cross_plan_revision(self):
        key = oq.request(self.root, 'task', 'topic', 'Вопрос')
        save(self.root/'plan.json', {'tasks': [], 'different': True})
        with self.assertRaises(ValueError):
            oq.answer(self.root, key, 'yes')
        self.assertEqual(oq.questions(self.root), [])

    def test_full_command_and_bootstrap_answer_while_locked(self):
        key = oq.request(self.root, 'task', 'topic', 'Вопрос', options=['Да', 'Нет'])
        argv = shlex.split(oq.command(self.root))
        if argv[0] == '&':  # PowerShell invocation prefix on Windows.
            argv = argv[1:]
        self.assertTrue(Path(argv[1]).is_absolute())
        self.assertEqual(argv[2:], ['--answers-config', str(self.root/'config.json')])
        inputs = iter(['1', 'Пояснение', 'да'])
        with FileLock(self.root/'run.lock'), patch('builtins.input', side_effect=lambda _: next(inputs)), \
             patch('bootstrap.Runner', side_effect=AssertionError('No controller')), redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main(argv[2:]), 0)
        self.assertEqual(oq.response(self.root, key)['text'], 'Да')

    def test_question_protocol_accepts_fences_not_incidental_prose(self):
        self.assertIsNotNone(oq.control_result('```json\n{"operator_question":{"question":"Что выбрать?"}}\n```'))
        self.assertIsNone(oq.control_result('Here is a question?'))


class ContributionTests(unittest.TestCase):
    def test_waiting_model_is_not_displayed_as_active_development(self):
        label = run_status.candidate_label({'model': 'model', 'stage': 'execute', 'status': 'waiting-user'})
        self.assertIn('нужен ответ', label)
        self.assertIn('разработка', label)

    def test_downstream_proof_is_required_but_not_an_implementation_prerequisite(self):
        task = {'id': 'frontend', 'requirements': ['REQ-LIVE'], 'depends_on': []}
        plan = {'tasks': [task, {'id': 'stand', 'depends_on': ['frontend']},
                          {'id': 'e2e', 'depends_on': ['stand']}]}
        proof = {'requirement': 'REQ-LIVE', 'owner': 'e2e', 'evidence': 'Live page screenshot',
                 'reason': 'Pinned plan assigns capture after stand startup'}
        self.assertEqual(ta.validate_deferred(plan, task, [proof]), [proof])
        for owner in ('frontend', 'missing'):
            with self.assertRaises(ValueError):
                ta.validate_deferred(plan, task, [{**proof, 'owner': owner}])
        with self.assertRaises(ValueError):
            ta.validate_deferred(plan, {'id': 'integration', 'requirements': ['REQ-LIVE']}, [proof])

    def test_legacy_missing_requirement_is_carried_to_named_owner_without_changing_plan(self):
        from types import SimpleNamespace
        task = {'id': 'e2e', 'prompt': 'Live test', 'requirements': ['REQ-OTHER']}
        runner = SimpleNamespace(state={'tasks': {'frontend': {'status': 'done', 'review': {
            'deferred_evidence': [{'owner': 'e2e', 'requirement': 'REQ-LIVE', 'evidence': 'creation', 'reason': 'assigned'}]}}}})
        enriched = ta.with_incoming(runner, task)
        self.assertIn('REQ-LIVE', enriched['requirements'])
        self.assertEqual(task['requirements'], ['REQ-OTHER'])


class CandidateQuestionsTests(unittest.TestCase):
    setUp = portfolio.ExecutionPortfolioTests.setUp
    result = portfolio.ExecutionPortfolioTests.result
    session = portfolio.ExecutionPortfolioTests.session
    checks = portfolio.ExecutionPortfolioTests.checks
    review = portfolio.ExecutionPortfolioTests.review
    run_task = portfolio.ExecutionPortfolioTests.run_task
    records = portfolio.ExecutionPortfolioTests.records

    def test_question_preserves_files_budget_and_resumes_with_answer(self):
        self.primary_only = True
        def ask(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.session(root, cwd, prompt, role, name, model, stop_event)
            result['text'] = json.dumps({'operator_question': {'topic': 'name', 'question': 'Какое имя?'}})
            return result
        with self.assertRaises(oq.WaitingForOperator) as caught:
            self.run_task(session=ask)
        value = next(v for v in self.records() if v['model'] == 'DeepSeek-V4.1-Flash-1017')
        self.assertEqual(value['failures'], 0)
        self.assertEqual(self.check_count, 0)
        self.assertEqual((Path(value['workspace'])/'backend/value.txt').read_text(), '2')
        oq.answer(self.runner.root, caught.exception.question_ids[0], 'Учебная проверка')
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['failures'], 0)
        self.assertIn('Учебная проверка', self.calls[-1][2])

    def test_verified_peer_can_finish_without_unnecessary_human_answer(self):
        def ask_one(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.session(root, cwd, prompt, role, name, model, stop_event)
            if model == 'DeepSeek-V4.1-Flash-1017':
                result['text'] = json.dumps({'operator_question': {'topic': 'name', 'question': 'Какое имя?'}})
            return result
        accepted = self.run_task(session=ask_one)
        self.assertEqual(accepted['model'], 'GLM-5.3-Flash')
        oq.resolve_task(self.runner.root, self.task['id'])
        self.assertFalse(oq.questions(self.runner.root, pending_only=True))

    def test_answer_resumes_candidate_while_peer_is_still_running(self):
        import time
        def execute(root, cwd, prompt, role, name, model=None, stop_event=None):
            result = self.session(root, cwd, prompt, role, name, model, stop_event)
            if model == 'DeepSeek-V4.1-Flash-1017':
                if 'Учебная проверка' not in prompt:
                    result['text'] = json.dumps({'operator_question': {'topic': 'name', 'question': 'Какое имя?'}})
                return result
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pending = oq.questions(self.runner.root, pending_only=True)
                if pending:
                    oq.answer(self.runner.root, pending[0]['id'], 'Учебная проверка')
                    break
                stop_event.wait(.02)
            self.assertTrue(stop_event.wait(5), 'answered candidate did not resume while peer was running')
            raise Interrupted('peer comparison completed')
        self.assertEqual(self.run_task(session=execute)['model'], 'DeepSeek-V4.1-Flash-1017')

    def test_legacy_external_blocker_rechecks_review_without_reimplementation(self):
        self.primary_only = True
        self.run_task()
        path = next(p for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')
                    if read(p)['status'] == 'accepted')
        value = read(path)
        value.update(status='external-blocker', stage='adjudicate', blocker={'findings': ['missing later screenshot']})
        value.pop('acceptance_protocol')
        save(path, value)
        self.calls.clear()
        self.check_count = 0
        accepted = self.run_task()
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.check_count, 0)
        self.assertEqual(len(accepted['prior_blockers']), 1)

    def test_external_answer_can_prepare_evidence_before_rechecking(self):
        self.primary_only = True
        self.run_task()
        path = next(p for p in (self.runner.root/'execution-cycles').glob('*/*/candidate.json')
                    if read(p)['status'] == 'accepted')
        key = oq.request(self.runner.root, self.task['id'], 'external-evidence', 'Как подготовить стенд?')
        value = read(path)
        value.update(status='waiting-user', stage='adjudicate', question_id=key, blocked_on_evidence=True,
                     blocker={'findings': ['Live evidence missing']})
        save(path, value)
        oq.answer(self.runner.root, key, 'Запросите управляемый сервис')
        self.calls.clear()
        self.assertEqual(self.run_task()['status'], 'accepted')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('Resolve only the missing external evidence', self.calls[0][2])


class SchedulerQuestionTests(unittest.TestCase):
    setUp = fixtures.ControllerE2E.setUp
    task = fixtures.ControllerE2E.task

    def test_waiting_task_releases_worker_and_independent_task_unblocks_it(self):
        runner = self.runner
        runner.cfg['jobs'] = 1
        save(runner.root/'plan.json', self.plan)
        runner.state.update(plan_sha256=digest(runner.root/'plan.json'),
                            tasks={t['id']: {'status': 'pending'} for t in self.plan['tasks']})
        calls = []
        def worker(task, record):
            calls.append(task['id'])
            if task['id'] == 'be' and calls.count('be') == 1:
                key = oq.request(runner.root, 'be', 'topic', 'Нужны данные')
                raise oq.WaitingForOperator('Нужны данные', [key])
            if task['id'] == 'fe':
                q = oq.questions(runner.root, pending_only=True)[0]
                oq.answer(runner.root, q['id'], 'Данные готовы')
            if task['id'] == 'integration':
                raise Interrupted('test completed')
            return {**record, 'status': 'accepted'}
        def integrate(task, record):
            record['status'] = 'done'
        with patch.object(runner, 'require_preflight'), patch.object(runner, 'worker', side_effect=worker), \
             patch.object(runner, 'integrate', side_effect=integrate), patch.object(runner, 'progress'):
            with self.assertRaises(Interrupted):
                runner.run()
        self.assertEqual(calls, ['be', 'fe', 'be', 'integration'])
        self.assertTrue(all(r['status'] == 'done' for r in runner.state['tasks'].values()))
        summary = run_status.snapshot(runner.root, runner.state)
        self.assertEqual(summary['done'], 2)
        self.assertFalse(summary['ready'])


class ManagedServicesTests(unittest.TestCase):
    setUp = fixtures.ControllerE2E.setUp
    task = fixtures.ControllerE2E.task

    def spec(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        return {'id': 'preview', 'cwd': '.', 'ports': [port],
                'argv': [sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'],
                'health': {'url': 'http://127.0.0.1:' + str(port), 'status': 200},
                'artifacts': ['backend/value.txt'], 'reason': 'Isolated test HTTP server'}

    def prepared(self, spec):
        with patch.object(self.runner, 'audit'):
            value = self.runner.services.prepare(spec, self.runner.main, 'be')
        oq.answer(self.runner.root, value['question_id'], 'Разрешить этот запуск')
        return value

    def owned_inventory(self):
        return {'complete': True, 'services': [{'pid': proc.pid, 'started': 'test-instance',
            'ports': read(self.runner.services.folder / identity / 'service.json')['spec']['ports']}
            for identity, proc in self.runner.services.active.items()]}

    def test_local_service_health_reuse_stop_and_restart_preserve_frozen_artifact(self):
        inventory = patch('managed_services.inspect_services', side_effect=self.owned_inventory)
        inventory.start()
        self.addCleanup(inventory.stop)
        spec = self.spec()
        value = self.prepared(spec)
        self.addCleanup(self.runner.services.close)
        started = self.runner.services.ensure(value['identity'], self.runner.stop)
        self.assertEqual(started['status'], 'ready')
        self.assertEqual(self.runner.services.ensure(value['identity'], self.runner.stop)['pid'], started['pid'])
        restarted = self.runner.services.restart('preview', self.runner.stop)
        self.assertEqual(restarted['files'], started['files'])
        self.runner.services.close()
        self.assertFalse(ms.port_open(spec['ports'][0]))
        self.runner.services.restore(self.runner.stop)
        self.assertTrue(ms.healthy(spec))
        (Path(value['workspace'])/'backend/value.txt').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'artifacts changed'):
            self.runner.services.ensure(value['identity'], self.runner.stop)

    def test_no_launch_without_approval_and_no_takeover_of_busy_port(self):
        spec = self.spec()
        with patch.object(self.runner, 'audit'):
            value = self.runner.services.prepare(spec, self.runner.main, 'be')
        with self.assertRaises(oq.WaitingForOperator):
            self.runner.services.ensure(value['identity'], self.runner.stop)
        oq.answer(self.runner.root, value['question_id'], 'Разрешить этот запуск')
        with socket.socket() as server:
            server.bind(('127.0.0.1', spec['ports'][0]))
            server.listen()
            with self.assertRaisesRegex(RuntimeError, 'occupied'):
                self.runner.services.ensure(value['identity'], self.runner.stop)
            self.assertTrue(ms.port_open(spec['ports'][0]))
        self.assertFalse(self.runner.services.active)

    def test_healthy_unowned_listener_is_not_accepted(self):
        spec = self.spec()
        value = self.prepared(spec)
        self.runner.cfg['service_startup_timeout'] = .6
        self.addCleanup(self.runner.services.close)
        with patch('managed_services.inspect_services', return_value={'complete': True, 'services': []}):
            with self.assertRaisesRegex(RuntimeError, 'ready'):
                self.runner.services.ensure(value['identity'], self.runner.stop)
        self.assertFalse(ms.port_open(spec['ports'][0]))

    def test_changed_launch_command_cannot_reuse_approval(self):
        value = self.prepared(self.spec())
        path = self.runner.services.folder / value['identity'] / 'service.json'
        value['spec']['argv'].append('--different')
        save(path, value)
        with self.assertRaisesRegex(ValueError, 'definition'):
            self.runner.services.ensure(value['identity'], self.runner.stop)

    def test_released_service_is_not_restored_and_replacement_keeps_old_artifacts(self):
        spec = self.spec()
        value = self.prepared(spec)
        self.addCleanup(self.runner.services.close)
        with patch('managed_services.inspect_services', side_effect=self.owned_inventory):
            self.runner.services.ensure(value['identity'], self.runner.stop)
            self.runner.services.release('preview')
            self.runner.services.restore(self.runner.stop)
            self.assertFalse(ms.port_open(spec['ports'][0]))
            self.runner.services.restart('preview', self.runner.stop)
            self.runner.services.release('preview')
            (self.runner.main/'backend/value.txt').write_text('new version')
            replacement = self.prepared(spec)
            self.assertNotEqual(replacement['identity'], value['identity'])
            self.runner.services.ensure(replacement['identity'], self.runner.stop)
            self.assertTrue(ms.port_open(spec['ports'][0]))
            self.assertEqual((Path(value['workspace'])/'backend/value.txt').read_text(), '0')

    def test_health_cannot_reach_external_host_or_undeclared_port(self):
        spec = self.spec()
        for url in ('http://example.com:8080', 'http://127.0.0.1:1', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                ms.validate({**spec, 'health': {'url': url}}, self.runner.main)
