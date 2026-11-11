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

    def test_grooming_link_rebasing_preserves_code(self):
        text = '[Example](example.json) and `[code](keep.json)`\n```text\n[code](keep.json)\n```'
        actual = grooming.rebase_links(text)
        self.assertIn('[Example](../../example.json)', actual)
        self.assertIn('`[code](keep.json)`', actual)
        self.assertIn('```text\n[code](keep.json)\n```', actual)

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
                     'description': 'Изменение\nhttps://example.invalid/?path=revision-1\ngrooming/' + 'a'*64}
        self.batch = {'tasks': [self.task], 'confirmation_sha256': 'b'*64}
        self.evidence = self.root / 'raw.json'
        self.mapping = self.root / 'mapping.json'
        self.mapping.write_text(json.dumps({name: '/data/' + name for name in
            ('key', 'type', 'summary', 'epic_key', 'estimate', 'labels', 'description', 'requirements_url')}))
        self.write_evidence()

    def write_evidence(self, **changes):
        self.evidence.write_text(json.dumps({'data': {**self.task, 'key': 'RSCON-123', **changes}}))

    def op(self, action, **args):
        return tracker.operate(self.root / 'journal', self.batch, action, 'be-01', **args)

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
        self.assertFalse(self.op('claim', confirmation='b'*64)['create_allowed'])
        self.op('bind', evidence=self.evidence, mapping=self.mapping)
        next_attempt = self.op('claim', confirmation='b'*64)
        self.assertTrue(next_attempt['update_allowed'])
        self.assertEqual(next_attempt['key'], 'RSCON-123')
        self.write_evidence()
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


if __name__ == '__main__':
    unittest.main()
