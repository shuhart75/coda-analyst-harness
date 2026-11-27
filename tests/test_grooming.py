"""Grooming coverage, immutable transport, write gates and retry safety."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_sdd_exchange as exchange_tests
import test_sdd_receive as receive_tests
import test_sdd_bundle as fixtures
import grooming
import grooming_tracker as tracker
import story_delivery
import sdd_bundle
import sdd_audit
from write_context_fixture import configure_write_context


def confirm(plan):
    for task in plan['tasks']:
        task['confirmation'] = {'text': 'Состав и оценку задачи подтверждаю.', 'sha256': grooming.approval_hash(task)}
    plan['confirmation'] = {'text': 'Весь состав подтверждаю.', 'sha256': grooming.approval_hash(plan)}
    return plan


def make_plan(text, package, delivery='demo'):
    blocks = grooming.requirement_blocks(text)
    tasks = []
    for role, contour in [('BE', 'backend'), ('FE', 'frontend')]:
        changes = [c for c in package['changes'] if c['contour'] == contour]
        ids = sorted({x['req_id'] for c in changes for x in c['coverage']})
        if not ids:
            continue
        tasks.append({'id': role.lower() + '-01', 'role': role, 'summary': role + ' Показать результат',
                      'estimate': 2.5, 'description': 'Показать результат обработки.',
                      'acceptance': 'При наличии результата возвращаются его данные.', 'depends_on': [],
                      'sdd_changes': [contour + '/' + c['change_id'] for c in changes],
                      'requirements': [{'req_id': req, 'scope': 'Работа контура ' + contour,
                                        'scenarios': list(blocks[req]['scenarios'])} for req in ids]})
    for task in tasks:
        task['source_sha256'] = grooming.task_source_hash(task, blocks)
    return confirm({'schema_version': 1, 'delivery': delivery, 'stage_id': 'stage-1',
                    'requirements_sha256': grooming.digest(text.encode()), 'epic_key': 'RSCON-10',
                    'estimate_unit': 'person-days', 'coverage_review': 'Сверены требования и контуры.',
                    'qa_review': 'Проверки входят в отдельную согласованную QA-задачу.', 'tasks': tasks})


def write_tasks(root, text, package, plan=None):
    plan = plan or make_plan(text, package)
    for name, value in grooming.files_for(plan, text, package).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return plan


class GroomingUnitTests(unittest.TestCase):
    def setUp(self):
        self.text = 'Этап поставки: stage-1\n\n### REQ-ONE. Результат\nПравило результата.\n\n#### Сценарий: Есть результат\nКогда результат есть.\nТогда он виден.\n'
        self.package = {'changes': [{'contour': 'backend', 'change_id': 'show-result',
                                    'coverage': [{'req_id': 'REQ-ONE'}]}]}
        self.plan = make_plan(self.text, self.package)

    def test_grooming_preview_one_task_titles_and_fractional_days(self):
        del self.plan['tasks'][0]['confirmation']
        value = grooming.preview(self.plan, self.text, self.package)
        self.assertEqual(value['remaining_tasks'], 1)
        self.assertEqual(value['task']['requirements'][0]['title'], 'Результат')
        self.assertNotIn('REQ-ONE', json.dumps(value['task']))
        self.assertEqual(value['task']['estimate_person_days'], 2.5)

    def test_grooming_dialogue_can_start_before_sdd_and_epic_resolution(self):
        plan = deepcopy(self.plan)
        plan.pop('epic_key')
        plan.pop('coverage_review')
        plan.pop('qa_review')
        plan['tasks'][0].pop('confirmation')
        plan['tasks'][0]['sdd_changes'] = []
        result = grooming.preview(plan, self.text, None)
        self.assertEqual(result['next_action'], 'confirm-one-task')
        with self.assertRaises(ValueError):
            grooming.validate(plan, self.text, self.package)

    def test_grooming_native_tracker_key_is_optional_but_validated(self):
        self.plan['tasks'][0]['tracker_key'] = 'RSCON-8201'
        grooming.validate(confirm(self.plan), self.text, self.package)
        self.plan['tasks'][0]['tracker_key'] = 'not-a-key'
        with self.assertRaisesRegex(ValueError, 'tracker key'):
            grooming.validate(confirm(self.plan), self.text, self.package)

    def test_grooming_link_rebasing_preserves_code(self):
        text = '[Example](example.json) and `[code](keep.json)`\n```text\n[code](keep.json)\n```'
        actual = grooming.rebase_links(text)
        self.assertIn('[Example](../../example.json)', actual)
        self.assertIn('`[code](keep.json)`', actual)
        self.assertIn('```text\n[code](keep.json)\n```', actual)

    def test_optional_estimates_validate_render_and_roundtrip(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                plan = deepcopy(self.plan)
                if missing:
                    plan['tasks'][0].pop('estimate')
                else:
                    plan['tasks'][0]['estimate'] = None
                confirm(plan)
                grooming.validate(plan, self.text, self.package)
                files = grooming.files_for(plan, self.text, self.package)
                self.assertIn('Оценка не указана.', files['be-01/requirements.md'])
                self.assertNotIn('0 чел.-дн.', files['be-01/requirements.md'])
                preview = grooming.preview(plan, self.text, self.package)
                self.assertIsNone(preview['total_person_days'])
                self.assertEqual(preview['unestimated_tasks'], 1)
                self.assertEqual(preview['estimated_person_days'], 0)

    def test_partial_estimates_do_not_claim_complete_total(self):
        plan = deepcopy(self.plan)
        second = deepcopy(plan['tasks'][0])
        second['id'] = 'be-02'
        second.pop('estimate')
        plan['tasks'].append(second)
        result = grooming.preview(plan, self.text, self.package)
        self.assertIsNone(result['total_person_days'])
        self.assertEqual(result['estimated_person_days'], 2.5)
        self.assertEqual(result['unestimated_tasks'], 1)
        self.assertIsNone(result['task']['estimate_person_days'])

    def test_invalid_supplied_estimates_remain_rejected(self):
        for estimate in (0, -1, True, '3', float('inf'), float('nan')):
            plan = deepcopy(self.plan)
            plan['tasks'][0]['estimate'] = estimate
            with self.subTest(estimate=estimate), self.assertRaises(ValueError):
                grooming.validate(confirm(plan), self.text, self.package)

    def test_grooming_changed_estimate_invalidates_both_approvals(self):
        self.plan['tasks'][0]['estimate'] = 3
        with self.assertRaisesRegex(ValueError, 'stale confirmation'):
            grooming.validate(self.plan, self.text, self.package)
        task = self.plan['tasks'][0]
        task['confirmation']['sha256'] = grooming.approval_hash(task)
        with self.assertRaisesRegex(ValueError, 'complete plan'):
            grooming.validate(self.plan, self.text, self.package)

    def test_grooming_missing_and_wrong_contour_and_cycles_rejected(self):
        for change in ('missing', 'contour', 'cycle', 'estimate', 'stage', 'unit'):
            plan = deepcopy(self.plan)
            if change == 'missing': plan['tasks'][0]['requirements'] = []
            if change == 'contour': plan['tasks'][0].update(role='FE', summary='FE Показать результат')
            if change == 'cycle': plan['tasks'][0]['depends_on'] = ['be-01']
            if change == 'estimate': plan['tasks'][0]['estimate'] = float('nan')
            if change == 'stage': plan['stage_id'] = 'stage-2'
            if change == 'unit': plan['estimate_unit'] = 'story-points'
            with self.subTest(change=change), self.assertRaises(ValueError):
                grooming.validate(confirm(plan), self.text, self.package)

    def test_grooming_contour_coverage_cannot_hide_fe_work(self):
        self.package['changes'].append({'contour': 'frontend', 'change_id': 'show-result',
                                        'coverage': [{'req_id': 'REQ-ONE'}]})
        with self.assertRaisesRegex(ValueError, 'coverage'):
            grooming.validate(self.plan, self.text, self.package)
        plan = make_plan(self.text, self.package)
        grooming.validate(plan, self.text, self.package)

    def test_grooming_changed_rule_requires_task_reconfirmation(self):
        changed = self.text.replace('Правило результата.', 'Другое правило результата.')
        self.plan['requirements_sha256'] = grooming.digest(changed.encode())
        confirm(self.plan)  # Reconfirming stale identifiers alone is insufficient.
        with self.assertRaisesRegex(ValueError, 'task source changed'):
            grooming.validate(self.plan, changed, self.package)
        result = grooming.preview(self.plan, changed, self.package)
        self.assertEqual(result['remaining_tasks'], 1)
        self.assertNotEqual(result['source_sha256'], self.plan['tasks'][0]['source_sha256'])

    def test_grooming_scenarios_and_authored_wording_preserved(self):
        files = grooming.files_for(self.plan, self.text, self.package)
        doc = files['be-01/requirements.md']
        self.assertIn('### REQ-ONE. Результат\nПравило результата.', doc)
        self.assertIn('Тогда он виден.', doc)
        self.assertIn('2.5 чел.-дн.', doc)
        self.plan['tasks'][0]['requirements'][0]['scenarios'] = ['invented']
        with self.assertRaisesRegex(ValueError, 'scenario'):
            grooming.files_for(confirm(self.plan), self.text, self.package)


class GroomingExchangeTests(exchange_tests.SddExchangeTests):
    def enable_grooming(self):
        self.text = (self.feature / 'requirements.md').read_text()
        self.plan = write_tasks(self.feature / 'tasks', self.text, self.package)
        self.package['schema_version'] = 3
        (self.sdd / 'package.json').write_text(json.dumps(self.package))

    def test_grooming_publish_receive_audit_and_old_revision(self):
        self.enable_grooming()
        self.authorize()
        result = self.prepare()
        revision = Path(result['requirements']).parent
        expected = sdd_bundle.inspect(self.sdd, self.text)
        self.assertEqual(sdd_audit.working_hash(self.feature), expected['sha256'])
        self.assertEqual(expected, sdd_bundle.inspect(revision / 'sdd', self.text))
        self.assertTrue((revision.parent.parent.parent / 'grooming.py').exists())
        received = receive_tests.receiver.receive(revision.parent.parent, 1, self.code, apply=True)
        self.assertEqual(received['tasks']['sha256'], expected['tasks']['sha256'])
        before = {p.relative_to(revision): p.read_bytes() for p in revision.rglob('*') if p.is_file()}
        self.plan['tasks'][0]['estimate'] = 4
        write_tasks(self.feature / 'tasks', self.text, self.package, confirm(self.plan))
        self.prepare(success=False)
        # Code has received SDD changes, so this test stops at the audit invalidation boundary.
        self.assertEqual(before, {p.relative_to(revision): p.read_bytes() for p in revision.rglob('*') if p.is_file()})

    def test_grooming_with_materials_and_standalone_receiver(self):
        from test_sdd_materials import write_materials
        import subprocess
        import sys
        self.enable_grooming()
        write_materials(self.feature / 'materials')
        self.authorize()
        result = self.prepare()
        packet = Path(result['requirements']).parent.parent.parent
        descriptor = self.manifest(result)['revisions'][0]['sdd_input']
        self.assertIn('tasks', descriptor)
        self.assertIn('materials', descriptor)
        response = subprocess.run([sys.executable, str(packet.parent / 'sdd-receive.py'),
            '--packet', str(packet), '--revision', '1', '--code-root', str(self.code)],
            cwd=self.parent, capture_output=True, text=True)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertEqual(json.loads(response.stdout)['tasks']['sha256'], descriptor['tasks']['sha256'])

    def test_unestimated_grooming_with_materials_and_standalone_receiver(self):
        from test_sdd_materials import write_materials
        import subprocess
        import sys
        self.enable_grooming()
        self.plan['tasks'][0].pop('estimate')
        write_tasks(self.feature / 'tasks', self.text, self.package, confirm(self.plan))
        write_materials(self.feature / 'materials')
        self.authorize()
        result = self.prepare()
        packet = Path(result['requirements']).parent.parent.parent
        descriptor = self.manifest(result)['revisions'][0]['sdd_input']
        self.assertIn('tasks', descriptor)
        self.assertIn('materials', descriptor)
        response = subprocess.run([sys.executable, str(packet.parent / 'sdd-receive.py'),
            '--packet', str(packet), '--revision', '1', '--code-root', str(self.code)],
            cwd=self.parent, capture_output=True, text=True)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertEqual(json.loads(response.stdout)['tasks']['sha256'], descriptor['tasks']['sha256'])

    def test_grooming_new_revision_preserves_old_tasks(self):
        self.enable_grooming()
        self.authorize()
        first = self.prepare()
        previous = Path(first['requirements']).parent / 'tasks/be-01/requirements.md'
        old = previous.read_bytes()
        self.plan['tasks'][0]['estimate'] = 5
        write_tasks(self.feature / 'tasks', self.text, self.package, confirm(self.plan))
        self.authorize()
        second = self.prepare()
        self.assertEqual(second['revision'], 2)
        self.assertEqual(previous.read_bytes(), old)
        self.assertNotEqual(Path(second['requirements']).parent.joinpath('tasks/be-01/requirements.md').read_bytes(), old)

    def test_grooming_code_publication_and_story_preview(self):
        self.enable_grooming()
        self.enable_code_exchange()
        self.authorize()
        before = self.code_snapshot()
        result = self.prepare()
        with self.assertRaises(ValueError):
            tracker.prepare(self.project, 'demo', 'https://onework.sigma.sbrf.ru/sc/RSCON/coda')
        self.accept(result)
        result = self.prepare()
        self.state('mark-published', '--manifest', result['manifest'], '--revision', str(result['revision']), '--destination-role', 'code')
        payload = tracker.prepare(self.project, 'demo', 'https://onework.sigma.sbrf.ru/sc/RSCON/coda')
        task = payload['tasks'][0]
        self.assertEqual(task['type'], 'Story')
        self.assertEqual(task['labels'], ['RSCON_dev'])
        self.assertEqual(task['epic_key'], 'RSCON-10')
        self.assertIn('revisions%2F001%2Ftasks%2Fbe-01%2Frequirements.md', task['requirements_url'])
        self.assertEqual(before, self.code_snapshot())

    def test_unestimated_publication_omits_estimate_from_story_payload(self):
        self.enable_grooming()
        self.plan['tasks'][0].pop('estimate')
        write_tasks(self.feature / 'tasks', self.text, self.package, confirm(self.plan))
        self.enable_code_exchange()
        self.authorize()
        before = self.code_snapshot()
        result = self.prepare()
        with self.assertRaises(ValueError):
            tracker.prepare(self.project, 'demo', 'https://onework.sigma.sbrf.ru/sc/RSCON/coda')
        self.accept(result)
        result = self.prepare()
        self.state('mark-published', '--manifest', result['manifest'], '--revision', str(result['revision']), '--destination-role', 'code')
        payload = tracker.prepare(self.project, 'demo', 'https://onework.sigma.sbrf.ru/sc/RSCON/coda')
        task = payload['tasks'][0]
        self.assertEqual(task['type'], 'Story')
        self.assertNotIn('estimate', task)
        self.assertEqual(task['labels'], ['RSCON_dev'])
        self.assertEqual(task['epic_key'], 'RSCON-10')
        self.assertIn('revisions%2F001%2Ftasks%2Fbe-01%2Frequirements.md', task['requirements_url'])
        self.assertEqual(before, self.code_snapshot())

    def test_grooming_tampering_and_legacy_omission_blocked(self):
        self.enable_grooming()
        path = self.feature / 'tasks/be-01/requirements.md'
        path.write_text(path.read_text() + 'Unreviewed content\n')
        with self.assertRaisesRegex(ValueError, 'differ'):
            sdd_bundle.inspect(self.sdd, self.text)
        self.package['schema_version'] = 1
        (self.sdd / 'package.json').write_text(json.dumps(self.package))
        with self.assertRaisesRegex(ValueError, 'schema_version 3'):
            sdd_bundle.inspect(self.sdd, self.text)

    def test_grooming_build_requires_owning_branch_and_preserves_foreign_files(self):
        self.enable_grooming()
        with self.assertRaises(ValueError):
            grooming.build(self.project, 'demo', self.plan)
        self.git(self.project, 'init', '-b', 'feature/demo')
        self.git(self.project, 'config', 'user.name', 'Fixture')
        self.git(self.project, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.project, 'add', '--all')
        self.git(self.project, 'commit', '-m', 'Fixture')
        runtime = configure_write_context(self, self.project, 'feature/demo', 'demo')
        (runtime / 'active-mode.md').write_text('mode: requirements\n')
        result = grooming.build(self.project, 'demo', self.plan)
        self.assertEqual(result['status'], 'grooming-built')
        foreign = self.feature / 'tasks/foreign.md'
        foreign.write_text('User work')
        with self.assertRaises(ValueError):
            grooming.build(self.project, 'demo', self.plan)
        self.assertEqual(foreign.read_text(), 'User work')

    def test_grooming_copy_failure_removes_only_new_snapshot(self):
        self.enable_grooming()
        expected = sdd_bundle.inspect(self.sdd, self.text)
        destination = self.parent / 'snapshot/sdd'
        original = sdd_bundle.shutil.copytree
        def fail(src, dst, *args, **kwargs):
            if Path(src) == self.feature / 'tasks':
                raise OSError('injected task copy failure')
            return original(src, dst, *args, **kwargs)
        with patch.object(sdd_bundle.shutil, 'copytree', side_effect=fail), self.assertRaises(OSError):
            sdd_bundle.copy_snapshot(self.sdd, destination, expected, self.text)
        self.assertFalse(destination.exists())
        self.assertFalse((destination.parent / 'tasks').exists())
        self.assertTrue((self.feature / 'tasks/grooming.json').exists())


class GroomingTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {'local_id': 'be-01', 'request_id': 'a'*64, 'type': 'Story',
                     'summary': 'BE Изменение', 'epic_key': 'RSCON-10', 'estimate': 2.5,
                     'estimate_unit': 'person-days', 'labels': ['RSCON_dev'],
                     'requirements_url': 'https://example.invalid/?path=revision-1',
                     'description': 'Изменение\nhttps://example.invalid/?path=revision-1\ngrooming/' + 'a'*64,
                     **story_delivery.REQUIRED_FIELDS}
        self.batch = {'tasks': [self.task], 'confirmation_sha256': 'b'*64}
        self.evidence = self.root / 'raw.json'
        self.mapping = self.root / 'mapping.json'
        self.mapping.write_text(json.dumps({name: '/data/' + name for name in
            ('key', 'type', 'summary', 'epic_key', 'estimate', 'labels', 'description', 'requirements_url',
             *story_delivery.REQUIRED_FIELDS)}))
        self.write_evidence()

    def write_evidence(self, **changes):
        self.evidence.write_text(json.dumps({'data': {**self.task, 'key': 'RSCON-123', **changes}}))

    def op(self, action, **args):
        return tracker.operate(self.root / 'journal', self.batch, action, 'be-01', **args)

    def test_unestimated_story_does_not_require_or_overwrite_tracker_estimate(self):
        self.task.pop('estimate')
        mapping = json.loads(self.mapping.read_text())
        mapping.pop('estimate')
        self.mapping.write_text(json.dumps(mapping))
        self.write_evidence(estimate=8)
        self.op('claim', confirmation='b'*64)
        self.assertEqual(self.op('bind', evidence=self.evidence, mapping=self.mapping)['key'], 'RSCON-123')

    def test_grooming_single_attempt_bind_and_no_duplicate(self):
        with self.assertRaises(ValueError): self.op('claim')
        self.assertTrue(self.op('claim', confirmation='b'*64)['create_allowed'])
        self.assertEqual(self.op('claim', confirmation='b'*64)['status'], 'reconcile-required')
        self.assertEqual(self.op('bind', evidence=self.evidence, mapping=self.mapping)['key'], 'RSCON-123')
        self.assertEqual(self.op('claim', confirmation='b'*64)['status'], 'already-bound')
        self.assertEqual(self.op('status')['next_action'], 'complete')

    def test_grooming_link_in_description_needs_no_invented_tracker_field(self):
        mapping = json.loads(self.mapping.read_text())
        del mapping['requirements_url']
        self.mapping.write_text(json.dumps(mapping))
        data = json.loads(self.evidence.read_text())
        del data['data']['requirements_url']
        self.evidence.write_text(json.dumps(data))
        self.op('claim', confirmation='b'*64)
        self.assertEqual(self.op('bind', evidence=self.evidence, mapping=self.mapping)['key'], 'RSCON-123')

    def test_grooming_update_reuses_key_and_old_pending_can_reconcile(self):
        self.op('claim', confirmation='b'*64)
        old = deepcopy(self.task)
        self.task['estimate'] = 4
        self.task['description'] += '\nNew requirements'
        self.assertFalse(self.op('claim', confirmation='b'*64)['create_allowed'])
        self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(self.op('claim', confirmation='b'*64)['status'], 'read-existing-task')
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        next_attempt = self.op('claim', confirmation='b'*64)
        self.assertTrue(next_attempt['update_allowed'])
        self.assertEqual(next_attempt['key'], 'RSCON-123')
        self.evidence.write_text(json.dumps({'data': {**next_attempt['payload'], 'key': 'RSCON-123'}}))
        self.op('bind', evidence=self.evidence, mapping=self.mapping)

    def test_grooming_wrong_epic_label_estimate_and_link_block_binding(self):
        self.op('claim', confirmation='b'*64)
        for change in ({'epic_key': 'RSCON-20'}, {'labels': []}, {'estimate': 8},
                       {'requirements_url': 'https://wrong.invalid/'}, {'type': 'Task'},
                       {'description': 'Missing identity'}):
            self.write_evidence(**change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(self.op('status')['next_action'], 'reconcile-required')

    def test_supplied_key_requires_read_then_updates_without_create(self):
        self.task['tracker_key'] = 'RSCON-123'
        result = self.op('claim', confirmation='b'*64)
        self.assertFalse(result['create_allowed'])
        self.assertEqual(result['key'], 'RSCON-123')
        self.write_evidence(summary='Existing title', description='Useful analyst text',
                            labels=['keep-me'], estimate=11)
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        result = self.op('claim', confirmation='b'*64)
        self.assertTrue(result['update_allowed'])
        self.assertFalse(result['create_allowed'])
        self.assertEqual(result['payload']['summary'], 'Existing title')
        self.assertEqual(result['write_fields']['labels'], ['keep-me', 'RSCON_dev'])
        self.assertTrue(result['write_fields']['description'].startswith('Useful analyst text'))
        self.assertNotIn('estimate', result['write_fields'])
        self.assertNotIn('summary', result['write_fields'])
        self.assertNotIn('type', result['write_fields'])
        self.evidence.write_text(json.dumps({'data': {**result['payload'], 'key': 'RSCON-123', 'estimate': 11}}))
        self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(tracker.batch_status(self.root/'journal', self.batch)['completed'], 1)

    def test_wrong_supplied_key_is_rejected_and_existing_type_is_preserved(self):
        self.task['tracker_key'] = 'RSCON-456'
        with self.assertRaisesRegex(ValueError, 'exact supplied'):
            self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        self.write_evidence(key='RSCON-456', type='Task')
        result = self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(result['payload']['type'], 'Task')
        self.assertNotIn('type', result['write_fields'])
        self.assertFalse(self.op('claim', confirmation='b'*64)['create_allowed'])

    def test_mandatory_fields_are_read_back_and_verified(self):
        self.op('claim', confirmation='b'*64)
        for field, value in [('configuration_item', 'CI99999999'),
                             ('architecture_changes', 'Влияет на архитектуру'),
                             ('load_testing_required', 'Да')]:
            self.write_evidence(**{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'mandatory field'):
                self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.write_evidence(configuration_item='CI07642362', load_testing_required=False)
        self.op('bind', evidence=self.evidence, mapping=self.mapping)

    def test_cross_machine_search_binds_existing_story_without_creating(self):
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(self.op('claim', confirmation='b'*64)['status'], 'already-bound')
        self.assertEqual(self.op('status')['record']['key'], 'RSCON-123')

    def test_partial_unknown_creation_is_reconciled_to_update_same_key(self):
        self.op('claim', confirmation='b'*64)
        self.write_evidence(labels=[])
        self.assertEqual(self.op('claim', confirmation='b'*64)['status'], 'reconcile-required')
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        result = self.op('claim', confirmation='b'*64)
        self.assertFalse(result['create_allowed'])
        self.assertTrue(result['update_allowed'])
        self.assertEqual(result['key'], 'RSCON-123')

    def test_search_without_marker_cannot_adopt_unrelated_task(self):
        self.write_evidence(description='Same title is not identity')
        with self.assertRaisesRegex(ValueError, 'stable request marker'):
            self.op('resolve', evidence=self.evidence, mapping=self.mapping)

    def test_dictionary_ids_are_verified_against_unchanged_metadata(self):
        metadata = self.root/'options.json'
        metadata.write_text(json.dumps({'values': [{'id': 73, 'code': 'CI07642362'}]}))
        mapping = json.loads(self.mapping.read_text())
        mapping['configuration_item'] = {'path': '/data/configuration_item',
            'lookup': {'evidence': str(metadata), 'id': '/values/0/id', 'value': '/values/0/code'}}
        self.mapping.write_text(json.dumps(mapping))
        self.op('claim', confirmation='b'*64)
        self.write_evidence(configuration_item=99)
        with self.assertRaisesRegex(ValueError, 'metadata evidence'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.write_evidence(configuration_item=73)
        result = self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(result['metadata_evidence'][str(metadata)], grooming.digest(metadata.read_bytes()))

    def test_unread_mandatory_field_and_lost_label_cannot_finish(self):
        self.task['tracker_key'] = 'RSCON-123'
        self.write_evidence(labels=['keep-me'], description='Existing text')
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        claimed = self.op('claim', confirmation='b'*64)
        self.evidence.write_text(json.dumps({'data': {**claimed['payload'], 'key': 'RSCON-123'}}))
        paths = json.loads(self.mapping.read_text())
        del paths['architecture_changes']
        self.mapping.write_text(json.dumps(paths))
        with self.assertRaisesRegex(ValueError, 'Read back the mandatory field'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping)
        paths['architecture_changes'] = '/data/architecture_changes'
        self.mapping.write_text(json.dumps(paths))
        data = json.loads(self.evidence.read_text())
        data['data']['labels'] = ['RSCON_dev']
        self.evidence.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'labels were lost'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping)
        self.assertEqual(tracker.batch_status(self.root/'journal', self.batch)['status'], 'tasks-pending')

    def test_creation_payload_is_story_only(self):
        self.task['type'] = 'Task'
        with self.assertRaisesRegex(ValueError, 'must be Story'):
            self.op('claim', confirmation='b'*64)

    def links(self, *parents):
        key = 'RSCON-123'
        edges = '\n'.join(f'{number}. {parent} **decomposition** -> {key}'
                          for number, parent in enumerate(parents, 1))
        text = f'Links for {key} ({len(parents)} total, page 0, has_next: False)\n\n' + edges
        path = self.root / ('links-' + '-'.join(parents) + '.json')
        path.write_text(json.dumps({'result': [{'type': 'text', 'text': text}]}))
        return {'links_responses': [path], 'links_text_pointer': '/result/0/text'}

    def test_epic_is_confirmed_through_link_list_when_card_has_no_epic_field(self):
        mapping = json.loads(self.mapping.read_text())
        del mapping['epic_key']
        self.mapping.write_text(json.dumps(mapping))
        data = json.loads(self.evidence.read_text())
        del data['data']['epic_key']
        self.evidence.write_text(json.dumps(data))
        self.op('claim', confirmation='b'*64)
        with self.assertRaisesRegex(ValueError, 'подтверждённым эпиком'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping,
                    **self.links('RSCON-77'))
        result = self.op('bind', evidence=self.evidence, mapping=self.mapping,
                         **self.links('RSCON-77', 'RSCON-10'))
        self.assertEqual(result['key'], 'RSCON-123')
        self.assertEqual(self.op('status')['next_action'], 'complete')

    def test_epic_field_contradicting_link_list_blocks_binding(self):
        self.op('claim', confirmation='b'*64)
        with self.assertRaisesRegex(ValueError, 'противоречит link.list'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping,
                    **self.links('RSCON-77'))

    def test_existing_empty_card_fields_are_valid_and_line_endings_are_not_a_gate(self):
        self.task['tracker_key'] = 'RSCON-123'
        self.write_evidence(description=None, labels=None)
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        claimed = self.op('claim', confirmation='b'*64)
        data = {**claimed['payload'], 'key': 'RSCON-123'}
        data['description'] = data['description'].replace('\n', '\r\n')
        self.evidence.write_text(json.dumps({'data': data}))
        self.op('bind', evidence=self.evidence, mapping=self.mapping)

    def test_short_creation_response_records_key_before_followup_updates(self):
        self.op('claim', confirmation='b'*64)
        created = self.root/'created.json'
        created.write_text(json.dumps({'key': 'RSCON-123'}))
        mapping = self.root/'created-mapping.json'
        mapping.write_text(json.dumps({'key': '/key'}))
        result = self.op('record-created', evidence=created, mapping=mapping)
        self.assertFalse(result['complete'])
        self.assertFalse(result['create_allowed'])
        self.write_evidence(description=None, labels=None)
        self.op('resolve', evidence=self.evidence, mapping=self.mapping)
        self.assertTrue(self.op('claim', confirmation='b'*64)['update_allowed'])

    def test_wrong_type_after_creation_cannot_be_accepted_as_existing_task(self):
        self.op('claim', confirmation='b'*64)
        self.write_evidence(type='Task')
        with self.assertRaisesRegex(ValueError, 'Created task must be Story'):
            self.op('resolve', evidence=self.evidence, mapping=self.mapping)


    def without_epic(self):
        mapping = json.loads(self.mapping.read_text())
        mapping.pop('epic_key', None)
        self.mapping.write_text(json.dumps(mapping))
        data = json.loads(self.evidence.read_text())
        data['data'].pop('epic_key', None)
        self.evidence.write_text(json.dumps(data))

    def test_link_evidence_is_hashed_and_resolve_is_repeatable(self):
        self.without_epic()
        links = self.links('RSCON-10', 'RSCON-77')
        path = links['links_responses'][0]
        raw = path.read_bytes()
        self.op('claim', confirmation='b'*64)
        bound = self.op('bind', evidence=self.evidence, mapping=self.mapping, **links)
        self.assertEqual(bound['links_evidence'], {str(path): grooming.digest(raw)})
        self.assertEqual(bound['links_text_pointer'], '/result/0/text')
        for _ in range(2):
            result = self.op('resolve', evidence=self.evidence, mapping=self.mapping, **links)
            self.assertEqual(result['next_action'], 'complete')
            self.assertEqual(result['links_evidence'], bound['links_evidence'])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.op('claim', confirmation='b'*64)['status'], 'already-bound')

    def test_wrong_card_epic_cannot_hide_among_other_parents(self):
        self.write_evidence(epic_key='RSCON-77')
        self.op('claim', confirmation='b'*64)
        with self.assertRaisesRegex(ValueError, 'epic_key'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping,
                    **self.links('RSCON-77', 'RSCON-10'))

    def test_missing_other_task_reversed_and_incomplete_links_do_not_bind(self):
        self.without_epic()
        self.op('claim', confirmation='b'*64)
        with self.assertRaisesRegex(ValueError, 'Нет поля эпика'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping)
        links = self.links('RSCON-10')
        path = links['links_responses'][0]
        original = path.read_text()
        variants = [original.replace('RSCON-123', 'RSCON-999'),
                    original.replace('RSCON-10 **decomposition** -> RSCON-123',
                                     'RSCON-123 **decomposition** -> RSCON-10'),
                    original.replace('**decomposition**', '**release**'),
                    original.replace('has_next: False', 'has_next: True'),
                    original.replace('1 total', '2 total')]
        for raw in variants:
            with self.subTest(raw=raw):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    self.op('bind', evidence=self.evidence, mapping=self.mapping, **links)
                self.assertEqual(self.op('status')['next_action'], 'reconcile-required')
        path.write_text(original)
        other = self.links('RSCON-77')['links_responses'][0]
        with self.assertRaisesRegex(ValueError, 'Противоречивые'):
            self.op('bind', evidence=self.evidence, mapping=self.mapping,
                    links_responses=[path, other], links_text_pointer='/result/0/text')


class StoryDeliveryContentTests(unittest.TestCase):
    def setUp(self):
        self.task = {'id': 'be-01', 'summary': 'BE Получение эффектов', 'description': 'Получить эффекты.',
                     'acceptance': 'Эффекты доступны.', 'sdd_changes': ['backend/effects']}

    def test_three_commented_links_and_sdd_input_pin_exact_revision(self):
        from urllib.parse import parse_qs, urlsplit
        result = story_delivery.delivery_content(self.task, 'effects', 2, 'c'*40,
                                                'https://onework.sigma.sbrf.ru/sc/RSCON/documents', 'a'*64)
        for field, suffix in [('revision_url', ''), ('general_requirements_url', '/requirements.md'),
                              ('task_folder_url', '/tasks/be-01')]:
            self.assertEqual(parse_qs(urlsplit(result[field]).query)['path'],
                             ['requirements-exchange/effects/revisions/002' + suffix])
            self.assertIn(result[field], result['description'])
        self.assertEqual(result['sdd_input_url'], result['revision_url'])
        self.assertIn('--packet <локальный documents>/requirements-exchange/effects --revision 2', result['sdd_prompt'])
        self.assertIn('backend/effects', result['sdd_prompt'])
        self.assertIn('tasks/be-01/requirements.md', result['sdd_prompt'])
        self.assertIn('c'*40, result['sdd_prompt'])

    def test_revision_update_replaces_block_and_preserves_user_text(self):
        old = story_delivery.delivery_content(self.task, 'effects', 1, 'c'*40, 'https://example.test/documents', 'a'*64)
        new = story_delivery.delivery_content(self.task, 'effects', 2, 'd'*40, 'https://example.test/documents', 'a'*64)
        existing = 'User prefix\n\n' + old['description'] + '\n\nUser suffix'
        result = story_delivery.merge_description(existing, {**new, 'request_id': 'a'*64})
        self.assertEqual(result, 'User prefix\n\n' + new['description'] + '\n\nUser suffix')
        self.assertEqual(story_delivery.merge_description(result, {**new, 'request_id': 'a'*64}), result)

    def test_task_keys_mixed_existing_and_new_and_conflicts(self):
        plan = {'tasks': [{'id': 'be', 'tracker_key': 'RSCON-123'}, {'id': 'fe'}, {'id': 'qa'}]}
        self.assertEqual(story_delivery.task_keys(plan, {'fe': 'RSCON-456'}), {'be': 'RSCON-123', 'fe': 'RSCON-456'})
        for keys in ({'missing': 'RSCON-1'}, {'be': 'RSCON-999'}, {'fe': 'RSCON-123'}):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                story_delivery.task_keys(plan, keys)


if __name__ == '__main__':
    unittest.main()
