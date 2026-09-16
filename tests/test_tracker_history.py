from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import sys
from pathlib import Path
import unittest

import test_trackerctl
import test_tracker_execution

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_history import JIRA_MAPPING, decode_history, pointer, validate_call
from tracker_lifecycle import StatusRules, calculate_task


class AdaptiveHistoryTests(unittest.TestCase):
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    configure = test_trackerctl.DirectTrackerWorkflowTests.configure
    write = test_trackerctl.DirectTrackerWorkflowTests.write
    registry = test_trackerctl.DirectTrackerWorkflowTests.registry
    jira_issue = test_trackerctl.DirectTrackerWorkflowTests.jira_issue
    setUp = test_tracker_execution.TrackerExecutionPreviewTests.setUp
    git = test_tracker_execution.TrackerExecutionPreviewTests.git
    save_sources = test_tracker_execution.TrackerExecutionPreviewTests.save_sources
    snapshot = test_tracker_execution.TrackerExecutionPreviewTests.snapshot

    def call(self, provider, selection=''):
        return {'provider': provider, 'tool': 'installation-specific-reader-v4',
                'arguments': {'query': selection}, 'selection': selection,
                'operation': 'read-only', 'capability_source': 'installed MCP tool schema',
                'captured_at': '2026-08-10T12:00:00+03:00'}

    def begin(self, intent='update-planning'):
        return self.run_tool(self.state, 'begin', '--scope-kind', 'tasks', '--scope-provider', 'jira',
                             '--scope-id', 'JIRA-1', '--label', 'Test', '--scope-source', 'analyst',
                             '--intent', intent, '--adaptive')

    def ingest(self, run, data, call=None, expected=0):
        action = run['next_action']
        response = self.write(self.state / f"{action['step_id']}-response.json", data)
        recorded = self.write(self.state / f"{action['step_id']}-call.json", call or self.call(
            action['provider'], action['selection']['query_semantics']))
        return self.run_tool(self.state, 'ingest', '--run-id', run['run_id'], '--step-id', action['step_id'],
                             '--response-file', str(response), '--response-source', 'mcp-file',
                             '--call-file', str(recorded), expected=expected)

    def reconciled(self, intent='update-planning'):
        self.registry(self.project, 'owner', ['| CORE | JIRA-1 | - | real | BE | done |'])
        self.save_sources()
        run = self.begin(intent)
        run = self.ingest(run, {'issues': [self.jira_issue('JIRA-1', roles={'BE': 2})]})
        run = self.ingest(run, [])
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        return run['run_id']

    def raw_history(self):
        return {'key': 'JIRA-1', 'assignee': {'key': 'qa'}, 'status': {'name': 'Done'},
                'changelogs': [
                    {'created': '2026-08-01T12:00:00+03:00', 'items': [
                        {'field': 'assignee', 'to_id': 'dev'}]},
                    {'created': '2026-08-02T12:00:00+03:00', 'items': [
                        {'field': 'assignee', 'from_id': 'dev', 'to_id': 'qa'}]},
                    {'created': '2026-08-03T12:00:00+03:00', 'items': [
                        {'field': 'status', 'from_id': 'todo', 'to_id': 'done'}]},
                ]}

    def manifest(self):
        response = self.write(self.state / 'history-response.json', self.raw_history())
        return {'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'confirmed test mapping',
                'feature': 'owner', 'provider': 'jira', 'participants': {'dev': 'BE', 'qa': 'QA'},
                'status_rules': {'not_started': ['todo'], 'qa_completed': ['done']},
                'responses': [{'provider': 'jira', 'key': 'JIRA-1', 'role': 'BE',
                               'response_file': str(response), 'sha256': hashlib.sha256(response.read_bytes()).hexdigest(),
                               'call': self.call('jira'), 'status_aliases': {'Done': 'done'}}]}

    def test_flexible_tool_selection_keeps_scope_and_resumes_same_run(self):
        run = self.begin()
        self.assertEqual(run['next_action']['type'], 'collect-tracker-data')
        self.assertNotIn('tool', run['next_action'])
        bad = self.call('jira', 'unrelated scope')
        self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, bad, expected=2)
        self.assertEqual(self.begin()['run_id'], run['run_id'])
        result = self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]})
        self.assertEqual(result['next_action']['provider'], 'sbertrek')

    def test_real_response_to_bound_review_without_project_writes(self):
        run_id = self.reconciled()
        preview = self.run_tool(self.state, 'execution-preview', '--run-id', run_id,
                                '--project-root', str(self.project), '--feature', 'owner')
        self.assertEqual(preview['next_action']['type'], 'collect-history')
        manifest = self.write(self.state / 'manifest.json', self.manifest())
        before = self.snapshot()
        args = ('history-review', '--run-id', run_id, '--project-root', str(self.project), '--manifest', str(manifest))
        review = self.run_tool(self.state, *args)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(review['features'][0]['qa']['progress_percent'], 100)
        self.assertIsNone(review['features'][0]['qa']['finished_at'])
        self.assertFalse(review['planning_application_allowed'])
        self.assertEqual(self.run_tool(self.state, *args)['review_file'], review['review_file'])
        source = Path(self.manifest()['responses'][0]['response_file'])
        source.write_text('{}')
        self.run_tool(self.state, *args, expected=2)

    def test_wrong_task_unconfirmed_mapping_and_unknown_role_are_not_invented(self):
        run_id = self.reconciled()
        for change in ('confirmation', 'key', 'role'):
            manifest = self.manifest()
            if change == 'confirmation':
                manifest['analyst_confirmed'] = False
            else:
                manifest['responses'][0][change] = 'OTHER-1' if change == 'key' else 'FE'
            path = self.write(self.state / 'manifest.json', manifest)
            self.run_tool(self.state, 'history-review', '--run-id', run_id,
                          '--project-root', str(self.project), '--manifest', str(path), expected=2)

    def test_structural_mapping_and_pagination_proof_for_another_provider(self):
        source = self.raw_history()
        source['total'] = 3
        source['start'] = 0
        entry = {'key': 'JIRA-1', 'role': 'BE', 'provider': 'sbertrek',
                 'mapping': {**JIRA_MAPPING, 'total': '/total', 'start': '/start'}, 'status_aliases': {'Done': 'done'}}
        history, limits = decode_history(source, entry, datetime.fromisoformat('2026-08-10T12:00:00+03:00'))
        self.assertTrue(history.complete)
        self.assertEqual(limits, [])
        calculated = calculate_task(history, {'dev': 'BE', 'qa': 'QA'}, StatusRules(qa_completed=frozenset({'done'})))
        self.assertEqual(calculated['development']['finished_at'], '2026-08-02T12:00:00+03:00')
        bad = copy.deepcopy(entry)
        bad['mapping']['events'] = '/absent'
        with self.assertRaises(ValueError):
            decode_history(source, bad, history.observed_at)
        self.assertEqual(pointer({'text': '{"value": 3}'}, '/text/value'), 3)

    def test_read_only_run_cannot_enter_history_application(self):
        run_id = self.reconciled('read-only')
        path = self.write(self.state / 'manifest.json', self.manifest())
        result = self.run_tool(self.state, 'history-review', '--run-id', run_id,
                              '--project-root', str(self.project), '--manifest', str(path), expected=2)
        self.assertIn('Read-only', result['error'])

    def test_another_role_on_same_card_remains_in_feature_qa_denominator(self):
        run_id = self.reconciled()
        self.registry(self.project, 'owner', ['| CORE | JIRA-1 | - | real | BE | done |',
                                              '| FRONT | JIRA-1 | - | real | FE | planned |'])
        self.save_sources()
        manifest = self.write(self.state / 'manifest.json', self.manifest())
        review = self.run_tool(self.state, 'history-review', '--run-id', run_id,
                               '--project-root', str(self.project), '--manifest', str(manifest))
        self.assertIsNone(review['features'][0]['qa']['progress_percent'])
        self.assertIn('qa-task-history-missing:JIRA-1/FE', review['features'][0]['limitations'])

    def test_unknown_assignee_and_partial_metadata_do_not_prove_exact_dates(self):
        source = self.raw_history()
        entry = {'key': 'JIRA-1', 'role': 'BE', 'provider': 'jira', 'status_aliases': {'Done': 'done'},
                 'mapping': {**JIRA_MAPPING, 'total': '/total', 'start': '/start'}}
        observed = datetime.fromisoformat('2026-08-10T12:00:00+03:00')
        source.update(total=4, start=0)
        history, limits = decode_history(source, entry, observed)
        self.assertFalse(history.complete)
        source['total'] = 3
        del source['assignee']
        history, limits = decode_history(source, entry, observed)
        self.assertFalse(history.complete)
        self.assertIn('snapshot-assignee-not-returned', limits)

    def test_credentials_mutation_and_future_capture_are_rejected(self):
        for changes in ({'operation': 'write'}, {'arguments': {'headers': {'Authorization': 'secret'}}},
                        {'captured_at': '2999-01-01T00:00:00+00:00'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_call({**self.call('jira'), **changes}, 'jira')


if __name__ == '__main__':
    unittest.main()
