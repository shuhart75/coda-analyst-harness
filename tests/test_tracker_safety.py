from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_tracker_history
import test_tracker_lifecycle
import test_trackerctl

moment = test_tracker_lifecycle.moment


class TrackerScopeSafetyTests(unittest.TestCase):
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    write = test_trackerctl.DirectTrackerWorkflowTests.write
    configure = test_trackerctl.DirectTrackerWorkflowTests.configure
    sber_issue = test_trackerctl.DirectTrackerWorkflowTests.sber_issue
    registry = test_trackerctl.DirectTrackerWorkflowTests.registry
    call = test_tracker_history.AdaptiveHistoryTests.call
    jira_issue = test_trackerctl.DirectTrackerWorkflowTests.jira_issue

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / 'state'
        self.project = Path(temporary.name) / 'project'
        self.configure(self.state)

    def test_scope_optional_epics_and_identity_requests(self):
        self.registry(self.project, 'owner', ['| INTERNAL | JIRA-1 | - | real | BE | done |'])
        self.write(self.project / 'features/owner/execution/tracker-scope.json',
                   {'schema_version': 1, 'epics': {'jira': ['JIRA-10', 'JIRA-20']}})
        preview = self.run_tool(self.state, 'scope-preview', '--project-root', str(self.project),
                                '--feature', 'owner', '--provider', 'sbertrek')
        self.assertEqual([item['id'] for item in preview['scope_choices']], ['tasks', 'epics', 'combined'])
        self.assertEqual(preview['features'][0]['epics']['jira'], ['JIRA-10', 'JIRA-20'])
        self.assertEqual(preview['epics']['owner'], [])
        self.assertEqual({item['source_key'] for item in preview['identity_requests']}, {'JIRA-1', 'JIRA-10', 'JIRA-20'})
        self.assertEqual(preview['scope']['ids'], [])

    def test_combined_and_multiple_epics_use_selected_provider(self):
        for provider in ('jira', 'sbertrek'):
            for kind in ('epic', 'combined'):
                with self.subTest(provider=provider, kind=kind), tempfile.TemporaryDirectory() as directory:
                    state = Path(directory)
                    self.configure(state)
                    args = ['begin', '--scope-kind', kind, '--scope-provider', provider,
                            '--scope-id', 'TEST-1', '--label', 'Test', '--scope-source', 'confirmed']
                    args += ['--scope-id', 'TEST-2'] if kind == 'epic' else ['--epic-id', 'TEST-2', '--epic-id', 'TEST-3']
                    result = self.run_tool(state, *args)
                    self.assertEqual(result['next_action']['provider'], provider)
                    query = result['next_action']['selection']['query_semantics']
                    self.assertIn('TEST-1', query)
                    self.assertIn('TEST-2', query)
                    self.assertIn(' or ', query)
                    self.assertEqual(self.run_tool(state, *args)['run_id'], result['run_id'])
                    if kind == 'combined':
                        self.run_tool(state, *args, '--epic-id', 'TEST-4', expected=2)

    def test_identity_lookup_uses_reference_not_equal_own_key(self):
        for provider, key in [('jira', 'JIRA-1'), ('sbertrek', 'SBER-7')]:
            args = ['identity-lookup', '--source-provider', provider, '--key', key]
            prepared = self.run_tool(self.state, *args)
            response = self.write(self.state / 'identity.json', [self.sber_issue('SBER-7', jira_key='JIRA-1')])
            call = self.write(self.state / 'call.json', self.call('sbertrek', prepared['query_semantics']))
            result = self.run_tool(self.state, *args, '--response-file', str(response), '--call-file', str(call))
            self.assertEqual(result['pairs'], [{'jira': 'JIRA-1', 'sbertrek': 'SBER-7'}])
            self.assertFalse(result['facts_application_allowed'])
            self.assertEqual(result['unresolved'], [])
        self.write(response, [self.sber_issue('SBER-7', jira_key='JIRA-1'), self.sber_issue('SBER-8', jira_key='JIRA-1')])
        prepared = self.run_tool(self.state, 'identity-lookup', '--source-provider', 'jira', '--key', 'JIRA-1')
        self.write(call, self.call('sbertrek', prepared['query_semantics']))
        self.run_tool(self.state, 'identity-lookup', '--source-provider', 'jira', '--key', 'JIRA-1',
                      '--response-file', str(response), '--call-file', str(call), expected=2)

    def test_combined_collection_counts_union_once_and_retries_missing_registry_task(self):
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'combined', '--scope-provider', 'jira',
                            '--scope-id', 'JIRA-1', '--epic-id', 'JIRA-10', '--epic-id', 'JIRA-20',
                            '--label', 'Test', '--scope-source', 'confirmed')
        action = run['next_action']
        issue = self.jira_issue('JIRA-2', roles={'BE': 1, 'QA': 2})
        response = self.write(self.state / 'union.json', {'issues': [issue]})
        call = self.write(self.state / 'union-call.json', self.call('jira', action['selection']['query_semantics']))
        ready = self.run_tool(self.state, 'ingest', '--run-id', run['run_id'], '--step-id', action['step_id'],
                              '--response-file', str(response), '--call-file', str(call), '--response-source', 'mcp-file')
        self.assertEqual(ready['next_action']['type'], 'verify-missing-tasks')
        self.assertEqual(ready['next_action']['keys'], ['JIRA-1'])
        retry = self.run_tool(self.state, 'retry-missing', '--run-id', run['run_id'])
        action = retry['next_action']
        self.assertEqual(action['selection']['requested_keys'], ['JIRA-1'])
        response = self.write(self.state / 'retry.json', {'issues': [self.jira_issue('JIRA-1', roles={'BE': 1})]})
        call = self.write(self.state / 'retry-call.json', self.call('jira', action['selection']['query_semantics']))
        self.run_tool(self.state, 'ingest', '--run-id', run['run_id'], '--step-id', action['step_id'],
                      '--response-file', str(response), '--call-file', str(call), '--response-source', 'mcp-file')
        result = self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        self.assertEqual(result['status'], 'tracker-read-reconciled')
        saved = json.loads((self.state / 'tracker-runs' / run['run_id'] / 'run.json').read_text())
        self.assertEqual([card['key'] for card in saved['cards']['jira']], ['JIRA-1', 'JIRA-2'])
        self.assertEqual(saved['cards']['sbertrek'], [])

    def test_preflight_blocks_main_wrong_feature_and_mode(self):
        self.project.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(self.project), *args], check=True, capture_output=True)
        git('init', '-b', 'main')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '--allow-empty', '-m', 'Initialize')
        self.write(self.state / 'workspace.json', {'roles': {'analytics': {'path': str(self.project)}}})
        state = {'mode': 'multi-user-branches', 'active_work': {'feature': 'owner', 'branch': 'main', 'status': 'active'}}
        self.write(self.state / 'collaboration.json', state)
        (self.state / 'active-mode.md').write_text('mode: execution-update\n')
        args = ('application-preflight', '--project-root', str(self.project), '--feature', 'owner')
        self.run_tool(self.state, *args, expected=2)
        git('switch', '-c', 'feature/owner/test')
        state['active_work']['branch'] = 'feature/owner/test'
        self.write(self.state / 'collaboration.json', state)
        self.assertEqual(self.run_tool(self.state, *args)['status'], 'tracker-application-preflight-ready')
        self.run_tool(self.state, *args[:-1], 'other', expected=2)
        (self.state / 'active-mode.md').write_text('mode: requirements\n')
        self.run_tool(self.state, *args, expected=2)

    def test_original_history_cannot_be_replaced_by_recomputed_checksum(self):
        from tracker_history import pin_history_response
        path = self.state / 'raw.json'
        with patch.dict(os.environ, {'ANALYST_HARNESS_STATE_ROOT': str(self.state)}):
            provenance = {'provider': 'jira', 'key': 'JIRA-1', 'call': self.call('jira')}
            pin_history_response('20260918T054027Z-fb23b050', path, b'original', provenance)
            pin_history_response('20260918T054027Z-fb23b050', path, b'original')
            with self.assertRaisesRegex(ValueError, 'raw history changed'):
                pin_history_response('20260918T054027Z-fb23b050', path, b'edited')
            with self.assertRaisesRegex(ValueError, 'raw history changed'):
                pin_history_response('20260918T054027Z-fb23b050', self.state / 'cleaned.json', b'edited', provenance)

    def test_qa_decision_preserves_unapproved_fields_and_checks_estimate(self):
        from tracker_qa_application import confirmed_qa_updates
        source = self.state / 'decision.txt'
        source.write_text('Accept QA completion and estimate 5; preserve start')
        comparison = {'rows': [{'feature': 'owner', 'task_id': 'QA', 'role': 'QA',
                                'registry': 'features/owner/execution/tasks.md',
                                'current': {'Actual Start': '2026-08-02', 'Status': 'in-progress',
                                            'Estimate': '3', 'Progress %': '30'}}]}
        decision = {'feature': 'owner', 'task_id': 'QA', 'analyst_confirmed': True,
                    'kind': 'reviewed-fields', 'fields': {'Status': 'completed', 'Estimate': '5', 'Progress %': '100'},
                    'source': {'file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                               'quote': source.read_text()}}
        update = confirmed_qa_updates(comparison, [decision])[0]
        self.assertEqual(update['expected_registry_fields']['Actual Start'], '2026-08-02')
        self.assertEqual(update['expected_registry_fields']['Estimate'], '5')
        decision['kind'], decision['fields'] = 'keep-current', {}
        update = confirmed_qa_updates(comparison, [decision])[0]
        self.assertEqual(update['expected_registry_fields']['Estimate'], '3')
        self.assertEqual(update['expected_registry_fields']['Status'], 'in-progress')


class HistoryGapTests(unittest.TestCase):
    setUp = test_tracker_lifecycle.TrackerLifecycleTests.setUp
    history = test_tracker_lifecycle.TrackerLifecycleTests.history
    assigned = test_tracker_lifecycle.TrackerLifecycleTests.assigned
    status = test_tracker_lifecycle.TrackerLifecycleTests.status
    task = test_tracker_lifecycle.TrackerLifecycleTests.task

    def test_repeated_handoff_retains_first_completion_without_raw_edits(self):
        history = self.history([self.assigned('start', 1, 'analyst', 'developer'),
                                self.assigned('handoff', 2, 'developer', 'tester'),
                                self.assigned('duplicate', 4, 'developer', 'tester'),
                                self.status('done', 5, 'created', 'done')], 'tester', 'done')
        result = self.task(history)
        self.assertEqual(result['development']['completed_by'], moment(2).isoformat())
        self.assertIsNone(result['development']['finished_at'])
        self.assertEqual(result['qa']['state'], 'completed')
        self.assertIn('assignment-history-gap', result['limitations'])
        self.assertTrue(any('repeated-assignment' in item['signals'] for item in result['evidence']))

    def test_post_qa_status_gap_reopens_qa_not_development(self):
        history = self.history([self.assigned('start', 1, 'analyst', 'developer'),
                                self.assigned('handoff', 2, 'developer', 'tester'),
                                self.status('done', 3, 'created', 'done'),
                                self.status('gap', 4, 'created', 'development')], 'tester', 'development')
        result = self.task(history)
        self.assertEqual(result['development']['state'], 'completed')
        self.assertEqual(result['qa']['state'], 'in-progress')
        self.assertIsNone(result['qa']['finished_at'])
        self.assertIn('status-history-gap', result['limitations'])

    def test_analyst_assignment_on_development_task_does_not_start_work(self):
        result = self.task(self.history([self.assigned('assign', 1, None, 'analyst')]))
        self.assertEqual(result['development']['progress_percent'], 0)
        self.assertIsNone(result['development']['started_at'])
