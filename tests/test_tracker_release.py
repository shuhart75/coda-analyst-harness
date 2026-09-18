from __future__ import annotations

from decimal import Decimal
import hashlib
from importlib import import_module
import json
from pathlib import Path
import sys
import tempfile
import unittest

import test_tracker_history

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_release import qa_groups
from tracker_workflow import role_from_summary
from tracker_registry import read_registry


class ReleasePartitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.scope = {'kind': 'release', 'provider': 'jira', 'ids': ['release-1']}
        self.rows = [{'feature': 'feature', 'task_id': f'TASK-{index}', 'role': 'FE', 'kind': 'real',
                      'jira_key': f'JIRA-{index}'} for index in range(1, 4)]
        self.rows.append({'feature': 'feature', 'task_id': 'QA-FEATURE', 'role': 'QA',
                          'registry': 'features/feature/execution/tasks.md', 'saved_facts': {'Estimate (дн)': '10'}})
        self.issues = [{'jira_key': f'JIRA-{index}', 'development': {'state': 'unknown'},
                        'releases': [{'key': 'release-1' if index == 1 else 'release-2'}]} for index in range(1, 4)]

    def calculate(self, members=None):
        return qa_groups(self.project, 'feature', self.rows, self.issues, self.scope, members or {'JIRA-1'})

    def test_partition_preserves_sum_members_and_original_id(self):
        result = self.calculate()
        groups = result['groups']
        self.assertEqual(groups[0]['task_id'], 'QA-FEATURE')
        self.assertEqual(groups[0]['members'], ['TASK-1'])
        self.assertEqual(groups[1]['members'], ['TASK-2', 'TASK-3'])
        self.assertEqual(sum(Decimal(group['estimate']) for group in groups), Decimal(10))
        self.assertEqual(groups[0]['history_keys'], ['JIRA-1/FE'])

    def test_saved_partition_is_idempotent_and_not_consolidated_in_feature_run(self):
        first = self.calculate()
        path = self.project / first['path']
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(first['document']))
        self.rows.append({**self.rows[-1], 'task_id': first['groups'][1]['task_id']})
        self.assertEqual(self.calculate()['document'], first['document'])
        self.assertFalse(self.calculate()['changed'])
        self.scope['kind'] = 'tasks'
        self.assertEqual(self.calculate()['document'], first['document'])
        self.rows[0]['task_id'] = 'NEW'
        self.assertEqual(self.calculate()['reason'], 'qa-membership-changed-review-partition')

    def test_missing_cards_and_multiple_releases_require_review(self):
        self.issues[0]['releases'].append({'key': 'release-2'})
        self.assertEqual(self.calculate()['reason'], 'confirm-sole-release-owner')
        self.scope['release_decisions'] = {'JIRA-1': 'release-1'}
        self.assertTrue(self.calculate()['ready'])
        self.issues[0]['releases'][0]['name'] = 'Release one'
        self.scope['ids'] = ['Release one']
        self.assertTrue(self.calculate()['ready'])
        self.issues.pop()
        self.assertFalse(self.calculate()['ready'])

    def test_cancelled_tasks_do_not_receive_proportional_estimate(self):
        self.issues[-1]['development']['state'] = 'excluded'
        groups = self.calculate()['groups']
        self.assertEqual([Decimal(group['estimate']) for group in groups], [Decimal(5), Decimal(5)])
        self.assertNotIn('TASK-3', [member for group in groups for member in group['members']])

    def test_role_prefix_is_independent_of_developer_specialty(self):
        self.assertEqual(role_from_summary('[FE][DevOps] Work'), 'FE')
        self.assertIsNone(role_from_summary('[DevOps] Work'))
        self.assertIsNone(role_from_summary('DEV work references [FE] elsewhere'))
        overlay = import_module('sync-actual-progress-overlay')
        task = object.__new__(overlay.Task)
        task.executor, task.role, task.summary = 'B2', 'FE', 'FE Work'
        self.assertEqual(overlay.explicit_executor(task, {'BE': ['B2'], 'FE': ['F1']}), 'B2')
        self.assertEqual(overlay.candidate_resources(task, {'BE': ['B2'], 'FE': ['F1']})[0], ['B2'])

    def test_discussion_estimate_columns_cannot_replace_registry_schema(self):
        path = self.project / 'tasks.md'
        path.write_text('| Task ID | Estimate FE | Estimate BE |\n|---|---|---|\n| TASK | 2 | 3 |\n')
        with self.assertRaisesRegex(ValueError, 'Estimate'):
            read_registry(path)

    def test_full_release_uses_existing_qa_without_remainder(self):
        result = self.calculate({'JIRA-1', 'JIRA-2', 'JIRA-3'})
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(result['groups'][0]['task_id'], 'QA-FEATURE')
        self.assertEqual(Decimal(result['groups'][0]['estimate']), Decimal(10))


