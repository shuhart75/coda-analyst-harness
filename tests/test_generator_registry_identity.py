from __future__ import annotations

from importlib import import_module
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_registry import read_registry


overlay = import_module('sync-actual-progress-overlay')


class GeneratorRegistryIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.feature = Path(temporary.name)
        self.path = self.feature / 'slices/legacy/execution/tasks.md'
        self.path.parent.mkdir(parents=True)

    def write_table(self, headers, rows):
        self.path.write_text('| ' + ' | '.join(headers) + ' |\n|' + '---|' * len(headers) + '\n' +
                             ''.join('| ' + ' | '.join(row) + ' |\n' for row in rows))

    def test_legacy_local_ids_and_shared_external_key_match_tracker_reader(self):
        self.write_table(['Jira', 'SberTrek', 'Summary', 'Kind', 'Role', 'Estimate (дн)', 'Status'], [
            ['OLD-1/FE', 'NEW-9', 'Form', 'real', 'FE', '3', 'created'],
            ['OLD-1/QA', 'NEW-9', 'Shared QA', 'real', 'QA', '7', 'created']])
        before = self.path.read_bytes()
        registry, _ = read_registry(self.path)
        tasks = overlay.load_tasks(self.feature)
        self.assertEqual(set(tasks), {row['Task ID'] for row in registry[0]})
        self.assertEqual(set(tasks), {'OLD-1/FE', 'OLD-1/QA'})
        self.assertEqual({task.tracker_key for task in tasks.values()}, {'NEW-9'})
        self.assertEqual(tasks['OLD-1/QA'].estimate, 7)
        self.assertEqual(self.path.read_bytes(), before)

    def test_historical_local_value_never_becomes_a_tracker_key(self):
        self.write_table(['Jira', 'Summary', 'Kind', 'Role', 'Estimate (дн)', 'Status'], [
            ['OLD-2', 'Local virtual work', 'virtual', 'BE', '2', 'planned']])
        task = overlay.load_tasks(self.feature)['OLD-2']
        self.assertEqual(task.tracker_key, '')
        self.assertEqual(task.kind, 'virtual')

    def test_explicit_local_id_wins_and_modern_validation_is_preserved(self):
        self.write_table(['Task ID', 'Jira', 'SberTrek', 'Summary', 'Kind', 'Role', 'Estimate (дн)', 'Status', 'Progress %'], [
            ['LOCAL', 'OLD-1', 'NEW-9', 'Explicit work', 'real', 'BE', '2', 'created', 'unknown']])
        self.assertEqual(set(overlay.load_tasks(self.feature)), {'LOCAL'})
        self.path.write_text(self.path.read_text().replace('unknown', ''))
        with self.assertRaisesRegex(ValueError, 'Progress %'):
            overlay.load_tasks(self.feature)

    def test_duplicate_ids_and_tracker_roles_in_additional_tables_are_rejected(self):
        headers = ['Jira', 'SberTrek', 'Summary', 'Kind', 'Role', 'Estimate (дн)', 'Status']
        self.write_table(headers, [['OLD-1/BE', 'NEW-1', 'One', 'real', 'BE', '2', 'created']])
        first = self.path.read_text()
        self.write_table(headers, [['OLD-1/BE', 'NEW-2', 'Two', 'real', 'BE', '2', 'created']])
        self.path.write_text(first + '\n' + self.path.read_text())
        with self.assertRaisesRegex(ValueError, 'Duplicate execution work item'):
            overlay.load_tasks(self.feature)
        self.write_table(headers, [['OLD-2/BE', 'NEW-1', 'Two', 'real', 'BE', '2', 'created']])
        self.path.write_text(first + '\n' + self.path.read_text())
        with self.assertRaisesRegex(ValueError, 'Duplicate tracker role'):
            overlay.load_tasks(self.feature)

    def test_malformed_legacy_table_blocks_generation_without_rewriting_it(self):
        self.write_table(['Jira', 'Summary', 'Kind', 'Role'], [['OLD-1', 'Work', 'real', 'BE']])
        self.path.write_text(self.path.read_text().replace('| real | BE |', '| real |'))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Повреждена строка'):
            overlay.load_tasks(self.feature)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
