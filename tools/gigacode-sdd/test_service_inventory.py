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
        self.sleep_patch = patch.object(inv.time, 'sleep')
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def collect(self, process_rows, listeners='', cwd='', diagnostics=None):
        responses = [(process_rows, None), (listeners, None), (cwd, None)]
        if diagnostics:
            responses = [(text, diagnostics.get(index)) for index, (text, _) in enumerate(responses)]
        with patch.object(inv.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                patch.object(inv.os, 'getuid', return_value=501, create=True), \
                patch.object(inv, '_command', side_effect=lambda argv: responses[0 if '-axo' in argv else 2 if 'cwd' in argv else 1]):
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

    def test_observed_pid_race_stops_retrying_after_success(self):
        failed = {'complete': False, 'limitations': ['listener-process-changed-or-unavailable'],
                  'agents': [], 'services': []}
        passed = {'complete': True, 'limitations': [], 'agents': [], 'services': []}
        with patch.object(inv, '_inspect_services_once', side_effect=[failed, passed]) as collect:
            result = inv.inspect_services()
        self.assertEqual(collect.call_count, 2)
        self.assertTrue(result['complete'])
        self.assertEqual(result['collection_attempts'], 2)

    def test_combined_cwd_and_listener_failure_gets_bounded_fresh_retries(self):
        failed = {'complete': False, 'limitations': ['agent-cwd-unavailable', 'cwd-command-failed',
                  'listener-cwd-unavailable', 'listener-process-changed-or-unavailable']}
        passed = {'complete': True, 'limitations': []}
        with patch.object(inv, '_inspect_services_once', side_effect=[dict(failed), dict(failed), passed]) as collect:
            result = inv.inspect_services()
        self.assertEqual(collect.call_count, 3)
        self.assertEqual(len(result['previous_attempts']), 2)
        self.assertTrue(result['complete'])
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [.25, 1.0])
        with patch.object(inv, '_inspect_services_once', side_effect=lambda: dict(failed)) as collect:
            result = inv.inspect_services()
        self.assertFalse(result['complete'])
        self.assertEqual(collect.call_count, 3)

    def test_nontransient_limitations_are_not_retried(self):
        with patch.object(inv, '_inspect_services_once', return_value={'complete': False,
                'limitations': ['lsof-unavailable']}) as collect:
            result = inv.inspect_services()
        self.assertEqual(collect.call_count, 1)
        self.assertFalse(result['complete'])
        self.sleep.assert_not_called()

    def test_failed_command_diagnostics_preserve_partial_lsof_but_never_ps_arguments(self):
        import subprocess
        responses = [subprocess.CompletedProcess([], 0, row(120, 1, 120, '/bin/java --secret SECRET').encode(), b''),
                     subprocess.CompletedProcess([], 1, b'p120\nn/work\n', b'lsof: actual failure')]
        context = {'deadline': inv.time.monotonic()+10, 'queries': []}
        token = inv._QUERY_CONTEXT.set(context)
        try:
            with patch.object(inv.subprocess, 'run', side_effect=responses):
                inv._command(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid=,lstart=,args='])
                self.assertEqual(inv._command(['/usr/sbin/lsof', '-d', 'cwd']), (None, 'command-failed'))
        finally:
            inv._QUERY_CONTEXT.reset(token)
        self.assertNotIn('SECRET', json.dumps(context))
        self.assertEqual(context['queries'][1]['returncode'], 1)
        self.assertEqual(context['queries'][1]['stderr'], 'lsof: actual failure')
        self.assertEqual(context['queries'][1]['stdout'], 'p120\nn/work\n')

    def test_parallel_collections_do_not_mix_query_diagnostics(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier = threading.Barrier(2)
        def collect():
            label = threading.current_thread().name
            inv._QUERY_CONTEXT.get()['queries'].append({'owner': label})
            barrier.wait(timeout=5)
            return {'complete': True, 'limitations': [], 'owner': label}
        with patch.object(inv, '_inspect_services_once', side_effect=collect), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(inv.inspect_services) for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertNotEqual(results[0]['owner'], results[1]['owner'])
        for result in results:
            self.assertEqual(result['query_diagnostics'], [{'owner': result['owner']}])
        self.assertIsNone(inv._QUERY_CONTEXT.get())

    def test_collection_deadline_prevents_more_commands_and_retries(self):
        context = {'deadline': inv.time.monotonic()-1, 'queries': []}
        token = inv._QUERY_CONTEXT.set(context)
        try:
            with patch.object(inv.subprocess, 'run') as run:
                self.assertEqual(inv._command(['/bin/ps']), (None, 'command-unavailable-or-timeout'))
            run.assert_not_called()
        finally:
            inv._QUERY_CONTEXT.reset(token)
        def exhausted():
            inv._QUERY_CONTEXT.get()['deadline'] = inv.time.monotonic()-1
            return {'complete': False, 'limitations': ['cwd-command-failed']}
        with patch.object(inv, '_inspect_services_once', side_effect=exhausted) as collect:
            self.assertFalse(inv.inspect_services()['complete'])
        self.assertEqual(collect.call_count, 1)
        self.sleep.assert_not_called()

    def test_complete_inventory_includes_reconciled_listener_and_cwd(self):
        original = row(120, 1, 120, '/usr/bin/other')
        fresh = original + row(121, 1, 121, '/usr/bin/java')
        with patch.object(inv.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                patch.object(inv.os, 'getuid', return_value=501, create=True), \
                patch.object(inv, '_command', side_effect=[(original, None), ('p121\nn*:8081\n', None),
                    (fresh, None), ('p121\nn*:8081\n', None), (fresh, None), ('p121\nn/work/stand\n', None)]):
            result = inv.inspect_services()
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['collection_attempts'], 1)
        self.assertEqual(result['services'][0]['pid'], 121)
        self.assertEqual(result['services'][0]['cwd'], '/work/stand')

    def test_listener_born_after_ps_is_reconciled_with_stable_identity(self):
        before = {120: {'pid': 120, 'uid': 501}}
        with patch.object(inv, '_command', side_effect=[
                (row(121, 1, 121, '/usr/bin/java'), None), ('p121\nn*:8081\n', None),
                (row(121, 1, 121, '/usr/bin/java'), None)]):
            processes, listeners, evidence = inv._reconcile_listeners('ps', 'lsof', 501, before, {121: {8081}})
        self.assertEqual(processes[121]['uid'], 501)
        self.assertEqual(listeners[121], {8081})
        self.assertEqual(evidence[0]['resolution'], 'stable-current-user-listener')

    def test_listener_absent_from_both_process_tables_is_reconciled(self):
        other = row(120, 1, 120, '/usr/bin/other')
        with patch.object(inv, '_command', side_effect=[(other, None), ('', None), (other, None)]):
            _, listeners, evidence = inv._reconcile_listeners('ps', 'lsof', 501, {}, {121: {8081}})
        self.assertEqual(listeners, {})
        self.assertEqual(evidence[0]['resolution'], 'exited')

    def test_mixed_disputed_live_and_exited_pids_are_queried_individually(self):
        current = row(1, 0, 1, '/sbin/init') + row(122, 1, 122, '/usr/bin/java')
        with patch.object(inv, '_command', side_effect=[
                (current, None), ('', None), ('p122\nn*:54321\n', None), (current, None)]) as command:
            processes, listeners, evidence = inv._reconcile_listeners(
                'ps', 'lsof', 501, {}, {121: {54321}, 122: {54321}})
        self.assertEqual(listeners, {122: {54321}})
        self.assertIn(122, processes)
        self.assertEqual([e['resolution'] for e in evidence], ['exited', 'stable-current-user-listener'])
        selections = [call.args[0][call.args[0].index('-p')+1] for call in command.call_args_list
                      if '-p' in call.args[0]]
        self.assertEqual(selections, ['121', '122'])

    def test_listener_exits_during_recheck_without_poisoning_live_cwd_query(self):
        # Reproduce ps -> new inherited listener -> ps sees it -> it exits ->
        # targeted lsof is empty -> final ps confirms exit. Including the dead
        # PID in the cwd batch would yield rc1 and lose all live cwd results.
        original = row(100, 1, 100, '/usr/bin/gigacode --token SECRET') + row(120, 1, 120, '/usr/bin/other')
        fresh = original + row(121, 120, 120, '/usr/bin/other --token SECRET')
        replies = [
            (0, original), (0, 'p120\nn127.0.0.1:63342\np121\nn127.0.0.1:63342\n'),
            (0, fresh), (1, ''), (0, original), (0, 'p100\nfcwd\nn/work/poc\np120\nfcwd\nn/\n')]
        def run(argv, **kwargs):
            if 'cwd' in argv:
                self.assertEqual(argv[argv.index('-p') + 1], '100,120')
            code, output = replies.pop(0)
            return subprocess.CompletedProcess(argv, code, output.encode(), b'')
        with patch.object(inv.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                patch.object(inv.os, 'getuid', return_value=501, create=True), \
                patch.object(inv.subprocess, 'run', side_effect=run):
            result = inv.inspect_services()
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['collection_attempts'], 1)
        self.assertEqual(result['agents'][0]['cwd'], '/work/poc')
        self.assertEqual([s['pid'] for s in result['services']], [120])
        evidence = result['listener_reconciliation'][0]
        self.assertEqual(evidence['resolution'], 'exited')
        self.assertEqual(evidence['before_process']['pid'], 121)
        self.assertIsNone(evidence['after_process'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['safe_to_stop'], 'not-established')

    def test_closed_inherited_socket_retains_live_agents_and_unix_databases(self):
        for executable, expected_agent, expected_service in [
                ('/usr/bin/other', False, False), ('/usr/bin/gigacode', True, False),
                ('/usr/bin/postgres', False, True)]:
            with self.subTest(executable=executable):
                original = row(120, 1, 120, '/usr/bin/other')
                fresh = original + row(121, 120, 120, executable + ' --secret SECRET')
                cwd = 'p120\nfcwd\nn/\n'
                if expected_agent or expected_service:
                    cwd += 'p121\nfcwd\nn/work/poc\n'
                replies = [
                    (0, original), (0, 'p120\nn*:54321\np121\nn*:54321\n'),
                    (0, fresh), (1, ''), (0, fresh), (0, cwd)]
                def run(argv, **kwargs):
                    if 'cwd' in argv:
                        self.assertEqual(argv[argv.index('-p') + 1],
                                         '120,121' if expected_agent or expected_service else '120')
                    code, output = replies.pop(0)
                    return subprocess.CompletedProcess(argv, code, output.encode(), b'')
                with patch.object(inv.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                        patch.object(inv.os, 'getuid', return_value=501, create=True), \
                        patch.object(inv.subprocess, 'run', side_effect=run):
                    result = inv.inspect_services()
                self.assertTrue(result['complete'], result)
                self.assertEqual(result['collection_attempts'], 1)
                self.assertIn(121, [p['pid'] for p in result['processes']])
                self.assertEqual([a['pid'] for a in result['agents']], [121] if expected_agent else [])
                self.assertEqual([s['pid'] for s in result['services']], [120, 121] if expected_service else [120])
                self.assertEqual(result['listener_reconciliation'][0]['resolution'], 'no-longer-listening')
                if expected_service:
                    self.assertEqual(result['services'][1]['ports'], [])
                    self.assertEqual(result['services'][1]['observations'], ['postgres-process'])
                self.assertNotIn('SECRET', json.dumps(result))

    def test_empty_listener_query_does_not_hide_reused_or_unverifiable_pid(self):
        other = row(120, 1, 120, '/usr/bin/other')
        target = row(121, 1, 121, '/usr/bin/java')
        variants = [
            (other + row(121, 1, 121, '/usr/bin/java', uid=999), None),
            (other + target.replace('10:20:30', '10:20:31'), None),
            ('', None),
            (other + 'malformed process row\n', None),
            (other, 'command-failed'),
        ]
        for after, error in variants:
            with self.subTest(after=after, error=error), patch.object(inv, '_command', side_effect=[
                    (other + target, None), ('', None), (after, error)]):
                _, listeners, evidence = inv._reconcile_listeners('ps', 'lsof', 501, {}, {121: {8081}})
            self.assertIn(121, listeners)
            self.assertEqual(evidence[0]['resolution'], 'unresolved')

    def test_empty_listener_query_does_not_adopt_another_users_stable_process(self):
        other = row(120, 1, 120, '/usr/bin/other')
        foreign = other + row(121, 1, 121, '/usr/bin/java', uid=999)
        with patch.object(inv, '_command', side_effect=[(foreign, None), ('', None), (foreign, None)]):
            processes, listeners, evidence = inv._reconcile_listeners('ps', 'lsof', 501, {}, {121: {8081}})
        self.assertNotIn(121, processes)
        self.assertIn(121, listeners)
        self.assertEqual(evidence[0]['resolution'], 'unresolved')

    def test_failed_or_malformed_listener_query_cannot_prove_exit(self):
        other = row(120, 1, 120, '/usr/bin/other')
        target = row(121, 1, 121, '/usr/bin/java')
        for listener, error in [('', 'command-failed'), ('pBAD\nn*:8081\n', None),
                                ('p999\nn*:8081\n', None)]:
            with self.subTest(listener=listener, error=error), patch.object(inv, '_command', side_effect=[
                    (other + target, None), (listener, error), (other, None)]):
                _, listeners, evidence = inv._reconcile_listeners('ps', 'lsof', 501, {}, {121: {8081}})
            self.assertIn(121, listeners)
            self.assertFalse(evidence[0]['queries_valid'])
            self.assertTrue(evidence[0]['query_limitations'])

    def test_changed_identity_or_failed_query_remains_unresolved(self):
        for after, error in [(row(121, 1, 121, '/usr/bin/java', uid=999), None),
                             (row(121, 1, 121, '/usr/bin/java').replace('10:20:30', '10:20:31'), None),
                             ('', 'command-failed')]:
            with self.subTest(after=after, error=error), patch.object(inv, '_command', side_effect=[
                    (row(121, 1, 121, '/usr/bin/java'), None), ('p121\nn*:8081\n', None), (after, error)]):
                processes, listeners, evidence = inv._reconcile_listeners('ps', 'lsof', 501, {}, {121: {8081}})
            self.assertNotIn(121, processes)
            self.assertIn(121, listeners)
            self.assertEqual(evidence[0]['resolution'], 'unresolved')

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

    def test_signed_nobody_uid_does_not_block_or_become_current_user(self):
        processes = (row(819, 1, 819, '/usr/bin/gigacode', uid=-2)
                     + row(100, 1, 100, '/usr/bin/gigacode')
                     + row(120, 100, 100, '/usr/bin/node server.js'))
        result = self.collect(processes, 'p120\nn*:3002\n',
                              'p100\nn/work/harness\np120\nn/work/poc/frontend\n')
        self.assertTrue(result['complete'], result['limitations'])
        nobody = next(p for p in result['processes'] if p['pid'] == 819)
        self.assertEqual(nobody['uid'], 4294967294)
        self.assertFalse(nobody['same_user'])
        self.assertEqual([p['pid'] for p in result['agents']], [100])
        self.assertEqual(result['services'][0]['agent_pids'], [100])
        self.assertTrue(inv.assess_handoff('/work/poc', result)['blocked'])

    def test_signed_uid_exception_does_not_relax_process_identifiers(self):
        for pid, ppid, pgid in ((0, 1, 1), (-2, 1, 1), (100, -2, 100), (100, 1, -2)):
            with self.subTest(pid=pid, ppid=ppid, pgid=pgid):
                processes, malformed = inv._parse_ps(row(pid, ppid, pgid, 'node', uid=-2))
                self.assertTrue(malformed)
                self.assertFalse(processes)
        for uid in (-(1 << 31) - 1, 1 << 32):
            processes, malformed = inv._parse_ps(row(100, 1, 100, 'node', uid=uid))
            self.assertTrue(malformed)
            self.assertFalse(processes)

    def test_argument_separators_do_not_create_process_rows(self):
        for separator in ('\u0085', '\u2028', '\u2029', '\v', '\f', '\r'):
            with self.subTest(separator=repr(separator)):
                processes = (row(100, 1, 100, '/usr/bin/gigacode --prompt first' + separator + 'SECRET')
                             + row(120, 100, 100, '/usr/bin/node server.js'))
                result = self.collect(processes, 'p120\nn*:3002\n',
                                      'p100\nn/work/harness\np120\nn/work/poc/frontend\n')
                self.assertTrue(result['complete'], result['limitations'])
                self.assertEqual(result['services'][0]['agent_pids'], [100])
                self.assertTrue(inv.assess_handoff('/work/poc', result)['blocked'])
                self.assertNotIn('SECRET', json.dumps(result))

    def test_real_incomplete_record_still_blocks_inventory(self):
        result = self.collect(row(100, 1, 100, '/usr/bin/gigacode') + '120 100 100\n',
                              '', 'p100\nn/work/harness\n')
        self.assertFalse(result['complete'])
        self.assertIn('ps-incomplete-rows', result['limitations'])
        self.assertTrue(inv.assess_handoff('/work/poc', result)['blocked'])

    def test_command_preserves_carriage_returns_in_actual_subprocess_output(self):
        data = row(100, 1, 100, '/usr/bin/gigacode --prompt first\rsecond\u2028third').encode('utf-8')
        output, error = inv._command([sys.executable, '-c',
                                     'import sys;sys.stdout.buffer.write(' + repr(data) + ')'])
        self.assertIsNone(error)
        self.assertEqual(output.encode('utf-8'), data)
        processes, malformed = inv._parse_ps(output)
        self.assertFalse(malformed)
        self.assertEqual(processes[100]['kind'], 'gigacode')

    def test_empty_lsof_selection_is_not_error(self):
        completed = subprocess.CompletedProcess(['lsof'], 1, b'', b'')
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
