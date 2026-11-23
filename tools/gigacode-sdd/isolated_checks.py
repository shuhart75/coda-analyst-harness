"""Disposable controller-owned checks using installed OS isolation, never a fallback.

Linux: bubblewrap network/PID namespaces. macOS: Seatbelt, outbound connections
only to controller-allocated local test ports. No persistent services, source or
shared-cache writes. Probe enforcement on every invocation before product code.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import platform as host_platform
import struct
import tempfile
from contextlib import ExitStack

from platform_support import launch_process
from transport import finish_process


class IsolationUnavailable(RuntimeError):
    pass


def command(scratch, argv, ports, *, platform=None, seccomp_fd=None):
    platform = platform or sys.platform
    if platform == 'linux' and shutil.which('bwrap'):
        if seccomp_fd is None:
            raise IsolationUnavailable('Unix-socket isolation filter unavailable')
        return [shutil.which('bwrap'), '--die-with-parent', '--new-session', '--unshare-all',
                '--seccomp', str(seccomp_fd), '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev',
                '--bind', str(scratch), str(scratch), '--chdir', str(scratch), '--', *argv]
    if platform == 'darwin' and shutil.which('sandbox-exec'):
        quoted = json.dumps(str(scratch))
        profile = ['(version 1)', '(deny default)', '(allow process-exec)', '(allow process-fork)',
                   '(allow file-read*)', '(allow sysctl-read)', f'(allow file-write* (subpath {quoted}))',
                   '(allow file-write* (literal "/dev/null"))']
        for port in ports:
            profile += [f'(allow network-bind (local ip "localhost:{port}"))',
                        f'(allow network-inbound (local ip "localhost:{port}"))',
                        f'(allow network-outbound (remote ip "localhost:{port}"))']
        return [shutil.which('sandbox-exec'), '-p', '\n'.join(profile), *argv]
    raise IsolationUnavailable('Installed OS isolation is unavailable; no unsandboxed retry')


def validate(request, work):
    from controller import under
    if not isinstance(request, dict) or not isinstance(request.get('reason'), str) or not request['reason'].strip():
        raise ValueError('Isolated check needs a reason')
    argv = request.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or '\0' in a for a in argv):
        raise ValueError('Isolated check needs an argv list')
    inputs = request.get('inputs')
    if not isinstance(inputs, list) or not inputs:
        raise ValueError('Declare input files/directories relative to the workspace')
    for name in inputs:
        path = under(work, name)
        if name == '.' or not path.exists() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Inputs must exist and cannot traverse symlinks or select the whole workspace')
        if path.is_dir() and any(p.is_symlink() for p in path.rglob('*')):
            raise ValueError('Input tree contains symlinks; provide regular test inputs')
    count = request.get('local_ports', 0)
    if type(count) is not int or not 0 <= count <= 8:
        raise ValueError('local_ports must be a count from 0 to 8, not existing service ports')
    timeout = request.get('timeout', 120)
    if type(timeout) not in (int, float) or not 0 < timeout <= 600:
        raise ValueError('Isolated check timeout must be between 0 and 600 seconds')
    under(work, request.get('cwd', '.'))
    return request


def socket_filter():
    # A read-only bind mount does not isolate pathname Unix sockets (Docker,
    # desktop/session services). Restrict socket() to IPv4/IPv6 inside the new
    # network namespace. Reject unexpected ABIs, including x32 syscall numbers.
    arch, syscall = {'x86_64': (0xc000003e, 41), 'aarch64': (0xc00000b7, 198)}.get(
        host_platform.machine(), (None, None))
    if arch is None:
        raise IsolationUnavailable('No verified socket filter for this Linux architecture')
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000),
        (0x15, 0, 4, syscall), (0x20, 0, 0, 16), (0x15, 2, 0, 2),
        (0x15, 1, 0, 10), (0x06, 0, 0, 0x50001), (0x06, 0, 0, 0x7fff0000)]
    stream = tempfile.TemporaryFile()
    stream.write(b''.join(struct.pack('HBBI', *i) for i in instructions))
    stream.flush()
    stream.seek(0)
    return stream


def run(runner, work, request, stop):
    with ExitStack() as resources:
        try:
            stream = resources.enter_context(socket_filter()) if sys.platform == 'linux' else None
            return _run(runner, work, request, stop, stream.fileno() if stream else None, resources)
        except OSError as exc:
            raise IsolationUnavailable('Isolated environment unavailable: ' + type(exc).__name__) from exc


def _run(runner, work, request, stop, seccomp_fd, resources):
    from controller import save, under, digest
    request = validate(request, work)
    pass_fds = () if seccomp_fd is None else (seccomp_fd,)
    folder = runner.logdir('isolated-check')
    scratch = folder / 'scratch'
    scratch.mkdir(parents=True, mode=0o700)
    work = Path(work).resolve()
    manifest = {}
    for name in request['inputs']:
        source, target = under(work, name), under(scratch, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
            for path in source.rglob('*'):
                if path.is_file():
                    manifest[path.relative_to(work).as_posix()] = digest(path)
        else:
            shutil.copy2(source, target)
            manifest[name] = digest(source)
    cwd = under(scratch, request.get('cwd', '.'))
    if not cwd.is_dir():
        raise ValueError('Isolated check cwd must be covered by its copied inputs')
    for name in ('home', 'tmp', 'cache'):
        (scratch / ('.' + name)).mkdir(exist_ok=True)
    env = {k: v for k, v in runner.env.items() if k in ('PATH', 'JAVA_HOME', 'LANG', 'LC_ALL', 'SYSTEMROOT')}
    env.update(HOME=str(scratch / '.home'), TMPDIR=str(scratch / '.tmp'),
               XDG_CACHE_HOME=str(scratch / '.cache'), NPM_CONFIG_CACHE=str(scratch / '.cache/npm'),
               NPM_CONFIG_USERCONFIG=str(scratch / '.home/.npmrc'), PIP_CONFIG_FILE=os.devnull,
               PIP_NO_INDEX='1', PIP_EXTRA_INDEX_URL='', PIP_FIND_LINKS='')
    reservations = []
    for _ in range(request.get('local_ports', 0)):
        sock = resources.enter_context(socket.socket())
        sock.bind(('127.0.0.1', 0))
        reservations.append(sock)
    ports = [s.getsockname()[1] for s in reservations]
    env['SDD_TEST_PORTS'] = json.dumps(ports)
    # Probe real restrictions, including a live host listener (no internet needed).
    guard = resources.enter_context(socket.socket())
    guard.bind(('127.0.0.1', 0))
    guard.listen()
    probe = '''import pathlib,socket,sys
p=pathlib.Path(sys.argv[1]); p.write_text('ok')
try:
 pathlib.Path(sys.argv[2]).write_text('escaped')
except OSError: pass
else: raise SystemExit('write isolation failed')
try:
 s=socket.create_connection(('127.0.0.1',int(sys.argv[3])),timeout=.5); s.close()
except OSError: pass
else: raise SystemExit('network isolation failed')
print('ISOLATION_OK')
'''
    try:
        prefix = command(scratch, [sys.executable, '-c', probe, str(scratch / '.probe'),
                                  str(folder / 'escape-probe'), str(guard.getsockname()[1])], ports, seccomp_fd=seccomp_fd)
        result = subprocess.run(prefix, cwd=scratch, env=env, capture_output=True, timeout=15, pass_fds=pass_fds)
        if result.returncode or b'ISOLATION_OK' not in result.stdout:
            save(folder / 'isolation.json', {'available': False, 'returncode': result.returncode,
                 'reason': 'OS isolation probe failed; product command was not run',
                 'probe_stderr': result.stderr.decode('utf-8', errors='replace')[-4000:]})
            raise IsolationUnavailable('OS isolation probe failed; see ' + str(folder / 'isolation.json'))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationUnavailable('OS isolation probe unavailable: ' + type(exc).__name__) from exc
    finally:
        guard.close()
        for sock in reservations:
            sock.close()
    # argv is never interpreted by a controller shell. Local mocks must bind only
    # the allocated ports, provided through SDD_TEST_PORTS.
    if seccomp_fd is not None:
        os.lseek(seccomp_fd, 0, os.SEEK_SET)
    argv = command(scratch, request['argv'], ports, seccomp_fd=seccomp_fd)
    if sys.platform == 'linux':
        argv[argv.index('--chdir') + 1] = str(cwd)
    log = folder / 'output.log'
    with log.open('wb') as output:
        proc = launch_process(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=output, stderr=subprocess.STDOUT, pass_fds=pass_fds)
        deadline = time.monotonic() + request.get('timeout', 120)
        try:
            while proc.poll() is None and not stop.is_set() and time.monotonic() < deadline:
                time.sleep(.05)
        finally:
            cleanup = finish_process(proc)
    result = {'ok': proc.returncode == 0 and not stop.is_set() and time.monotonic() < deadline,
              'returncode': proc.returncode, 'log': str(log), 'inputs': manifest,
              'network': 'isolated-loopback' if sys.platform == 'linux' else 'allocated-loopback-ports',
              'ports': ports, 'isolation_probe': 'passed', 'scratch': str(scratch), 'cleanup': cleanup}
    save(folder / 'result.json', result)
    return result
