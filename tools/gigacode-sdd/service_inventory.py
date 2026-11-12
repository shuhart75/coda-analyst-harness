"""Read-only process/listener inventory for handoff, never a safe-stop certificate.

POSIX uses ps/lsof; Windows uses read-only native APIs. Arguments are used transiently to
identify GigaCode and are never returned, printed, or saved. No process is
stopped, detached, signalled, adopted, restarted, or reconfigured by this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid


def _command(argv):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=20, check=False, env={**os.environ, 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        return None, 'command-unavailable-or-timeout'
    # lsof returns 1 when its selection has no matches. Empty output without a
    # diagnostic is therefore an ordinary empty result, not a failed inventory.
    if result.returncode and not (Path(argv[0]).name == 'lsof' and result.returncode == 1
                                 and not result.stdout.strip() and not result.stderr.strip()):
        return None, 'command-failed'
    # Text-mode universal newlines would turn a CR inside an argument into a
    # new process row. Decode without altering the command's record delimiters.
    return result.stdout.decode('utf-8', 'replace'), 'command-diagnostic' if result.stderr.strip() else None


def _identity(arguments):
    """Return a fixed public label, never an executable's arbitrary argument."""
    try:
        tokens = shlex.split(arguments)
    except ValueError:
        tokens = arguments.split()
    first = Path(tokens[0]).name.lower() if tokens else ''
    for token in tokens[:3]:
        if any(c.isspace() for c in token) or token.startswith('-'):
            continue
        parts = [p.lower() for p in token.replace('\\', '/').split('/')]
        if any(p in {'gigacode', 'gigacode-cli', 'gigacode.app', '@gigacode'} for p in parts):
            return 'gigacode'
    if first in {'postgres', 'postmaster'}:
        return 'postgres'
    if first in {'java', 'java.exe'}:
        return 'java'
    if first in {'node', 'nodejs', 'npm', 'npx', 'bun', 'deno'}:
        return 'node'
    if first.startswith('python'):
        return 'python'
    return 'other'


def _parse_ps(text):
    result = {}
    malformed = False
    # ps emits LF-delimited records. Unicode separators, VT, FF and CR may
    # occur inside arguments and must not become extra process records.
    for line in text.split('\n'):
        if not line.strip():
            continue
        # pid, ppid, pgid, uid, five locale-stable lstart fields, args.
        parts = line.strip().split(None, 9)
        try:
            if len(parts) != 10:
                raise ValueError('Incomplete process row')
            pid, ppid, pgid, uid = map(int, parts[:4])
            if pid <= 0 or min(ppid, pgid, uid) < 0:
                raise ValueError('Invalid process identifier')
        except ValueError:
            malformed = True
            continue
        result[pid] = {'pid': pid, 'ppid': ppid, 'pgid': pgid, 'uid': uid,
                       'started': ' '.join(parts[4:9]), 'kind': _identity(parts[9])}
    return result, malformed


def _parse_lsof(text, kind):
    entries = {}
    pid = None
    malformed = False
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith('p'):
            try:
                pid = int(line[1:])
                if pid <= 0:
                    raise ValueError('Invalid PID')
            except ValueError:
                pid = None
                malformed = True
            continue
        if not line.startswith('n'):
            continue
        if pid is None:
            malformed = True
            continue
        value = line[1:]
        if kind == 'cwd':
            if value.startswith('/') and not value.endswith(' (deleted)'):
                entries[pid] = value
            else:
                malformed = True
        else:
            match = re.search(r':(\d+)(?:\s+\(LISTEN\))?$', value)
            if not match or not 0 < int(match[1]) < 65536:
                malformed = True
            else:
                entries.setdefault(pid, set()).add(int(match[1]))
    return entries, malformed


def _ancestors(pid, processes):
    ancestors = []
    seen = {pid}
    current = processes.get(pid)
    complete = current is not None
    while current and current['ppid'] > 1:
        parent = current['ppid']
        if parent in seen or len(ancestors) >= 128:
            return ancestors, False
        seen.add(parent)
        ancestors.append(parent)
        current = processes.get(parent)
        if current is None:
            complete = False
    return ancestors, complete


