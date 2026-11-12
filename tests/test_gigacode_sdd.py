"""Run the standalone SDD controller suite in its own import/process namespace."""
from pathlib import Path
import subprocess
import sys
import unittest


class GigaCodeSDDTests(unittest.TestCase):
    def test_standalone_runner_suite(self):
        tool = Path(__file__).resolve().parents[1] / 'tools' / 'gigacode-sdd'
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-v'],
                                cwd=tool, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
