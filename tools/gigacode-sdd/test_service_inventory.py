import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import service_inventory as inv


def row(pid, ppid, pgid, args, uid=501):
    return f'{pid} {ppid} {pgid} {uid} Thu Nov 12 10:20:30 2026 {args}\n'


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.native_platform = sys.platform
        self.posix_platform = patch.object(inv.sys, 'platform', 'linux')
        self.posix_platform.start()
        self.addCleanup(self.posix_platform.stop)

    def collect(self, process_rows, listeners='', cwd='', diagnostics=None):
        responses = [(process_rows, None), (listeners, None), (cwd, None)]
        if diagnostics:
            responses = [(text, diagnostics.get(index)) for index, (text, _) in enumerate(responses)]
        with patch.object(inv.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                patch.object(inv.os, 'getuid', return_value=501, create=True), \
                patch.object(inv, '_command', side_effect=responses * 2):
            return inv.inspect_services()

    def test_agent_ancestry_and_process_group_survive_sanitization(self):
        processes = (row(100, 1, 100, '/usr/local/bin/node /opt/gigacode/cli.js --token super-secret')
                     + row(110, 100, 100, '/bin/bash backend.sh --password hidden')
                     + row(120, 110, 100, '/usr/bin/java -Dsecret=do-not-leak')
                     + row(130, 1, 100, '/usr/local/bin/postgres -D /private/data'))
        result = self.collect(processes, 'p120\nf12\nn127.0.0.1:8081\np130\nf6\nn[::1]:5433\n',
                              'p100\nfcwd\nn/work/poc\np120\nfcwd\nn/work/poc/backend\np130\nfcwd\nn/work/stand\n')
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['agents'][0]['kind'], 'gigacode')
        self.assertEqual(result['services'][0]['dependency_evidence'], ['ancestor'])
        self.assertEqual(result['services'][1]['dependency_evidence'], ['process-group'])
        self.assertEqual(result['services'][1]['ports'], [5433])
        self.assertEqual(len(inv.services_in_group(result, 100)), 2)
        rendered = json.dumps(result)
        for secret in ('super-secret', 'hidden', 'do-not-leak', '/private/data', '--password'):
            self.assertNotIn(secret, rendered)
        assessment = inv.assess_handoff('/work/poc', result)
        self.assertTrue(assessment['blocked'])
        self.assertEqual(assessment['safe_to_stop'], 'not-established')

    def test_separate_server_is_not_claimed_safe_to_stop(self):
        result = self.collect(row(200, 1, 200, '/usr/local/bin/postgres'),
                              'p200\nn*:5432\n', 'p200\nn/work/poc/pgdata\n')
        self.assertTrue(result['complete'])
        self.assertEqual(result['services'][0]['agent_dependency'], 'not-observed')
        assessment = inv.assess_handoff('/work/poc', result)
        self.assertFalse(assessment['blocked'])
        self.assertEqual(assessment['safe_to_stop'], 'not-established')
        self.assertTrue(assessment['warnings'])

    def test_postgres_without_tcp_listener_is_still_a_preserved_service(self):
        processes = (row(100, 1, 100, '/usr/bin/gigacode')
                     + row(130, 100, 100, '/usr/local/bin/postgres -D /private/data -c listen_addresses='))
        responses = [(processes, None), ('', None),
                     ('p100\nn/work/poc\np130\nn/work/poc/database\n', None)]
        with patch.object(inv.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                patch.object(inv.os, 'getuid', return_value=501, create=True), \
                patch.object(inv, '_command', side_effect=responses) as commands:
            result = inv.inspect_services()
        self.assertTrue(result['complete'], result)
        self.assertEqual(len(result['services']), 1)
        service = result['services'][0]
        self.assertEqual(service['pid'], 130)
        self.assertEqual(service['ports'], [])
        self.assertEqual(service['observations'], ['postgres-process'])
        self.assertEqual(service['cwd'], '/work/poc/database')
        self.assertEqual(inv.services_in_group(result, 100), [service])
        self.assertTrue(inv.assess_handoff('/work/poc', result)['blocked'])
        cwd_command = commands.call_args_list[2].args[0]
        self.assertIn('130', cwd_command[cwd_command.index('-p') + 1].split(','))
        self.assertEqual(len(inv.detect_new_services({'services': []}, result, '/work/poc')), 1)

    def test_other_users_postgres_is_not_added_to_current_user_inventory(self):
        result = self.collect(row(130, 1, 130, '/usr/local/bin/postgres', uid=999), '', '')
        self.assertTrue(result['complete'])
        self.assertFalse(result['services'])

    def test_agent_owned_stand_outside_repo_blocks_handoff(self):
        result = self.collect(row(100, 1, 100, '/usr/bin/gigacode') + row(120, 100, 120, '/usr/bin/java'),
                              'p120\nn*:8081\n', 'p100\nn/work/other\np120\nn/work/separate-stand\n')
        assessment = inv.assess_handoff('/work/poc', result)
        self.assertTrue(assessment['blocked'])
        self.assertFalse(assessment['active_agents'])
        self.assertEqual(len(assessment['relevant_services']), 1)

    def test_unrelated_agent_with_known_cwd_is_not_an_editing_claim(self):
        result = self.collect(row(100, 1, 100, '/usr/bin/gigacode'), '', 'p100\nn/work/other\n')
        assessment = inv.assess_handoff('/work/poc', result)
        self.assertFalse(assessment['blocked'])
        self.assertFalse(assessment['active_agents'])
        self.assertEqual(assessment['safe_to_stop'], 'not-established')

    def test_missing_tools_fail_closed_without_process_actions(self):
        with patch.object(inv.shutil, 'which', return_value=None), \
                patch.object(inv, '_command') as command:
            result = inv.inspect_services()
        command.assert_not_called()
        self.assertFalse(result['complete'])
        self.assertIn('lsof-unavailable', result['limitations'])
        self.assertTrue(inv.assess_handoff('/work/poc', result)['blocked'])

    def test_missing_cwd_or_ancestry_is_unknown_not_safe(self):
        result = self.collect(row(120, 119, 120, '/usr/bin/java'), 'p120\nn*:8081\n', '')
        self.assertFalse(result['complete'])
        self.assertIn('listener-cwd-unavailable', result['limitations'])
        self.assertIn('listener-ancestry-incomplete', result['limitations'])
        self.assertEqual(result['services'][0]['agent_dependency'], 'unknown')

    def test_current_user_scope_and_no_arbitrary_args_in_reports(self):
        result = self.collect(row(100, 1, 100, '/usr/bin/gigacode', uid=999)
                              + row(120, 1, 120, '/usr/bin/unknown --password secret'),
                              'p120\nn*:8081\n', 'p120\nn/work/poc\n')
        self.assertTrue(result['complete'])
        self.assertFalse(result['agents'])
        self.assertEqual(result['services'][0]['kind'], 'other')
        self.assertNotIn('secret', json.dumps(result))

    def test_process_races_and_lsof_diagnostics_are_visible(self):
        result = self.collect(row(120, 1, 120, '/usr/bin/java'), 'p121\nn*:8081\n', '',
                              diagnostics={1: 'command-diagnostic'})
        self.assertFalse(result['complete'])
        self.assertIn('listener-process-changed-or-unavailable', result['limitations'])
        self.assertIn('listeners-command-diagnostic', result['limitations'])

    def test_observed_pid_race_is_retried_only_once(self):
        failed = {'complete': False, 'limitations': ['listener-process-changed-or-unavailable'],
                  'agents': [], 'services': []}
        passed = {'complete': True, 'limitations': [], 'agents': [], 'services': []}
        with patch.object(inv, '_inspect_services_once', side_effect=[failed, passed]) as collect:
            result = inv.inspect_services()
        self.assertEqual(collect.call_count, 2)
        self.assertTrue(result['complete'])
        self.assertEqual(result['collection_attempts'], 2)

    def test_new_service_attribution_and_pid_reuse(self):
        old = {'pid': 40, 'started': 'first', 'cwd': '/work/poc/backend', 'ports': [8081]}
        unrelated = {'pid': 41, 'started': 'first', 'cwd': '/work/other', 'ports': [8082]}
        newer = {**old, 'started': 'second'}
        found = inv.detect_new_services({'services': [old]}, {'services': [newer, unrelated]}, '/work/poc')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['attribution'], 'cwd-inside-workspace')
        self.assertFalse(inv.detect_new_services({'services': [old]}, {'services': [old]}, '/work/poc'))

    def test_unknown_cwd_is_not_fabricated_attribution(self):
        result = inv.detect_new_services({'services': []}, {'complete': False, 'services': [
            {'pid': 10, 'started': 'first', 'cwd': None, 'ports': [8000]}]}, '/work/poc')
        self.assertEqual(result, [])

    def test_parser_rejects_bad_pid_and_listener_port(self):
        processes, malformed = inv._parse_ps('bad row\n' + row(0, 1, 2, 'node'))
        self.assertTrue(malformed)
        self.assertFalse(processes)
        entries, malformed = inv._parse_lsof('p10\nn*:99999\npBAD\nn*:80\n', 'listener')
        self.assertTrue(malformed)
        self.assertFalse(entries)

    def test_empty_lsof_selection_is_not_error(self):
        completed = subprocess.CompletedProcess(['lsof'], 1, '', '')
        with patch.object(inv.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(inv._command(['/usr/bin/lsof']), ('', None))
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_private_report_never_overwrites_or_follows_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / 'private/report.json'
            inv.save_report(path, {'safe_to_stop': 'not-established'})
            if self.native_platform != 'win32':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            with self.assertRaises(FileExistsError):
                inv.save_report(path, {})
            alias = root / 'alias'
            try:
                alias.symlink_to(path.parent, target_is_directory=True)
            except OSError:
                if self.native_platform != 'win32':
                    raise
                return  # Windows may require Developer Mode for symlinks.
            with self.assertRaises(ValueError):
                inv.save_report(alias / 'other.json', {})

    def test_real_read_only_smoke_keeps_listener_alive(self):
        self.posix_platform.stop()
        try:
            listener = socket.socket()
        except PermissionError:
            self.skipTest('This sandbox does not permit opening sockets')
        with listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            before = listener.getsockname()
            result = inv.inspect_services()
            self.assertIn('complete', result)
            self.assertEqual(result['safe_to_stop'], 'not-established')
            self.assertEqual(listener.getsockname(), before)
            with socket.create_connection(before, timeout=2):
                connection, _ = listener.accept()
                connection.close()


if __name__ == '__main__':
    unittest.main()
