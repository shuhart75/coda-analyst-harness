"""Delivery-aware exchange contracts across immutable historical transport keys."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_requirements_exchange as fixtures

sys.path.insert(0, str(fixtures.ROOT / 'scripts'))
import layoutctl
import project_layout


class DeliveryExchangeTests(unittest.TestCase):
    prepare_project = fixtures.RequirementsExchangeTests.prepare_project
    command = fixtures.RequirementsExchangeTests.command
    authorize = fixtures.RequirementsExchangeTests.authorize
    prepare = fixtures.RequirementsExchangeTests.prepare
    write_receipt = fixtures.RequirementsExchangeTests.write_receipt
    result_review_fixture = fixtures.RequirementsExchangeTests.result_review_fixture
    record_review = fixtures.RequirementsExchangeTests.record_review
    git = fixtures.RequirementsExchangeTests.git
    prepare_code_repository = fixtures.RequirementsExchangeTests.prepare_code_repository

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.project, self.q3_prepared, review_path, review = self.result_review_fixture(self.parent)
        self.state_command('mark-published', 'demo', '--manifest', self.q3_prepared['manifest'],
                           '--revision', '1', '--destination-role', 'analytics')
        recorded = self.record_review(self.project, review_path, review)
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.state_command('close-stage', 'demo', '--return-id', review['return_id'],
                           '--note', 'Complete explicitly reviewed historical scope', '--analyst-confirmed')
        self.q4 = self.project / 'features/demo-q4'
        self.q4.mkdir()
        self.q4_requirements = fixtures.requirements().replace('Функциональность: `demo`', 'Функциональность: `demo-q4`')
        (self.q4 / 'requirements.md').write_text(self.q4_requirements)
        self.migrated = False

    def state_command(self, action, feature, *args, success=True):
        result = fixtures.run(sys.executable, str(fixtures.STATE_SCRIPT), action, str(self.project), feature, *args)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def migrate(self, register=True):
        self.git(self.project, 'init', '-b', 'feature/delivery-exchange')
        self.git(self.project, 'config', 'user.name', 'Exchange tests')
        self.git(self.project, 'config', 'user.email', 'exchange@example.invalid')
        self.git(self.project, 'add', '--all')
        self.git(self.project, 'commit', '-m', 'Preserve historical exchange fixture')
        assignments = {'demo': {'feature_id': 'demo', 'delivery_id': 'mvp', 'quarter': '2026-Q3'},
                       'demo-q4': {'feature_id': 'demo', 'delivery_id': 'expansion', 'quarter': '2026-Q4'}}
        layoutctl.apply(self.project, layoutctl.plan(self.project, assignments), True)
        self.q4 = project_layout.feature_root(self.project, 'demo-q4')
        self.q3 = project_layout.feature_root(self.project, 'demo')
        self.migrated = True
        if register:
            fixtures.register_stage(self.project, 'demo-q4')

    def binding(self):
        return {'feature_id': 'demo', 'delivery_id': 'expansion', 'quarter': '2026-Q4', 'delivery_key': 'demo-q4'}

    def prepare_q4(self):
        self.authorize(self.project, 'demo-q4')
        return self.command('prepare', str(self.project), 'demo-q4', '--analyst', 'ivan')

    def q3_snapshot(self):
        roots = [self.project / 'requirements-exchange/demo', self.q3]
        return {str(path): path.read_bytes() for root in roots for path in root.rglob('*') if path.is_file()}

    def test_q3_immutable_q4_two_revisions_keep_transport_and_business_identity(self):
        self.migrate()
        before = self.q3_snapshot()
        first = self.prepare_q4()
        first_path = Path(first['requirements'])
        original = first_path.read_bytes()
        manifest = json.loads(Path(first['manifest']).read_text())
        self.assertEqual(manifest['schema_version'], 4)
        self.assertEqual(manifest['feature'], 'demo-q4')
        self.assertEqual(manifest['delivery_binding'], self.binding())
        self.assertEqual(manifest['revisions'][0]['delivery_binding'], self.binding())
        (self.q4 / 'requirements.md').write_text(self.q4_requirements.replace('показать результат.', 'показать обновлённый результат.'))
        self.state_command('record-change', 'demo-q4', '--origin', 'analyst')
        second = self.prepare_q4()
        self.assertEqual(second['revision'], 2)
        self.assertEqual(first_path.read_bytes(), original)
        second_manifest = json.loads(Path(second['manifest']).read_text())
        self.assertEqual(second_manifest['revisions'][1]['delivery_binding'], self.binding())
        self.assertEqual(before, self.q3_snapshot())
        self.assertEqual(Path(second['manifest']).parent, self.project / 'requirements-exchange/demo-q4')

    def test_mark_published_and_return_scan_route_to_delivery_state(self):
        self.migrate()
        before = self.q3_snapshot()
        prepared = self.prepare_q4()
        self.state_command('mark-published', 'demo-q4', '--manifest', prepared['manifest'],
                           '--revision', str(prepared['revision']), '--destination-role', 'analytics')
        receipt = self.write_receipt(prepared)
        value = json.loads(receipt.read_text())
        value['feature'] = 'demo-q4'
        receipt.write_text(json.dumps(value))
        tasks = receipt.parent / 'tasks.md'
        tasks.write_text('# Returned tasks for the Q4 delivery\n')
        scan = self.command('scan', str(self.project), '--analyst', 'ivan')
        item = next(item for item in scan['items'] if item['feature'] == 'demo-q4')
        returned = next(entry for entry in item['new_returns'] if entry['relative_path'].endswith('tasks.md'))
        self.assertTrue(returned['return_id'].startswith('demo-q4:001:'))
        self.command('record-processed', str(self.project), 'demo-q4', '--return-id', returned['return_id'],
                     '--decision', 'no-change', '--analyst', 'ivan')
        state = json.loads((self.q4 / 'development-results-state.json').read_text())
        self.assertEqual(state['feature'], 'demo-q4')
        self.assertIn(returned['return_id'], json.dumps(state))
        self.assertEqual(before, self.q3_snapshot())
        published = json.loads((self.q4 / 'requirements-state.json').read_text())['last_published']
        self.assertEqual(published['revision'], 1)

    def test_changed_quarter_after_audit_rejects_prepare_without_writes(self):
        self.migrate()
        self.authorize(self.project, 'demo-q4')
        index_path = self.project / 'delivery-index.json'
        index = json.loads(index_path.read_text())
        index['deliveries']['demo-q4']['quarter'] = '2027-Q1'
        index_path.write_text(json.dumps(index))
        result = fixtures.run(sys.executable, str(fixtures.SCRIPT), 'prepare', str(self.project), 'demo-q4', '--analyst', 'ivan')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / 'requirements-exchange/demo-q4').exists())

    def test_changed_delivery_before_audit_confirmation_is_rejected(self):
        self.migrate()
        self.state_command('begin-preparation', 'demo-q4')
        self.state_command('record-audit', 'demo-q4', '--finding-count', '0', '--blocking-finding-count', '0', '--summary', 'Reviewed')
        index_path = self.project / 'delivery-index.json'
        index = json.loads(index_path.read_text())
        index['deliveries']['demo-q4']['delivery_id'] = 'different-result'
        index_path.write_text(json.dumps(index))
        self.state_command('confirm-audit', 'demo-q4', success=False)

    def test_wrong_manifest_binding_rejected_before_new_revision(self):
        self.migrate()
        prepared = self.prepare_q4()
        manifest_path = Path(prepared['manifest'])
        manifest = json.loads(manifest_path.read_text())
        manifest['delivery_binding']['feature_id'] = 'unrelated-feature'
        manifest_path.write_text(json.dumps(manifest))
        self.authorize(self.project, 'demo-q4')
        before = manifest_path.read_bytes()
        result = fixtures.run(sys.executable, str(fixtures.SCRIPT), 'prepare', str(self.project), 'demo-q4', '--analyst', 'ivan')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_schema_three_legacy_manifest_stays_unchanged_without_layout(self):
        fixtures.register_stage(self.project, 'demo-q4')
        prepared = self.prepare_q4()
        manifest = json.loads(Path(prepared['manifest']).read_text())
        self.assertEqual(manifest['schema_version'], 3)
        self.assertNotIn('delivery_binding', manifest)
        self.assertNotIn('delivery_binding', manifest['revisions'][0])

    def test_migration_upgrade_preserves_unbound_history_and_bounds_legacy_ceiling(self):
        fixtures.register_stage(self.project, 'demo-q4')
        first = self.prepare_q4()
        old = json.loads(Path(first['manifest']).read_text())['revisions'][0]
        old_requirements = Path(first['requirements']).read_bytes()
        self.migrate(register=False)
        (self.q4 / 'requirements.md').write_text(self.q4_requirements.replace('показать результат.', 'показать обновлённый результат.'))
        self.state_command('record-change', 'demo-q4', '--origin', 'analyst')
        second = self.prepare_q4()
        manifest_path = Path(second['manifest'])
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['legacy_revision_ceiling'], 1)
        self.assertEqual(manifest['schema_version'], 4)
        # Lifecycle state can become superseded; immutable revision identity cannot.
        self.assertEqual({k: v for k, v in manifest['revisions'][0].items() if k != 'state'},
                         {k: v for k, v in old.items() if k != 'state'})
        self.assertEqual(Path(first['requirements']).read_bytes(), old_requirements)
        self.assertNotIn('delivery_binding', manifest['revisions'][0])
        self.assertEqual(manifest['revisions'][1]['delivery_binding'], self.binding())
        del manifest['revisions'][1]['delivery_binding']
        manifest_path.write_text(json.dumps(manifest))
        result = fixtures.run(sys.executable, str(fixtures.SCRIPT), 'validate', str(self.project))
        self.assertNotEqual(result.returncode, 0)

    def test_old_unbound_audit_cannot_authorize_new_migrated_publication(self):
        fixtures.register_stage(self.project, 'demo-q4')
        self.authorize(self.project, 'demo-q4')
        self.migrate(register=False)
        result = fixtures.run(sys.executable, str(fixtures.SCRIPT), 'prepare', str(self.project), 'demo-q4', '--analyst', 'ivan')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / 'requirements-exchange/demo-q4').exists())

    def test_sibling_draft_without_stage_does_not_block_but_open_stage_does(self):
        self.migrate()
        index_path = self.project / 'delivery-index.json'
        index = json.loads(index_path.read_text())
        relative = 'quarters/2027-Q1/features/demo/deliveries/future'
        future = self.project / relative
        future.mkdir(parents=True)
        (future / 'requirements.md').write_text(self.q4_requirements.replace('demo-q4', 'demo-future'))
        index['deliveries']['demo-future'] = {'feature_id': 'demo', 'delivery_id': 'future',
                                             'quarter': '2027-Q1', 'path': relative}
        index_path.write_text(json.dumps(index))
        self.prepare_q4()
        self.state_command('start-stage', 'demo-future', '--stage-id', 'stage-1', '--title', 'Первый результат',
                           '--goal', 'Показать результат пользователю', '--analyst-confirmed', success=False)

    def test_unresolved_legacy_published_sibling_blocks_new_stage(self):
        self.migrate(register=False)
        path = self.q3 / 'requirements-state.json'
        state = json.loads(path.read_text())
        state.pop('delivery_stages')
        path.write_text(json.dumps(state))
        self.state_command('start-stage', 'demo-q4', '--stage-id', 'stage-1', '--title', 'Первый результат',
                           '--goal', 'Показать результат пользователю', '--analyst-confirmed', success=False)

    def test_migrated_code_publication_roundtrip_preserves_clone_and_pending_commit(self):
        self.migrate()
        remote, code = self.prepare_code_repository(self.parent)
        (code / 'local-untracked.txt').write_text('User-owned working file')
        before = {name: self.git(code, *args) for name, args in {
            'head': ('rev-parse', 'HEAD'), 'branch': ('branch', '--show-current'),
            'status': ('status', '--porcelain=v1'), 'refs': ('show-ref',)}.items()}
        environment = os.environ.copy()
        environment['CODA_ANALYST_STATE_ROOT'] = str(self.parent / 'isolated-exchange-state')
        environment['ANALYST_HARNESS_STATE_ROOT'] = str(self.parent / 'isolated-exchange-state')
        self.authorize(self.project, 'demo-q4')
        arguments = ('prepare', str(self.project), 'demo-q4', '--analyst', 'ivan', '--code-root', str(code))
        first = self.command(*arguments, env=environment)
        self.assertEqual(first['destination_role'], 'code')
        self.assertEqual(first['status'], 'awaiting-merge')
        self.assertFalse(first['publication_confirmed'])
        self.assertEqual(first['delivery_binding'], self.binding())
        self.assertEqual(self.git(remote, 'rev-parse', 'refs/heads/main'), before['head'])
        refs = self.git(remote, 'show-ref')
        repeated = self.command(*arguments, env=environment)
        self.assertEqual(repeated['request_branch'], first['request_branch'])
        self.assertEqual(repeated['request_commit'], first['request_commit'])
        self.assertEqual(repeated['revision'], first['revision'])
        self.assertEqual(self.git(remote, 'show-ref'), refs)
        self.state_command('mark-published', 'demo-q4', '--manifest', first['manifest'],
                           '--revision', '1', '--destination-role', 'code', success=False)
        human = self.parent / 'human-merge'
        self.git(self.parent, 'clone', '--quiet', str(remote), str(human))
        self.git(human, 'config', 'user.name', 'Human reviewer')
        self.git(human, 'config', 'user.email', 'human@example.invalid')
        self.git(human, 'merge', '--no-ff', 'origin/' + first['request_branch'], '-m', 'Accept reviewed delivery requirements')
        self.git(human, 'push', 'origin', 'main')
        merged = self.command(*arguments, env=environment)
        self.assertTrue(merged['publication_confirmed'])
        self.assertEqual(merged['status'], 'already-current')
        self.assertEqual(merged['delivery_binding'], self.binding())
        self.assertEqual(merged['revision'], 1)
        self.assertEqual(merged['published_commit'], self.git(remote, 'rev-parse', 'refs/heads/main'))
        marked = self.state_command('mark-published', 'demo-q4', '--manifest', merged['manifest'],
                                    '--revision', '1', '--destination-role', 'code')
        self.assertEqual(marked['state']['last_published']['revision'], 1)
        after = {name: self.git(code, *args) for name, args in {
            'head': ('rev-parse', 'HEAD'), 'branch': ('branch', '--show-current'),
            'status': ('status', '--porcelain=v1'), 'refs': ('show-ref',)}.items()}
        self.assertEqual(after, before)
        self.assertEqual((code / 'local-untracked.txt').read_text(), 'User-owned working file')
        self.assertFalse((self.project / 'requirements-exchange/demo-q4').exists())

    def test_wrong_pending_remote_binding_blocks_without_fallback(self):
        self.migrate()
        remote, code = self.prepare_code_repository(self.parent)
        environment = os.environ.copy()
        environment['CODA_ANALYST_STATE_ROOT'] = str(self.parent / 'isolated-exchange-state')
        environment['ANALYST_HARNESS_STATE_ROOT'] = str(self.parent / 'isolated-exchange-state')
        self.authorize(self.project, 'demo-q4')
        arguments = ('prepare', str(self.project), 'demo-q4', '--analyst', 'ivan', '--code-root', str(code))
        prepared = self.command(*arguments, env=environment)
        other = self.parent / 'remote-change'
        self.git(self.parent, 'clone', '--quiet', str(remote), str(other))
        self.git(other, 'config', 'user.name', 'Other actor')
        self.git(other, 'config', 'user.email', 'other@example.invalid')
        self.git(other, 'switch', prepared['request_branch'])
        path = other / 'requirements-exchange/demo-q4/manifest.json'
        manifest = json.loads(path.read_text())
        manifest['delivery_binding']['feature_id'] = 'unrelated-feature'
        path.write_text(json.dumps(manifest))
        self.git(other, 'add', '--', 'requirements-exchange/demo-q4/manifest.json')
        self.git(other, 'commit', '-m', 'Alter remote delivery identity')
        self.git(other, 'push', 'origin', prepared['request_branch'])
        refs = self.git(remote, 'show-ref')
        failed = subprocess.run([sys.executable, str(fixtures.SCRIPT), *arguments],
                                capture_output=True, text=True, env=environment)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.git(remote, 'show-ref'), refs)
        self.assertFalse((self.project / 'requirements-exchange/demo-q4').exists())

    def test_changed_closed_sibling_review_blocks_new_quarter_stage(self):
        self.migrate(register=False)
        results_path = self.q3 / 'development-results-state.json'
        results = json.loads(results_path.read_text())
        detailed = next(item for item in reversed(results['processed']) if item['decision'] == 'reviewed')
        detailed['review']['items'][0]['actual_behavior'] = 'A later review invalidated the previous closure decision'
        results_path.write_text(json.dumps(results))
        rejected = self.state_command('start-stage', 'demo-q4', '--stage-id', 'stage-1', '--title', 'Первый результат',
                                      '--goal', 'Показать результат пользователю', '--analyst-confirmed', success=False)
        self.assertIn('После закрытия изменилось подробное решение', rejected.stdout + rejected.stderr)
        state_path = self.q4 / 'requirements-state.json'
        if state_path.exists():
            self.assertEqual(json.loads(state_path.read_text()).get('delivery_stages', {}).get('stages', []), [])


if __name__ == '__main__':
    unittest.main()
