"""Read-only Windows inventory through native APIs, without external programs.

Command lines exist only in transient memory for classification. Remote PEB
layout is deliberately narrow: unsupported architecture or unreadable fields
remain unknown, never inferred from the executable's directory.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import ntpath
import re
import socket
import struct
import sys


class UnsupportedArchitecture(OSError):
    pass


def identity(command, executable=''):
    # Windows quoting preserves backslashes; only fixed labels leave this code.
    tokens = re.findall(r'"([^"]*)"|([^\s"]+)', command)
    words = [a or b for a, b in tokens]
    for word in words[:4]:
        parts = word.lower().replace('\\', '/').split('/')
        if any(p in {'gigacode', 'gigacode.exe', 'gigacode.cmd', 'gigacode-cli',
                     'gigacode-cli.exe', '@gigacode'} for p in parts):
            return 'gigacode'
    name = ntpath.basename(executable or (words[0] if words else '')).lower()
    name = name.removesuffix('.exe')
    if name in {'gigacode', 'gigacode-cli'}:
        return 'gigacode'
    if name in {'postgres', 'postmaster'}:
        return 'postgres'
    if name == 'java':
        return 'java'
    if name in {'node', 'nodejs', 'npm', 'npx', 'bun', 'deno'}:
        return 'node'
    if name.startswith('python'):
        return 'python'
    return 'other'


def parse_tcp_table(data, ipv6=False):
    """Decode MIB_TCP[6]TABLE_OWNER_PID, validating bounded complete rows."""
    if len(data) < 4:
        raise ValueError('Truncated TCP table')
    count = struct.unpack_from('<I', data)[0]
    stride = 56 if ipv6 else 24
    if count > (len(data) - 4) // stride:
        raise ValueError('Truncated TCP rows')
    result = {}
    for index in range(count):
        offset = 4 + index * stride
        state, port, pid = (struct.unpack_from('<I', data, offset + field)[0]
                            for field in ((48, 20, 52) if ipv6 else (0, 8, 20)))
        if state != 2:  # MIB_TCP_STATE_LISTEN
            continue
        port = socket.ntohs(port & 0xffff)
        if pid > 0 and port > 0:
            result.setdefault(pid, set()).add(port)
    return result


class NativeWindows:
    """Bound Win32 functions. All handles are closed by the collecting call."""
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('Windows APIs unavailable')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.ntdll = C.WinDLL('ntdll')
        self.iphlp = C.WinDLL('iphlpapi', use_last_error=True)
        self._bind(self.kernel, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
        self._bind(self.kernel, 'CloseHandle', [W.HANDLE], W.BOOL)
        self._bind(self.kernel, 'CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE)
        self._bind(self.kernel, 'ReadProcessMemory', [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL)
        self._bind(self.kernel, 'GetProcessTimes', [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL)
        self._bind(self.kernel, 'IsWow64Process', [W.HANDLE, C.POINTER(W.BOOL)], W.BOOL)
        self._bind(self.advapi, 'OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
        self._bind(self.advapi, 'GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        self._bind(self.advapi, 'GetLengthSid', [C.c_void_p], W.DWORD)
        self._bind(self.ntdll, 'NtQueryInformationProcess', [W.HANDLE, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)], W.LONG)
        self._bind(self.iphlp, 'GetExtendedTcpTable', [C.c_void_p, C.POINTER(W.DWORD), W.BOOL, W.ULONG, C.c_int, W.ULONG], W.DWORD)

    @staticmethod
    def _bind(dll, name, args, result):
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
        return function

    def processes(self):
        class Entry(C.Structure):
            _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                        ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                        ('ppid', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                        ('exe', W.WCHAR * 260)]
        first = self._bind(self.kernel, 'Process32FirstW', [W.HANDLE, C.POINTER(Entry)], W.BOOL)
        next_entry = self._bind(self.kernel, 'Process32NextW', [W.HANDLE, C.POINTER(Entry)], W.BOOL)
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == C.c_void_p(-1).value:
            raise OSError('Process snapshot unavailable')
        entries = []
        try:
            entry = Entry()
            entry.size = C.sizeof(entry)
            if not first(handle, C.byref(entry)):
                raise OSError('Process snapshot unavailable')
            while True:
                entries.append({'pid': entry.pid, 'ppid': entry.ppid, 'executable': entry.exe})
                if not next_entry(handle, C.byref(entry)):
                    if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise OSError('Process enumeration incomplete')
                    break
        finally:
            self.kernel.CloseHandle(handle)
        return entries

    def listeners(self):
        result = {}
        for family, ipv6 in ((2, False), (23, True)):
            size = W.DWORD()
            code = self.iphlp.GetExtendedTcpTable(None, C.byref(size), False, family, 3, 0)
            for _ in range(3):
                if code not in (0, 122) or size.value > 64 * 1024 * 1024:
                    raise OSError('TCP table unavailable')
                buffer = C.create_string_buffer(size.value)
                code = self.iphlp.GetExtendedTcpTable(buffer, C.byref(size), False, family, 3, 0)
                if code == 0:
                    for pid, ports in parse_tcp_table(buffer.raw[:size.value], ipv6).items():
                        result.setdefault(pid, set()).update(ports)
                    break
            else:
                raise OSError('TCP table changed repeatedly')
        return result

    def owner(self, handle):
        token = W.HANDLE()
        if not self.advapi.OpenProcessToken(handle, 8, C.byref(token)):
            return None
        try:
            size = W.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not 0 < size.value < 1024 * 1024:
                return None
            buffer = C.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, size, C.byref(size)):
                return None
            sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            length = self.advapi.GetLengthSid(sid)
            return C.string_at(sid, length) if 0 < length < 1024 else None
        finally:
            self.kernel.CloseHandle(token)

    def read(self, handle, address, size):
        if not address or not 0 < size <= 131072:
            raise OSError('Invalid remote memory range')
        buffer = C.create_string_buffer(size)
        read = C.c_size_t()
        if not self.kernel.ReadProcessMemory(handle, address, buffer, size, C.byref(read)) or read.value != size:
            raise OSError('Remote memory unavailable')
        return buffer.raw

    def parameters(self, handle):
        width = C.sizeof(C.c_void_p)
        wow = W.BOOL()
        if not self.kernel.IsWow64Process(handle, C.byref(wow)):
            raise OSError('Process architecture unavailable')
        if width == 4 and not wow.value:
            # A 32-bit caller on 64-bit Windows cannot inspect native PEBs.
            own_wow = W.BOOL()
            if not self.kernel.IsWow64Process(W.HANDLE(-1), C.byref(own_wow)) or own_wow.value:
                raise UnsupportedArchitecture('Unsupported cross-architecture process')
        if width == 8 and wow.value:
            peb = C.c_void_p()
            code = self.ntdll.NtQueryInformationProcess(handle, 26, C.byref(peb), C.sizeof(peb), None)
            width = 4
            address = peb.value
        else:
            fields = (C.c_void_p * 6)()
            code = self.ntdll.NtQueryInformationProcess(handle, 0, fields, C.sizeof(fields), None)
            address = fields[1]
        if code != 0 or not address:
            raise OSError('Process parameters unavailable')
        ptr_format = '<Q' if width == 8 else '<I'
        parameters = struct.unpack(ptr_format, self.read(handle, address + (0x20 if width == 8 else 0x10), width))[0]

        def unicode_string(offset):
            raw = self.read(handle, parameters + offset, 16 if width == 8 else 8)
            length, maximum = struct.unpack_from('<HH', raw)
            pointer = struct.unpack_from(ptr_format, raw, 8 if width == 8 else 4)[0]
            if length % 2 or length > maximum or maximum > 65534:
                raise OSError('Invalid remote string')
            return self.read(handle, pointer, length).decode('utf-16-le', errors='strict') if length else ''
        return unicode_string(0x38 if width == 8 else 0x24), unicode_string(0x70 if width == 8 else 0x40)

    def describe(self, pid, own_sid):
        result = {'owner_known': False, 'same_user': False, 'started': None, 'cwd': None,
                  'command': '', 'parameters_known': False}
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return result
        try:
            sid = self.owner(handle)
            result.update(owner_known=sid is not None, same_user=sid is not None and sid == own_sid)
            times = [W.FILETIME() for _ in range(4)]
            if self.kernel.GetProcessTimes(handle, *(C.byref(t) for t in times)):
                result['started'] = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            self.kernel.CloseHandle(handle)
        if not result['same_user']:
            return result
        handle = self.kernel.OpenProcess(0x410, False, pid)  # QUERY_INFORMATION | VM_READ
        if not handle:
            return result
        try:
            try:
                result['cwd'], result['command'] = self.parameters(handle)
                result['parameters_known'] = True
            except UnsupportedArchitecture:
                result['parameter_limitation'] = 'unsupported-cross-architecture'
            except (OSError, UnicodeError, ValueError):
                pass
        finally:
            self.kernel.CloseHandle(handle)
        return result

    def own_sid(self):
        sid = self.owner(W.HANDLE(-1))  # GetCurrentProcess pseudo-handle
        if sid is None:
            raise OSError('Current user SID unavailable')
        return sid


def ancestors(pid, processes):
    chain, seen = [], {pid}
    process = processes.get(pid)
    while process and process['ppid']:
        parent_id = process['ppid']
        parent = processes.get(parent_id)
        if parent_id in seen or len(chain) >= 128 or parent is None:
            return chain, False
        if process.get('started') and parent.get('started') and parent['started'] > process['started']:
            return chain, False  # parent PID was reused
        chain.append(parent_id)
        seen.add(parent_id)
        process = parent
    return chain, process is not None


def collect(native=None):
    result = {'schema': 1, 'platform': 'win32', 'captured_at': datetime.now(timezone.utc).isoformat(),
              'complete': False, 'limitations': [], 'warnings': [], 'agents': [], 'services': [],
              'processes': [], 'unclassified_services': [], 'scope': 'current-user-processes', 'safe_to_stop': 'not-established'}
    limits, warnings = result['limitations'], result['warnings']
    try:
        native = native or NativeWindows()
        own_sid = native.own_sid()
        rows = native.processes()
    except (OSError, ValueError):
        limits.append('windows-process-table-unavailable')
        return result
    try:
        listeners = native.listeners()
    except (OSError, ValueError):
        listeners = {}
        limits.append('windows-listeners-unavailable')
    processes, cwd = {}, {}
    for row in rows:
        pid = row['pid']
        details = native.describe(pid, own_sid) if pid else {}
        kind = identity(details.get('command', ''), row['executable'])
        process = {'pid': pid, 'ppid': row['ppid'], 'pgid': None, 'uid': None,
                   'started': details.get('started'), 'kind': kind,
                   'owner_known': details.get('owner_known', False), 'same_user': details.get('same_user', False)}
        processes[pid] = process
        candidate = kind != 'other' or ntpath.basename(row['executable']).lower() in {
            'cmd.exe', 'powershell.exe', 'pwsh.exe', 'wscript.exe', 'cscript.exe'}
        if pid and not process['owner_known']:
            (limits if candidate else warnings).append('process-owner-unavailable')
        if process['same_user']:
            if candidate and not details.get('parameters_known'):
                limits.append('process-parameters-unavailable')
                if details.get('parameter_limitation') == 'unsupported-cross-architecture':
                    limits.append('windows-unsupported-cross-architecture')
            value = details.get('cwd')
            cwd[pid] = value if value and ntpath.isabs(value) else None
            if not process['started'] and (candidate or pid in listeners):
                limits.append('process-identity-unavailable')
    result['processes'] = list(processes.values())
    agents = {pid: p for pid, p in processes.items() if p['same_user'] and p['kind'] == 'gigacode'}
    databases = {pid for pid, p in processes.items() if p['same_user'] and p['kind'] == 'postgres'}
    for pid, process in sorted(agents.items()):
        chain, complete = ancestors(pid, processes)
        result['agents'].append({**process, 'cwd': cwd.get(pid), 'ancestors': chain, 'ancestry_complete': complete})
        if not cwd.get(pid):
            limits.append('agent-cwd-unavailable')
        if not complete:
            limits.append('agent-ancestry-incomplete')
    for pid in sorted(set(listeners) | databases):
        process = processes.get(pid)
        if process is None:
            limits.append('listener-process-changed-or-unavailable')
            continue
        if not process['same_user']:
            if not process['owner_known']:
                # Protected system listeners are outside current-user scope;
                # their unknown owner is visible without fabricating ownership.
                warnings.append('listener-owner-unavailable')
                chain, complete = ancestors(pid, processes)
                result['unclassified_services'].append({**process, 'cwd': None,
                    'ports': sorted(listeners.get(pid, [])), 'ancestors': chain,
                    'ancestry_complete': complete, 'agent_pids': [],
                    'agent_dependency': 'unknown', 'dependency_evidence': [],
                    'observations': ['tcp-listener', 'owner-unavailable']})
            continue
        chain, complete = ancestors(pid, processes)
        agent_pids = sorted(set(chain + [pid]) & set(agents))
        result['services'].append({**process, 'cwd': cwd.get(pid), 'ports': sorted(listeners.get(pid, [])),
            'observations': (['postgres-process'] if pid in databases else []) + (['tcp-listener'] if pid in listeners else []),
            'ancestors': chain, 'ancestry_complete': complete, 'agent_pids': agent_pids,
            'dependency_evidence': (['agent-listener'] if pid in agents else ['ancestor']) if agent_pids else [],
            'agent_dependency': 'observed' if agent_pids else ('not-observed' if complete else 'unknown')})
        if not cwd.get(pid):
            limits.append('database-cwd-unavailable' if pid in databases else 'listener-cwd-unavailable')
        if not complete:
            limits.append('listener-ancestry-incomplete')
    result['limitations'] = sorted(set(limits))
    result['warnings'] = sorted(set(warnings))
    result['complete'] = not limits
    return result
