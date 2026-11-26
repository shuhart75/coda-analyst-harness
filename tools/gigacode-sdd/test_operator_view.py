import copy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from operator_attention import project_attention
import operator_view


class AttentionTests(unittest.TestCase):
    def test_proven_exit_does_not_become_missing_process(self):
        result = project_attention({'outcome': 'finished_unverified', 'liveness': {'verdict': 'exited'}})
        self.assertEqual(result, {'categories': [], 'requires_action': False})

    def test_missing_proof_is_distinct_from_stale_observation(self):
        stale = project_attention({'liveness': {'verdict': 'unverifiable', 'reason': 'stale_status'}})
        missing = project_attention({'liveness': {'verdict': 'unverifiable', 'reason': 'missing_status'}})
        self.assertEqual(stale['categories'], ['stale'])
        self.assertFalse(stale['requires_action'])
        self.assertEqual(missing['categories'], ['unverifiable'])
        self.assertTrue(missing['requires_action'])

    def test_completed_result_can_still_need_a_human_answer(self):
        result = project_attention({'is_root': True, 'outcome': 'succeeded', 'pending_input': True})
        self.assertEqual(result['categories'], ['input', 'root_completion'])
        self.assertTrue(result['requires_action'])

    def test_unsupervised_settlement_is_not_a_missing_worker(self):
        result = project_attention({'outcome': 'succeeded',
                                    'liveness': {'verdict': 'unverifiable', 'reason': 'unsupervised_settled'}})
        self.assertFalse(result['requires_action'])

    def test_future_liveness_state_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            project_attention({'liveness': {'verdict': 'probably-dead'}})


class OperatorViewTests(unittest.TestCase):
    def value(self):
        return {'status': 'running', 'ready': False, 'integration': None,
                'tasks': [{'id': 'prepare', 'status': 'done'},
                          {'id': 'check', 'status': 'running', 'depends_on': ['prepare'],
                           'candidates': [{'model': 'model-a', 'status': 'accepted', 'stage': 'accepted'}]}],
                'questions': [], 'operations': [{'task': 'check', 'model': 'model-b', 'role': 'review'}]}

    def test_web_preserves_final_blocker_and_session_event_details(self):
        value = self.value()
        value['integration'] = 'blocked'
        value['operations'][0]['activity'] = {'pending_actions': ['Read: frontend/page.tsx']}
        result = operator_view.project(value)
        from run_status import health_summary
        self.assertEqual(result['summary'], health_summary(value))
        self.assertIn('Есть блокировка', result['summary'])
        self.assertTrue(result['run']['attention']['requires_action'])
        self.assertEqual(result['execution']['operations'][0]['activity']['pending_actions'], ['Read: frontend/page.tsx'])
        self.assertEqual(next(p for p in result['stages'] if p['id']=='integration')['status'], 'blocked')

    def test_candidate_acceptance_is_not_task_or_run_acceptance(self):
        result = operator_view.project(self.value(), observed_at=10)
        self.assertEqual(result['run']['accepted_tasks'], 1)
        self.assertFalse(result['run']['ready'])
        self.assertFalse(result['tasks'][1]['accepted'])
        self.assertEqual(result['execution']['process_exit'], 'not_observed')

    def test_active_final_retry_does_not_request_unneeded_intervention(self):
        value = self.value()
        value['integration'] = 'blocked'
        value['operations'] = [{'task_id': 'integration', 'kind': 'check', 'elapsed_seconds': 30}]
        result = operator_view.project(value)
        self.assertEqual(result['run']['integration'], 'running')
        self.assertEqual(result['run']['saved_integration'], 'blocked')
        self.assertFalse(result['run']['attention']['requires_action'])
        self.assertFalse(result['run']['ready'])
        value['telemetry_stale'] = True
        result = operator_view.project(value)
        self.assertEqual(result['run']['integration'], 'blocked')
        self.assertTrue(result['run']['attention']['requires_action'])

    def test_stopped_controller_does_not_prove_service_exit(self):
        value = self.value()
        value['status'] = 'interrupted'
        result = operator_view.project(value)
        self.assertFalse(result['execution']['operations'][0]['current'])
        self.assertEqual(result['execution']['process_exit'], 'not_observed')
        self.assertIn('interruption', result['run']['attention']['categories'])

    def test_stale_telemetry_keeps_question_visible_without_inventing_failure(self):
        value = self.value()
        value['telemetry_stale'] = True
        value['questions'] = [{'id': 'q1', 'task': 'check', 'question': 'Какой источник?', 'options': ['Отложить']}]
        result = operator_view.project(value)
        self.assertFalse(result['execution']['operations'][0]['current'])
        self.assertEqual(result['questions'][0]['id'], 'q1')
        self.assertEqual(result['tasks'][1]['attention']['categories'], ['input'])
        self.assertIn('stale', result['run']['attention']['categories'])
        self.assertNotIn('failure', result['run']['attention']['categories'])

    def test_completion_requires_final_readiness_not_all_tasks(self):
        value = self.value()
        value['status'] = 'complete'
        for task in value['tasks']:
            task['status'] = 'done'
        self.assertFalse(operator_view.project(value)['run']['ready'])
        value['ready'] = True
        self.assertEqual(operator_view.project(value)['run']['attention']['categories'], ['root_completion'])

    def test_projection_does_not_mutate_inputs_or_expose_raw_execution_payloads(self):
        value = self.value()
        value['operations'][0]['argv'] = ['PRIVATE ARGUMENT']
        value['tasks'][0]['prompt'] = 'PRIVATE PROMPT'
        original = copy.deepcopy(value)
        result = operator_view.project(value, observed_at=10)
        self.assertEqual(value, original)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(result['capabilities']['answer_questions'])

    def test_export_is_new_file_only_and_prints_only_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root/'run'
            run.mkdir()
            config = run/'config.json'
            config.write_text('{}')
            output = root/'operator.json'
            args = ['operator_view.py', '--config', str(config), '--output', str(output)]
            with patch('sys.argv', args), patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                    patch('run_status.snapshot', return_value=self.value()):
                operator_view.main()
                self.assertEqual(stdout.getvalue(), str(output)+'\n')
            saved = output.read_bytes()
            with patch('sys.argv', args), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                operator_view.main()
            self.assertEqual(output.read_bytes(), saved)
            self.assertEqual(json.loads(saved)['schema'], operator_view.SCHEMA)

    def test_export_cannot_write_into_controller_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'config.json'
            config.write_text('{}')
            args = ['operator_view.py', '--config', str(config), '--output', str(root/'state.json')]
            with patch('sys.argv', args), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                operator_view.main()
            self.assertFalse((root/'state.json').exists())


if __name__ == '__main__':
    unittest.main()
