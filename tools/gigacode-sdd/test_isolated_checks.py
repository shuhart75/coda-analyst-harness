import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import isolated_checks as isolated


class IsolatedChecksTests(unittest.TestCase):
    def test_unavailable_backend_never_returns_plain_command(self):
        with patch('isolated_checks.shutil.which', return_value=None):
            for platform in ('linux', 'darwin', 'win32'):
                with self.assertRaises(isolated.IsolationUnavailable):
                    isolated.command(Path('/tmp/check'), ['npm', 'install'], [], platform=platform)

    def test_macos_profile_only_allows_allocated_ports_and_scratch_writes(self):
        with patch('isolated_checks.shutil.which', return_value='/usr/bin/sandbox-exec'):
            argv = isolated.command(PurePosixPath('/tmp/check'), ['python3', 'test.py'], [43111], platform='darwin')
        profile = argv[2]
        self.assertIn('(deny default)', profile)
        self.assertIn('localhost:43111', profile)
        self.assertNotIn('(allow default)', profile)
        self.assertNotIn('localhost:*', profile)
        self.assertIn('(subpath "/tmp/check")', profile)

    def test_real_disposable_check(self):
        if sys.platform not in ('linux', 'darwin'):
            self.skipTest('No supported isolation backend')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = root / 'work'; work.mkdir()
            (work / 'check.py').write_text('''import json,os,pathlib,socket
assert 'SECRET_TOKEN' not in os.environ
assert not os.environ.get('HTTP_PROXY')
s=socket.socket(); s.bind(('127.0.0.1',json.loads(os.environ['SDD_TEST_PORTS'])[0])); s.listen()
c=socket.create_connection(s.getsockname()); peer,_=s.accept(); c.close(); peer.close(); s.close()
pathlib.Path('result.txt').write_text('passed')
print('TEST_PASSED')
''')
            runner = SimpleNamespace(env={**os.environ, 'SECRET_TOKEN': 'secret', 'HTTP_PROXY': 'http://host:1'},
                                     logdir=lambda name: root / name)
            request = {'reason': 'local negative fixture', 'inputs': ['check.py'],
                       'argv': [sys.executable, 'check.py'], 'local_ports': 1, 'timeout': 5}
            try:
                result = isolated.run(runner, work, request, threading.Event())
            except isolated.IsolationUnavailable as exc:
                if sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true':
                    diagnostic = root / 'isolated-check/isolation.json'
                    self.fail(diagnostic.read_text() if diagnostic.exists() else str(exc))
                self.assertFalse((root / 'isolated-check/output.log').exists())
                self.skipTest(str(exc))
            self.assertTrue(result['ok'], Path(result['log']).read_text())
            self.assertFalse((work / 'result.txt').exists())
            self.assertEqual((Path(result['scratch']) / 'result.txt').read_text(), 'passed')
            self.assertFalse((root / 'isolated-check/escape-probe').exists())

    def test_escape_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); (root/'check.py').write_text('')
            for name in ('..', '.', '../check.py'):
                with self.assertRaises((ValueError, RuntimeError)):
                    isolated.validate({'reason': 'test', 'inputs': [name], 'argv': ['python3']}, root)


if __name__ == '__main__':
    unittest.main()
