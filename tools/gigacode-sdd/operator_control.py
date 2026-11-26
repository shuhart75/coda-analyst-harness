"""Cooperative controller control, addressed to one run-lock owner. Never signals PIDs."""
from pathlib import Path
import threading
import re
import time
import uuid

from platform_support import FileLock


def active(root):
    try:
        with FileLock(Path(root) / 'run.lock'):
            return False
    except BlockingIOError:
        return True


def status(root):
    from controller import read
    root = Path(root)
    running = active(root)
    try:
        owner = read(root/'controller-runtime.json')
        instance = owner.get('instance', '')
        value = read(root/'controller-instances'/(instance+'.json')) if isinstance(instance, str) and re.fullmatch(r'[0-9a-f]{32}', instance) else {}
        if value.get('instance') != instance:
            value = {}
    except (OSError, ValueError):
        value = {}
    supported = bool(running and value.get('protocol') == 1 and value.get('state') == 'running'
                     and time.time() - value.get('heartbeat', 0) < 10)
    return {'active': running, 'controllable': supported,
            'instance': value.get('instance') if supported else None,
            'stop_requested': value.get('stop_requested', False) if supported else False,
            'reason': ('Работающий контроллер не поддерживает управление из браузера или не отвечает. '
                       'Новый запуск после обновления будет управляемым.') if running and not supported else ''}


def request_stop(root, instance, request_id):
    from controller import save
    observed = status(root)
    if not observed['controllable'] or observed['instance'] != instance:
        raise ValueError('Контроллер изменился или недоступен. Обновите состояние.')
    save(Path(root)/'controller-stop-request.json', {'instance': instance, 'id': request_id})
    return {'requested': True, 'instance': instance, 'id': request_id}


class ControllerControl:
    def __init__(self, root, stop):
        self.root, self.stop = Path(root), stop
        self.instance = uuid.uuid4().hex
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def publish(self, state):
        from controller import save
        save(self.root/'controller-instances'/(self.instance+'.json'), {'protocol': 1, 'instance': self.instance,
            'state': state, 'heartbeat': time.time(), 'stop_requested': self.stop.is_set()})

    def start(self):
        from controller import save
        self.publish('running')
        save(self.root/'controller-runtime.json', {'protocol': 1, 'instance': self.instance})
        self.thread.start()

    def watch(self):
        from controller import read, save
        while not self.closed.wait(.25):
            try:
                path = self.root/'controller-stop-request.json'
                if path.is_file() and not path.is_symlink():
                    request = read(path)
                    if request.get('instance') == self.instance:
                        self.stop.set()
                        save(self.root/'controller-stop-receipt.json', {**request, 'accepted': True})
                self.publish('running')
            except (OSError, ValueError):
                # Inability to read a request is never permission to stop a different process.
                continue

    def close(self):
        self.closed.set()
        self.thread.join(2)
        self.publish('exited')
