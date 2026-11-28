"""The harness adapter pins executable code and never mutates saved runs."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = ROOT / 'tools' / 'sdd-adapter'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ADAPTER_ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


adapter = module('sdd_adapter_under_test', 'adapter.py')
with patch.dict(sys.modules, {'adapter': adapter}):
    installer = module('sdd_installer_under_test', 'install.py')


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sdd adapter test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source repo'
        self.source.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'core.autocrlf', 'false')
        (self.source / '.gitignore').write_text('__pycache__/\nignored-*\n', encoding='utf-8')
        (self.source / 'bootstrap.py').write_text(
            'import json, os, sys\n'
            'print(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),'
            '"harness":os.environ.get("SDD_HARNESS_ROOT")}))\n', encoding='utf-8')
        self.git('add', '.gitignore', 'bootstrap.py')
        self.git('-c', 'user.name=SDD Test', '-c', 'user.email=sdd-test@example.invalid',
                 'commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD')
        self.lock = self.root / 'runtime-lock.json'
        self.lock.write_text(json.dumps({'schema': 1, 'repository': str(self.source),
                                         'commit': self.commit}), encoding='utf-8')
        self.cache = self.root / 'user cache'
        environment = dict(os.environ)
        environment.pop('SDD_ORCHESTRATOR_HOME', None)
        environment['SDD_ORCHESTRATOR_CACHE'] = str(self.cache)
        self.env = patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.source), *args], capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def install(self):
        return installer.install(source=self.source, destination_root=self.cache, lock_path=self.lock)

    def test_missing_runtime_explains_explicit_install_without_creating_run_data(self):
        with self.assertRaisesRegex(adapter.AdapterError, 'install.py'):
            adapter.resolve_runtime(self.lock)
        self.assertFalse(self.cache.exists())

    def test_missing_runtime_quotes_python_path_with_spaces(self):
        with patch.object(adapter.sys, 'executable', '/Python installation/python3'):
            with self.assertRaises(adapter.AdapterError) as caught:
                adapter.resolve_runtime(self.lock)
        self.assertIn("'/Python installation/python3'", str(caught.exception))

    def test_custom_cache_install_explains_launch_setting(self):
        output = io.StringIO()
        with patch.object(installer, 'install', return_value=self.cache/'versions'/self.commit), contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(['--cache-root', str(self.cache)]), 0)
        self.assertIn('SDD_ORCHESTRATOR_CACHE=' + str(self.cache.resolve()), output.getvalue())

    def test_pin_is_required_and_never_falls_back_to_main(self):
        self.lock.write_text(json.dumps({'schema': 1, 'repository': str(self.source), 'commit': '0'*40}))
        with self.assertRaises(adapter.AdapterError):
            installer.install(source=self.source, destination_root=self.cache, lock_path=self.lock)
        self.assertFalse(self.cache.exists())
        for value in ([], None, {'schema': 1, 'repository': str(self.source), 'commit': []}):
            self.lock.write_text(json.dumps(value))
            with self.assertRaises(adapter.AdapterError):
                adapter.load_lock(self.lock)

    def test_install_is_pinned_atomic_and_reusable(self):
        root = self.install()
        self.assertEqual(root, self.cache / 'versions' / self.commit)
        self.assertEqual(adapter.resolve_runtime(self.lock), root)
        with patch.object(installer.subprocess, 'run', wraps=subprocess.run) as execute:
            self.assertEqual(self.install(), root)
            self.assertFalse(any('clone' in call.args[0] for call in execute.call_args_list))
        self.assertEqual(list((self.cache / 'versions').iterdir()), [root])

    def test_install_fetches_committed_code_without_changing_dirty_source(self):
        source = self.source / 'bootstrap.py'
        committed = source.read_text()
        source.write_text('uncommitted and not executable')
        status = self.git('status', '--porcelain')
        installed = self.install()
        self.assertEqual((installed / 'bootstrap.py').read_text(), committed)
        self.assertEqual(source.read_text(), 'uncommitted and not executable')
        self.assertEqual(self.git('status', '--porcelain'), status)

    def test_wrong_revision_and_dirty_runtime_refuse_launch_without_repairing_files(self):
        installed = self.install()
        with patch.dict(os.environ, {'SDD_ORCHESTRATOR_HOME': str(self.source)}):
            wrong = adapter.load_lock(self.lock)
            wrong['commit'] = '1'*40
            with self.assertRaisesRegex(adapter.AdapterError, 'отличается'):
                adapter.validate_runtime(self.source, wrong)
        path = installed / 'bootstrap.py'
        path.write_text('changed')
        with self.assertRaisesRegex(adapter.AdapterError, 'изменены'):
            adapter.resolve_runtime(self.lock)
        with self.assertRaisesRegex(adapter.AdapterError, 'изменены'):
            self.install()
        self.assertEqual(path.read_text(), 'changed')

    def test_index_flags_cannot_hide_changed_executable_bytes(self):
        installed = self.install()
        adapter.git(installed, 'update-index', '--assume-unchanged', 'bootstrap.py')
        (installed / 'bootstrap.py').write_text('tampered behind assume-unchanged')
        self.assertEqual(adapter.git(installed, 'status', '--porcelain', '--untracked-files=no'), '')
        with self.assertRaisesRegex(adapter.AdapterError, 'Содержимое файла'):
            adapter.resolve_runtime(self.lock)

    def test_untracked_modules_cannot_shadow_imports_but_diagnostics_do_not_block(self):
        installed = self.install()
        (installed / 'diagnostics.txt').write_text('ordinary runtime output')
        cache = installed / '__pycache__'
        cache.mkdir()
        (cache / 'bootstrap.cpython-312.pyc').write_bytes(b'untrusted cache, never loaded')
        self.assertEqual(adapter.resolve_runtime(self.lock), installed)
        for name in ('rogue.py', 'ignored-rogue.py', 'rogue.pyc'):
            path = installed / name
            path.write_text('raise SystemExit(91)')
            with self.assertRaisesRegex(adapter.AdapterError, 'Незарегистрированный'):
                adapter.resolve_runtime(self.lock)
            path.unlink()

    def test_launch_preserves_arguments_cwd_exit_and_harness_discovery(self):
        installed = self.install()
        launcher = self.root / 'old launcher.py'
        launcher.write_text('import sys\nsys.path.insert(0, ' + repr(str(ADAPTER_ROOT)) + ')\n'
            'from adapter import main\nraise SystemExit(main("bootstrap.py", lock_path=' + repr(str(self.lock)) + '))\n')
        work = self.root / 'saved work'
        work.mkdir()
        sentinel = work / 'state.json'
        sentinel.write_text('{"saved": true}')
        result = subprocess.run([sys.executable, str(launcher), '--config', 'path with spaces', 'юникод'],
                                cwd=work, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['args'], ['--config', 'path with spaces', 'юникод'])
        self.assertEqual(Path(value['cwd']).resolve(), work.resolve())
        self.assertEqual(Path(value['harness']).resolve(), ROOT.resolve())
        self.assertEqual(sentinel.read_text(), '{"saved": true}')
        self.assertEqual(adapter.resolve_runtime(self.lock), installed)

    def test_launch_has_no_network_and_replaces_process_with_isolated_python(self):
        installed = self.install()
        with patch.object(adapter.subprocess, 'run', wraps=subprocess.run) as execute, \
                patch.object(adapter, 'launch', return_value=0) as replace:
            self.assertEqual(adapter.main('bootstrap.py', ['--help'], lock_path=self.lock), 0)
        self.assertFalse(any(any(word in call.args[0] for word in ('clone', 'fetch', 'pull'))
                             for call in execute.call_args_list))
        command, env = replace.call_args.args
        self.assertEqual(command[-2:], [str(installed / 'bootstrap.py'), '--help'])
        self.assertIn('-E', command)
        self.assertIn('-B', command)
        self.assertEqual(env['SDD_HARNESS_ROOT'], str(ROOT))
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertFalse(Path(env['PYTHONPYCACHEPREFIX']).exists())

    def test_windows_waits_for_controller_cleanup_after_console_interrupt(self):
        with patch.object(adapter.sys, 'platform', 'win32'), patch.object(adapter.subprocess, 'Popen') as create:
            create.return_value.wait.side_effect = [KeyboardInterrupt(), 17]
            self.assertEqual(adapter.launch(['python', 'controller.py'], {}), 17)
            create.return_value.kill.assert_not_called()
            create.return_value.terminate.assert_not_called()

    def test_failed_install_does_not_publish_partial_version(self):
        lock = adapter.load_lock(self.lock)
        lock['commit'] = '1'*40
        self.lock.write_text(json.dumps(lock))
        with self.assertRaises(adapter.AdapterError):
            self.install()
        self.assertEqual(list((self.cache / 'versions').iterdir()), [])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.commit)

    def test_missing_or_foreign_entrypoint_is_not_executed(self):
        self.install()
        for name in ('../bootstrap.py', 'missing.py', 'ignored-rogue.py'):
            with patch.object(adapter.os, 'execve') as replace, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(adapter.main(name, lock_path=self.lock), 2)
                replace.assert_not_called()


if __name__ == '__main__':
    unittest.main()
