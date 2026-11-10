from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import json
import hashlib
import unittest
from unittest.mock import patch

from test_productivity_forecast import tool, task
import test_actual_progress_sources as sources
import test_tracker_release
import test_tracker_history
from defect_metrics import assignment_facts, quarterly_defects
from tracker_lifecycle import HistoryEvent, TaskHistory
from schedule_diagnostics import resource_report


class SchedulingCorrectionsTests(unittest.TestCase):
    def schedule(self, tasks, unavailable=None):
        return tool.task_schedules(tasks, set(), date(2026, 11, 10),
            {'BE': ['B1'], 'FE': ['F1', 'F2'], 'QA': ['Q1']}, unavailable)

    def test_independent_frontend_starts_today_despite_old_plan_and_backend(self):
        tasks = {'f/b': task('b', 'BE', 5, '', 'B1'),
                 'f/f': task('f', 'FE', 1, '2026-11-20', '')}
        result = self.schedule(tasks)
        self.assertEqual(result['f/f'].start, date(2026, 11, 10))

    def test_both_frontend_resources_take_ready_work_and_explain_waiting(self):
        tasks = {'f/one': task('one', 'FE', 1, '', ''),
                 'f/two': task('two', 'FE', 1, '', ''),
                 'f/later': replace(task('later', 'FE', 1, '', ''), not_before='2026-11-13')}
        result = self.schedule(tasks)
        self.assertEqual({result[key].assignee for key in ('f/one', 'f/two')}, {'F1', 'F2'})
        self.assertTrue(all(result[key].start == date(2026, 11, 10) for key in ('f/one', 'f/two')))
        report = resource_report(tasks, result, {'FE': ['F1', 'F2']}, set(), {},
                                 date(2026, 11, 10), tool.candidate_resources, tool.needs_forecast_schedule)
        for row in report['days']:
            if row['date'] == '2026-11-10':
                self.assertEqual(row['load'], 1)
                self.assertEqual(row['free'], 0)
            if row['date'] == '2026-11-11':
                self.assertEqual(row['load'], 0)
                self.assertEqual(row['waiting'][0]['reasons'], ['not-before'])

    def test_dependency_and_not_before_are_respected(self):
        tasks = {'f/b': task('b', 'BE', 2, '', 'B1'),
                 'f/f': replace(task('f', 'FE', 1, '', ''), depends_on=('b',), not_before='2026-11-13')}
        self.assertEqual(self.schedule(tasks)['f/f'].start, date(2026, 11, 13))
        tasks['f/b'].depends_on = ('f',)
        with self.assertRaisesRegex(ValueError, 'Цикл зависимостей'):
            self.schedule(tasks)

    def test_unknown_dependency_fails_without_mutating_tasks(self):
        frontend = replace(task('f', 'FE', 1, '', ''), depends_on=('missing',))
        with self.assertRaisesRegex(ValueError, 'неизвестная'):
            self.schedule({'f': frontend})
        self.assertEqual(frontend.actual_start, '')

    def test_quarter_day_qa_shares_one_resource_instead_of_three_full_days(self):
        tasks = {'a/b': replace(task('b', 'BE', 1, '', 'B1'), release_id='REL'),
                 'a/q': replace(task('q', 'QA', .25, '', ''), release_id='REL'),
                 'b/f': replace(task('f', 'FE', 1, '', 'F1'), release_id='REL'),
                 'b/q2': replace(task('q2', 'QA', .25, '', ''), release_id='REL')}
        result = self.schedule(tasks)
        first, second = result['a/q'], result['b/q2']
        self.assertEqual(first.assignee, second.assignee)
        self.assertEqual(first.start, first.finish)
        self.assertEqual(first.finish, second.finish)
        self.assertEqual(sum(amount for _, amount in first.allocations + second.allocations), .5)
        self.assertEqual(first.finish, date(2026, 11, 13))

    def test_qa_never_forecasts_past_work_or_uses_vacation_capacity(self):
        tasks = {'b': replace(task('b', 'BE', 1, '', 'B1'), actual_finish='2026-11-02', status='completed', progress=100),
                 'q': replace(task('q', 'QA', 2, '', 'Q1'), progress=50, actual_start='2026-11-03', status='in-progress')}
        result = self.schedule(tasks, {'Q1': {date(2026, 11, 10)}})['q']
        self.assertEqual(result.start, date(2026, 11, 11))
        self.assertEqual(sum(value for _, value in result.allocations), 1)


