"""Portable locking and Windows ownership/launch boundaries."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import platform_support as platform
from transport import finish_process


class PlatformTests(unittest.TestCase):
    def test_lock_excludes_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'run.lock'
            code = ('from platform_support import FileLock; import sys\n'
                    'try:\n with FileLock(sys.argv[1]): pass\n'
                    'except BlockingIOError: sys.exit(17)\n')
            command = [sys.executable, '-c', code, str(lock_path)]
            with platform.FileLock(lock_path):
                self.assertEqual(subprocess.run(command, cwd=Path(__file__).parent).returncode, 17)
            self.assertEqual(subprocess.run(command, cwd=Path(__file__).parent).returncode, 0)

    def test_current_process_exists_without_signalling_windows(self):
        self.assertTrue(platform.process_exists(os.getpid()))
        api = Mock()
        api.OpenProcess.return_value = 51
        def exit_code(handle, destination):
            destination._obj.value = 259
            return True
        api.GetExitCodeProcess.side_effect = exit_code
        with patch.object(platform, 'WINDOWS', True), patch.object(platform, '_kernel32', return_value=api), \
                patch.object(platform.os, 'kill') as kill:
            self.assertTrue(platform.process_exists(321))
            kill.assert_not_called()
            api.CloseHandle.assert_called_once_with(51)

    def test_windows_inaccessible_process_is_preserved(self):
        api = Mock()
        api.OpenProcess.return_value = 0
        with patch.object(platform, 'WINDOWS', True), patch.object(platform, '_kernel32', return_value=api), \
                patch.object(ctypes, 'get_last_error', return_value=5, create=True):
            self.assertTrue(platform.process_exists(321))
        with patch.object(platform, 'WINDOWS', True), patch.object(platform, '_kernel32', return_value=api), \
                patch.object(ctypes, 'get_last_error', return_value=87, create=True):
            self.assertFalse(platform.process_exists(321))

    def test_unowned_windows_scope_is_never_terminated(self):
        process = Mock(pid=123)
        with patch.object(platform, 'WINDOWS', True):
            with self.assertRaisesRegex(OSError, 'unowned'):
                platform.stop_process_scope(process)
        process.kill.assert_not_called()

    def test_owned_windows_scope_terminates_job_not_pid(self):
        api, process = Mock(), Mock(pid=123)
        with patch.object(platform, 'WINDOWS', True), patch.dict(platform._jobs, {123: (api, 51)}):
            platform.stop_process_scope(process)
            api.TerminateJobObject.assert_called_once_with(51, 1)
            api.CloseHandle.assert_called_once_with(51)
        process.kill.assert_not_called()

    def test_windows_npm_shim_is_resolved_without_shell_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'node_modules/@vendor/gigacode/cli.js'
            script.parent.mkdir(parents=True)
            script.write_text('// fixture', encoding='utf-8')
            (root / 'node.exe').touch()
            shim = root / 'gigacode.cmd'
            shim.write_text('@"%dp0%\\node.exe" "%dp0%\\node_modules\\@vendor\\gigacode\\cli.js" %*', encoding='utf-8')
            literal = 'argument & echo injected %PATH% "quoted"'
            with patch.object(platform, 'WINDOWS', True), patch.object(platform.shutil, 'which', return_value=str(shim)):
                argv = platform.resolve_argv(['gigacode', literal])
            self.assertEqual(argv, [str(root / 'node.exe'), str(script.resolve()), literal])
            shim.write_text('echo unsupported %*', encoding='utf-8')
            with patch.object(platform, 'WINDOWS', True), patch.object(platform.shutil, 'which', return_value=str(shim)):
                with self.assertRaisesRegex(OSError, 'Unsupported Windows batch'):
                    platform.resolve_argv(['gigacode'])


@unittest.skipUnless(os.name == 'nt', 'Native Windows Job Objects')
class WindowsJobTests(unittest.TestCase):
    def test_descendant_remains_owned_after_cli_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'child-survived'
            child = f'import time,pathlib;time.sleep(1.5);pathlib.Path({str(marker)!r}).touch()'
            parent = f'import subprocess,sys;subprocess.Popen([sys.executable,"-c",{child!r}])'
            process = platform.launch_process([sys.executable, '-c', parent], cwd=directory)
            process.wait(timeout=10)
            self.assertTrue(platform.process_scope_pids(process.pid))
            finish_process(process)
            time.sleep(1.7)
            self.assertFalse(marker.exists())

    def test_guard_preserves_job_on_timeout(self):
        process = platform.launch_process([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            result = finish_process(process, lambda _: {'reason': 'service'})
            self.assertTrue(result['preserved'])
            self.assertIsNone(process.poll())
            self.assertTrue(platform.process_scope_pids(process.pid))
        finally:
            platform.stop_process_scope(process)
            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
