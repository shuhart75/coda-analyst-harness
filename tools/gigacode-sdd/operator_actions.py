"""Durable web command receipts. Only fixed actions, never user-supplied commands."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from controller import digest, read, save
from platform_support import FileLock
import operator_control

ACTIONS = {'resume', 'new', 'stop', 'restart', 'diagnostics', 'export'}
ID = re.compile(r'[0-9a-f]{32}')


def revision(config):
    state = operator_control.status(Path(config).parent) if config else {'active': False, 'instance': None}
    data = [str(config) if config else None, digest(config) if config else None, state['active'], state['instance']]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


class Actions:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.children = []

    def path(self, key):
        if not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError('Некорректный идентификатор операции.')
        folder = self.folder/key
        if folder.is_symlink():
            raise ValueError('Некорректный каталог операции.')
        return folder

    def operations(self):
        self.children = [p for p in self.children if p.poll() is None]
        result = []
        for folder in sorted(self.folder.iterdir(), key=lambda p: p.name):
            if not ID.fullmatch(folder.name) or folder.is_symlink():
                continue
            try:
                request = read(folder/'request.json')
                state = read(folder/'state.json') if (folder/'state.json').exists() else {'status': 'queued'}
                if state['status'] in ('queued', 'running', 'waiting') and time.time()-request['created_at'] > 30:
                    try:
                        with FileLock(folder/'worker.lock'):
                            state = {**state, 'status': 'failed', 'question': None,
                                     'error': 'Процесс операции завершился без квитанции. Проверьте состояние запуска перед повтором.'}
                    except BlockingIOError:
                        pass
                result.append({**state, 'id': folder.name, 'action': request['action'],
                               'created_at': request['created_at'], 'config': state.get('config', request.get('config'))})
            except (OSError, ValueError, KeyError):
                continue
        ordered = sorted(result, key=lambda x: x['created_at'], reverse=True)
        return [op for i, op in enumerate(ordered) if i < 30 or op['status'] in ('queued', 'running', 'waiting')]

    def submit(self, config, payload):
        if (not isinstance(payload, dict) or set(payload) != {'id', 'action', 'revision'}
                or payload.get('action') not in ACTIONS):
            raise ValueError('Некорректное действие.')
        folder = self.path(payload['id'])
        with FileLock(self.folder/'submit.lock'):
            if (folder/'request.json').exists():
                previous = read(folder/'request.json')
                if previous['intent'] != payload:
                    raise ValueError('Идентификатор уже относится к другой операции.')
                return {'id': folder.name, 'accepted': True}
            if payload['revision'] != revision(config):
                raise ValueError('Выбранный запуск или контроллер изменился. Обновите страницу.')
            action = payload['action']
            current = operator_control.status(config.parent) if config else {'active': False, 'controllable': False}
            if action != 'new' and config is None:
                raise ValueError('Сначала выберите или создайте запуск.')
            # A stop/diagnostic may run while the lifecycle worker is executing SDD.
            busy = [op for op in self.operations() if op['status'] in ('queued', 'running', 'waiting')
                    and op['action'] in ('new', 'resume', 'restart', 'export')]
            if action in ('new', 'resume', 'export') and (current['active'] or busy):
                raise ValueError('Уже выполняется запуск или мастер. Завершите текущую операцию.')
            if action == 'restart' and (not current['controllable'] or any(op.get('phase') != 'execution' for op in busy)):
                raise ValueError('Перезапуск сейчас недоступен: нужна управляемая работающая сессия.')
            if action == 'diagnostics' and any(op['action'] == 'diagnostics' and op['status'] in ('queued', 'running', 'waiting') for op in self.operations()):
                raise ValueError('Диагностика уже собирается.')
            if action == 'stop' and not current['controllable']:
                raise ValueError(current.get('reason') or 'Нет управляемого работающего контроллера.')
            request = {'intent': payload, 'action': action, 'created_at': time.time(),
                       'config': str(config) if config else None, 'config_sha256': digest(config) if config else None,
                       'instance': current.get('instance')}
            folder.mkdir(mode=0o700)
            save(folder/'request.json', request)
            if action == 'stop':
                try:
                    operator_control.request_stop(config.parent, current['instance'], folder.name)
                    save(folder/'state.json', {'status': 'succeeded', 'phase': 'stop-requested',
                         'notice': 'Запрос остановки отправлен. Дождитесь освобождения контроллера; сервисы проверяются отдельно.'})
                except (OSError, ValueError) as exc:
                    save(folder/'state.json', {'status': 'failed', 'error': str(exc)})
                    raise
            else:
                save(folder/'state.json', {'status': 'queued'})
                kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                try:
                    with (folder/'worker.log').open('ab') as log:
                        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('operator_worker.py')),
                            '--operation', str(folder)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            env={**os.environ, 'PYTHONUTF8': '1'}, **kwargs)
                    self.children.append(child)
                except OSError as exc:
                    save(folder/'state.json', {'status': 'failed', 'error': str(exc)})
                    raise
        return {'id': folder.name, 'accepted': True}

    def reply(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'operation', 'question', 'value'}:
            raise ValueError('Некорректный ответ мастеру.')
        folder = self.path(payload['operation'])
        if not isinstance(payload['question'], str) or not ID.fullmatch(payload['question']):
            raise ValueError('Некорректный вопрос мастера.')
        path = folder/('reply-' + payload['question'] + '.json')
        with FileLock(self.folder/'submit.lock'):
            if path.exists():
                if read(path)['value'] != payload['value']:
                    raise ValueError('Другой ответ уже сохранён.')
                return {'saved': True}
            if (folder/'cancel.json').exists():
                raise ValueError('Мастер уже отменён. Ответ не применён.')
            state = read(folder/'state.json')
            q = state.get('question') or {}
            if state['status'] != 'waiting' or q.get('id') != payload['question']:
                raise ValueError('Вопрос мастера уже изменился.')
            value = payload['value']
            if q['kind'] in ('choose', 'confirm'):
                valid = type(value) is int and 0 <= value < len(q['options'])
            else:
                valid = isinstance(value, str) and 0 < len(value) <= 4096
            if not valid:
                raise ValueError('Выберите вариант или укажите полный путь.')
            save(path, {'value': value})
        return {'saved': True}

    def cancel(self, key):
        folder = self.path(key)
        with FileLock(self.folder/'submit.lock'):
            state = read(folder/'state.json')
            qid = (state.get('question') or {}).get('id', '')
            if state.get('status') != 'waiting' or (folder/('reply-' + qid + '.json')).exists():
                raise ValueError('Мастер уже продолжен. Для остановки работы используйте отдельное действие.')
            save(folder/'cancel.json', {'cancel': True})
        return {'cancelled': True}

    def artifact(self, key):
        folder = self.path(key)
        state = read(folder/'state.json')
        path = Path(state.get('artifact', ''))
        if state.get('status') != 'succeeded' or not path.is_file() or path.is_symlink():
            raise ValueError('Файл результата ещё не готов.')
        if digest(path) != state.get('artifact_sha256'):
            raise ValueError('Файл результата изменился после создания.')
        return path