class WholeFeatureAllocationTests(unittest.TestCase):
    setUp = test_tracker_release.ReleasePartitionTests.setUp
    calculate = test_tracker_release.ReleasePartitionTests.calculate
    def test_bug_role_can_be_confirmed_after_transfer_between_developers(self):
        (self.project / 'features/owner').mkdir(parents=True)
        evidence = self.project / 'decision.txt'
        evidence.write_text('Сохранить роль FE у дефекта после передачи BE-разработчику.')
        result = {'scope': self.scope, 'issues': [{'sbertrek_key': 'BUG-1',
                  'issue_type': 'bug', 'summary': 'Broken button', 'task_role': 'BE',
                  'role_estimates': {}, 'development': {'state': 'unknown'}}]}
        decision = {'schema_version': 1, 'provider': 'sbertrek', 'release': 'release-1',
                    'analyst_confirmed': True, 'source': {'file': str(evidence),
                    'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    'quote': evidence.read_text()},
                    'tasks': [{'key': 'BUG-1', 'feature': 'owner', 'role': 'FE'}]}
        reviewed = test_tracker_release.apply_scope_decisions(self.project, result, decision)
        self.assertEqual(reviewed['issues'][0]['task_role'], 'FE')
        self.assertEqual(result['issues'][0]['task_role'], 'BE')
        decision['tasks'][0]['role'] = 'QA'
        with self.assertRaisesRegex(ValueError, 'development role'):
            test_tracker_release.apply_scope_decisions(self.project, result, decision)

    def test_new_task_does_not_split_an_old_residual_budget(self):
        first = self.calculate()
        doc = first['document']
        path = self.project / first['path']
        path.parent.mkdir(parents=True)
        remainder = doc['groups'][1]
        self.rows.append({**self.rows[-1], 'task_id': remainder['task_id']})
        self.rows.append({**self.rows[1], 'task_id': 'TASK-4', 'sbertrek_key': 'SBER-4'})
        self.issues.append({'sbertrek_key': 'SBER-4', 'development': {'state': 'unknown'}, 'releases': [{'key': 'release-2'}]})
        # Explicitly reviewed membership change, with the old estimate retained
        # until the algorithm proposes reallocation from the entire feature.
        remainder['members'].append('TASK-4')
        path.write_text(json.dumps(doc))
        self.scope['ids'] = ['release-2']
        result = self.calculate({'SBER-4'})
        allocations = {tuple(group['members']): Decimal(group['estimate']) for group in result['groups']}
        self.assertEqual(allocations[('TASK-1',)], Decimal('2.5'))
        self.assertEqual(allocations[('TASK-4',)], Decimal('2.5'))
        self.assertEqual(allocations[('TASK-2', 'TASK-3')], Decimal('5'))
        self.assertEqual(sum(allocations.values()), Decimal('10'))


class BugViewTests(unittest.TestCase):
    setUp = sources.ActualProgressSourcesTests.setUp
    write_tasks = sources.ActualProgressSourcesTests.write_tasks
    quarter = sources.ActualProgressSourcesTests.quarter
    snapshot = sources.ActualProgressSourcesTests.snapshot

    def add_bug(self, estimate='2'):
        lines = self.registry.read_text().splitlines()
        lines[0] += ' Suit |'
        lines[1] += '---|'
        for index in range(2, len(lines)):
            if lines[index].startswith('|'):
                lines[index] += ' story |'
        lines.append(f'| BUG-1/FE | BUG-1 | Broken button | real | FE | {estimate} | F1 | | | | | not-started | 0 | | bug |')
        self.registry.write_text('\n'.join(lines) + '\n')

    def test_bug_is_only_in_estimates_view_and_its_export(self):
        self.add_bug()
        before = self.registry.read_bytes()
        self.quarter()
        for name in ('actual-progress', 'actual-progress-confluence', 'actual-progress-focus', 'actual-progress-focus-confluence'):
            self.assertNotIn('TASK_BUG_1', (self.gantt / (name + '.puml')).read_text())
        for name in ('actual-progress-estimates-only', 'actual-progress-estimates-only-confluence'):
            self.assertIn('TASK_BUG_1', (self.gantt / (name + '.puml')).read_text())
        self.assertEqual(self.registry.read_bytes(), before)

    def test_unestimated_bug_does_not_change_normal_view_or_invent_estimate(self):
        with patch.object(sources.OVERLAY, 'harness_today', return_value=date(2026, 9, 9)):
            normal = sources.OVERLAY.prepare_outputs(self.root, '2026-Q3', ['cohorts'])
            self.add_bug('')
            self.assertEqual(normal, sources.OVERLAY.prepare_outputs(self.root, '2026-Q3', ['cohorts']))
            with self.assertRaisesRegex(ValueError, 'требует оценки'):
                sources.OVERLAY.prepare_outputs(self.root, '2026-Q3', ['cohorts'], estimates_only=True)


class DefectMetricsTests(unittest.TestCase):
    def test_collected_bug_uses_confirmed_developer_not_title_prefix(self):
        from tracker_workflow import reconcile_data
        run = {'run_id': 'example', 'role_policy': 'prefix-v1', 'scope': {}, 'limitations': [],
               'config': {'status_rules': {'sbertrek': {'completed': [], 'excluded': []}},
                          'participants': {'sbertrek': {'dev': {'role': 'BE', 'team_id': 'B1'}}}},
               'cards': {'sbertrek': [{'key': 'BUG-1', 'issue_type': 'bug',
                   'summary': '[FE] Broken button', 'status': 'created', 'assignee': {'id': 'dev'},
                   'role_estimates': {'BE': {'value': 2, 'unit': 'person-days'},
                                      'QA': {'value': 5, 'unit': 'person-days'}}}]}}
        result = reconcile_data(run)
        self.assertEqual(result['issues'][0]['task_role'], 'BE')
        self.assertEqual(result['skipped'], [])
        self.assertEqual([item['role'] for item in result['work_items']], ['BE'])
        self.assertEqual(result['summary']['role_totals_person_days']['QA'], 0)

    def test_assignments_deduplicate_and_use_quarter_local_time(self):
        at = datetime.fromisoformat
        history = TaskHistory('BUG-1/FE', 'FE', at('2026-10-05T12:00:00+03:00'), 'qa', 'testing', (
            HistoryEvent('one', at('2026-09-30T22:00:00+00:00'), (None, 'dev')),
            HistoryEvent('two', at('2026-10-02T12:00:00+03:00'), ('dev', 'qa')),
            HistoryEvent('three', at('2026-10-03T12:00:00+03:00'), ('qa', 'dev')),
            HistoryEvent('four', at('2026-10-04T12:00:00+03:00'), ('dev', 'qa')),
        ), True)
        fact = assignment_facts('BUG-1', history, {'dev': 'FE', 'qa': 'QA'})
        report = quarterly_defects([fact, fact], '2026-Q4')
        self.assertEqual(report['developers'][0]['assigned_bug_count'], 1)
        self.assertEqual(report['developers'][0]['keys'], ['BUG-1'])
        self.assertEqual(quarterly_defects([fact], '2026-Q3')['developers'], [])

    def test_current_assignee_does_not_invent_assignment_date(self):
        history = TaskHistory('BUG-1', 'BE', datetime.fromisoformat('2026-10-05T12:00:00+03:00'),
                              'dev', 'created', (), False)
        report = quarterly_defects([assignment_facts('BUG-1', history, {'dev': 'BE'})], '2026-Q4')
        self.assertEqual(report['developers'][0]['assigned_bug_count'], 0)
        self.assertEqual(report['developers'][0]['undated_keys'], ['BUG-1'])
        self.assertTrue(report['limitations'])


class BugHistoryWorkflowTests(unittest.TestCase):
    fixture = test_tracker_history.AdaptiveHistoryTests
    setUp = fixture.setUp
    run_tool = fixture.run_tool
    configure = fixture.configure
    write = fixture.write
    registry = fixture.registry
    source_issue = fixture.source_issue
    git = fixture.git
    save_sources = fixture.save_sources
    snapshot = fixture.snapshot
    begin = fixture.begin
    ingest = fixture.ingest
    call = fixture.call
    raw_history = fixture.raw_history
    manifest = fixture.manifest

    def test_bug_history_produces_metric_and_does_not_require_feature_qa_update(self):
        self.registry(self.project, 'owner', ['| CORE | SBER-1 | Broken button | real | BE | done |'])
        path = self.project / 'features/owner/execution/tasks.md'
        lines = path.read_text().splitlines()
        table = [index for index, line in enumerate(lines) if line.startswith('|')]
        for offset, index in enumerate(table):
            lines[index] += ' Suit |' if offset == 0 else '---|' if offset == 1 else ' bug |'
        path.write_text('\n'.join(lines) + '\n')
        self.save_sources()
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'tasks', '--scope-provider', 'sbertrek',
                            '--scope-id', 'SBER-1', '--scope-id', 'SBER-8191', '--label', 'Test',
                            '--scope-source', 'analyst', '--intent', 'update-planning')
        card = self.source_issue('SBER-1', summary='Broken button', roles={'BE': 2})
        del card['fields']['issuetype']
        card['suit'] = 'bug'
        architecture_bug = self.source_issue('SBER-8191', summary='Architecture defect')
        del architecture_bug['fields']['issuetype']
        architecture_bug['suit'] = 'bug'
        architecture_bug['fields']['assignee'] = None
        run = self.ingest(run, {'issues': [card, architecture_bug]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        manifest = self.manifest()
        proof = self.state / 'bug-decision.md'
        proof.write_text('Сохранить принятый факт дефекта. SBER-8191 не учитывать: архитектурный дефект.')
        source = {'file': str(proof), 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest(),
                  'quote': 'Сохранить принятый факт дефекта. SBER-8191 не учитывать: архитектурный дефект.'}
        manifest['scope_decisions'] = {'schema_version': 1, 'provider': 'sbertrek',
            'run_id': run['run_id'], 'scope': run['scope'], 'analyst_confirmed': True,
            'source': source, 'tasks': [{'key': 'SBER-8191',
                'action': 'exclude-from-execution', 'reason': 'Architecture defect outside execution'}]}
        manifest['development_decision'] = {'kind': 'keep-current', 'analyst_confirmed': True,
            'source': {'file': str(proof), 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest(),
                       'quote': 'Сохранить принятый факт дефекта. SBER-8191 не учитывать: архитектурный дефект.'}}
        manifest_path = self.write(self.state / 'bug-manifest.json', manifest)
        review = self.run_tool(self.state, 'history-review', '--run-id', run['run_id'],
                              '--project-root', str(self.project), '--manifest', str(manifest_path))
        self.assertEqual(review['status'], 'history-review-ready')
        self.assertEqual(review['skipped'][0]['sbertrek_key'], 'SBER-8191')
        self.assertEqual(review['skipped'][0]['reason'], 'analyst-excluded-from-execution')
        self.assertEqual(review['qa_application_blockers'], [])
        self.assertEqual([row['role'] for row in review['comparison']['rows']], ['BE'])
        self.assertEqual(review['comparison']['rows'][0]['suit'], 'bug')
        self.assertEqual(review['defect_assignments'][0]['assignments'][0]['developer'], 'dev')
        self.assertEqual(review['features'][0]['qa_task_keys'], [])
        self.run_tool(self.state, 'qa-application-check', '--project-root', str(self.project),
                      '--review-file', review['review_file'])
