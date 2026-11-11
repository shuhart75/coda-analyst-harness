from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import test_trackerctl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_workflow import compact_issue, collection_diagnostics


class TrackerContinuationTests(unittest.TestCase):
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    configure = test_trackerctl.DirectTrackerWorkflowTests.configure
    write = test_trackerctl.DirectTrackerWorkflowTests.write

    def start(self, state, intent='update-planning', kind='epic'):
        self.configure(state)
        return self.run_tool(state, 'begin', '--scope-kind', kind, '--scope-id', 'TEST-1',
                             '--label', 'Test', '--scope-source', 'analyst', '--intent', intent)

    def ingest(self, state, run, cards, expected=0):
        step = run['next_action']
        response = self.write(state / 'response.json', cards)
        call = self.write(state / 'call.json', {
            'provider': 'sbertrek', 'tool': 'issue.exportJson',
            'arguments': {'query': step['selection']['query_semantics'], 'fields': ['key', 'attributes']},
            'selection': step['selection']['query_semantics'], 'operation': 'read-only',
            'capability_source': 'test tool schema', 'captured_at': '2026-01-01T00:00:00+00:00',
        })
        return self.run_tool(state, 'ingest', '--run-id', run['run_id'], '--step-id', step['step_id'],
                             '--response-file', str(response), '--response-source', 'inline-json-capture',
                             '--call-file', str(call), expected=expected)

    def card(self):
        return {'key': 'TEST-2', 'summary': '[BE] Test', 'suit': 'story', 'status': 'created',
                'assignee': None, 'created_at': '2026-01-01', 'updated_at': '2026-01-01'}

    def test_missing_projection_fails_then_retries_same_step_without_losing_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            run = self.start(state)
            error = self.ingest(state, run, [self.card()], expected=2)
            self.assertIn('estimate-projection-not-returned', str(error))
            status = self.run_tool(state, 'run-status', '--run-id', run['run_id'])
            self.assertEqual(status['next_action']['step_id'], run['next_action']['step_id'])
            self.assertEqual(status['status'], 'tracker-read-failed')
            self.ingest(state, run, [{**self.card(), 'attributes': [
                {'code': 'estimation_for_dev_back', 'value': 4}]}])
            self.run_tool(state, 'reconcile', '--run-id', run['run_id'])
            result = json.loads((state / 'tracker-runs' / run['run_id'] / 'reconciled.json').read_text())
            self.assertEqual(result['counts']['issues'], 1)
            self.assertEqual(result['issues'][0]['role_estimates']['BE']['value'], 4)

    def test_pending_actualization_continues_even_when_created_and_without_estimates(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            run = self.start(state)
            self.ingest(state, run, [{**self.card(), 'attributes': []}])
            self.run_tool(state, 'reconcile', '--run-id', run['run_id'])
            root = state / 'tracker-runs' / run['run_id']
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result = self.run_tool(state, 'result-status', '--run-id', run['run_id'])
            self.assertEqual(result['counts']['work_items'], 0)
            self.assertTrue(result['collection_complete'])
            self.assertTrue(result['report_allowed'])
            self.assertFalse(result['workflow_complete'])
            self.assertFalse(result['final_response_allowed'])
            self.assertTrue(result['response_contract']['must_continue'])
            self.assertEqual(result['response_contract']['delivery'], 'progress')
            self.assertEqual(result['next_action']['type'], 'execution-preview')
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_readonly_keeps_report_and_diagnoses_missing_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            run = self.start(state, 'read-only')
            self.ingest(state, run, [self.card()])
            self.run_tool(state, 'reconcile', '--run-id', run['run_id'])
            result = self.run_tool(state, 'result-status', '--run-id', run['run_id'])
            self.assertTrue(result['workflow_complete'])
            self.assertTrue(result['final_response_allowed'])
            self.assertFalse(result['response_contract']['must_continue'])
            self.assertIsNone(result['next_action'])
            self.assertEqual(result['collection_diagnostics']['estimate_fields_not_returned'], ['TEST-2'])

    def test_paused_and_applied_runs_do_not_resume(self):
        for applied in (False, True):
            with self.subTest(applied=applied), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                run = self.start(state)
                self.ingest(state, run, [{**self.card(), 'attributes': []}])
                self.run_tool(state, 'reconcile', '--run-id', run['run_id'])
                if applied:
                    self.write(state / 'collaboration.json', {'completed_execution_runs': {
                        run['run_id']: {'state': 'applied', 'evidence': 'collaboration-finish'}}})
                else:
                    self.run_tool(state, 'application-state', '--run-id', run['run_id'],
                                  '--state', 'paused', '--reason', 'Analyst pause', '--analyst-confirmed')
                result = self.run_tool(state, 'result-status', '--run-id', run['run_id'])
                self.assertFalse(result['response_contract']['must_continue'])
                self.assertIsNone(result['next_action'])
                self.assertEqual(result['workflow_complete'], applied)
                status = self.run_tool(state, 'run-status', '--run-id', run['run_id'])
                for field in ('workflow_complete', 'final_response_allowed', 'next_action', 'response_contract'):
                    self.assertEqual(status[field], result[field])

    def test_all_status_surfaces_agree_and_legacy_completion_is_not_rewritten(self):
        for intent in ('read-only', 'update-planning'):
            with self.subTest(intent=intent), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                run = self.start(state, intent, kind='release')
                self.ingest(state, run, [{**self.card(), 'attributes': [], 'releases': ['TEST-1']}])
                reconciled = self.run_tool(state, 'reconcile', '--run-id', run['run_id'])
                root = state / 'tracker-runs' / run['run_id']
                for legacy in (False, True):
                    if legacy:
                        path = root / 'completion-status.json'
                        value = json.loads(path.read_text())
                        value.pop('completion_contract_version')
                        value.pop('collection_complete')
                        value.update(workflow_complete=True, final_response_allowed=True)
                        value['response_contract'] = {'type': 'emit-verbatim',
                            'text': value['response_contract']['text'], 'additional_commentary_allowed': False}
                        self.write(path, value)
                    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    for command in ('run-status', 'result-status'):
                        status = self.run_tool(state, command, '--run-id', run['run_id'])
                        for field in ('workflow_complete', 'collection_complete', 'final_response_allowed',
                                      'response_contract', 'next_action'):
                            self.assertEqual(status[field], reconciled[field], (legacy, command, field))
                        self.assertEqual(status['workflow_complete'], intent == 'read-only')
                    self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_flat_estimates_and_null_are_distinct_from_missing(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                fields = {**self.card(), 'estimation_for_analyze': None,
                          'estimation_for_dev_back': 3, 'estimation_for_dev_front': 0, 'estimation_qa': 2}
                card = compact_issue({'key': 'TEST-2', 'fields': fields} if nested else fields, 'sbertrek')
                self.assertEqual(card['role_estimates']['BE']['value'], 3)
                self.assertEqual(card['role_estimates']['QA']['value'], 2)
                self.assertEqual(card['role_estimates']['FE']['value'], 0)
                self.assertEqual(card['role_estimate_observations']['AN'], 'absent')
                self.assertFalse(collection_diagnostics({'cards': {'sbertrek': [card]}})['projection_review_required'])
        empty = compact_issue({**self.card(), 'story_points': None}, 'sbertrek')
        self.assertEqual(empty['field_observations']['estimate'], 'absent')
        self.assertEqual(compact_issue(self.card(), 'sbertrek')['field_observations']['estimate'], 'not-returned')


if __name__ == '__main__':
    unittest.main()
