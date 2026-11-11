from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import test_trackerctl
import test_quarter_delivery_integration as quarter
from tracker_registry import read_registry
from tracker_scope import preview_scope
from tracker_execution import preview_execution
from tracker_release import apply_scope_decisions
from project_layout import feature_root


class LegacyRegistryTests(unittest.TestCase):
    def test_historical_ids_are_local_only_and_bytes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.md'
            raw = '| Jira | Summary | Kind | Role |\n|---|---|---|---|\n| OLD-7 | Old work | real | BE |\n| FE-LOCAL | Virtual | virtual | FE |\n'
            path.write_text(raw)
            tables, note = read_registry(path)
            self.assertEqual(note, 'legacy-local-identities')
            self.assertEqual([r['Task ID'] for r in tables[0]], ['OLD-7', 'FE-LOCAL'])
            self.assertTrue(all('SberTrek' not in r for r in tables[0]))
            self.assertEqual(path.read_text(), raw)
            path.write_text(raw.replace('| real | BE |', '| real |'))
            with self.assertRaisesRegex(ValueError, 'Повреждена строка'):
                read_registry(path, identity_only=True)

    def test_explicit_modern_identity_is_not_replaced_by_historical_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.md'
            path.write_text('| Task ID | Jira | SberTrek | Summary | Kind | Role |\n'
                            '|---|---|---|---|---|---|\n| LOCAL | OLD-7 | NEW-1 | Task | real | BE |\n')
            tables, _ = read_registry(path)
            self.assertEqual(tables[0][0]['Task ID'], 'LOCAL')
            self.assertEqual(tables[0][0]['SberTrek'], 'NEW-1')


