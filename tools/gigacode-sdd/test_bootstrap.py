"""Launcher selection, privacy and safe handoff tests without macOS dialogs."""
import argparse
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
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
        self.temp = tempfile.TemporaryDirectory(prefix='sdd-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'work' / 'PoC with spaces'
        self.repo.mkdir(parents=True)
        for name in ('backend', 'frontend'):
            (self.repo / name).mkdir()
        subprocess.run(['git', 'init', str(self.repo)], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self.packet = self.home / 'Downloads' / 'new packet'
        self.packet.mkdir(parents=True)
        (self.packet / 'requirements.md').write_text('# Модульная симуляция\nREQ-SIM-1\n')
        (self.packet / 'manifest.json').write_text(json.dumps({'revision': 3, 'feature': 'sim'}))
        self.safe_inventory = types.SimpleNamespace(
            inspect_services=Mock(return_value={'complete': True, 'limitations': [], 'services': []}),
            assess_handoff=Mock(return_value={'blocked': False, 'blockers': [], 'warnings': []}))

    def test_bounded_discovery_uses_workspace_declared_code_and_no_symlink(self):
        harness = self.home / 'work' / 'harness'
        harness.mkdir()
        (harness / '.analyst-workspace.json').write_text(json.dumps({'code': {'path': '../PoC with spaces'}}))
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'private').mkdir()
        (self.home / 'work' / 'link').symlink_to(outside, target_is_directory=True)
        repos, protected = bootstrap.discover_repositories([self.home / 'work'])
        self.assertEqual(repos, [self.repo])
        self.assertEqual(protected, [self.repo])
        self.assertNotIn(outside / 'private', list(bootstrap.bounded_dirs([self.home / 'work'])))

    def test_packet_choice_does_not_choose_largest_revision_or_newest_mtime(self):
        old = self.home / 'Downloads' / 'old packet'
        old.mkdir()
        (old / 'requirements.md').write_text('# Старая\nREQ-OLD-1\n')
        (old / 'manifest.json').write_text(json.dumps({'revision': 99}))
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
            'code': {'repository': 'coda', 'path': str(self.repo), 'availability': 'ready'}}}))
        repos, protected = bootstrap.discover_repositories([self.home / 'work'])
        self.assertEqual(repos, [self.repo])
        self.assertEqual(protected, [self.repo])
        (state / 'workspace.json').write_text(json.dumps({'schema_version': 4, 'roles': {
            'code': {'repository': 'coda', 'path': '../PoC with spaces', 'availability': 'ready'}}}))
        self.assertEqual(bootstrap.declared_code_roots(harness), [])

    def test_invalid_repository_selection_is_not_replaced(self):
        with self.assertRaises(Blocked):
            bootstrap.choose_repository(UI(folder=self.home), [])

    def test_nexus_inference_never_logs_credentials_or_accepts_suffix_host(self):
        npm = self.repo / 'frontend/.npmrc'
        npm.write_text('registry=https://nexus-ci.delta.sbrf.ru/repository/npm/\n//nexus-ci.delta.sbrf.ru/:_authToken=secret\n')
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
            'agents': [{'pid': 120, 'pgid': 120, 'cwd': '/different-project'}]}
        with patch.dict(sys.modules, {'service_inventory': self.safe_inventory}), \
                patch.object(bootstrap.os, 'getpid', return_value=123), \
                patch.object(bootstrap.os, 'getpgrp', return_value=123), \
                patch.object(bootstrap.subprocess, 'check_output', return_value='123 122\n122 120\n120 1\n'):
            with self.assertRaisesRegex(Blocked, 'Finder'):
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

    def test_applescript_values_are_arguments_not_executable_source(self):
        hostile = 'project "; do shell script "touch /tmp/forbidden"'
        completed = subprocess.CompletedProcess([], 0, '1\n', '')
        with patch.object(bootstrap.sys, 'platform', 'darwin'), \
                patch.object(bootstrap.subprocess, 'run', return_value=completed) as call:
            self.assertEqual(bootstrap.MacUI().choose('Select', [hostile]), 0)
        argv = call.call_args.args[0]
        self.assertEqual(argv[-1], hostile)
        self.assertNotIn(hostile, argv[2])

    def test_command_resolves_its_directory_with_spaces_from_arbitrary_cwd(self):
        target = self.home / 'Unpacked tool with spaces'
        target.mkdir()
        shutil.copy2(Path(bootstrap.__file__).with_name('Start-GigaCode-SDD.command'), target)
        fake_bin = self.home / 'bin'
        fake_bin.mkdir()
        fake = fake_bin / 'python3'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" >> "$CAPTURE"\nexit 0\n')
        fake.chmod(0o700)
        capture = self.home / 'capture'
        environment = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', ''), CAPTURE=str(capture))
        result = subprocess.run(['bash', str(target / 'Start-GigaCode-SDD.command'), '--diagnose'],
                                cwd=self.home, env=environment, input='\n', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = capture.read_text().splitlines()
        self.assertIn(str(target), lines)
        self.assertIn(str(target / 'bootstrap.py'), lines)
        self.assertIn('--diagnose', lines)

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
                           + json.dumps({'role': 'assistant', 'content': 'Old implementation was attempted.'}) + '\n')
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
                           + json.dumps({'role': 'assistant', 'content': 'Historical result.'}) + '\n')
        ui = UI()
        selected, mapping, checksum = bootstrap.confirm_history_scope(ui, str(history), self.repo)
        self.assertEqual(selected, str(history))
        self.assertFalse(mapping)
        self.assertEqual(checksum, bootstrap.digest(history))
        self.assertEqual(ui.options, [])


if __name__ == '__main__':
    unittest.main()