def _inside(path, root):
    if isinstance(path, str) and (ntpath.splitdrive(path)[0] or ntpath.splitdrive(str(root))[0]):
        try:
            if not ntpath.isabs(path) or not ntpath.isabs(str(root)):
                return False
            path, root = ntpath.normcase(ntpath.normpath(path)), ntpath.normcase(ntpath.normpath(str(root)))
            return ntpath.commonpath([path, root]) == root
        except ValueError:
            return False
    if not isinstance(path, str) or not path.startswith('/'):
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _inspect_services_once():
    """Inspect own TCP listeners and PostgreSQL processes, including Unix-only DBs.

    complete describes collection of this snapshot only. It says nothing about
    files opened by a service, database durability, launchd ownership, remote
    dependencies, future process changes, or what an agent's shutdown will do.
    """
    if sys.platform == 'win32':
        from windows_inventory import collect
        return collect()
    limitations = []
    ps = shutil.which('ps')
    lsof = shutil.which('lsof')
    uid = os.getuid() if hasattr(os, 'getuid') else None
    result = {'schema': 1, 'platform': sys.platform,
              'captured_at': datetime.now(timezone.utc).isoformat(),
              'complete': False, 'limitations': limitations,
              'agents': [], 'services': [], 'processes': [], 'safe_to_stop': 'not-established'}
    if uid is None or not ps or not lsof:
        limitations.extend(name + '-unavailable' for name, value in
                           (('uid', uid is not None), ('ps', ps), ('lsof', lsof)) if not value)
        return result
    # ps parsing relies only on field count; the fixed C locale also makes the
    # start-time identity stable between consecutive snapshots on one machine.
    ps_text, error = _command([ps, '-axo', 'pid=,ppid=,pgid=,uid=,lstart=,args='])
    if error:
        limitations.append('ps-' + error)
    processes, malformed = _parse_ps(ps_text or '')
    result['processes'] = [{**process, 'owner_known': True, 'same_user': process['uid'] == uid}
                           for process in processes.values()]
    if malformed:
        limitations.append('ps-incomplete-rows')
    if not processes:
        limitations.append('process-table-unavailable')
    listener_text, error = _command([lsof, '-nP', '-a', '-u', str(uid), '-iTCP',
                                     '-sTCP:LISTEN', '-Fpn'])
    if error:
        limitations.append('listeners-' + error)
    listeners, malformed = _parse_lsof(listener_text or '', 'listener')
    if malformed:
        limitations.append('listeners-incomplete-rows')
    own_agents = {pid: p for pid, p in processes.items() if p['uid'] == uid and p['kind'] == 'gigacode'}
    own_databases = {pid for pid, p in processes.items() if p['uid'] == uid and p['kind'] == 'postgres'}
    service_pids = set(listeners) | own_databases
    relevant_pids = sorted(set(own_agents) | service_pids)
    # A deleted cwd in an unrelated application must not block handoff. Read
    # cwd only for agents, TCP listeners and recognized PostgreSQL processes.
    # PostgreSQL may serve Unix sockets only, so absence from TCP is not safety.
    cwd_text, error = (_command([lsof, '-nP', '-a', '-u', str(uid), '-p',
                                ','.join(map(str, relevant_pids)), '-d', 'cwd', '-Fpn'])
                       if relevant_pids else ('', None))
    if error:
        limitations.append('cwd-' + error)
    cwd, malformed = _parse_lsof(cwd_text or '', 'cwd')
    if malformed:
        limitations.append('cwd-incomplete-rows')
    for pid, process in sorted(own_agents.items()):
        ancestors, complete = _ancestors(pid, processes)
        result['agents'].append({**process, 'cwd': cwd.get(pid), 'ancestors': ancestors,
                                 'ancestry_complete': complete})
        if pid not in cwd:
            limitations.append('agent-cwd-unavailable')
        if not complete:
            limitations.append('agent-ancestry-incomplete')
    for pid in sorted(service_pids):
        ports = listeners.get(pid, set())
        process = processes.get(pid)
        if process is None or process['uid'] != uid:
            limitations.append('listener-process-changed-or-unavailable')
            continue
        ancestors, complete = _ancestors(pid, processes)
        evidence = []
        agent_pids = []
        for agent_pid, agent in own_agents.items():
            if agent_pid == pid:
                evidence.append('agent-listener')
                agent_pids.append(agent_pid)
            elif agent_pid in ancestors:
                evidence.append('ancestor')
                agent_pids.append(agent_pid)
            elif agent['pgid'] > 1 and agent['pgid'] == process['pgid']:
                evidence.append('process-group')
                agent_pids.append(agent_pid)
        result['services'].append({**process, 'cwd': cwd.get(pid), 'ports': sorted(ports),
                                   'observations': (['postgres-process'] if pid in own_databases else [])
                                       + (['tcp-listener'] if pid in listeners else []),
                                   'ancestors': ancestors, 'ancestry_complete': complete,
                                   'agent_pids': sorted(set(agent_pids)),
                                   'dependency_evidence': sorted(set(evidence)),
                                   'agent_dependency': 'observed' if evidence else
                                       ('not-observed' if complete else 'unknown')})
        if pid not in cwd:
            limitations.append('database-cwd-unavailable' if pid in own_databases else 'listener-cwd-unavailable')
        if not complete:
            limitations.append('listener-ancestry-incomplete')
    limitations[:] = sorted(set(limitations))
    result['complete'] = not limitations
    return result


def inspect_services():
    """Collect one snapshot, retrying once for an observed PID/cwd race only."""
    result = _inspect_services_once()
    result['collection_attempts'] = 1
    race_limits = {'listener-process-changed-or-unavailable', 'listener-cwd-unavailable', 'database-cwd-unavailable'}
    other_limits = set(result['limitations']) - race_limits
    if race_limits.intersection(result['limitations']) and not other_limits:
        result = _inspect_services_once()
        result['collection_attempts'] = 2
    return result


