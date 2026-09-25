import copy
import json
from pathlib import Path
import tempfile
import unittest

import release_actualization_lab as lab
import test_release_actualization_e2e as scenario


class PersistentLabTests(unittest.TestCase):
    def test_persistent_trial_and_independent_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'trial'
            metadata = lab.create(root)
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
            fixture.expected = json.loads((fixture.inputs / 'expected.json').read_text())
            fixture.manifest = json.loads(initial.read_text())
            fixture.decision = fixture.inputs / 'decision.txt'
            _, fixture.environment = lab.load(root)
            from unittest.mock import patch
            with patch.dict('os.environ', fixture.environment, clear=True):
                fixture.write(fixture.registry.with_name('qa-groups.json'), metadata['groups'])
                fixture.write_registry(fixture.registry, metadata['rows'].values())
                review = fixture.review('final', confirmed=True)
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
