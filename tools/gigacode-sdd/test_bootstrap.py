"""Portable console selection, privacy and safe handoff tests."""
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import bootstrap
from controller import Blocked, save


class UI:
    def __init__(self, choices=(), folder=None, file=None):
        self.choices = iter(choices)
        self.folder_value = folder
        self.file_value = file
        self.confirmations = []
        self.options = []

    def choose(self, title, items):
        self.options.append(items)
        return next(self.choices)

    def folder(self, _):
        return self.folder_value

    def file(self, _):
        return self.file_value

    def confirm(self, message, action='Запустить'):
        self.confirmations.append(message)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'XDG_STATE_HOME': '', 'LOCALAPPDATA': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='sdd-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.repo = self.home / 'work' / 'PoC with spaces'
        self.repo.mkdir(parents=True)
        for name in ('backend', 'frontend'):
            (self.repo / name).mkdir()
        subprocess.run(['git', 'init', str(self.repo)], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self.packet = self.home / 'Downloads' / 'new packet'
        self.packet.mkdir(parents=True)
        (self.packet / 'requirements.md').write_text('# Модульная симуляция\nREQ-SIM-1\n', encoding='utf-8')
        (self.packet / 'manifest.json').write_text(json.dumps({'revision': 3, 'feature': 'sim'}), encoding='utf-8')
        self.safe_inventory = types.SimpleNamespace(
            inspect_services=Mock(return_value={'complete': True, 'limitations': [], 'services': []}),
            assess_handoff=Mock(return_value={'blocked': False, 'blockers': [], 'warnings': []}))

    def test_bounded_discovery_uses_workspace_declared_code_and_no_symlink(self):
        harness = self.home / 'work' / 'harness'
        harness.mkdir()
        (harness / '.analyst-workspace.json').write_text(json.dumps({'code': {'path': '../PoC with spaces'}}), encoding='utf-8')
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'private').mkdir()
        try:
            (self.home / 'work' / 'link').symlink_to(outside, target_is_directory=True)
        except OSError:
            pass  # Windows may restrict symlinks; registry assertions still run.
        repos, protected = bootstrap.discover_repositories([self.home / 'work'])
        self.assertEqual(repos, [self.repo])
        self.assertEqual(protected, [self.repo])
        self.assertNotIn(outside / 'private', list(bootstrap.bounded_dirs([self.home / 'work'])))

    def test_packet_choice_does_not_choose_largest_revision_or_newest_mtime(self):
        old = self.home / 'Downloads' / 'old packet'
        old.mkdir()
        (old / 'requirements.md').write_text('# Старая\nREQ-OLD-1\n', encoding='utf-8')
        (old / 'manifest.json').write_text(json.dumps({'revision': 99}), encoding='utf-8')
        candidates = bootstrap.discover_requirements(self.repo, [self.home / 'Downloads'])
        selected = bootstrap.choose_requirements(UI([0]), candidates)
        self.assertEqual(Path(selected['file']), self.packet / 'requirements.md')
        self.assertIn('редакция 3', selected['label'])
        self.assertEqual(len(candidates), 2)

    def test_coda_registry_explicit_absolute_code_path_is_protected(self):
        harness = self.home / 'work' / 'coda-harness'
        state = harness / '.workspace-state'
        state.mkdir(parents=True)
        (state / 'workspace.json').write_text(json.dumps({'schema_version': 4, 'roles': {
            'code': {'repository': 'coda', 'path': str(self.repo), 'availability': 'ready'}}}), encoding='utf-8')
        repos, protected = bootstrap.discover_repositories([self.home / 'work'])
        self.assertEqual(repos, [self.repo])
        self.assertEqual(protected, [self.repo])
        (state / 'workspace.json').write_text(json.dumps({'schema_version': 4, 'roles': {
            'code': {'repository': 'coda', 'path': '../PoC with spaces', 'availability': 'ready'}}}), encoding='utf-8')
        self.assertEqual(bootstrap.declared_code_roots(harness), [])

    def test_invalid_repository_selection_is_not_replaced(self):
        with self.assertRaises(Blocked):
            bootstrap.choose_repository(UI(folder=self.home), [])

    def test_nexus_inference_never_logs_credentials_or_accepts_suffix_host(self):
        npm = self.repo / 'frontend/.npmrc'
        npm.write_text('registry=https://nexus-ci.delta.sbrf.ru/repository/npm/\n//nexus-ci.delta.sbrf.ru/:_authToken=secret\n', encoding='utf-8')
        result = bootstrap.infer_nexus(self.repo, self.home, {})
        self.assertEqual(result['npm_registry'], 'https://nexus-ci.delta.sbrf.ru/repository/npm/')
        self.assertNotIn('secret', str(result))
        for value in ('https://nexus-ci.delta.sbrf.ru.evil.test/npm',
                      'https://user:secret@nexus-ci.delta.sbrf.ru/npm'):
            with self.assertRaises(Blocked) as caught:
                bootstrap.infer_nexus(self.repo, self.home, {'NPM_CONFIG_REGISTRY': value})
            self.assertNotIn('secret', str(caught.exception))

    def test_unknown_registry_is_offline_not_invented(self):
        settings = bootstrap.infer_nexus(self.repo, self.home, {})
        self.assertIsNone(settings['npm_registry'])
        self.assertIsNone(settings['pip_index_url'])

    def test_resume_uses_exact_saved_config_and_rejects_outside_reference(self):
        root = bootstrap.app_root(self.home)
        state = bootstrap.new_state_dir(root)
        config = state / 'config.json'
        save(config, {'state_dir': str(state), 'source': str(self.repo), 'requirements_sha256': 'a' * 64})
        save(state / 'state.json', {'status': 'blocked'})
        save(root / 'last-run.json', {'config': str(config)})
        self.assertEqual(bootstrap.known_runs(root)[0]['config'], str(config))
        save(root / 'last-run.json', {'config': str(self.packet / 'manifest.json')})
        self.assertEqual(bootstrap.known_runs(root), [])

    def test_dependent_stand_blocks_before_initialization_without_killing(self):
        self.safe_inventory.assess_handoff.return_value = {'blocked': True, 'blockers': ['PostgreSQL depends on old GigaCode']}
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}), \
                patch.object(bootstrap, 'initialize') as init:
            with self.assertRaisesRegex(Blocked, 'Текущую сессию и сервисы не завершайте'):
                bootstrap.launch(UI(), self.home, repo_arg=str(self.repo))
        init.assert_not_called()
        report = next((bootstrap.app_root(self.home) / 'diagnostics').glob('*.json'))
        if os.name != 'nt':
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertFalse((bootstrap.app_root(self.home) / 'runs').exists())

    def test_incomplete_inventory_blocks_even_without_known_service(self):
        self.safe_inventory.inspect_services.return_value = {'complete': False, 'limitations': ['lsof unavailable']}
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}):
            with self.assertRaisesRegex(Blocked, 'lsof unavailable'):
                bootstrap.require_handoff(bootstrap.app_root(self.home), self.repo)

    def test_diagnosis_does_not_start_runner_or_need_gui(self):
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}), \
                patch.object(bootstrap, 'Runner') as runner, \
                patch.object(bootstrap, 'initialize') as init, redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.launch(UI(), self.home, diagnose_only=True, repo_arg=str(self.repo)), 0)
        runner.assert_not_called()
        init.assert_not_called()

    def test_new_run_automates_paths_and_hash_and_waits_for_confirmation(self):
        other = self.home / 'work' / 'registered-primary'
        other.mkdir()
        (other / 'backend').mkdir()
        (other / 'frontend').mkdir()
        subprocess.run(['git', 'init', str(other)], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        ui = UI([0])  # exact requirements choice; source is provided by advanced caller
        fake_runner = Mock()
        fake_runner.locked.return_value = nullcontext()
        fake_runner.state = {'status': 'complete'}
        fake_runner.main = self.home / 'managed-result'
        fake_runner.stop.is_set.return_value = False
        captured = []
        def init(args):
            self.assertEqual(len(ui.confirmations), 1)
            captured.append(args)
            return str(Path(args.state_dir) / 'config.json')
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}), \
                patch.object(bootstrap, 'choose_prior_history', return_value=None), \
                patch.object(bootstrap, 'infer_nexus', return_value={'nexus_host': [bootstrap.NEXUS_HOST], 'npm_registry': None, 'pip_index_url': None}), \
                patch.object(bootstrap, 'find_cli', return_value='/installed/gigacode'), \
                patch.object(bootstrap, 'initialize', side_effect=init), \
                patch.object(bootstrap, 'Runner', return_value=fake_runner), redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.launch(ui, self.home, repo_arg=str(self.repo)), 0)
        args = captured[0]
        self.assertEqual(args.requirements_sha256, bootstrap.digest(self.packet / 'requirements.md'))
        self.assertEqual(args.packet, str(self.packet))
        self.assertTrue(Path(args.state_dir).is_relative_to(bootstrap.app_root(self.home) / 'runs'))
        self.assertFalse(Path(args.state_dir).is_relative_to(self.repo))
        self.assertEqual(args.commit_prefix, 'SDD PoC')
        # Discovery of an unrelated PoC is not proof that it is the registered code clone.
        self.assertEqual(args.protected_root, [])
        fake_runner.start.assert_called_once_with()

    def test_launcher_rejects_agent_ancestor_from_another_working_directory(self):
        self.safe_inventory.inspect_services.return_value = {
            'complete': True, 'limitations': [], 'services': [],
            'processes': [{'pid': 123, 'ppid': 122, 'pgid': None},
                          {'pid': 122, 'ppid': 120}, {'pid': 120, 'ppid': 1}],
            'agents': [{'pid': 120, 'pgid': 120, 'cwd': '/different-project'}]}
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}), \
                patch.object(bootstrap.os, 'getpid', return_value=123):
            with self.assertRaisesRegex(Blocked, 'отдельном терминале'):
                bootstrap.require_handoff(bootstrap.app_root(self.home), self.repo)

    def test_git_failure_does_not_expose_hook_stderr(self):
        ui = UI([0])
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}), \
                patch.object(bootstrap, 'choose_prior_history', return_value=None), \
                patch.object(bootstrap, 'infer_nexus', return_value={'nexus_host': [bootstrap.NEXUS_HOST], 'npm_registry': None, 'pip_index_url': None}), \
                patch.object(bootstrap, 'find_cli', return_value='/installed/gigacode'), \
                patch.object(bootstrap, 'initialize', side_effect=bootstrap.WorkspaceError('hook token=verysecret')):
            with self.assertRaises(Blocked) as caught:
                bootstrap.launch(ui, self.home, repo_arg=str(self.repo))
        self.assertNotIn('verysecret', str(caught.exception))
        self.assertIn('Не обходите hook', str(caught.exception))
        reports = list(bootstrap.app_root(self.home).glob('runs/*/initialization-error.json'))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report['phase'], 'initialize')
        self.assertIn('hook', report['detail'])
        self.assertNotIn('verysecret', reports[0].read_text())
        if os.name != 'nt':
            self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)

    def test_verified_windows_ancestry_boundary_does_not_need_dead_parent(self):
        for boundary in ('desktop-shell', 'different-user'):
            inventory = {'agents': [{'pid': 400}], 'processes': [
                {'pid': 123, 'ppid': 122},
                {'pid': 122, 'ppid': 999, 'ancestry_boundary': boundary}]}
            with patch.object(bootstrap.os, 'getpid', return_value=123):
                self.assertEqual(bootstrap.launcher_dependency(inventory),
                                 {'complete': True, 'agent_pids': []})

    def test_ancestry_boundary_does_not_hide_agent_or_accept_unverified_label(self):
        inventory = {'agents': [{'pid': 122}], 'processes': [
            {'pid': 123, 'ppid': 122},
            {'pid': 122, 'ppid': 999, 'ancestry_boundary': 'desktop-shell'}]}
        with patch.object(bootstrap.os, 'getpid', return_value=123):
            self.assertEqual(bootstrap.launcher_dependency(inventory),
                             {'complete': True, 'agent_pids': [122]})
            inventory['agents'] = [{'pid': 400}]
            inventory['processes'][1]['ancestry_boundary'] = 'explorer.exe'
            self.assertEqual(bootstrap.launcher_dependency(inventory),
                             {'complete': False, 'agent_pids': []})

    def test_console_reprompts_invalid_numbers_and_never_runs_display_data(self):
        responses = iter(['', '0', '99', '1+1', '2'])
        output = []
        ui = bootstrap.ConsoleUI(lambda _: next(responses), output.append)
        hostile = 'project "; touch /tmp/forbidden; $(exit 1)'
        with patch.object(bootstrap.subprocess, 'run') as run:
            self.assertEqual(ui.choose('Select', ['first', hostile]), 1)
        run.assert_not_called()
        self.assertIn('2. ' + hostile, output)

    def test_console_cancel_eof_and_confirmation_are_conservative(self):
        for value in ('q', 'отмена', ''):
            ui = bootstrap.ConsoleUI(lambda _: value, lambda _: None)
            with self.assertRaises(Blocked):
                ui.confirm('Start?')
        for exception in (EOFError, KeyboardInterrupt):
            ui = bootstrap.ConsoleUI(Mock(side_effect=exception), lambda _: None)
            with self.assertRaises(Blocked):
                ui.choose('Select', ['one'])

    def test_console_accepts_full_unicode_path_with_spaces_and_quotes(self):
        directory = self.home / "Пакет O'Brien with spaces"
        directory.mkdir()
        ui = bootstrap.ConsoleUI(lambda _: '"' + str(directory) + '"', lambda _: None)
        self.assertEqual(ui.folder('Folder'), directory.resolve())

    def test_state_directories_follow_platform_conventions(self):
        local, xdg = self.home / 'Local State', self.home / 'XDG State'
        self.assertEqual(bootstrap.app_root(self.home, 'win32', {'LOCALAPPDATA': str(local)}),
                         local / 'GigaCode SDD')
        self.assertEqual(bootstrap.app_root(self.home, 'win32', {}),
                         self.home / 'AppData/Local/GigaCode SDD')
        self.assertEqual(bootstrap.app_root(self.home, 'darwin', {}),
                         self.home / 'Library/Application Support/GigaCode SDD')
        self.assertEqual(bootstrap.app_root(self.home, 'linux', {'XDG_STATE_HOME': str(xdg)}),
                         xdg / 'gigacode-sdd')
        self.assertEqual(bootstrap.app_root(self.home, 'linux', {'XDG_STATE_HOME': 'relative'}),
                         self.home / '.local/state/gigacode-sdd')

    def test_entrypoint_checks_python_from_arbitrary_directory(self):
        result = subprocess.run([sys.executable, str(Path(bootstrap.__file__).resolve()), '--check-python'],
                                cwd=self.home, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_redirected_console_output_is_utf8_with_legacy_default_encoding(self):
        message = 'Выберите проект'
        script = ('import bootstrap; '
                  'bootstrap.launch = lambda **kwargs: bootstrap.ConsoleUI().notify('
                  + repr(message) + '); bootstrap.main([])')
        result = subprocess.run([sys.executable, '-c', script],
                                cwd=Path(bootstrap.__file__).parent,
                                env=dict(os.environ, PYTHONIOENCODING='ascii'), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode('utf-8').strip(), message)

    def test_optional_history_is_not_latest_thread_and_supports_explicit_choice(self):
        discovered = types.SimpleNamespace(discover_prior_context=Mock(return_value={'candidates': [
            {'path': str(self.home / 'older.jsonl')}, {'path': str(self.home / 'newer.jsonl')}]}))
        with patch.dict(sys.modules, {'prior_context': discovered}):
            self.assertIsNone(bootstrap.choose_prior_history(UI([0]), self.repo))
            self.assertEqual(bootstrap.choose_prior_history(UI([1]), self.repo), str(self.home / 'older.jsonl'))

    def test_history_from_wrapper_needs_explicit_mapping_and_pins_exact_export(self):
        history = self.home / 'old.jsonl'
        wrapper = self.home / 'work' / 'coda-local-dev'
        history.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(wrapper)}}) + '\n'
                           + json.dumps({'role': 'assistant', 'content': 'Old implementation was attempted.'}) + '\n', encoding='utf-8')
        skipped = UI([0])
        self.assertEqual(bootstrap.confirm_history_scope(skipped, str(history), self.repo), (None, False, None))
        confirmed = UI([1])
        selected, mapping, checksum = bootstrap.confirm_history_scope(confirmed, str(history), self.repo)
        self.assertEqual(selected, str(history))
        self.assertTrue(mapping)
        self.assertEqual(checksum, bootstrap.digest(history))
        self.assertEqual(confirmed.options[0][0], 'Без журнала')

    def test_matching_history_does_not_add_an_exception_confirmation(self):
        history = self.home / 'matched.jsonl'
        history.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(self.repo)}}) + '\n'
                           + json.dumps({'role': 'assistant', 'content': 'Historical result.'}) + '\n', encoding='utf-8')
        ui = UI()
        selected, mapping, checksum = bootstrap.confirm_history_scope(ui, str(history), self.repo)
        self.assertEqual(selected, str(history))
        self.assertFalse(mapping)
        self.assertEqual(checksum, bootstrap.digest(history))
        self.assertEqual(ui.options, [])


if __name__ == '__main__':
    unittest.main()
