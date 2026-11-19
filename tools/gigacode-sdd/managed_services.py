"""Controller-owned local services, frozen launch inputs and explicit first approval."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import threading
import time
import urllib.request
from urllib.parse import urlsplit

import operator_questions as oq
from platform_support import launch_process, process_exists, process_scope_pids, stop_process_scope
from service_inventory import inspect_services
from workspace import clone_snapshot


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def healthy(spec):
    try:
        health = spec['health']
        if 'url' not in health:
            return all(port_open(p) for p in spec['ports'])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(health['url'], timeout=2) as response:
            return response.status == health.get('status', 200)
    except (OSError, ValueError):
        return False


def manifest(root, paths):
    from controller import under, digest
    result = {}
    for name in paths:
        if any(Path(root, *Path(name).parts[:i]).is_symlink() for i in range(1, len(Path(name).parts)+1)):
            raise ValueError('Service artifact path must not traverse symlinks')
        target = under(root, name)
        if not target.exists() or target.is_symlink():
            raise ValueError('Service artifact is missing or a symlink: ' + name)
        files = [target] if target.is_file() else list(target.rglob('*'))
        for path in files:
            if path.is_symlink():
                raise ValueError('Service artifacts must not contain symlinks')
            if path.is_file():
                result[path.relative_to(root).as_posix()] = digest(path)
    if not result:
        raise ValueError('Service launch needs nonempty frozen artifacts')
    return result


def validate(spec, work):
    from controller import under, relative
    if not isinstance(spec, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', str(spec.get('id', ''))):
        raise ValueError('Invalid service id')
    argv = spec.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and '\0' not in a for a in argv):
        raise ValueError('Service argv must be an argument list')
    if not under(work, spec.get('cwd', '.')).is_dir():
        raise ValueError('Service cwd does not exist')
    ports = spec.get('ports')
    if not isinstance(ports, list) or not ports or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
        raise ValueError('Service needs explicit unprivileged local ports')
    health = spec.get('health')
    if not isinstance(health, dict):
        raise ValueError('Service needs a health check')
    if 'url' in health:
        url = urlsplit(health['url'])
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.port not in ports or url.username or url.password):
            raise ValueError('Health URL must target a declared loopback port')
        if type(health.get('status', 200)) is not int or not 200 <= health.get('status', 200) <= 299:
            raise ValueError('Health check must expect a successful HTTP status')
    elif health != {'tcp': True}:
        raise ValueError('Health check must specify local HTTP or tcp=true')
    artifacts = spec.get('artifacts')
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError('Declare frozen service artifacts')
    for name in artifacts:
        relative(name)
        if name == '.' or any(p in ('.git', '.gigacode', '.codex') for p in Path(name).parts):
            raise ValueError('Invalid service artifact path')
    if not isinstance(spec.get('reason'), str) or not spec['reason'].strip():
        raise ValueError('Service needs its purpose and data scope')
    return {k: spec[k] for k in ('id', 'cwd', 'argv', 'ports', 'health', 'artifacts', 'reason')}


class Manager:
    def __init__(self, runner):
        self.runner = runner
        self.lock = threading.RLock()
        self.active = {}

    @property
    def folder(self):
        return self.runner.root / 'managed-services'

    def prepare(self, spec, work, task):
        from controller import read, save, under
        spec = validate(spec, Path(work))
        self.runner.audit(Path(work))
        files = manifest(Path(work), spec['artifacts'])
        identity = oq.fingerprint({'spec': spec, 'files': files})
        slot = self.folder / identity
        with self.lock:
            record = slot / 'service.json'
            if record.exists():
                value = read(record)
                if value['identity'] != identity:
                    raise ValueError('Service identity mismatch')
                return value
            slot.mkdir(parents=True, exist_ok=True)
            frozen = slot / 'work'
            if frozen.exists():
                # An incomplete preparation is never used as a frozen launch.
                frozen = slot / ('work-' + str(time.time_ns()))
            clone_snapshot(work, frozen, include_dirty=True)
            for name in files:
                source, dest = Path(work) / name, under(frozen, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            if manifest(frozen, spec['artifacts']) != files:
                raise ValueError('Artifacts changed while freezing service inputs')
            key = oq.request(self.runner.root, task, 'service:' + spec['id'],
                'Разрешить запуск локального сервиса ' + spec['id'] + ' для этого прогона?',
                options=['Разрешить этот запуск', 'Не запускать'],
                details={'command': spec['argv'], 'cwd': str(frozen / spec['cwd']),
                         'ports': spec['ports'], 'health': spec['health'],
                         'purpose': spec['reason'], 'artifacts_sha256': files}, generation=identity)
            value = {'identity': identity, 'spec': spec, 'files': files, 'workspace': str(frozen),
                     'question_id': key, 'status': 'awaiting-approval', 'task': task}
            save(record, value)
            return value

    def ensure(self, identity, stop):
        from controller import read, save
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity):
            raise ValueError('Invalid service identity')
        with self.lock:
            path = self.folder / identity / 'service.json'
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError('Service registry must not contain symlinks')
            value = read(path)
            spec = value['spec']
            frozen = Path(value['workspace'])
            if (value['identity'] != identity or oq.fingerprint({'spec': spec, 'files': value['files']}) != identity
                    or frozen.is_symlink() or not frozen.resolve().is_relative_to(path.parent.resolve())):
                raise ValueError('Approved service definition or workspace changed')
            validate(spec, frozen)
            reply = oq.response(self.runner.root, value['question_id'])
            if not reply or reply['text'] != 'Разрешить этот запуск':
                raise oq.WaitingForOperator('Сервис ожидает разрешения: ' + spec['id'])
            if manifest(Path(value['workspace']), spec['artifacts']) != value['files']:
                raise ValueError('Frozen service artifacts changed; a new launch approval is required')
            if identity in self.active:
                proc = self.active[identity]
                if proc.poll() is not None or not healthy(spec):
                    raise RuntimeError('Managed service lost readiness: ' + spec['id'])
                return value
            # A PID alone is never authority to signal/adopt a surviving process.
            if value.get('pid') and value.get('status') != 'stopped' and process_exists(value['pid']):
                raise RuntimeError('Previous service process needs reconciliation: ' + str(path))
            if any(port_open(p) for p in spec['ports']):
                raise RuntimeError('Service port is occupied; existing listener preserved: ' + str(spec['ports']))
            log = path.parent / ('launch-' + str(time.time_ns()) + '.log')
            value.update(status='starting', log=str(log))
            save(path, value)
            self.runner.progress('Стенд: запускаю ' + spec['id'] + '; проверяю готовность; журнал: ' + str(log))
            with log.open('wb') as output:
                proc = launch_process(spec['argv'], cwd=Path(value['workspace']) / spec['cwd'],
                    env=self.runner.env, stdin=-3, stdout=output, stderr=-2)
            self.active[identity] = proc
            value.update(pid=proc.pid, status='starting')
            save(path, value)
            timeout = self.runner.cfg.get('service_startup_timeout', 120)
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                raise ValueError('service_startup_timeout must be in (0, 120] seconds')
            deadline = time.monotonic() + timeout
            while not stop.is_set() and proc.poll() is None and time.monotonic() < deadline:
                if healthy(spec):
                    inventory = inspect_services()
                    members = set(process_scope_pids(proc.pid) or [proc.pid])
                    listeners = [p for p in inventory.get('services', []) if p['pid'] in members]
                    owned_ports = {port for p in listeners for port in p.get('ports', [])}
                    if not inventory.get('complete') or not set(spec['ports']) <= owned_ports:
                        stop.wait(.25)
                        continue
                    value['listener_identities'] = [{'pid': p['pid'], 'started': p.get('started'),
                                                    'ports': p['ports']} for p in listeners]
                    value.update(status='ready', ready_at=time.time())
                    save(path, value)
                    self.runner.progress('Стенд: ' + spec['id'] + ' готов; ' +
                        str(spec['health'].get('url', spec['ports'])) + '; журнал: ' + str(log))
                    return value
                stop.wait(.25)
            value.update(status='failed', error='Service exited, cancelled or health timeout')
            save(path, value)
            if proc.poll() is None:
                stop_process_scope(proc)
            proc.wait(timeout=10)
            self.active.pop(identity, None)
            raise RuntimeError('Service did not become ready; inspect ' + str(log))

    def context(self):
        from controller import read
        with self.lock:
            result = []
            for identity, proc in self.active.items():
                value = read(self.folder / identity / 'service.json')
                if proc.poll() is None and value['status'] == 'ready':
                    result.append({'id': value['spec']['id'], 'health': value['spec']['health'],
                                   'ports': value['spec']['ports'], 'artifacts_sha256': value['files'],
                                   'log': value['log']})
            return result

    def restart(self, name, stop):
        from controller import read, save
        with self.lock:
            matches = [p for p in self.folder.glob('*/service.json')
                       if read(p)['spec']['id'] == name and read(p)['status'] in ('ready', 'stopped')]
            if len(matches) != 1:
                raise ValueError('Restart requires one previously approved service')
            path = matches[0]
            value = read(path)
            identity = value['identity']
            if identity in self.active:
                proc = self.active[identity]
                if proc.poll() is None:
                    stop_process_scope(proc)
                proc.wait(timeout=10)
                self.active.pop(identity)
                value.update(status='stopped', stopped_at=time.time())
                save(path, value)
            return self.ensure(identity, stop)

    def restore(self, stop):
        from controller import read
        if not self.folder.exists():
            return
        for path in sorted(self.folder.glob('*/service.json')):
            value = read(path)
            if value['status'] in ('ready', 'stopped', 'starting'):
                self.ensure(value['identity'], stop)

    def close(self):
        from controller import read, save
        with self.lock:
            for identity, proc in list(self.active.items()):
                # Only process handles launched by this controller may be stopped.
                if proc.poll() is None:
                    stop_process_scope(proc)
                proc.wait(timeout=10)
                path = self.folder / identity / 'service.json'
                value = read(path)
                value.update(status='stopped', stopped_at=time.time())
                save(path, value)
                del self.active[identity]
