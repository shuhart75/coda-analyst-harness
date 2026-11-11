from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_trackerctl


class SberOnlyTests(unittest.TestCase):
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    write = test_trackerctl.DirectTrackerWorkflowTests.write
    configure = test_trackerctl.DirectTrackerWorkflowTests.configure
    sber_issue = test_trackerctl.DirectTrackerWorkflowTests.sber_issue

    def test_failed_adaptive_run_retries_unchanged_export_and_keeps_every_card(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.configure(state)
            run = self.run_tool(state, 'begin', '--scope-kind', 'epic', '--scope-id', 'TEST-1',
                                '--label', 'Retry export', '--scope-source', 'analyst', '--intent', 'update-planning')
            action = run['next_action']
            root = state / 'tracker-runs' / run['run_id']
            saved = json.loads((root / 'run.json').read_text())
            # Simulate the previous version's failed single-provider state without replacing the run.
            saved.update(status='tracker-read-failed', failure={'reason': 'obsolete link validation'})
            saved['config']['schema_version'] = 4
            saved['config']['jira_enabled'] = True
            saved['cards']['jira'] = []
            self.write(root / 'run.json', saved)
            cards = [self.sber_issue(f'TEST-{index}', summary='[BE] Work', roles={'BE': 1})
                     for index in range(2, 23)]
            for index, value in enumerate(('REMOVED_BY_CLONING', 'OTHER-99', 'malformed link')):
                cards[index]['issue_key'] = value
                cards[index]['attributes'].append({'code': 'issue_key', 'value': value})
            response = self.write(state / 'export.json', {'issues': cards})
            original = response.read_bytes()
            call = self.write(state / 'call.json', {
                'provider': 'sbertrek', 'tool': 'issue.exportJson',
                'arguments': {'query': action['selection']['query_semantics']},
                'selection': action['selection']['query_semantics'], 'operation': 'read-only',
                'capability_source': 'test connector schema', 'captured_at': '2026-08-01T00:00:00+00:00',
            })
            args = ('ingest', '--run-id', run['run_id'], '--step-id', action['step_id'],
                    '--response-file', str(response), '--response-source', 'mcp-file', '--call-file', str(call))
            retried = self.run_tool(state, *args)
            self.assertEqual(retried['status'], 'tracker-read-ready')
            self.assertEqual(retried['run_id'], run['run_id'])
            self.assertEqual(self.run_tool(state, *args)['ingest'], 'idempotent-no-op')
            self.run_tool(state, 'reconcile', '--run-id', run['run_id'])
            completed = self.run_tool(state, 'result-status', '--run-id', run['run_id'])
            self.assertEqual(completed['counts']['sbertrek'], 21)
            self.assertEqual(completed['counts']['issues'], 21)
            self.assertEqual(completed['summary']['story_points_total'], 21)
            self.assertNotIn('jira', completed['response_contract']['text'].lower())
            self.assertFalse((root / 'providers/jira.json').exists())
            self.assertEqual(response.read_bytes(), original)
            updated = json.loads((root / 'run.json').read_text())
            self.assertEqual(updated['steps'][0]['step_id'], action['step_id'])
            self.assertTrue(all('jira_key' not in card for card in updated['cards']['sbertrek']))

    def test_config_migration_preserves_confirmed_settings_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.configure(state)
            old = json.loads((state / 'tracker-config.json').read_text())
            old.update(schema_version=4, jira_enabled=True)
            for field in ('projects', 'participants', 'status_rules'):
                old[field]['jira'] = copy.deepcopy(old[field]['sbertrek'])
            self.write(state / 'tracker-config.json', old)
            self.assertEqual(self.run_tool(state, 'config-status')['status'], 'tracker-config-ready')
            migrated = json.loads((state / 'tracker-config.json').read_text())
            self.assertEqual(migrated['schema_version'], 5)
            self.assertTrue(migrated['setup_complete'])
            self.assertNotIn('jira', json.dumps(migrated))
            for field in ('projects', 'participants', 'status_rules'):
                self.assertEqual(migrated[field]['sbertrek'], old[field]['sbertrek'])
            backups = list(state.glob('tracker-config.pre-sber-only.*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), old)
            before = {p: p.read_bytes() for p in state.iterdir()}
            self.run_tool(state, 'config-status')
            self.assertEqual(before, {p: p.read_bytes() for p in state.iterdir()})

    def test_removed_commands_and_provider_are_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.configure(state)
            before = {p: p.read_bytes() for p in state.iterdir()}
            for args in [('set-jira-mode', 'enabled'), ('identity-lookup',), ('resolve-conflict',),
                         ('ingest-error',), ('scope-preview', '--provider', 'jira'),
                         ('begin', '--compare-trackers')]:
                with self.subTest(args=args):
                    result = subprocess.run(['python3', str(test_trackerctl.SCRIPT), *args], capture_output=True,
                                            env={**os.environ, 'ANALYST_HARNESS_STATE_ROOT': str(state)})
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(before, {p: p.read_bytes() for p in state.iterdir()})

    def test_old_multi_provider_run_is_not_silently_reinterpreted(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.configure(state)
            run = self.run_tool(state, 'begin', '--scope-kind', 'tasks', '--scope-id', 'TEST-1',
                                '--label', 'Historical', '--scope-source', 'analyst')
            path = state / 'tracker-runs' / run['run_id'] / 'run.json'
            saved = json.loads(path.read_text())
            saved['scope']['tracker_mode'] = 'compare'
            self.write(path, saved)
            before = {p: p.read_bytes() for p in state.rglob('*') if p.is_file()}
            self.run_tool(state, 'run-status', '--run-id', run['run_id'], expected=2)
            self.assertEqual(before, {p: p.read_bytes() for p in state.rglob('*') if p.is_file()})
