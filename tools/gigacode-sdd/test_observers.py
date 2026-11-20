import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import observers


def event(kind, content):
    return {'type': kind, 'message': {'content': content}}


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'run'
        self.root.mkdir()
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.activities = []
        self.runner = SimpleNamespace(
            root=self.root, cfg={'cli': ['gigacode'], 'execution_models': ['model-a', 'model-b']},
            stop=threading.Event(), env={}, progress=Mock(),
            state={'status': 'running', 'tasks': {'task': {'status': 'running'}}},
            _display=SimpleNamespace(telemetry=Mock()))

        @contextmanager
        def activity(label, **kwargs):
            self.activities.append((label, kwargs))
            yield 'activity-token'
        self.runner.activity = activity
        self.manager = observers.Manager(self.runner)

    def write_events(self, name, events):
        path = self.logs / name / 'stdout.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
        return path

    def snapshot(self):
        return {'status': 'running', 'done': 0, 'total': 1, 'questions': [], 'service_stop': {},
                'telemetry_stale': False, 'telemetry_incomplete': False, 'operations': [],
                'tasks': [{'id': 'task', 'title': 'Tests', 'status': 'running', 'waiting_for': [],
                           'deferred_evidence': [], 'candidates': [{'model': 'model-a', 'stage': 'execute'}]}]}

    def packet(self, snapshot=None, previous=None):
        (self.root / 'plan.json').write_text(json.dumps({'tasks': [
            {'id': 'task', 'prompt': 'Implement tests; no production changes', 'requirements': ['REQ-1']}]}))
        with patch('run_status.snapshot', return_value=snapshot or self.snapshot()):
            return observers.packet(self.root, previous)

    def fake_transport(self, *, tools=None, response=None, reported_model=None):
        calls = []
        lock = threading.Lock()

        def run(argv, cwd, prompt, log, **kwargs):
            requested = argv[argv.index('--model') + 1]
            profile = json.loads((Path(cwd) / '.gigacode/settings.json').read_text())
            with lock:
                calls.append({'argv': argv, 'cwd': Path(cwd), 'prompt': prompt, 'log': Path(log),
                              'kwargs': kwargs, 'profile': profile})
            Path(log).mkdir(parents=True, exist_ok=True)
            path = Path(log) / 'stdout.jsonl'
            init = {'type': 'system', 'subtype': 'init', 'model': reported_model or requested,
                    'tools': [] if tools is None else tools, 'mcp_servers': []}
            path.write_text(json.dumps(init) + '\n')
            kwargs['activity_callback']({'events_path': str(path)})
            return {'ok': True, 'events_path': str(path), 'tool_calls': [],
                    'text': response if response is not None else
                        json.dumps({'status': 'waiting', 'summary': 'Проверка выполняется.'})}
        return run, calls

    def test_public_window_is_bounded_redacted_and_omits_reasoning(self):
        events = [event('assistant', [{'type': 'text', 'text': 'old claim'}])]
        events += [event('assistant', [{'type': 'thinking', 'thinking': 'PRIVATE_THOUGHT'},
                   {'type': 'tool_use', 'id': str(i), 'name': 'read_file',
                    'input': {'command': 'curl --token credential-value', 'padding': 'x' * 900}}])
                   for i in range(30)]
        events.append(event('user', [{'type': 'tool_result', 'tool_use_id': '29',
                      'content': 'Authorization: Bearer credential-result\n' + 'y' * 2000}]))
        events.append(event('assistant', [{'type': 'text', 'text': 'Current check running.'}]))
        path = self.write_events('execute', events)
        with path.open('rb') as stream:
            data = stream.read()
        path.write_bytes(b'x' * (128 * 1024 + 10) + b'\n' + data)
        result = observers.recent_events(self.root, path)
        encoded = json.dumps(result)
        self.assertEqual(len(result['events']), 20)
        self.assertTrue(result['window_limited'])
        self.assertGreater(result['bytes_observed'], 128 * 1024)
        for hidden in ('PRIVATE_THOUGHT', 'credential-value', 'credential-result', 'old claim'):
            self.assertNotIn(hidden, encoded)
        self.assertEqual(result['events'][-1]['kind'], 'model_claim')
        for item in result['events']:
            self.assertLessEqual(len(item.get('input_excerpt', '')), 600)
            self.assertLessEqual(len(item.get('result_excerpt', '')), 1000)

    def test_packet_filters_observers_and_only_carries_one_previous_interval(self):
        path = self.write_events('execute', [event('assistant', [{'type': 'text', 'text': 'real progress'}])])
        snapshot = self.snapshot()
        snapshot['operations'] = [
            {'task_id': 'task', 'model': 'model-a', 'events_path': str(path)},
            {'purpose': 'observation', 'events_path': '/must/not/read/observer', 'model': 'model-b'}]
        first = self.packet(snapshot)
        self.assertEqual(first['delta']['accepted_since_previous'], 0)
        self.assertIsNone(first['delta']['previous_snapshot_at'])
        self.assertEqual(len(first['operations']), 1)
        self.assertEqual(len(first['recent_events']), 1)
        snapshot['done'] = 1
        snapshot['tasks'][0]['status'] = 'done'
        second = self.packet(snapshot, first)
        self.assertEqual(second['delta']['accepted_since_previous'], 1)
        self.assertEqual(second['delta']['changed_tasks'], ['task'])
        self.assertNotIn('previous', second['previous'])
        self.assertEqual(second['previous']['recent_events'], first['recent_events'])
        third = self.packet(snapshot, second)
        self.assertEqual(third['delta']['accepted_since_previous'], 0)
        self.assertEqual(third['delta']['changed_tasks'], [])
        # A new manager has no baseline from a different controller lifetime.
        self.assertIsNone(observers.Manager(self.runner).previous)
        reset = self.packet(snapshot)
        self.assertEqual(reset['delta']['accepted_since_previous'], 0)

    def test_all_tasks_accepted_does_not_erase_final_integration_gate(self):
        snapshot = self.snapshot()
        snapshot.update(done=1, integration='running', ready=False)
        snapshot['tasks'][0]['status'] = 'done'
        current = self.packet(snapshot)
        self.assertEqual(current['integration'], 'running')
        self.assertFalse(current['ready'])
        self.assertIn('Итоговая', current['integration_scope'])
        complete = copy.deepcopy(current)
        complete.update(integration='done', ready=True)
        self.assertNotEqual(observers.fingerprint(current), observers.fingerprint(complete))

    def test_packet_never_reads_workspace_paths_and_rejects_oversized_input(self):
        product = self.root.parent / 'product'
        product.mkdir()
        secret = product / 'source.py'
        secret.write_text('PRODUCT_WORKSPACE_CONTENT')
        snapshot = self.snapshot()
        snapshot['operations'] = [{'task_id': 'task', 'events_path': str(secret)}]
        value = self.packet(snapshot)
        self.assertTrue(value['recent_events'][0]['unavailable'])
        self.assertNotIn('PRODUCT_WORKSPACE_CONTENT', json.dumps(value))
        self.assertEqual(secret.read_text(), 'PRODUCT_WORKSPACE_CONTENT')
        large = self.root / 'large.json'
        large.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            observers.read_small(large)

    def test_missing_nonempty_or_mcp_registry_cannot_start_audits(self):
        for init in [
                {'type': 'system', 'subtype': 'init', 'model': 'model-a'},
                {'type': 'system', 'subtype': 'init', 'model': 'model-a', 'tools': ['read_file']},
                {'type': 'system', 'subtype': 'init', 'model': 'model-a', 'tools': [], 'mcp_servers': ['server']}]:
            with self.subTest(init=init):
                path = self.write_events('probe', [init])
                manager = observers.Manager(self.runner)
                with patch.object(manager, 'session', return_value={
                        'ok': True, 'events_path': str(path), 'tool_calls': []}) as session:
                    result = manager.evaluate('progress', 'model-a', self.packet())
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(session.call_count, 1)
                self.assertFalse(manager.probed)

    def test_two_roles_receive_same_saved_packet_with_verified_models_and_profiles(self):
        value = self.packet()
        before = copy.deepcopy(value)
        transport, calls = self.fake_transport()
        simultaneous = threading.Barrier(2)
        def run(argv, cwd, prompt, log, **kwargs):
            if 'СНИМОК:\n' in prompt:
                simultaneous.wait(timeout=5)
            return transport(argv, cwd, prompt, log, **kwargs)
        with patch.object(observers, 'run_session', side_effect=run):
            self.manager.observe(value)
        self.assertEqual(value, before)
        self.assertEqual(len(calls), 4)  # one capability probe and one assessment per model
        actual = [c for c in calls if 'СНИМОК:\n' in c['prompt']]
        self.assertEqual(len(actual), 2)
        self.assertEqual({c['argv'][-1] for c in actual}, {'model-a', 'model-b'})
        self.assertEqual([json.loads(c['prompt'].split('СНИМОК:\n')[1]) for c in actual], [value, value])
        for call in calls:
            self.assertTrue(call['cwd'].is_relative_to(self.root / 'observer/sessions'))
            self.assertEqual(call['profile']['tools']['core'], ['__sdd_observer_no_tools__'])
            self.assertEqual(call['profile']['tools']['exclude'], observers.DENY_TOOLS)
            self.assertEqual(call['profile']['mcp']['allowed'], [])
            self.assertTrue(call['profile']['disableAllHooks'])
            self.assertEqual(call['profile']['context']['fileName'], 'OBSERVER-CONTEXT.md')
            self.assertEqual(call['kwargs']['approval_mode'], 'plan')
            self.assertFalse(call['kwargs']['stop_event'].is_set())
            self.assertIsNot(call['kwargs']['stop_event'], self.runner.stop)
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'waiting')
        self.assertTrue(latest['advisory_only'])
        self.assertEqual(json.loads(Path(latest['snapshot']).read_text()), value)
        self.assertFalse(self.runner.stop.is_set())
        self.assertEqual(self.runner.state['tasks']['task']['status'], 'running')
        self.assertEqual(len(self.activities), 4)
        self.assertTrue(all(a[1]['metadata']['purpose'] == 'observation' for a in self.activities))

    def test_wrong_reported_model_requests_next_round_without_changing_acceptance(self):
        transport, calls = self.fake_transport(reported_model='unexpected-model')
        state = copy.deepcopy(self.runner.state)
        with patch.object(observers, 'run_session', side_effect=transport):
            self.manager.observe(self.packet())
        self.assertEqual(len(calls), 2)  # no assessment after either failed capability probe
        self.assertTrue(self.manager.retry_pending)
        self.assertEqual(self.runner.state, state)
        self.assertFalse(self.runner.stop.is_set())
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'unavailable')

    def test_unstructured_answer_is_kept_without_retry_or_main_state_change(self):
        prose = '**Проверки ещё идут.** Небольшая опечатка не блокирует выполнение.'
        transport, calls = self.fake_transport(response=prose)
        state = copy.deepcopy(self.runner.state)
        with patch.object(observers, 'run_session', side_effect=transport):
            self.manager.observe(self.packet())
        self.assertEqual(len(calls), 4)
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'unknown')
        self.assertTrue(all(r['details'] == prose for r in latest['results'].values()))
        self.assertEqual(self.runner.state, state)
        self.assertFalse(self.runner.stop.is_set())
        self.assertFalse(self.manager.retry_pending)

    def test_one_observer_exception_keeps_peer_and_requests_next_round(self):
        state = copy.deepcopy(self.runner.state)
        def evaluate(role, model, value):
            if role == 'scope':
                raise OSError('unavailable CLI')
            return {'status': 'waiting', 'summary': 'Tests running'}
        with patch.object(self.manager, 'evaluate', side_effect=evaluate) as run:
            self.manager.observe(self.packet())
        self.assertEqual(run.call_count, 2)
        self.assertTrue(self.manager.retry_pending)
        self.assertEqual(self.runner.state, state)
        self.assertFalse(self.runner.stop.is_set())
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['results']['progress']['status'], 'waiting')
        self.assertEqual(latest['results']['scope']['status'], 'unavailable')

    def test_profile_write_failure_prevents_cli_launch(self):
        with patch.object(observers, 'write_projection', return_value=False), \
                patch.object(observers, 'run_session') as run:
            with self.assertRaisesRegex(OSError, 'profile'):
                self.manager.session('progress', 'model-a', 'test')
        run.assert_not_called()

    def test_timeout_keeps_successful_peer_and_next_round_uses_new_snapshot(self):
        for failed_role in ('progress', 'scope'):
            with self.subTest(failed_role=failed_role):
                manager = observers.Manager(self.runner)
                transport, calls = self.fake_transport()
                def run(argv, cwd, prompt, log, **kwargs):
                    result = transport(argv, cwd, prompt, log, **kwargs)
                    if 'СНИМОК:\n' in prompt and ('observer-'+failed_role+'-') in Path(log).name:
                        result.update(ok=False, timed_out=True, terminal_result=False,
                                      errors=['Missing terminal root result event'])
                    return result
                first = self.packet()
                with patch.object(observers, 'run_session', side_effect=run):
                    manager.observe(first)
                failed = json.loads((manager.root/'latest.json').read_text())
                self.assertTrue(failed['partial'])
                self.assertEqual(failed['status'], 'waiting')
                self.assertEqual(set(failed['audit_errors']), {failed_role})
                self.assertTrue(failed['audit_errors'][failed_role]['timed_out'])
                self.assertTrue(manager.retry_pending)
                second = copy.deepcopy(first)
                second['snapshot_at'] += 180
                second['done'] = 1
                with patch.object(observers, 'run_session', side_effect=transport):
                    manager.observe(second)
                latest = json.loads((manager.root/'latest.json').read_text())
                self.assertFalse(manager.retry_pending)
                self.assertFalse(latest['partial'])
                self.assertEqual(latest['audit_errors'], {})
                assessments = [c for c in calls if 'СНИМОК:\n' in c['prompt']]
                self.assertEqual(len(assessments), 4)
                self.assertEqual(len({str(c['cwd']) for c in calls}), len(calls))
                for call in assessments[-2:]:
                    self.assertEqual(json.loads(call['prompt'].split('СНИМОК:\n')[1]), second)
                self.assertFalse(self.runner.stop.is_set())

    def test_both_failures_retry_next_assignment_without_stopping_main(self):
        with patch.object(self.manager, 'evaluate', side_effect=OSError('temporary outage')):
            self.manager.observe(self.packet())
        latest = json.loads((self.manager.root/'latest.json').read_text())
        self.assertEqual(latest['status'], 'unavailable')
        self.assertEqual(len(latest['audit_errors']), 2)
        self.assertTrue(self.manager.retry_pending)
        with patch.object(self.manager, 'evaluate', return_value={'status': 'progress', 'summary': 'Checks passed'}) as run:
            self.manager.observe(self.packet())
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.manager.retry_pending)
        self.assertFalse(self.runner.stop.is_set())

    def test_uncertain_cleanup_preserves_ledger_but_not_a_global_observer_stop(self):
        transport, calls = self.fake_transport()
        def uncertain(*args, **kwargs):
            result = transport(*args, **kwargs)
            result['cleanup'] = {'preserved': True, 'process_group': 1234}
            return result
        with patch.object(observers, 'run_session', side_effect=uncertain):
            self.manager.observe(self.packet())
        self.assertTrue((self.root/'preserved-processes/1234.json').is_file())
        self.assertTrue(self.manager.retry_pending)
        with patch.object(observers, 'run_session', side_effect=transport):
            self.manager.observe(self.packet())
        self.assertFalse(self.manager.retry_pending)
        self.assertTrue((self.root/'preserved-processes/1234.json').is_file())
        self.assertFalse(self.runner.stop.is_set())

    def test_scheduler_retries_failure_with_fresh_packet_even_when_fingerprint_is_same(self):
        now = [1000]
        waits = []
        snapshots = []
        class Stop:
            def is_set(inner): return False
            def wait(inner, seconds):
                waits.append(seconds)
                now[0] += seconds
                return len(snapshots) >= 2
        self.manager.stop = Stop()
        def observe(value):
            snapshots.append(value)
            if len(snapshots) == 1:
                raise OSError('Snapshot/report failure')
        def packet(*args): return {'status': 'running', 'snapshot_at': now[0]}
        with patch.object(observers.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(observers, 'packet', side_effect=packet), \
                patch.object(observers, 'fingerprint', return_value='unchanged'), \
                patch.object(self.manager, 'observe', side_effect=observe):
            self.manager.loop()
        self.assertEqual(snapshots[1]['snapshot_at']-snapshots[0]['snapshot_at'], 180)
        self.assertTrue(list((self.manager.root/'reports').glob('error-*.json')))
        self.assertFalse(self.runner.stop.is_set())

    def test_partial_status_shows_surviving_scope_auditor_and_error(self):
        from run_status import observation_lines
        lines = observation_lines({'status': 'running', 'observation': {
            'status': 'concern', 'partial': True, 'retry_pending': True,
            'results': {'progress': {'status': 'unavailable', 'summary': 'ошибка сессии'},
                        'scope': {'status': 'concern', 'summary': 'Repeated failure without new checks'}},
            'audit_errors': {'progress': {'model': 'DeepSeek-V4.1-Flash-1017',
                                         'summary': 'превышено время ожидания'}}}})
        text = '\n'.join(lines)
        self.assertIn('Наблюдение частичное', text)
        self.assertIn('Repeated failure without new checks', text)
        self.assertIn('DeepSeek: превышено время ожидания', text)
        self.assertIn('автоматически', text)

    def test_context_exit_cancels_observer_lifecycle_without_main_stop(self):
        entered = threading.Event()
        def loop():
            entered.set()
            self.manager.stop.wait(5)
        with patch.object(self.manager, 'loop', side_effect=loop):
            with self.manager:
                self.assertTrue(entered.wait(2))
        self.assertTrue(self.manager.stop.is_set())
        self.assertFalse(self.manager.thread.is_alive())
        self.assertFalse(self.runner.stop.is_set())

    def test_transport_observes_observer_cancellation_without_setting_main_stop(self):
        transport, calls = self.fake_transport()
        def cancel(argv, cwd, prompt, log, **kwargs):
            self.assertFalse(kwargs['stop_event'].is_set())
            self.manager.stop.set()
            self.assertTrue(kwargs['stop_event'].is_set())
            self.assertFalse(self.runner.stop.is_set())
            return transport(argv, cwd, prompt, log, **kwargs)
        with patch.object(observers, 'run_session', side_effect=cancel):
            self.manager.session('progress', 'model-a', 'test')
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.runner.stop.is_set())

    def test_main_cancellation_prevents_new_observer_sessions(self):
        self.runner.stop.set()
        with patch.object(observers, 'run_session') as run:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                self.manager.session('progress', 'model-a', 'test')
        run.assert_not_called()

    def test_assessment_model_is_rechecked_after_successful_probe(self):
        good_transport, calls = self.fake_transport()
        def run(argv, cwd, prompt, log, **kwargs):
            result = good_transport(argv, cwd, prompt, log, **kwargs)
            if 'СНИМОК:\n' in prompt:
                path = Path(result['events_path'])
                init = json.loads(path.read_text())
                init['model'] = 'unexpected-model'
                path.write_text(json.dumps(init) + '\n')
            return result
        with patch.object(observers, 'run_session', side_effect=run):
            self.manager.observe(self.packet())
        self.assertEqual(len(calls), 4)
        self.assertTrue(self.manager.retry_pending)
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'unavailable')
        self.assertFalse(self.runner.stop.is_set())

    def test_disabled_option_does_not_start_thread(self):
        self.runner.cfg['observers_enabled'] = False
        with patch.object(self.manager, 'loop') as loop:
            with self.manager:
                self.assertIsNone(self.manager.thread)
        loop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
