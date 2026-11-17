from __future__ import annotations

import copy
import hashlib
import json
import unittest

import test_tracker_history


class NarrowTaskReviewTests(unittest.TestCase):
    setUp = test_tracker_history.AdaptiveHistoryTests.setUp
    run_tool = test_tracker_history.AdaptiveHistoryTests.run_tool
    configure = test_tracker_history.AdaptiveHistoryTests.configure
    write = test_tracker_history.AdaptiveHistoryTests.write
    registry = test_tracker_history.AdaptiveHistoryTests.registry
    source_issue = test_tracker_history.AdaptiveHistoryTests.source_issue
    git = test_tracker_history.AdaptiveHistoryTests.git
    save_sources = test_tracker_history.AdaptiveHistoryTests.save_sources
    call = test_tracker_history.AdaptiveHistoryTests.call
    begin = test_tracker_history.AdaptiveHistoryTests.begin
    ingest = test_tracker_history.AdaptiveHistoryTests.ingest
    raw_history = test_tracker_history.AdaptiveHistoryTests.raw_history
    manifest = test_tracker_history.AdaptiveHistoryTests.manifest

    def prepare(self):
        self.registry(self.project, 'owner', [
            f'| TASK-{index} | SBER-{index} | - | real | BE | done |'
            for index in range(1, 20)])
        registry = self.project / 'features/owner/execution/tasks.md'
        lines = registry.read_text().splitlines()
        lines = [line + (' Actual Finish |' if 'Task ID' in line else ' --- |' if line.startswith('|---')
                         else ' 2026-08-02 |' if 'TASK-1 |' in line else ' 2026-08-01 |')
                 if line.startswith('|') else line for line in lines]
        registry.write_text('\n'.join(lines) + '\n')
        self.save_sources()
        run = self.run_tool(self.state, 'begin', '--scope-kind', 'tasks', '--scope-provider', 'sbertrek',
                            '--scope-id', 'SBER-1', '--label', 'One selected task', '--scope-source', 'analyst',
                            '--intent', 'update-planning', '--include-history')
        self.ingest(run, {'issues': [self.source_issue('SBER-1', roles={'BE': 2})]})
        self.run_tool(self.state, 'reconcile', '--run-id', run['run_id'])
        self.run_id = run['run_id']
        self.data = self.manifest()
        entry = self.data['responses'][0]
        raw = self.raw_history()
        raw.update(start=0, total=3)
        path = self.write(self.state / 'complete-1.json', raw)
        entry.update(response_file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     mapping={**entry['mapping'], 'start': '/start', 'total': '/total'})
        self.original_entry = copy.deepcopy(entry)
        self.keys = [f'SBER-{index}' for index in range(2, 20)]

    def supplement(self):
        cards = self.write(self.state / 'remainder-cards.json', {'issues': [
            self.source_issue(key, roles={'BE': 2}) for key in self.keys]})
        self.data['supplemental_responses'] = [{
            'keys': self.keys, 'response_file': str(cards),
            'sha256': hashlib.sha256(cards.read_bytes()).hexdigest(),
            'call': self.call('sbertrek', ' or '.join(f'unit = "{key}"' for key in self.keys))}]
        for key in self.keys:
            raw = self.raw_history()
            raw.update(key=key, start=0, total=3)
            response = self.write(self.state / (key + '-history.json'), raw)
            entry = copy.deepcopy(self.original_entry)
            entry.update(key=key, response_file=str(response),
                         sha256=hashlib.sha256(response.read_bytes()).hexdigest())
            self.data['responses'].append(entry)

    def review(self, expected=0):
        path = self.write(self.state / 'review-manifest.json', self.data)
        return self.run_tool(self.state, 'history-review', '--run-id', self.run_id,
                             '--project-root', str(self.project), '--manifest', str(path), expected=expected)

    def test_one_of_nineteen_tasks_recovers_same_run_without_broadening_application(self):
        self.prepare()
        root = self.state / 'tracker-runs' / self.run_id
        immutable = {name: (root / name).read_bytes() for name in
                     ('run.json', 'reconciled.json', 'completion-status.json', 'report.md')}
        registry = self.project / 'features/owner/execution/tasks.md'
        before = registry.read_bytes()
        preview = self.run_tool(self.state, 'execution-preview', '--run-id', self.run_id,
                                '--project-root', str(self.project), '--feature', 'owner')
        self.assertEqual(preview['next_action']['type'], 'collect-feature-cards')
        self.assertEqual(set(preview['next_action']['keys']), set(self.keys))
        pending = self.review()
        self.assertEqual(pending['next_action']['type'], 'collect-feature-cards')
        self.assertEqual(set(pending['pending_history']), {key + '/BE' for key in self.keys})
        self.supplement()
        review = self.review()
        self.assertEqual(review['status'], 'history-review-ready')
        self.assertEqual(review['pending_history'], [])
        self.assertEqual(review['qa_application_blockers'], [])
        self.assertEqual(len(review['evidence']), 19)
        self.assertEqual(review['comparison']['qa_start_from_current_execution']['owner']['started_on'], '2026-08-01')
        self.assertEqual(len(review['features'][0]['tasks']), 19)
        self.assertEqual([row['task_id'] for row in review['comparison']['rows'] if row['role'] == 'BE'], ['TASK-1'])
        self.assertEqual(review['application_scope']['development_keys'], ['SBER-1'])
        self.assertEqual(len(review['application_scope']['protected_rows']), 18)
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(self.review()['review_file'], review['review_file'])
        for name, content in immutable.items():
            self.assertEqual((root / name).read_bytes(), content, name)
        # Full QA evidence is not authorization to apply QA or to edit the remainder.
        self.run_tool(self.state, 'qa-application-check', '--project-root', str(self.project),
                      '--review-file', review['review_file'], expected=2)
        evidence = self.state / 'decision.txt'
        evidence.write_text('Apply selected development only')
        self.data['development_decision'] = {
            'kind': 'accept-dates-and-statuses', 'analyst_confirmed': True,
            'source': {'file': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
                       'quote': evidence.read_text()}}
        approved = self.review()
        self.assertEqual([item['task_id'] for item in approved['development_application']], ['TASK-1'])
        from tracker_registry import check_protected_rows
        registry.write_text(registry.read_text().replace('| TASK-2 | SBER-2 | - | real | BE | done |',
                                                        '| TASK-2 | SBER-2 | - | real | BE | planned |'))
        self.assertTrue(check_protected_rows(self.project, approved['application_scope']['protected_rows']))

    def test_remainder_history_still_requires_completeness_and_exact_card_keys(self):
        self.prepare()
        self.supplement()
        complete = copy.deepcopy(self.data)
        self.data['responses'] = self.data['responses'][:1]
        self.assertEqual(len(self.review()['pending_history']), 18)
        self.data = copy.deepcopy(complete)
        self.data['supplemental_responses'][0]['keys'] = ['SBER-999']
        self.review(expected=2)
        self.data = copy.deepcopy(complete)
        self.data['supplemental_responses'][0]['sha256'] = '0' * 64
        self.review(expected=2)
        self.data = complete
        # A fresh cancelled protected task must not silently disappear from QA.
        cards = self.data['supplemental_responses'][0]
        from pathlib import Path
        path = Path(cards['response_file'])
        payload = json.loads(path.read_text())
        payload['issues'][0]['fields']['status'] = {'name': 'cancelled'}
        path = self.write(self.state / 'fresh-remainder-cards.json', payload)
        cards['response_file'] = str(path)
        cards['call']['captured_at'] = '2026-08-10T12:01:00+03:00'
        cards['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        review = self.review()
        self.assertIn('fresh-state-conflicts-with-protected-execution',
                      {item['reason'] for item in review['qa_application_blockers']})


if __name__ == '__main__':
    unittest.main()
