from __future__ import annotations

import shutil
import unittest

import test_release_actualization_e2e as release


class DeliveryActualizationTests(unittest.TestCase):
    setUp = release.ReleaseActualizationEndToEndTests.setUp
    prepare_lab = release.ReleaseActualizationEndToEndTests.prepare_lab
    prepare_workspace = release.ReleaseActualizationEndToEndTests.prepare_workspace
    create_seed = release.ReleaseActualizationEndToEndTests.create_seed
    configure_identity = release.ReleaseActualizationEndToEndTests.configure_identity
    git = release.ReleaseActualizationEndToEndTests.git
    collaboration = release.ReleaseActualizationEndToEndTests.collaboration
    run_tool = release.ReleaseActualizationEndToEndTests.run_tool
    write = release.ReleaseActualizationEndToEndTests.write
    sber_issue = release.ReleaseActualizationEndToEndTests.sber_issue
    call = release.ReleaseActualizationEndToEndTests.call
    ingest = release.ReleaseActualizationEndToEndTests.ingest
    collab = release.ReleaseActualizationEndToEndTests.collab
    command = release.ReleaseActualizationEndToEndTests.command
    row = release.ReleaseActualizationEndToEndTests.row
    write_registry = release.ReleaseActualizationEndToEndTests.write_registry
    prepare_sources = release.ReleaseActualizationEndToEndTests.prepare_sources
    evidence = release.ReleaseActualizationEndToEndTests.evidence
    history_entry = release.ReleaseActualizationEndToEndTests.history_entry
    qa_check = release.ReleaseActualizationEndToEndTests.qa_check
    generate = release.ReleaseActualizationEndToEndTests.generate
    snapshot = release.ReleaseActualizationEndToEndTests.snapshot

    def test_two_deliveries_review_apply_generate_and_save_without_collecting_neighbor(self):
        bindings = {}
        for key, feature, delivery, quarter in (
            ('registry', 'registry', 'mvp', '2026-Q4'),
            ('other', 'prevalidation', 'mvp', '2026-Q4'),
            ('kib', 'kib', 'mvp', '2026-Q3'),
            ('kib-next', 'kib', 'post-mvp', '2026-Q4'),
        ):
            relative = f'quarters/{quarter}/features/{feature}/deliveries/{delivery}'
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if key in ('registry', 'other'):
                shutil.move(str(self.project / 'features' / key), target)
            else:
                shutil.copytree(self.project / bindings['other']['path'], target)
                for path in target.rglob('*.md'):
                    path.write_text(path.read_text().replace('LAB-301', 'LAB-401' if key == 'kib' else 'LAB-501')
                                    .replace('QA-OTHER', 'QA-' + key.upper()).replace('STORY-OTHER', 'STORY-' + key.upper()))
                (target / 'feature.md').write_text('# FEATURE-KIB — КИБ\n')
                self.write(target / 'execution/qa-groups.json', {
                    'schema_version': 1, 'provider': 'sbertrek', 'total_estimate': '1', 'groups': [
                        {'task_id': 'QA-' + key.upper(), 'members': ['LAB-401/FE' if key == 'kib' else 'LAB-501/FE'],
                         'release': None, 'estimate': '1'}]})
            bindings[key] = {'feature_id': feature, 'delivery_id': delivery, 'quarter': quarter, 'path': relative}
        self.write(self.project / 'delivery-index.json', {'schema_version': 1, 'deliveries': bindings})
        new_gantt = self.project / 'quarters/2026-Q4/gantt'
        shutil.move(str(self.gantt), new_gantt)
        self.gantt = new_gantt
        self.registry = self.project / bindings['registry']['path'] / 'execution/tasks.md'
        self.other_registry = self.project / bindings['other']['path'] / 'execution/tasks.md'
        self.groups_path = self.registry.with_name('qa-groups.json')
        self.write(self.groups_path, self.expected['groups'])
        self.rows['QA-REGISTRY']['Estimate (дн)'] = '7.2'
        self.rows['QA-REST']['Estimate (дн)'] = '1.8'
        self.write_registry(self.registry, self.rows.values())
        (self.gantt / 'includes/actual-progress/FEATURE-kib-next.puml').write_text("' existing KIB\n")
        self.git(self.project, 'add', '--all')
        self.git(self.project, 'commit', '-m', 'Prepare quarterly deliveries')
        before = self.snapshot(self.project)
        self.run_tool(self.state, 'config-status')
        run = self.run_tool(self.state, 'begin', '--adaptive', '--scope-kind', 'epic',
                            '--scope-provider', 'sbertrek', '--scope-id', 'LAB-900',
                            '--label', 'Two deliveries', '--scope-source', 'analyst', '--intent', 'update-planning')
        tasks = self.expected['members'] + self.expected['remainder'] + ['LAB-301/FE']
        cards = [self.sber_issue(task.split('/')[0], summary=f'[{task.split("/")[1]}] Delivery task') for task in tasks]
        self.ingest(run, {'issues': cards}, self.call('sbertrek', run['next_action']['selection']['query_semantics']))
        self.run_id = run['run_id']
        self.run_tool(self.state, 'reconcile', '--run-id', self.run_id)
        self.collab('set-execution-scope', '--delivery', 'registry', '--delivery', 'other', '--quarter', '2026-Q4',
                    '--run-id', self.run_id, '--reason', 'Update two deliveries and retain KIB', '--analyst-confirmed')
        preview = self.run_tool(self.state, 'execution-preview', '--run-id', self.run_id,
                                '--project-root', str(self.project), '--quarter', '2026-Q4')
        self.assertEqual(set(preview['selected_features']), {'registry', 'other'})
        self.assertTrue(preview['ownership_ready'])
        explicit = self.run_tool(self.state, 'execution-preview', '--run-id', self.run_id,
                                 '--project-root', str(self.project), '--delivery', 'registry', '--delivery', 'other')
        self.assertEqual(explicit['selected_deliveries'], preview['selected_deliveries'])
        excluded = self.run_tool(self.state, 'execution-preview', '--run-id', self.run_id,
                                 '--project-root', str(self.project), '--delivery', 'registry')
        self.assertFalse(excluded['ownership_ready'])
        self.assertIn('owner-outside-selected-scope', {entry['reason'] for entry in excluded['blockers']})
        self.run_tool(self.state, 'execution-preview', '--run-id', self.run_id, '--project-root', str(self.project),
                      '--quarter', '2026-Q4', '--delivery', 'kib', expected=2)
        self.decision = self.inputs / 'delivery-decision.txt'
        self.decision.write_text('Accept development facts for registry and prevalidation; retain all QA fields and KIB.')
        manifest = {'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'Delivery fixture decision',
                    'quarter': '2026-Q4', 'deliveries': ['registry', 'other'], 'provider': 'sbertrek',
                    'participants': {'dev': 'BE', 'qa': 'QA'}, 'status_rules': {'not_started': ['created']},
                    'responses': [self.history_entry(task) for task in tasks], 'qa_confirmations': [
                        {'feature': key, 'task_id': task, 'kind': 'keep-current', 'fields': {},
                         'analyst_confirmed': True, 'source': self.evidence(self.decision)}
                        for key, task in (('registry', 'QA-REGISTRY'), ('registry', 'QA-REST'), ('other', 'QA-OTHER'))]}
        path = self.write(self.inputs / 'deliveries.json', manifest)
        manifest['development_decision'] = {'kind': 'accept-dates-and-statuses',
            'analyst_confirmed': True, 'source': self.evidence(self.decision)}
        self.write(path, manifest)
        review = self.run_tool(self.state, 'history-review', '--run-id', self.run_id,
                               '--project-root', str(self.project), '--manifest', str(path))
        self.assertEqual(review['qa_application_blockers'], [])
        self.generation_review = review['review_file']
        self.assertEqual(review['pending_history'], [])
        self.assertEqual(review['application_scope']['kind'], 'deliveries-only')
        self.assertEqual({row['feature'] for row in review['comparison']['rows']}, {'registry', 'other'})
        for key in ('registry', 'other'):
            self.run_tool(self.state, 'application-preflight', '--project-root', str(self.project), '--delivery', key)
        for task in self.expected['members'] + self.expected['remainder']:
            self.rows[task].update(self.expected['development']['finish_only' if task == 'LAB-112/BE' else 'regular'])
        self.write_registry(self.registry, self.rows.values())
        other = self.other_registry.read_text()
        self.other_registry.write_text(other.replace('| - | - | planned | 0 |', '| 2026-08-02 | 2026-08-03 | completed | 100 |'))
        self.assertEqual(self.qa_check(review)['status'], 'qa-application-verified')
        neighbor = self.project / bindings['kib-next']['path'] / 'execution/tasks.md'
        neighbor_bytes = neighbor.read_bytes()
        neighbor.write_text(neighbor.read_text().replace('| planned |', '| unknown |'))
        self.assertEqual(self.qa_check(review, expected=2)['status'], 'qa-application-mismatch')
        neighbor.write_bytes(neighbor_bytes)
        self.generate()
        export = (self.gantt / 'actual-progress-confluence.puml').read_text()
        self.assertIn('КИБ — поставка post-mvp (2026-Q4)', export)
        self.assertNotIn('TASK_LAB_401', export)
        self.assertIn('TASK_LAB_501', export)
        self.assertFalse((self.gantt / 'includes/actual-progress/FEATURE-kib.puml').exists())
        saved = self.collab('save-preview', '--review-file', review['review_file'])
        self.assertEqual(saved['qa_verified_runs'], [self.run_id])
        self.assertEqual(saved['confluence_verified'], ['2026-Q4'])
        for relative, content in before.items():
            if '/features/kib/' in relative or relative.endswith(('quarter-plan.puml', 'commander-plan.puml')):
                self.assertEqual((self.project / relative).read_bytes(), content, relative)
        index = self.project / 'delivery-index.json'
        index.write_bytes(index.read_bytes() + b'\n')
        self.assertIn('Delivery index changed', self.qa_check(review, expected=2)['error'])


if __name__ == '__main__':
    unittest.main()
