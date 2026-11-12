"""Portable locks and owned process scopes, without shell interpolation.

Windows children are assigned to a Job before executing the requested command.
Jobs deliberately have no kill-on-close flag: a controller crash must not kill
a service that needs a human handoff. This is process ownership, not a sandbox.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'


class FileLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.file = self.path.open('a+b')
        try:
            if WINDOWS:
                import msvcrt
                if self.file.seek(0, 2) == 0:
                    self.file.write(b'\0')
                    self.file.flush()
                self.file.seek(0)
                try:
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise BlockingIOError('Another controller owns this run') from exc
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise
        return self

    def __exit__(self, *_):
        try:
            if WINDOWS:
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
        finally:
            self.file.close()


def _kernel32():
    from ctypes import wintypes as w
    lib = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'GetExitCodeProcess': ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        'CloseHandle': ([w.HANDLE], w.BOOL),
        'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
        'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
        'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        'CreateEventW': ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        'SetEvent': ([w.HANDLE], w.BOOL),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    return lib


def process_exists(pid):
    """Probe without signals on Windows; inaccessible identities fail closed."""
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError('Invalid process identifier')
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    from ctypes import wintypes as w
    api = _kernel32()
    handle = api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return False
        return True
    try:
        code = w.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        api.CloseHandle(handle)


_jobs = {}


def process_scope_pids(pid):
    """Return exact live Job membership, or None for a scope we do not own."""
    if not WINDOWS or pid not in _jobs:
        return None
    api, handle = _jobs[pid]
    capacity = 64
    while capacity <= 1048576:
        class Members(ctypes.Structure):
            _fields_ = [('assigned', ctypes.c_uint32), ('count', ctypes.c_uint32),
                        ('pids', ctypes.c_size_t * capacity)]
        members = Members()
        if api.QueryInformationJobObject(handle, 3, ctypes.byref(members),
                                         ctypes.sizeof(members), None):
            return list(members.pids[:members.count])
        if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
            raise OSError('Cannot inspect owned Windows Job')
        capacity *= 2
    raise OSError('Windows Job membership exceeded inspection capacity')


def process_scope_exists(pid):
    if WINDOWS:
        members = process_scope_pids(pid)
        return bool(members) if members is not None else process_exists(pid)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch_kwargs():
    return ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
            else {'start_new_session': True})


def resolve_argv(argv, env=None):
    """Resolve standard npm .cmd shims to node + script, never execute cmd.exe.

    Arbitrary batch programs need an explicit native interpreter argv. Passing
    them to CreateProcess would let Windows interpret metacharacters as shell.
    """
    argv = list(argv)
    if not WINDOWS:
        return argv
    search_path = (os.environ if env is None else env).get('PATH', '')
    executable = shutil.which(argv[0], path=search_path)
    if executable is None:
        raise FileNotFoundError(argv[0])
    shim = Path(executable)
    if shim.suffix.lower() not in ('.cmd', '.bat'):
        return [str(shim), *argv[1:]]
    content = shim.read_text(encoding='utf-8-sig')
    matches = set(re.findall(r'"%dp0%[\\/](node_modules[\\/][^"%\r\n]+)"',
                             content, flags=re.IGNORECASE))
    if len(matches) != 1:
        raise OSError('Unsupported Windows batch launcher; configure native executable argv')
    script = (shim.parent / next(iter(matches)).replace('\\', '/')).resolve()
    if not script.is_file() or not script.is_relative_to((shim.parent / 'node_modules').resolve()):
        raise OSError('npm launcher script is missing or outside node_modules')
    sibling_node = shim.parent / 'node.exe'
    node = str(sibling_node) if sibling_node.is_file() else shutil.which('node.exe', path=search_path)
    if not node:
        raise OSError('node.exe is required for the npm launcher')
    return [node, str(script), *argv[1:]]


def launch_process(argv, **kwargs):
    argv = resolve_argv(argv, kwargs.get('env'))
    if not WINDOWS:
        return subprocess.Popen(argv, **kwargs, **launch_kwargs())
    api = _kernel32()
    event = api.CreateEventW(None, True, False, None)
    job = api.CreateJobObjectW(None, None)
    if not event or not job:
        if event:
            api.CloseHandle(event)
        if job:
            api.CloseHandle(job)
        raise OSError('Cannot create Windows process ownership scope')
    process = None
    try:
        # Inherit a reference before the parent closes its handle. A named
        # event alone can disappear before a delayed helper calls OpenEvent.
        os.set_handle_inheritable(event, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [event]}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    '--child', str(event), json.dumps(argv)],
                                   startupinfo=startup, close_fds=True,
                                   **kwargs, **launch_kwargs())
        if not api.AssignProcessToJobObject(job, int(process._handle)):
            raise OSError('Cannot assign child to Windows Job; parent policy may prohibit nested jobs')
        _jobs[process.pid] = (api, job)
        if not api.SetEvent(event):
            raise OSError('Cannot release owned Windows process')
        return process
    except BaseException:
        # The helper has not been released and cannot have launched the CLI.
        if process is not None:
            _jobs.pop(process.pid, None)
            process.kill()
            process.wait()
        api.CloseHandle(job)
        raise
    finally:
        api.CloseHandle(event)


def stop_process_scope(process):
    """Terminate only the owned group/Job after the caller's service guard."""
    if WINDOWS:
        owned = _jobs.get(process.pid)
        if owned is None:
            raise OSError('Refusing to terminate an unowned Windows process scope')
        api, handle = owned
        if not api.TerminateJobObject(handle, 1):
            raise OSError('Cannot terminate owned Windows Job')
        api.CloseHandle(handle)
        _jobs.pop(process.pid, None)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        process.poll()
        if not process_scope_exists(process.pid):
            return
        time.sleep(.025)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _child(event_handle, encoded_argv):
    api = _kernel32()
    event = int(event_handle)
    try:
        if api.WaitForSingleObject(event, 15000) != 0:
            return 125
    finally:
        api.CloseHandle(event)
    try:
        return subprocess.call(json.loads(encoded_argv))
    except OSError:
        return 126


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--child' or os.name != 'nt':
        raise SystemExit('Internal process helper')
    raise SystemExit(_child(sys.argv[2], sys.argv[3]))