class ReleaseWorkflowTests(unittest.TestCase):
    setUp = test_tracker_history.AdaptiveHistoryTests.setUp
    run_tool = test_tracker_history.AdaptiveHistoryTests.run_tool
    configure = test_tracker_history.AdaptiveHistoryTests.configure
    write = test_tracker_history.AdaptiveHistoryTests.write
    jira_issue = test_tracker_history.AdaptiveHistoryTests.jira_issue
    git = test_tracker_history.AdaptiveHistoryTests.git
    save_sources = test_tracker_history.AdaptiveHistoryTests.save_sources
    call = test_tracker_history.AdaptiveHistoryTests.call
    ingest = test_tracker_history.AdaptiveHistoryTests.ingest
    raw_history = test_tracker_history.AdaptiveHistoryTests.raw_history

    def test_release_membership_is_checked_and_jira_id_is_not_a_task_key(self):
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'release', '--scope-provider', 'jira',
                            '--scope-id', '12345', '--label', 'Release', '--scope-source', 'analyst')
        self.assertEqual(run['next_action']['selection']['query_semantics'], 'fixVersion = "12345"')
        result = self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, expected=2)
        self.assertIn('membership', result['error'])

    def test_release_remainder_history_and_independent_qa_groups(self):
        path = self.project / 'features/owner/execution/tasks.md'
        path.parent.mkdir(parents=True)
        path.write_text('| Task ID | Jira | Kind | Role | Status | Estimate (дн) |\n'
                        '|---|---|---|---|---|---|\n'
                        '| FIRST | JIRA-1 | real | BE | done | 2 |\n'
                        '| SECOND | JIRA-2 | real | BE | planned | 2 |\n'
                        '| QA-OWNER | - | real | QA | done | 8 |\n')
        self.save_sources()
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'release', '--scope-provider', 'jira',
                            '--scope-id', 'Release one', '--label', 'Release', '--scope-source', 'analyst',
                            '--intent', 'update-planning')
        first = self.jira_issue('JIRA-1', roles={'BE': 2, 'QA': 8})
        first['fields']['fixVersions'] = [{'id': '101', 'name': 'Release one'}]
        skipped = self.jira_issue('JIRA-3', roles={'BE': 10})
        skipped['fields']['summary'] = '[DevOps] Skip this'
        skipped['fields']['fixVersions'] = [{'id': '101', 'name': 'Release one'}]
        self.ingest(run, {'issues': [first, skipped]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        preview = self.run_tool(self.state, 'release-preview', '--run-id', run['run_id'], '--project-root', str(self.project))
        self.assertEqual(preview['next_action']['keys'], ['JIRA-2'])
        self.assertEqual(len(preview['skipped']), 1)
        second = self.jira_issue('JIRA-2', roles={'BE': 2})
        response = self.write(self.state / 'remainder.json', {'issues': [second]})
        histories = []
        for key in ('JIRA-1', 'JIRA-2'):
            raw = self.raw_history()
            raw['key'] = key
            raw.update(total=3, start=0)
            if key == 'JIRA-2':
                raw['status']['name'] = 'todo'
                raw['changelogs'] = raw['changelogs'][:2]
                raw['total'] = 2
            source = self.write(self.state / (key + '.json'), raw)
            histories.append({'provider': 'jira', 'key': key, 'role': 'BE', 'call': self.call('jira'),
                              'response_file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                              'mapping': {**test_tracker_history.JIRA_MAPPING, 'total': '/total', 'start': '/start'},
                              'status_aliases': {'Done': 'done'}})
        manifest = self.write(self.state / 'manifest.json', {
            'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'test analyst decision',
            'provider': 'jira', 'participants': {'dev': 'BE', 'qa': 'QA'},
            'status_rules': {'not_started': ['todo'], 'qa_completed': ['done']}, 'responses': histories,
            'supplemental_responses': [{'keys': ['JIRA-2'], 'call': self.call('jira', 'key IN ("JIRA-2")'),
                                       'response_file': str(response), 'sha256': hashlib.sha256(response.read_bytes()).hexdigest()}]})
        before = path.read_bytes()
        review = self.run_tool(self.state, 'history-review', '--run-id', run['run_id'],
                               '--project-root', str(self.project), '--manifest', str(manifest))
        self.assertEqual(path.read_bytes(), before)
        groups = review['features'][0]['qa_groups']
        self.assertEqual([group['qa']['progress_percent'] for group in groups], [100, 0])
        self.assertEqual([Decimal(group['estimate']) for group in groups], [Decimal(4), Decimal(4)])
        self.assertEqual(len([row for row in review['comparison']['rows'] if row['role'] == 'QA']), 2)
        self.assertIn('| FE | BE | QA |', review['comparison']['table'])
        repeated = self.run_tool(self.state, 'history-review', '--run-id', run['run_id'],
                                '--project-root', str(self.project), '--manifest', str(manifest))
        self.assertEqual(repeated['review_file'], review['review_file'])
        evidence = self.state / 'analyst.txt'
        evidence.write_text('Approve both QA groups and their current states')
        document = review['feature_qa_proposals'][0]['partition']['document']
        confirmations = []
        for index, group in enumerate(groups):
            confirmations.append({'feature': 'owner', 'task_id': group['task_id'], 'kind': 'reviewed-fields',
                                  'analyst_confirmed': True, 'fields': {'Status': 'done' if index == 0 else 'in-progress',
                                                                      'Estimate (дн)': group['estimate']},
                                  'source': {'file': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
                                             'quote': evidence.read_text()}})
        value = json.loads(manifest.read_text())
        value['qa_confirmations'] = confirmations
        self.write(manifest, value)
        approved = self.run_tool(self.state, 'history-review', '--run-id', run['run_id'],
                                 '--project-root', str(self.project), '--manifest', str(manifest))
        path.write_text(path.read_text().replace('| QA-OWNER | - | real | QA | done | 8 |',
                                                '| QA-OWNER | - | real | QA | done | 4.000000 |')
                        + f"| {groups[1]['task_id']} | - | real | QA | in-progress | {groups[1]['estimate']} |\n")
        partition_path = self.project / 'features/owner/execution/qa-groups.json'
        self.write(partition_path, document)
        applied = self.run_tool(self.state, 'qa-application-check', '--review-file', approved['review_file'],
                                '--project-root', str(self.project))
        self.assertEqual(applied['status'], 'qa-application-verified')
        path.write_text(path.read_text().replace('4.000000', '8.000000'))
        rejected = self.run_tool(self.state, 'qa-application-check', '--review-file', approved['review_file'],
                                 '--project-root', str(self.project), expected=2)
        self.assertEqual(rejected['status'], 'qa-application-mismatch')


if __name__ == '__main__':
    unittest.main()
