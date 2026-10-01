import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/validate-links.py'


class SnapshotLinksTest(unittest.TestCase):
    def check_tree(self, files):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, text in files.items():
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
            return subprocess.run([sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True)

    def test_external_source_relative_link_survives_immutable_snapshot(self):
        result = self.check_tree({
            'baseline-review.md': '# Evidence',
            'baseline/versions/before-hash/VERSION.md': '[Evidence](../../baseline-review.md)',
            'baseline/versions/documentation/v1/VERSION.md': '[Evidence](../../baseline-review.md)',
        })
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_missing_snapshot_internal_file_is_not_borrowed_from_current(self):
        result = self.check_tree({
            'baseline/current/domain/rules.md': '# Current rules',
            'baseline/versions/old/README.md': '[Rules](domain/rules.md)',
        })
        self.assertEqual(result.returncode, 1)

    def test_relocated_review_resolves_only_in_immutable_snapshot(self):
        files = {'releases/baseline-reconciliation/baseline-review.md': '# Evidence',
                 'baseline/versions/before-hash/VERSION.md': '[Evidence](../../baseline-review.md)'}
        self.assertEqual(self.check_tree(files).returncode, 0)
        files['features/demo/README.md'] = '[Stale](../../baseline-review.md)'
        self.assertEqual(self.check_tree(files).returncode, 1)

    def test_broken_current_and_missing_external_links_still_fail(self):
        for name, link in [
            ('baseline/current/README.md', 'missing.md'),
            ('baseline/versions/old/VERSION.md', '../../missing.md'),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.check_tree({name: f'[Missing]({link})'}).returncode, 1)


if __name__ == '__main__':
    unittest.main()