def assess_handoff(repo, inventory):
    """Decide whether to defer snapshotting a repo, not whether to kill anything.

    A process's cwd does not prove it is currently editing. A matching active
    agent is a conservative handoff blocker because concurrent changes cannot
    be ruled out. Agent-dependent servers are also blockers outside repo cwd:
    an agent may have started its own stand in another directory.
    """
    root = str(Path(repo).resolve())
    blockers, warnings = [], []
    agents = inventory.get('agents', [])
    active = [p for p in agents if _inside(p.get('cwd'), root)]
    unknown_agents = [p for p in agents if not p.get('cwd')]
    relevant = [p for p in inventory.get('services', [])
                if _inside(p.get('cwd'), root) or p.get('agent_pids')]
    if not inventory.get('complete'):
        blockers.append('Снимок процессов неполный; автоматический перенос пока заблокирован.')
    if active:
        blockers.append('В выбранном проекте обнаружена активная сессия GigaCode. '
                        'По списку процессов нельзя исключить одновременную правку кода.')
    if unknown_agents:
        blockers.append('Не удалось определить рабочий каталог активной сессии GigaCode.')
    if any(p.get('agent_pids') for p in relevant):
        blockers.append('Обнаружены сервисы, связанные с GigaCode через родительский процесс '
                        'или общую группу процессов. Сначала нужен отдельный план их сохранения.')
    if relevant:
        warnings.append('Сохранение кода не сохраняет состояние PostgreSQL и других сервисов. '
                        'Пути данных, способ остановки и проверенный запуск нужно зафиксировать отдельно.')
    warnings.append('Этот снимок не разрешает завершать процессы и не доказывает, '
                    'что стенд переживёт закрытие агента.')
    return {'repo': root, 'blocked': bool(blockers), 'blockers': blockers,
            'warnings': warnings, 'active_agents': active,
            'relevant_services': relevant, 'safe_to_stop': 'not-established'}


def detect_new_services(before, after, workspace):
    """Return observed new services attributable by cwd to this workspace.

    An empty result is not proof of absence when either inventory is incomplete.
    Detached services with a cwd outside workspace cannot be attributed by this
    check. Callers must retain and report those collection/attribution limits.
    """
    prior = {(p.get('pid'), p.get('started'), tuple(p.get('ports', [])))
             for p in before.get('services', [])}
    return [{**p, 'attribution': 'cwd-inside-workspace'}
            for p in after.get('services', [])
            if _inside(p.get('cwd'), workspace)
            and (p.get('pid'), p.get('started'), tuple(p.get('ports', []))) not in prior]


def services_in_group(inventory, pgid, member_pids=None):
    """Observed services in a process group; callers must also check complete."""
    if not isinstance(pgid, int) or pgid <= 1:
        raise ValueError('Expected a positive owned process group identifier')
    if inventory.get('platform') == 'win32':
        members = set(member_pids) if member_pids is not None else None
        return [p for p in inventory.get('services', []) + inventory.get('unclassified_services', [])
                if (p['pid'] in members if members is not None else
                    p['pid'] == pgid or pgid in p.get('ancestors', []))]
    return [p for p in inventory.get('services', []) if p.get('pgid') == pgid]


def save_report(path, value):
    path = Path(path).expanduser().absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('Report path must not traverse a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Снимок процессов стенда без остановки и перезапуска.')
    parser.add_argument('--repo', help='Каталог проекта для проверки готовности передачи кода')
    parser.add_argument('--output', help='Новый JSON-файл отчёта; существующий файл не заменяется')
    args = parser.parse_args(argv)
    inventory = inspect_services()
    if args.repo:
        inventory['handoff'] = assess_handoff(args.repo, inventory)
    folder = (Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'GigaCode SDD/diagnostics'
              if sys.platform == 'win32' else
              Path.home() / 'Library/Application Support/GigaCode SDD/diagnostics' if sys.platform == 'darwin'
              else Path.home() / '.local/state/gigacode-sdd/diagnostics')
    name = 'services-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json'
    try:
        report = save_report(Path(args.output) if args.output else folder / name, inventory)
    except (OSError, ValueError) as error:
        print('Не удалось сохранить отчёт: ' + type(error).__name__, file=sys.stderr)
        return 2
    print('Процессы не останавливались и не изменялись.')
    print('Отчёт: ' + str(report))
    print('Снимок: ' + ('полный в пределах доступных средств ОС' if inventory['complete'] else 'неполный'))
    for service in inventory['services']:
        relation = 'связан с GigaCode' if service['agent_pids'] else 'связь с GigaCode не установлена'
        ports = ','.join(map(str, service['ports'])) or 'TCP-порты не обнаружены'
        print(f"PID {service['pid']}: {service['kind']}; {ports}; {relation}")
    print('Этот отчёт не подтверждает безопасность завершения текущего агента.')
    if args.repo:
        for message in inventory['handoff']['blockers']:
            print(message)
    return 2 if not inventory['complete'] or inventory.get('handoff', {}).get('blocked') else 0


if __name__ == '__main__':
    raise SystemExit(main())
