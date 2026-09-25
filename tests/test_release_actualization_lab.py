import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

import release_actualization_lab as lab
import test_release_actualization_e2e as scenario


class PersistentLabTests(unittest.TestCase):
    def test_persistent_trial_and_independent_verification(self):
        self.exercise_trial()

    def test_raw_history_requires_extraction(self):
        self.exercise_trial(raw_history=True)

    def exercise_trial(self, *, raw_history=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'trial'
            metadata = lab.create(root, raw_history=raw_history)
            with self.assertRaises(ValueError):
                lab.create(root)
            initial = root / 'inputs/initial.json'
            self.assertTrue(initial.is_file())
            self.assertTrue((root / 'AGENT-TASK.md').is_file())
            loaded, environment = lab.load(root)
            resolved = lab.execute(loaded, environment, 'workspace.py', ['project-root'])
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(Path(resolved.stdout.strip()), Path(metadata['project']))
            with self.assertRaises(ValueError):
                lab.execute(loaded, environment, 'collaboration.py', ['save'])
            with self.assertRaisesRegex(ValueError, 'Registry fields'):
                lab.verify(root, initial)
            fixture = scenario.ReleaseActualizationEndToEndTests()
            fixture.root = root
            fixture.workspace = Path(metadata['workspace'])
            fixture.project = Path(metadata['project'])
            fixture.state = Path(metadata['state'])
            fixture.inputs = root / 'inputs'
            fixture.registry = fixture.project / 'features/registry/execution/tasks.md'
            fixture.gantt = fixture.project / 'planning/2026-Q3/gantt'
            fixture.run_id = metadata['run_id']
            fixture.initial_head = metadata['head']
            fixture.expected = json.loads(scenario.FIXTURE.read_text())
            fixture.manifest = json.loads(initial.read_text())
            fixture.decision = fixture.inputs / 'decision.txt'
            _, fixture.environment = lab.load(root)
            from unittest.mock import patch
            with patch.dict('os.environ', fixture.environment, clear=True):
                if raw_history:
                    self.assertFalse((fixture.inputs / 'expected.json').exists())
                    for entry in fixture.manifest['responses']:
                        self.assertEqual(entry['mapping'], {'request_key': '/key', 'text': ''})
                    old_review = next((fixture.state / 'tracker-runs' / fixture.run_id / 'history').glob('*.json'))
                    with self.assertRaises(ValueError):
                        lab.verify_history(json.loads(old_review.read_text()), metadata)
                    for entry in fixture.manifest['responses']:
                        entry['mapping']['text_extraction'] = self.extract(Path(entry['response_file']))
                fixture.write(fixture.registry.with_name('qa-groups.json'), metadata['groups'])
                fixture.write_registry(fixture.registry, metadata['rows'].values())
                review = fixture.review('final', confirmed=True)
                if raw_history:
                    lab.verify_history(review, metadata)
                    wrong = copy.deepcopy(review)
                    wrong['features'][0]['tasks']['LAB-112/BE']['development']['started_at'] = '2026-08-01T12:00:00+00:00'
                    with self.assertRaisesRegex(ValueError, 'lifecycle mismatch'):
                        lab.verify_history(wrong, metadata)
                fixture.qa_check(review)
                fixture.generate()
            self.assertEqual(lab.verify(root, Path(review['review_file']))['status'], 'agent-artifacts-verified')
            bad_rows = copy.deepcopy(metadata['rows'])
            bad_rows['LAB-202/FE']['Status'] = 'cancelled'
            fixture.write_registry(fixture.registry, bad_rows.values())
            with self.assertRaisesRegex(ValueError, 'Registry fields'):
                lab.verify(root, Path(review['review_file']))
            fixture.write_registry(fixture.registry, metadata['rows'].values())
            (fixture.inputs / 'LAB-101-history.txt').write_text('Changed evidence')
            with self.assertRaisesRegex(ValueError, 'Protected file changed'):
                lab.verify(root, Path(review['review_file']))

    def extract(self, path):
        text = path.read_text(encoding='utf-8')
        segments = []
        offset = 0

        def reference(value, start):
            begin = text.index(value, start)
            return {'start': begin, 'end': begin + len(value), 'quote': value}

        for index, line in enumerate(text.splitlines(keepends=True)):
            value = line.rstrip('\n')
            source = {'start': offset, 'end': offset + len(value),
                      'sha256': hashlib.sha256(value.encode()).hexdigest()}
            if index < 2:
                segments.append({'kind': 'context' if index == 0 else 'metadata',
                                 'source': source, 'reason': 'Pagination' if index == 0 else 'Creation actor only'})
            else:
                field = 'assigned_to' if 'assigned_to' in value else 'assignee'
                field_start = text.index(field, offset)
                operation = 'ADD' if 'ADD' in value else 'CHANGE'
                segments.append({'kind': 'assignment', 'source': source,
                    'field': reference(field, offset),
                    'at': reference(re.search(r'2026-[^\s\];]+', value).group(), offset),
                    'before': None if operation == 'ADD' else reference('dev', field_start),
                    'after': reference('dev' if operation == 'ADD' else 'qa', field_start),
                    'operation': {'kind': 'add' if operation == 'ADD' else 'change',
                                  'source': reference(operation, offset)}})
            offset += len(line)
        return {'schema_version': 1, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'segments': segments, 'pagination': {
                    'count': reference(str(len(segments) - 1), 0),
                    'start': reference('0', 0), 'has_next': reference('False', 0)}}

    def test_refuses_repository_and_symlink(self):
        with self.assertRaisesRegex(ValueError, 'outside existing Git'):
            lab.create(lab.ROOT / 'never-create-this-lab')
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / 'link'
            link.symlink_to(Path(temporary) / 'missing')
            with self.assertRaises(ValueError):
                lab.create(link)


if __name__ == '__main__':
    unittest.main()
