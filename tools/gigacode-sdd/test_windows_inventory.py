import json
import ctypes
import os
from pathlib import Path
import socket
import struct
import sys
import unittest

import service_inventory as inventory
import windows_inventory as win


class FakeNative:
    def __init__(self, rows, details, listeners=None):
        self.rows, self.details, self.ports = rows, details, listeners or {}

    def own_sid(self):
        return b'current-user'

    def processes(self):
        return self.rows

    def describe(self, pid, sid):
        return self.details.get(pid, {})

    def listeners(self):
        return self.ports


def row(pid, parent, executable):
    return {'pid': pid, 'ppid': parent, 'executable': executable}


def own(cwd, command='', started=10):
    return {'owner_known': True, 'same_user': True, 'started': started,
            'cwd': cwd, 'command': command, 'parameters_known': True}


class WindowsInventoryTests(unittest.TestCase):
    def remote_reader(self, width, malformed=False):
        native = object.__new__(win.NativeWindows)

        class Kernel:
            def IsWow64Process(self, handle, output):
                output._obj.value = width == 4
                return True

        class Nt:
            def NtQueryInformationProcess(self, handle, mode, output, size, returned):
                if mode == 26:
                    output._obj.value = 0x1000
                else:
                    output[1] = 0x1000
                return 0

        def string_record(value, pointer):
            length = len(value.encode('utf-16-le'))
            return (struct.pack('<HHIQ', length + (1 if malformed else 0), length, 0, pointer)
                    if width == 8 else struct.pack('<HHI', length, length, pointer))

        cwd, command = r'C:\work\poc', 'gigacode.exe --token not-public'
        memory = {0x1000 + (0x20 if width == 8 else 0x10): struct.pack('<Q' if width == 8 else '<I', 0x2000),
                  0x2000 + (0x38 if width == 8 else 0x24): string_record(cwd, 0x3000),
                  0x2000 + (0x70 if width == 8 else 0x40): string_record(command, 0x4000),
                  0x3000: cwd.encode('utf-16-le'), 0x4000: command.encode('utf-16-le')}

        def read(handle, address, size):
            data = memory[address]
            self.assertEqual(len(data), size)
            return data

        native.kernel, native.ntdll, native.read = Kernel(), Nt(), read
        return native, cwd, command

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, '64-bit reader layout fixture')
    def test_actual_cwd_and_command_parameter_offsets_native_and_wow64(self):
        for width in (8, 4):
            with self.subTest(width=width):
                native, cwd, command = self.remote_reader(width)
                self.assertEqual(native.parameters(99), (cwd, command))

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, '64-bit reader layout fixture')
    def test_remote_unicode_lengths_are_validated_before_memory_read(self):
        native, _, _ = self.remote_reader(8, malformed=True)
        with self.assertRaises(OSError):
            native.parameters(99)

    def test_unsupported_cross_architecture_is_explicit_in_snapshot(self):
        details = own(None)
        details.update(parameters_known=False, parameter_limitation='unsupported-cross-architecture')
        result = win.collect(FakeNative([row(100, 0, 'node.exe')], {100: details}))
        self.assertFalse(result['complete'])
        self.assertIn('windows-unsupported-cross-architecture', result['limitations'])

    def test_native_rows_ancestry_no_tcp_database_and_no_secrets(self):
        fake = FakeNative([row(100, 0, 'node.exe'), row(120, 100, 'postgres.exe'), row(130, 100, 'java.exe')],
            {100: own(r'C:\work\poc', r'node.exe C:\tools\gigacode\cli.js --token hidden'),
             120: own(r'C:\work\poc\database', r'postgres.exe -D C:\secret\data'),
             130: own(r'C:\work\poc\backend', 'java.exe --password secret')}, {130: {8080}})
        result = win.collect(fake)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['agents'][0]['pid'], 100)
        self.assertEqual([p['pid'] for p in result['services']], [120, 130])
        self.assertEqual(result['services'][0]['ports'], [])
        self.assertEqual(result['services'][0]['agent_pids'], [100])
        self.assertEqual(result['services'][1]['dependency_evidence'], ['ancestor'])
        self.assertTrue(all(p['pgid'] is None for p in result['services']))
        self.assertEqual(len(inventory.services_in_group(result, 100)), 2)
        self.assertEqual([p['pid'] for p in inventory.services_in_group(result, 100, [130])], [130])
        for secret in ('hidden', 'secret', '--password', 'cli.js'):
            self.assertNotIn(secret, json.dumps(result))

    def test_owner_filter_and_unclassified_listener_still_protect_owned_job(self):
        fake = FakeNative([row(100, 0, 'svchost.exe'), row(120, 0, 'postgres.exe')],
            {120: {'owner_known': True, 'same_user': False, 'started': 10}}, {100: {445}, 120: {5432}})
        result = win.collect(fake)
        self.assertTrue(result['complete'], result)
        self.assertFalse(result['services'])
        self.assertIn('listener-owner-unavailable', result['warnings'])
        found = inventory.services_in_group(result, 999, [100])
        self.assertEqual([p['pid'] for p in found], [100])
        self.assertEqual(found[0]['agent_dependency'], 'unknown')

    def test_unknown_relevant_owner_or_parameters_fail_closed(self):
        result = win.collect(FakeNative([row(100, 0, 'node.exe')], {}))
        self.assertFalse(result['complete'])
        self.assertIn('process-owner-unavailable', result['limitations'])
        details = own(None, 'gigacode.exe')
        details['parameters_known'] = False
        result = win.collect(FakeNative([row(100, 0, 'gigacode.exe')], {100: details}))
        self.assertFalse(result['complete'])
        self.assertIsNone(result['agents'][0]['cwd'])
        self.assertIn('process-parameters-unavailable', result['limitations'])

    def test_missing_listener_cwd_is_not_executable_directory(self):
        result = win.collect(FakeNative([row(100, 0, r'C:\bin\server.exe')], {100: own(None)}, {100: {8080}}))
        self.assertFalse(result['complete'])
        self.assertIsNone(result['services'][0]['cwd'])
        self.assertIn('listener-cwd-unavailable', result['limitations'])

    def test_reused_parent_pid_breaks_ancestry(self):
        fake = FakeNative([row(100, 0, 'gigacode.exe'), row(120, 100, 'postgres.exe')],
                          {100: own(r'C:\work', started=20), 120: own(r'C:\work', started=10)})
        result = win.collect(fake)
        self.assertFalse(result['complete'])
        self.assertFalse(result['services'][0]['ancestry_complete'])
        self.assertFalse(result['services'][0]['agent_pids'])

    def test_os_desktop_shell_bounds_historical_orphan_parent(self):
        fake = FakeNative([row(80, 79, 'explorer.exe'), row(100, 80, 'gigacode.exe'), row(120, 100, 'postgres.exe')],
            {80: own(r'C:\Windows'), 100: own(r'C:\work'), 120: own(r'C:\work')})
        fake.desktop_shell_pid = lambda: 80
        result = win.collect(fake)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['services'][0]['ancestors'], [100, 80])
        self.assertEqual(result['services'][0]['agent_pids'], [100])
        # The executable name alone never creates a trusted boundary.
        fake.desktop_shell_pid = lambda: None
        self.assertFalse(win.collect(fake)['complete'])

    def test_confirmed_other_user_bounds_current_user_ancestry(self):
        fake = FakeNative([row(80, 79, 'service.exe'), row(120, 80, 'postgres.exe')],
            {80: {'owner_known': True, 'same_user': False, 'started': 1}, 120: own(r'C:\work')})
        result = win.collect(fake)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['services'][0]['ancestors'], [80])

    def test_unknown_owner_is_not_a_trusted_ancestry_boundary(self):
        fake = FakeNative([row(80, 79, 'service.exe'), row(120, 80, 'postgres.exe')], {120: own(r'C:\work')})
        result = win.collect(fake)
        self.assertFalse(result['complete'])
        self.assertFalse(result['services'][0]['ancestry_complete'])

    def test_tcp_ipv4_ipv6_ports_and_malformed_rows(self):
        ipv4 = struct.pack('<7I', 1, 2, 0, socket.htons(5432), 0, 0, 120)
        ipv6 = bytearray(60)
        struct.pack_into('<I', ipv6, 0, 1)
        for offset, value in ((24, socket.htons(8080)), (52, 2), (56, 130)):
            struct.pack_into('<I', ipv6, offset, value)
        self.assertEqual(win.parse_tcp_table(ipv4), {120: {5432}})
        self.assertEqual(win.parse_tcp_table(ipv6, True), {130: {8080}})
        with self.assertRaises(ValueError):
            win.parse_tcp_table(ipv4[:-1])
        with self.assertRaises(ValueError):
            win.parse_tcp_table(b'')

    def test_quoted_windows_paths_and_case_insensitive_workspace_matching(self):
        self.assertEqual(win.identity(r'"C:\Program Files\node.exe" "C:\tools\@gigacode\cli.js"', 'node.exe'), 'gigacode')
        self.assertTrue(inventory._inside(r'C:\WORK\Poc\backend', r'c:\work\poc'))
        self.assertFalse(inventory._inside(r'C:\work\poc-other', r'C:\work\poc'))
        self.assertFalse(inventory._inside(r'D:\work\poc', r'C:\work\poc'))
        self.assertTrue(inventory._inside(r'\\server\share\work\poc', r'\\server\share\work'))

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows API check')
    def test_native_current_process_cwd_identity_and_listener_survive(self):
        native = win.NativeWindows()
        details = native.describe(os.getpid(), native.own_sid())
        self.assertTrue(details['owner_known'])
        self.assertTrue(details['same_user'])
        self.assertTrue(details['parameters_known'], details)
        self.assertEqual(Path(details['cwd']).resolve(), Path.cwd())
        self.assertGreater(details['started'], 0)
        self.assertIn(os.getpid(), {p['pid'] for p in native.processes()})
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertIn(port, native.listeners().get(os.getpid(), set()))
            result = win.collect(native)
            service = next(p for p in result['services'] if p['pid'] == os.getpid())
            self.assertIn(port, service['ports'])
            self.assertEqual(Path(service['cwd']).resolve(), Path.cwd())
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                connection, _ = listener.accept()
                connection.close()


if __name__ == '__main__':
    unittest.main()
