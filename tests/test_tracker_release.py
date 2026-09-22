from __future__ import annotations

from decimal import Decimal
import copy
import hashlib
from importlib import import_module
import json
from pathlib import Path
import sys
import tempfile
import unittest

import test_tracker_history

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_release import apply_scope_decisions, qa_groups, release_owners
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

    def test_unknown_qa_estimate_is_a_reviewable_blocker(self):
        self.rows[-1]['saved_facts']['Estimate (дн)'] = '-'
        self.assertEqual(self.calculate()['reason'], 'qa-estimate-unknown')

    def test_scope_ownership_reads_keys_but_does_not_relax_selected_schema(self):
        path = self.project / 'features/other/execution/tasks.md'
        path.parent.mkdir(parents=True)
        path.write_text('| Task ID | Jira | Estimate FE | Estimate BE |\n'
                        '|---|---|---|---|\n| OTHER | JIRA-99 | 3 | 4 |\n')
        result = {'scope': self.scope, 'issues': [{'jira_key': 'JIRA-1', 'summary': '[BE] New'}]}
        owners = release_owners(self.project, result)
        self.assertEqual(owners['blockers'], [])
        self.assertEqual(owners['items'][0]['basis'], 'analyst-required')
        result['issues'][0]['jira_key'] = 'JIRA-99'
        self.assertEqual(release_owners(self.project, result)['selected_features'], ['other'])
        with self.assertRaises(ValueError):
            read_registry(path)
        path.write_text(path.read_text().replace('| OTHER | JIRA-99 | 3 | 4 |', '| OTHER | JIRA-99 | 3 |'))
        self.assertEqual(release_owners(self.project, result)['blockers'][0]['reason'], 'ownership-index-unreadable')

    def test_confirmed_scope_and_unprefixed_role_are_separate_from_raw_cards(self):
        (self.project / 'features/new-feature').mkdir(parents=True)
        answer = self.project / 'answer.txt'
        answer.write_text('All tasks belong to new-feature; TASK-1 is BE.')
        result = {'scope': self.scope, 'issues': [], 'work_items': [],
                  'skipped': [{'jira_key': 'TASK-1', 'summary': 'Unprefixed work',
                               'reason': 'outside-supported-roles', 'role_estimates': {},
                               'development': {'state': 'unknown'}}]}
        decisions = {'schema_version': 1, 'provider': 'jira', 'release': 'release-1',
                     'analyst_confirmed': True, 'source': {'file': str(answer),
                     'sha256': hashlib.sha256(answer.read_bytes()).hexdigest(), 'quote': answer.read_text()},
                     'tasks': [{'key': 'TASK-1', 'feature': 'new-feature', 'role': 'BE'}]}
        original = copy.deepcopy(result)
        reviewed = apply_scope_decisions(self.project, result, decisions)
        self.assertEqual(result, original)
        self.assertEqual(reviewed['skipped'], [])
        ownership = release_owners(self.project, reviewed)
        self.assertEqual(ownership['items'][0]['features'], ['new-feature'])
        self.assertTrue(ownership['items'][0]['registration_required'])
        decisions['source']['quote'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'evidence'):
            apply_scope_decisions(self.project, result, decisions)


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

    def begin_release(self, provider='jira'):
        return self.run_tool(self.state, 'begin', '--scope-kind', 'release', '--scope-provider', provider,
                             '--scope-id', 'REL-1', '--label', 'Release', '--scope-source', 'analyst',
                             '--intent', 'update-planning')

    def linked_call(self, run, payload=None, mapping=None, basis='links'):
        call = self.call(run['next_action']['provider'], run['next_action']['selection']['query_semantics'])
        payload = payload or {'owner': 'REL-1', 'edges': [{'type': 'ships', 'target': 'JIRA-1'}], 'size': 1, 'offset': 0}
        source = self.write(self.state / 'links.json', payload)
        call['release_membership'] = {'schema_version': 1, 'sources': [{
            'response_file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'call': copy.deepcopy(call), 'basis': basis,
            'mapping': mapping or {'records': '/edges', 'member_key': '/target', 'release_root': '/owner',
                                  'relation': '/type', 'relation_value': 'ships', 'total': '/size', 'start': '/offset'}}]}
        return call

    def test_link_evidence_without_editing_cards_and_unknown_not_zero(self):
        run = self.begin_release()
        issue = self.jira_issue('JIRA-1')
        original = copy.deepcopy(issue)
        call = self.linked_call(run)
        result = self.ingest(run, {'issues': [issue]}, call)
        self.assertEqual(result['status'], 'tracker-read-ready')
        self.assertEqual(issue, original)
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        report = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        self.assertIsNone(report['summary']['role_totals_person_days']['QA'])
        self.assertIsNone(report['summary']['story_points_total'])
        self.assertIn('Пустые оценки не равны нулю', report['response_contract']['text'])
        self.assertNotIn('release-result-incomplete', str(report['limitations']))
        preview = self.run_tool(self.state, 'release-preview', '--run-id', run['run_id'], '--project-root', str(self.project))
        self.assertFalse(preview['registration']['gantt_presence_required'])
        self.assertTrue(preview['items'][0]['registration_required'])

    def test_selected_query_mapping_accepts_changed_tool_and_response_shape(self):
        run = self.begin_release()
        call = self.linked_call(run, {'response': {'members': [{'identity': {'key': 'JIRA-1'}}]}},
                                {'records': '/response/members', 'member_key': '/identity/key'}, 'selected-query')
        call['release_membership']['sources'][0]['call']['tool'] = 'new-installation-v99-search'
        self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, call)
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        report = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        self.assertIn('release-result-incomplete:membership-completeness-not-proven', report['limitations'])

    def test_sbertrek_links_use_same_evidence_contract_without_jira_calls(self):
        from test_trackerctl import DirectTrackerWorkflowTests

        run = self.begin_release('sbertrek')
        issue = DirectTrackerWorkflowTests.sber_issue(self, 'JIRA-1', jira_key='OTHER-1')
        self.ingest(run, {'issues': [issue]}, self.linked_call(run))
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        report = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        self.assertEqual(report['counts']['jira'], 0)
        self.assertEqual(report['counts']['matched'], 0)

    def test_complete_unpaginated_capability_is_bound_to_real_description(self):
        run = self.begin_release()
        call = self.linked_call(run, {'items': [{'key': 'JIRA-1'}]},
                                {'records': '/items', 'member_key': '/key'}, 'selected-query')
        quote = 'Returns all release members; this operation has no pagination.'
        capability = self.write(self.state / 'capabilities.json', {'description': quote})
        source = call['release_membership']['sources'][0]
        source['call']['capability_source'] = str(capability)
        source['unpaginated_contract'] = {'response_file': str(capability),
                                         'sha256': hashlib.sha256(capability.read_bytes()).hexdigest(), 'quote': quote}
        bad = copy.deepcopy(call)
        bad['release_membership']['sources'][0]['unpaginated_contract']['quote'] = 'invented guarantee'
        self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, bad, expected=2)
        self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, call)
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        report = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        self.assertNotIn('release-result-incomplete:membership-completeness-not-proven', report['limitations'])

    def test_pages_are_separate_immutable_sources_not_hand_merged_json(self):
        run = self.begin_release()
        call = self.linked_call(run, {'owner': 'REL-1', 'edges': [{'type': 'ships', 'target': 'JIRA-1'}],
                                      'size': 2, 'offset': 0})
        second = copy.deepcopy(call['release_membership']['sources'][0])
        source = self.write(self.state / 'links-page-two.json', {
            'owner': 'REL-1', 'edges': [{'type': 'ships', 'target': 'JIRA-2'}], 'size': 2, 'offset': 1})
        second.update(response_file=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        second['call']['arguments']['page'] = 2
        call['release_membership']['sources'].append(second)
        self.ingest(run, {'issues': [self.jira_issue('JIRA-1'), self.jira_issue('JIRA-2')]}, call)
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        self.assertNotIn('release-result-incomplete:membership-completeness-not-proven',
                         self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])['limitations'])

    def test_wrong_relation_other_release_and_wrong_checksum_rejected(self):
        run = self.begin_release()
        for kind in ('relation', 'release', 'checksum'):
            call = self.linked_call(run)
            source = call['release_membership']['sources'][0]
            if kind == 'relation':
                source['mapping']['relation_value'] = 'unrelated'
            elif kind == 'release':
                source['call']['selection'] = 'another release'
            else:
                source['sha256'] = '0' * 64
            self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, call, expected=2)
        self.assertEqual(self.begin_release()['run_id'], run['run_id'])
        self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, self.linked_call(run))

    def test_pausing_application_preserves_reconciliation_and_requires_confirmation(self):
        run = self.begin_release()
        issue = self.jira_issue('JIRA-1')
        issue['fields']['fixVersions'] = [{'name': 'REL-1'}]
        self.ingest(run, {'issues': [issue]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        before = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        arguments = ('application-state', '--run-id', run['run_id'], '--state', 'paused', '--reason', 'analyst stopped application')
        self.run_tool(self.state, *arguments, expected=2)
        self.run_tool(self.state, *arguments, '--analyst-confirmed')
        paused = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        self.assertEqual(paused['reconciled_sha256'], before['reconciled_sha256'])
        self.assertEqual(paused['planning_update']['state'], 'paused')
        self.assertFalse(paused['planning_application_allowed'])
        self.run_tool(self.state, 'release-preview', '--run-id', run['run_id'], '--project-root', str(self.project), expected=2)
        self.run_tool(self.state, 'application-state', '--run-id', run['run_id'], '--state', 'pending',
                      '--reason', 'analyst resumes', '--analyst-confirmed')
        self.assertTrue(self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])['planning_application_allowed'])

    def test_new_feature_without_gantt_and_unrelated_role_columns_does_not_block_history(self):
        path = self.project / 'features/owner/execution/tasks.md'
        path.parent.mkdir(parents=True)
        path.write_text('| Task ID | Jira | Kind | Role | Status | Estimate (дн) |\n'
                        '|---|---|---|---|---|---|\n| FIRST | JIRA-1 | real | BE | unknown | - |\n'
                        '| QA-OWNER | - | real | QA | unknown | - |\n')
        unrelated = self.project / 'features/cohorts/execution/tasks.md'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('| Task ID | Jira | Estimate FE | Estimate BE |\n'
                             '|---|---|---|---|\n| OTHER | JIRA-99 | 3 | 4 |\n')
        self.save_sources()
        unrelated.write_text(unrelated.read_text().replace('| 3 | 4 |', '| 8 | 4 |'))
        original = unrelated.read_bytes()
        run = self.begin_release()
        issue = self.jira_issue('JIRA-1')
        issue['fields']['fixVersions'] = [{'name': 'REL-1'}]
        self.ingest(run, {'issues': [issue]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        preview = self.run_tool(self.state, 'release-preview', '--run-id', run['run_id'], '--project-root', str(self.project))
        self.assertTrue(preview['execution']['ownership_ready'])
        source = self.write(self.state / 'history.json', self.raw_history())
        manifest = {'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'analyst rules',
                    'provider': 'jira', 'participants': {'dev': 'BE', 'qa': 'QA'},
                    'status_rules': {'not_started': ['todo'], 'qa_completed': ['done']},
                    'responses': [{'provider': 'jira', 'key': 'JIRA-1', 'role': 'BE', 'call': self.call('jira'),
                                   'response_file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                   'status_aliases': {'Done': 'done'}}]}
        recorded = self.write(self.state / 'manifest.json', manifest)
        review = self.run_tool(self.state, 'history-review', '--run-id', run['run_id'], '--project-root', str(self.project),
                               '--manifest', str(recorded))
        self.assertTrue(review['history_processed'])
        self.assertEqual(review['qa_application_blockers'][0]['reason'], 'qa-estimate-unknown')
        self.assertEqual(unrelated.read_bytes(), original)
        self.run_tool(self.state, 'qa-application-check', '--project-root', str(self.project),
                      '--review-file', review['review_file'], expected=2)

    def test_release_membership_is_checked_and_jira_id_is_not_a_task_key(self):
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'release', '--scope-provider', 'jira',
                            '--scope-id', '12345', '--label', 'Release', '--scope-source', 'analyst')
        self.assertEqual(run['next_action']['selection']['query_semantics'], 'fixVersion = "12345"')
        result = self.ingest(run, {'issues': [self.jira_issue('JIRA-1')]}, expected=2)
        self.assertIn('membership', result['error'])

    def test_explicit_role_keeps_release_membership_after_remainder_collection(self):
        registry = self.project / 'features/owner/execution/tasks.md'
        registry.parent.mkdir(parents=True)
        registry.write_text('| Task ID | Jira | Kind | Role | Status | Estimate (дн) |\n'
                            '|---|---|---|---|---|---|\n'
                            '| FIRST | JIRA-1 | real | BE | unknown | - |\n'
                            '| REST | JIRA-2 | real | BE | unknown | - |\n'
                            '| QA | - | real | QA | unknown | 4 |\n')
        self.save_sources()
        run = self.begin_release()
        issue = self.jira_issue('JIRA-1')
        issue['fields'].update(summary='Work without prefix', fixVersions=[{'name': 'REL-1'}])
        self.ingest(run, {'issues': [issue]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        answer = self.state / 'answer.txt'
        answer.write_text('JIRA-1 is BE work in owner.')
        decisions = {'schema_version': 1, 'provider': 'jira', 'release': 'REL-1', 'analyst_confirmed': True,
                     'source': {'file': str(answer), 'sha256': hashlib.sha256(answer.read_bytes()).hexdigest(),
                                'quote': answer.read_text()},
                     'tasks': [{'key': 'JIRA-1', 'feature': 'owner', 'role': 'BE'}]}
        source = self.write(self.state / 'remainder.json', {'issues': [self.jira_issue('JIRA-2')]})
        manifest = self.write(self.state / 'manifest.json', {
            'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'analyst rules',
            'provider': 'jira', 'participants': {}, 'status_rules': {}, 'responses': [],
            'release_scope_decisions': decisions,
            'unavailable_history': {'JIRA-1/BE': 'access unavailable', 'JIRA-2/BE': 'access unavailable'},
            'supplemental_responses': [{'keys': ['JIRA-2'], 'response_file': str(source),
                                       'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                       'call': self.call('jira', 'key IN ("JIRA-2")')}]})
        review = self.run_tool(self.state, 'history-review', '--run-id', run['run_id'],
                               '--project-root', str(self.project), '--manifest', str(manifest))
        groups = review['features'][0]['qa_groups']
        self.assertEqual(groups[0]['members'], ['FIRST'])
        self.assertEqual(groups[1]['members'], ['REST'])
        self.assertIsNone(groups[0]['qa']['finished_at'])

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
        self.run_tool(self.state, 'application-state', '--run-id', run['run_id'], '--state', 'paused',
                      '--reason', 'analyst stopped after review', '--analyst-confirmed')
        paused = self.run_tool(self.state, 'qa-application-check', '--review-file', approved['review_file'],
                               '--project-root', str(self.project), expected=2)
        self.assertIn('paused', paused['error'])
        self.run_tool(self.state, 'application-state', '--run-id', run['run_id'], '--state', 'pending',
                      '--reason', 'analyst resumed after review', '--analyst-confirmed')
        path.write_text(path.read_text().replace('4.000000', '8.000000'))
        rejected = self.run_tool(self.state, 'qa-application-check', '--review-file', approved['review_file'],
                                 '--project-root', str(self.project), expected=2)
        self.assertEqual(rejected['status'], 'qa-application-mismatch')


if __name__ == '__main__':
    unittest.main()
