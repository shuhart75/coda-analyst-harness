from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from release_plan_review import check_review, review_inputs, review_marker
from release_plan_forecast import HEADERS


class ReleasePlanReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.quarter = self.root / 'planning/2026-Q4'
        self.gantt = self.quarter / 'gantt'
        self.gantt.mkdir(parents=True)
        self.plan = self.quarter / 'release-plan.md'
        self.binding = self.quarter / 'release-plan-sources.json'
        self.feature = self.root / 'features/kib'
        (self.feature / 'execution').mkdir(parents=True)
        (self.feature / 'requirements.md').write_text('КИБ входит в RSCON-8082.')
        self.mapping = {'schema_version': 1, 'rows': [{
            'release': 'RSCON-8082', 'feature': 'КИБ', 'deliveries': ['kib'],
            'evidence': [{'path': 'features/kib/requirements.md', 'quote': 'КИБ входит в RSCON-8082.'}]}]}
        self.binding.write_text(json.dumps(self.mapping))
        headers = ['Task ID', 'SberTrek', 'Summary', 'Kind', 'Role', 'Estimate (дн)', 'Status', 'Progress %', 'Actual Finish', 'Completed By']
        tasks = [('DEV', 'BE'), ('QA', 'QA'), ('OLD', 'BE'), ('OLDQA', 'QA')]
        content = '| ' + ' | '.join(headers) + ' |\n|' + '|'.join('---' for _ in headers) + '|\n'
        content += ''.join('| ' + ' | '.join([key, '-', key, 'real', role, '1', 'not-started', '0', '-', '-']) + ' |\n' for key, role in tasks)
        self.registry = self.feature / 'execution/tasks.md'
        self.registry.write_text(content)
        self.groups = self.feature / 'execution/qa-groups.json'
        self.groups.write_text(json.dumps({'schema_version': 1, 'total_estimate': 2, 'groups': [
            {'task_id': 'QA', 'members': ['DEV'], 'estimate': 1, 'release': 'RSCON-8082'},
            {'task_id': 'OLDQA', 'members': ['OLD'], 'estimate': 1, 'release': 'RSCON-7548'}]}))
        self.chart = ('@startgantt\n[BE] as [TASK_DEV] starts 2026/11/17\n[TASK_DEV] ends 2026/11/20\n'
                      '[QA] as [TASK_QA] starts 2026/11/20\n[TASK_QA] ends 2026/11/23\n'
                      '[Old] as [TASK_OLD] starts 2026/10/01\n[TASK_OLD] ends 2026/12/30\n'
                      '[Old QA] as [TASK_OLDQA] starts 2026/10/01\n[TASK_OLDQA] ends 2026/12/31\n@endgantt\n')
        self.write_chart()
        self.write_plan('03.12.2026')

    def write_chart(self):
        for name in ('actual-progress.puml', 'actual-progress-confluence.puml'):
            (self.gantt / name).write_text(self.chart)

    def write_plan(self, forecast):
        self.plan.write_text('# План релизов\n\nОбновлено: 17.11.2026.\n\n| ' + ' | '.join(HEADERS)
                             + ' |\n|---|---|---|---|---|---|\n'
                             + f'| RSCON-8082 | 20.11.2026 | {forecast} | КИБ | Функция | Ценность |\n')

    def test_new_stamp_cannot_certify_stale_conservative_forecast(self):
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with self.assertRaisesRegex(ValueError, '03.12.2026 -> 23.11.2026'):
            review_marker(self.quarter)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})
        report = review_inputs(self.quarter)[3][0]
        self.assertEqual(report['expected_forecast'], '23.11.2026')
        self.assertEqual({task['task_id'] for task in report['tasks']}, {'DEV', 'QA'})
        self.write_plan('23.11.2026')
        self.plan.write_text(self.plan.read_text() + review_marker(self.quarter) + '\n')
        check_review(self.quarter)
        self.assertIn('| 20.11.2026 | 23.11.2026 |', self.plan.read_text())
        # Changing the chart and its exact export does not permit restamping stale cells.
        self.chart = self.chart.replace('2026/11/23', '2026/11/25')
        self.write_chart()
        with self.assertRaisesRegex(ValueError, '23.11.2026 -> 25.11.2026'):
            review_marker(self.quarter)
        with self.assertRaises(ValueError):
            check_review(self.quarter)

    def test_other_release_and_unknown_remaining_work_never_supply_ready_date(self):
        groups = json.loads(self.groups.read_text())
        for group in groups['groups']:
            group['release'] = 'RSCON-7548'
        self.groups.write_text(json.dumps(groups))
        self.assertEqual(review_inputs(self.quarter)[3][0]['expected_forecast'], 'Не определён')
        self.write_plan('Не определён')
        review_marker(self.quarter)
        groups['groups'][0]['release'] = 'RSCON-8082'
        self.groups.write_text(json.dumps(groups))
        self.chart = self.chart.replace('[QA] as [TASK_QA] starts 2026/11/20\n[TASK_QA] ends 2026/11/23',
                                        '[QA unknown] as [TASK_QA] happens at 2026/11/20')
        self.write_chart()
        self.assertEqual(review_inputs(self.quarter)[3][0]['expected_forecast'], 'Не определён')

    def test_completion_bound_is_not_relabelled_as_exact_date(self):
        self.registry.write_text(self.registry.read_text().replace(
            'QA | - | QA | real | QA | 1 | not-started | 0 | - | -',
            'QA | - | QA | real | QA | 1 | completed | 100 | - | 2026-11-23'))
        self.chart = self.chart.replace('[QA] as [TASK_QA] starts 2026/11/20\n[TASK_QA] ends 2026/11/23',
                                        '[QA completed by] as [TASK_QA] happens at 2026/11/23')
        self.write_chart()
        self.assertEqual(review_inputs(self.quarter)[3][0]['expected_forecast'], 'Не позднее 23.11.2026')

    def test_missing_mapping_forged_evidence_and_stale_document_date_block(self):
        self.binding.unlink()
        with self.assertRaises(ValueError):
            review_marker(self.quarter)
        self.mapping['rows'][0]['evidence'][0]['quote'] = 'Invented scope'
        self.binding.write_text(json.dumps(self.mapping))
        with self.assertRaisesRegex(ValueError, 'quote'):
            review_marker(self.quarter)
        self.mapping['rows'] = [{'release': 'RSCON-8082', 'feature': 'КИБ',
                                 'unresolved_reason': 'Business scope not mapped yet'}]
        self.binding.write_text(json.dumps(self.mapping))
        with self.assertRaisesRegex(ValueError, 'Не определён'):
            review_marker(self.quarter)
        self.write_plan('Не определён')
        (self.gantt / 'actual-progress-resources.json').write_text(json.dumps({'as_of': '2026-11-18'}))
        with self.assertRaisesRegex(ValueError, 'document date'):
            review_marker(self.quarter)

    def test_quarter_delivery_mapping_and_foreign_quarter_rejection(self):
        import shutil
        target = self.root / 'quarters/2026-Q4/features/kib/deliveries/improvements'
        target.parent.mkdir(parents=True)
        shutil.move(str(self.feature), target)
        index = {'schema_version': 1, 'deliveries': {'kib': {
            'feature_id': 'kib', 'delivery_id': 'improvements', 'quarter': '2026-Q4',
            'path': 'quarters/2026-Q4/features/kib/deliveries/improvements'}}}
        (self.root / 'delivery-index.json').write_text(json.dumps(index))
        migrated_quarter = self.root / 'quarters/2026-Q4'
        for path in self.quarter.iterdir():
            shutil.move(str(path), migrated_quarter / path.name)
        self.quarter = migrated_quarter
        self.gantt = self.quarter / 'gantt'
        self.plan = self.quarter / 'release-plan.md'
        self.binding = self.quarter / 'release-plan-sources.json'
        self.mapping['rows'][0]['evidence'][0]['path'] = index['deliveries']['kib']['path'] + '/requirements.md'
        self.binding.write_text(json.dumps(self.mapping))
        report = review_inputs(self.quarter)[3][0]
        self.assertEqual(report['expected_forecast'], '23.11.2026')
        foreign = self.root / 'quarters/2026-Q3/features/kib/deliveries/improvements'
        foreign.parent.mkdir(parents=True)
        shutil.move(str(target), foreign)
        entry = index['deliveries']['kib']
        entry.update(quarter='2026-Q3', path='quarters/2026-Q3/features/kib/deliveries/improvements')
        (self.root / 'delivery-index.json').write_text(json.dumps(index))
        self.mapping['rows'][0]['evidence'][0]['path'] = entry['path'] + '/requirements.md'
        self.binding.write_text(json.dumps(self.mapping))
        with self.assertRaisesRegex(ValueError, 'belongs to'):
            review_inputs(self.quarter)

    def test_mapping_requires_complete_rows_and_safe_sources(self):
        self.mapping['rows'].append(dict(self.mapping['rows'][0]))
        self.binding.write_text(json.dumps(self.mapping))
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            review_inputs(self.quarter)
        self.mapping['rows'].pop()
        self.mapping['rows'][0]['evidence'][0]['path'] = '../outside.md'
        self.binding.write_text(json.dumps(self.mapping))
        with self.assertRaisesRegex(ValueError, 'outside project'):
            review_inputs(self.quarter)
        # Omitting QA from the source cannot produce a development-only ready date.
        self.mapping['rows'][0]['evidence'][0]['path'] = 'features/kib/requirements.md'
        self.binding.write_text(json.dumps(self.mapping))
        self.groups.unlink()
        report = review_inputs(self.quarter)[3][0]
        self.assertEqual(report['expected_forecast'], 'Не определён')

    def test_old_stamp_and_different_export_are_rejected(self):
        self.plan.write_text(self.plan.read_text() + '<!-- release-plan-review:v1 gantt=' + 'a'*64 + ' plan=' + 'b'*64 + ' -->\n')
        with self.assertRaisesRegex(ValueError, 'missing or stale'):
            check_review(self.quarter)
        (self.gantt / 'actual-progress-confluence.puml').write_text('@startgantt\n@endgantt\n')
        with self.assertRaisesRegex(ValueError, 'Confluence'):
            review_marker(self.quarter)


if __name__ == '__main__':
    unittest.main()
