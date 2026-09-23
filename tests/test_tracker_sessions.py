from __future__ import annotations

import json
from pathlib import Path
import unittest

import test_tracker_release


class TrackerSessionTests(unittest.TestCase):
    setUp = test_tracker_release.ReleaseWorkflowTests.setUp
    run_tool = test_tracker_release.ReleaseWorkflowTests.run_tool
    configure = test_tracker_release.ReleaseWorkflowTests.configure
    git = test_tracker_release.ReleaseWorkflowTests.git
    write = test_tracker_release.ReleaseWorkflowTests.write
    jira_issue = test_tracker_release.ReleaseWorkflowTests.jira_issue
    call = test_tracker_release.ReleaseWorkflowTests.call
    ingest = test_tracker_release.ReleaseWorkflowTests.ingest

    def begin(self, release):
        return self.run_tool(self.state, 'begin', '--scope-kind', 'release', '--scope-provider', 'jira',
                             '--scope-id', release, '--label', release, '--scope-source', 'analyst',
                             '--intent', 'update-planning')

    def reconcile(self, release):
        run = self.begin(release)
        card = self.jira_issue('JIRA-1')
        card['fields']['fixVersions'] = [{'name': release}]
        self.ingest(run, {'issues': [card]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        return run['run_id']

    def finish_record(self, run_id):
        self.write(self.state / 'collaboration.json', {'active_work': None, 'completed_work': [{
            'status': 'merged', 'branch': 'feature/owner/analyst', 'origin_main': 'a' * 40,
            'execution_scope': {'run_ids': [run_id]}, 'last_saved_commit': 'b' * 40}]})

    def resume(self, release, *extra, expected=0):
        return self.run_tool(self.state, 'resume', '--scope-kind', 'release', '--scope-provider', 'jira',
                             '--scope-id', release, '--intent', 'update-planning', *extra, expected=expected)

    def state_bytes(self):
        return {str(path.relative_to(self.state)): path.read_bytes() for path in self.state.rglob('*') if path.is_file()}

    def test_finished_release_cannot_replace_new_request_after_reconciliation(self):
        old = self.reconcile('REL-1')
        self.finish_record(old)
        current = self.reconcile('REL-2')
        before = self.state_bytes()
        restored = self.resume('REL-2')
        self.assertEqual(restored['run_id'], current)
        self.assertEqual(restored['scope']['ids'], ['REL-2'])
        self.assertEqual(Path(restored['run_json']), self.state / 'tracker-runs' / current / 'run.json')
        self.assertEqual(restored['next_action']['type'], 'result-status')
        self.assertTrue(restored['planning_application_allowed'])
        self.assertEqual(self.resume('REL-1', expected=2)['archived'][0]['state'], 'applied')
        self.resume('REL-2', '--run-id', old, expected=2)
        completed = self.run_tool(self.state, 'result-status', '--run-id', old)
        self.assertFalse(completed['planning_application_allowed'])
        self.assertTrue(completed['planning_update']['actualization_complete'])
        self.assertIsNone(completed['planning_update']['next_action'])
        self.run_tool(self.state, 'application-state', '--run-id', old, '--state', 'pending',
                      '--reason', 'Try reopening', '--analyst-confirmed', expected=2)
        self.run_tool(self.state, 'release-preview', '--run-id', old, '--project-root', str(self.project), expected=2)
        self.assertEqual(self.state_bytes(), before)

    def test_fresh_same_release_ignores_applied_archive(self):
        old = self.reconcile('REL-1')
        self.finish_record(old)
        current = self.begin('REL-1')
        restored = self.resume('REL-1')
        self.assertEqual(restored['run_id'], current['run_id'])
        self.assertEqual(restored['next_action']['type'], 'collect-tracker-data')

    def test_ambiguity_requires_exact_run_not_latest(self):
        first = self.reconcile('REL-1')
        second = self.reconcile('REL-1')
        before = self.state_bytes()
        ambiguous = self.resume('REL-1', expected=2)
        self.assertEqual(ambiguous['status'], 'tracker-resume-ambiguous')
        self.assertEqual({item['run_id'] for item in ambiguous['candidates']}, {first, second})
        self.assertEqual(self.resume('REL-1', '--run-id', first)['run_id'], first)
        self.assertEqual(self.resume('REL-1', '--run-id', second)['run_id'], second)
        self.assertEqual(self.state_bytes(), before)

    def test_pause_missing_run_and_config_gate_do_not_start_or_unpause(self):
        current = self.reconcile('REL-1')
        self.run_tool(self.state, 'application-state', '--run-id', current, '--state', 'paused',
                      '--reason', 'Analyst pause', '--analyst-confirmed')
        before = self.state_bytes()
        resumed = self.resume('REL-1')
        self.assertEqual(resumed['application_state']['state'], 'paused')
        self.assertFalse(resumed['planning_application_allowed'])
        self.resume('REL-2', expected=2)
        self.assertEqual(self.state_bytes(), before)
        config = json.loads((self.state / 'tracker-config.json').read_text())
        config['setup_complete'] = False
        self.write(self.state / 'tracker-config.json', config)
        gate = self.resume('REL-1', expected=3)
        self.assertTrue(gate['must_stop'])
        self.assertIn('next_question', gate)

    def test_release_begin_requires_explicit_intent_without_creating_run(self):
        before = self.state_bytes()
        self.run_tool(self.state, 'begin', '--scope-kind', 'release', '--scope-provider', 'jira',
                      '--scope-id', 'REL-1', '--label', 'Release', '--scope-source', 'analyst', expected=2)
        self.assertEqual(self.state_bytes(), before)

    def test_completion_index_survives_pruned_archive(self):
        current = self.reconcile('REL-1')
        self.write(self.state / 'collaboration.json', {'active_work': None, 'completed_work': [],
                   'completed_execution_runs': {current: {'state': 'applied', 'evidence': 'collaboration-finish'}}})
        result = self.run_tool(self.state, 'result-status', '--run-id', current)
        self.assertTrue(result['planning_update']['actualization_complete'])
        self.resume('REL-1', expected=2)


if __name__ == '__main__':
    unittest.main()