class EpicDeliveryFlowTests(unittest.TestCase):
    write_tasks = quarter.QuarterDeliveryIntegrationTests.write_tasks
    git = quarter.QuarterDeliveryIntegrationTests.git
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    configure = test_trackerctl.DirectTrackerWorkflowTests.configure
    write = test_trackerctl.DirectTrackerWorkflowTests.write
    sber_issue = test_trackerctl.DirectTrackerWorkflowTests.sber_issue

    def setUp(self):
        quarter.QuarterDeliveryIntegrationTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.configure(self.state)
        self.roles = ['FE'] * 3 + ['BE'] * 13 + ['BE', 'FE', 'FE', 'FE', 'BE']
        self.keys = [f'NEW-{n + 1}' for n in range(21)]
        registry = self.feature / 'execution/tasks.md'
        registry.write_text('| Task ID | SberTrek | Summary | Kind | Role | Status |\n'
                            '|---|---|---|---|---|---|\n' + ''.join(
                            f'| LOCAL-{i} | {key} | Task | real | {role} | planned |\n'
                            for i, (key, role) in enumerate(zip(self.keys[:16], self.roles[:16]))) +
                            '| QA-ONE | - | QA | real | QA | planned |\n')
        q3 = feature_root(self.project, self.q3key)
        self.legacy_paths = []
        for i in range(19):
            path = q3 / f'slices/old-{i}/execution/tasks.md'
            path.parent.mkdir(parents=True)
            path.write_text('| Jira | Summary | Kind | Role | Status |\n|---|---|---|---|---|\n'
                            f'| OLD-{i + 1} | Historical | real | BE | done |\n')
            self.legacy_paths.append(path)
        self.git('add', '--', 'quarters')
        self.git('commit', '-m', 'Prepare historical and current registries')

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*')
                if p.is_file() and '.git' not in p.relative_to(self.project).parts}

    def collected(self):
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'epic', '--scope-id', 'EPIC-1',
                            '--label', 'Quarter expansion', '--scope-source', 'analyst', '--intent', 'update-planning')
        action = run['next_action']
        self.assertEqual(action['export_hint']['projection_argument'], 'attributes')
        selection = action['selection']['query_semantics']
        cards = [self.sber_issue(key, summary=f'[{role}] Task', roles={role: 2, **({'QA': 10} if i == 0 else {})})
                 for i, (key, role) in enumerate(zip(self.keys, self.roles))]
        response = self.write(self.state / 'response.json', cards)
        call = self.write(self.state / 'call.json', self.call(selection, tool='issue.exportJson'))
        self.run_tool(self.state, 'ingest', '--run-id', run['run_id'], '--step-id', action['step_id'],
                      '--response-file', str(response), '--response-source', 'inline-json-capture', '--call-file', str(call))
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        return self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])

    def call(self, selection='', tool='history-reader'):
        return {'provider': 'sbertrek', 'tool': tool, 'arguments': {'query': selection}, 'selection': selection,
                'operation': 'read-only', 'capability_source': 'offline fixture schema',
                'captured_at': '2026-10-01T00:00:00+00:00'}

    def decisions(self, status):
        quote = 'Confirm the five new tasks in Q4 expansion with their existing role prefixes.'
        answer = self.state / 'answer.txt'; answer.write_text(quote)
        return {'schema_version': 1, 'analyst_confirmed': True, 'provider': 'sbertrek',
                'run_id': status['run_id'], 'scope': status['scope'],
                'source': {'file': str(answer), 'sha256': hashlib.sha256(answer.read_bytes()).hexdigest(), 'quote': quote},
                'tasks': [{'key': key, 'feature': self.q4key} for key in self.keys[16:]]}

    def test_epic_with_legacy_neighbors_reaches_history_without_registry_migration(self):
        before = self.snapshot()
        status = self.collected()
        self.assertFalse(status['final_response_allowed'])
        result_bytes = Path(status['paths']['reconciled']).read_bytes()
        args = ('execution-preview', '--run-id', status['run_id'], '--project-root', str(self.project), '--delivery', self.q4key)
        pending = self.run_tool(self.state, *args)
        self.assertFalse(pending['ownership_ready'])
        self.assertNotIn('unreadable-registry', {b['reason'] for b in pending['blockers']})
        self.assertEqual(len([w for w in pending['warnings'] if w['reason'] == 'legacy-local-identities']), 19)
        decisions = self.decisions(status)
        path = self.write(self.state / 'decisions.json', decisions)
        preview = self.run_tool(self.state, *args, '--decisions', str(path))
        self.assertTrue(preview['ownership_ready'])
        self.assertEqual(len(preview['proposed_registrations']), 5)
        self.assertEqual(preview['next_action']['type'], 'collect-history')
        self.assertEqual(set(preview['next_action']['keys']), set(self.keys))
        self.assertEqual(preview['selected_features'], [self.q4key])
        manifest = {'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'confirmed test rules',
                    'provider': 'sbertrek', 'deliveries': [self.q4key], 'scope_decisions': decisions,
                    'participants': {}, 'status_rules': {'not_started': ['created'], 'qa_completed': ['done']},
                    'responses': []}
        file = self.write(self.state / 'manifest.json', manifest)
        review_args = ('history-review', '--run-id', status['run_id'], '--project-root', str(self.project), '--manifest', str(file))
        pending = self.run_tool(self.state, *review_args)
        self.assertEqual(len(pending['pending_history']), 21)
        for key, role in zip(self.keys, self.roles):
            history = self.write(self.state / f'{key}.json', {'key': key, 'status': 'created', 'assignee': None,
                                                            'events': [], 'start': 0, 'total': 0})
            manifest['responses'].append({'provider': 'sbertrek', 'key': key, 'role': role,
                'response_file': str(history), 'sha256': hashlib.sha256(history.read_bytes()).hexdigest(),
                'call': {**self.call(), 'arguments': {'key': key}},
                'mapping': {'key': '/key', 'status': '/status', 'assignee': '/assignee', 'events': '/events',
                            'start': '/start', 'total': '/total', 'field': '/field', 'at': '/at',
                            'from': '/from', 'to': '/to', 'assignment_field': 'assignee', 'status_field': 'status'}})
        self.write(file, manifest)
        reviewed = self.run_tool(self.state, *review_args)
        self.assertEqual(reviewed['pending_history'], [])
        self.assertEqual(reviewed['pending_history_dates'], [])
        self.assertIsNotNone(reviewed['comparison'])
        for row in reviewed['comparison']['rows']:
            if row['role'] in {'BE', 'FE'}:
                self.assertEqual(row['history']['state'], 'not-started')
                self.assertIsNone(row['history']['started_at'])
                self.assertIsNone(row['history']['finished_at'])
        self.assertEqual(Path(status['paths']['reconciled']).read_bytes(), result_bytes)
        self.assertEqual(len(reviewed['proposed_registrations']), 5)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.gantt / 'quarter-plan.puml').read_bytes(), self.approved_bytes)
        # Review does not authorize writes in an unrelated collaboration session.
        blocked = self.run_tool(self.state, 'application-preflight', '--project-root', str(self.project),
                                '--feature', self.q4key, expected=2)
        self.assertIn('error', blocked)

    def test_stale_decision_and_local_id_collision_still_block(self):
        status = self.collected()
        result = json.loads(Path(status['paths']['reconciled']).read_text())
        decision = self.decisions(status)
        bad = copy.deepcopy(decision); bad['run_id'] = 'another-run'
        with self.assertRaises(ValueError):
            apply_scope_decisions(self.project, result, bad)
        bad = copy.deepcopy(decision); bad['scope']['ids'] = ['ANOTHER-1']
        with self.assertRaises(ValueError):
            apply_scope_decisions(self.project, result, bad)
        bad = copy.deepcopy(decision); bad['source']['quote'] = 'not in the answer'
        with self.assertRaises(ValueError):
            apply_scope_decisions(self.project, result, bad)
        bad = copy.deepcopy(decision); bad['tasks'][0]['role'] = 'FE'
        with self.assertRaisesRegex(ValueError, 'prefix'):
            apply_scope_decisions(self.project, result, bad)
        path = self.legacy_paths[0]
        path.write_text(path.read_text().replace('OLD-1', self.keys[0]))
        self.git('add', '--', str(path.relative_to(self.project)))
        self.git('commit', '-m', 'Preserve ambiguous historical identity')
        preview = preview_execution(self.project, None, None, result, deliveries=[self.q4key])
        item = next(x for x in preview['items'] if x['tracker_key'] == self.keys[0])
        self.assertIn('unconfirmed-internal-id-collision', item['blockers'])
        self.assertEqual(item['owners'], [self.q4key])
        scoped = preview_scope(self.project, 'sbertrek', None, None, [self.q3key])
        self.assertNotIn(self.keys[0], scoped['scope']['ids'])


if __name__ == '__main__':
    unittest.main()
