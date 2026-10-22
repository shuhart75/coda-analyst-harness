from pathlib import Path
import hashlib
import json
import unittest
import test_tracker_history as fixtures


class HistoryReportingTests(unittest.TestCase):
    for _name in ('run_tool', 'configure', 'write', 'registry', 'source_issue', 'setUp', 'git',
                  'save_sources', 'snapshot', 'call', 'begin', 'ingest', 'reconciled', 'raw_history', 'manifest'):
        locals()[_name] = getattr(fixtures.AdaptiveHistoryTests, _name)

    def test_explicit_history_request_continues_and_reports_without_role_approval(self):
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'tasks', '--scope-id', 'SBER-1',
                            '--label', 'History', '--scope-source', 'analyst', '--intent', 'read-only', '--include-history')
        self.ingest(run, {'issues': [self.source_issue('SBER-1', roles={'BE': 2})]})
        result = self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        self.assertFalse(result['final_response_allowed'])
        self.assertEqual(result['next_action']['type'], 'collect-history')
        root = self.state / 'tracker-runs' / run['run_id']
        before = (root / 'reconciled.json').read_bytes()
        manifest = self.manifest()
        manifest.pop('analyst_confirmed'); manifest.pop('participants'); manifest.pop('status_rules')
        entry = manifest['responses'][0]
        raw = self.raw_history();raw['total'] = 3; raw['start'] = 0
        raw.pop('status'); raw.pop('assignee')
        response = self.write(self.state / 'dated-events.json', raw)
        entry.update(response_file=str(response), sha256=hashlib.sha256(response.read_bytes()).hexdigest())
        entry['mapping'].update(total='/total', start='/start')
        path = self.write(self.state / 'report-manifest.json', manifest)
        report = self.run_tool(self.state, 'history-report', '--run-id', run['run_id'], '--manifest', str(path))
        self.assertEqual(report['status'], 'history-report-ready')
        self.assertEqual(len(report['histories'][0]['events']), 3)
        self.assertFalse(report['planning_application_allowed'])
        result = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'])
        self.assertTrue(result['final_response_allowed'])
        self.assertIn('История получена', result['response_contract']['text'])
        self.assertEqual(before, (root / 'reconciled.json').read_bytes())
        response.write_text('{}')
        error = self.run_tool(self.state, 'result-status', '--run-id', run['run_id'], expected=2)
        self.assertIn('history source changed', str(error))

    def test_missing_history_is_not_a_successful_report(self):
        run_id = self.reconciled('read-only')
        path = self.write(self.state / 'empty.json', {'schema_version': 1, 'provider': 'sbertrek', 'responses': []})
        result = self.run_tool(self.state, 'history-report', '--run-id', run_id, '--manifest', str(path))
        self.assertEqual(result['pending_history'], ['SBER-1'])
        self.assertEqual(result['next_action']['type'], 'collect-history')

    def test_history_review_is_visible_without_rewriting_collection(self):
        run_id = self.reconciled()
        path = self.write(self.state / 'review.json', self.manifest())
        review = self.run_tool(self.state, 'history-review', '--run-id', run_id,
                               '--project-root', str(self.project), '--manifest', str(path))
        result = self.run_tool(self.state, 'result-status', '--run-id', run_id)
        self.assertEqual(result['history_status']['current']['file'], review['review_file'])
        self.assertEqual(result['history_status']['current']['keys'], ['SBER-1'])
        self.assertIn('не описывает текущее состояние', result['response_contract']['text'])

    def test_changed_qa_meaning_requires_explicit_exception(self):
        run_id = self.reconciled()
        manifest = self.manifest()
        manifest['status_rules']['development_completed'] = ['ready_for_uat']
        path = self.write(self.state / 'review.json', manifest)
        error = self.run_tool(self.state, 'history-review', '--run-id', run_id,
                              '--project-root', str(self.project), '--manifest', str(path), expected=2)
        self.assertIn('qa_completion_override', str(error))
        manifest['qa_completion_override'] = {'analyst_confirmed': True, 'reason': 'Specific workflow',
                                               'source': 'analyst decision', 'quote': 'QA ends only at done here'}
        path = self.write(path, manifest)
        result = self.run_tool(self.state, 'history-review', '--run-id', run_id,
                               '--project-root', str(self.project), '--manifest', str(path))
        self.assertEqual(result['qa_completion_override'], manifest['qa_completion_override'])
