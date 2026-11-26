"""Detached worker for fixed web actions and the existing bootstrap wizard."""
import argparse
import json
from pathlib import Path
import time
import uuid

from controller import Blocked, Interrupted, Runner, digest, read, save
from platform_support import FileLock
import bootstrap
import operator_control


class WizardCancelled(Blocked):
    pass


class WebUI:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.state = {'status': 'running', 'phase': 'preparation', 'messages': []}
        self.write()

    def write(self, **values):
        self.state.update(values)
        if values.get('artifact'):
            self.state['artifact_sha256'] = digest(values['artifact'])
        self.state['updated_at'] = time.time()
        save(self.folder/'state.json', self.state)

    def notify(self, message):
        self.state['messages'] = (self.state['messages'] + [str(message)])[-12:]
        self.write()

    def ask(self, kind, title, options=None):
        q = {'id': uuid.uuid4().hex, 'kind': kind, 'title': title, 'options': options or []}
        self.write(status='waiting', question=q)
        while True:
            if (self.folder/'cancel.json').exists():
                raise WizardCancelled('Мастер отменён. Работающий запуск не останавливался.')
            path = self.folder/('reply-' + q['id'] + '.json')
            if path.is_file():
                response = read(path)
                self.write(status='running', question=None)
                return response['value']
            time.sleep(.2)

    def choose(self, title, items):
        if not items:
            raise ValueError('Empty chooser')
        return self.ask('choose', title, [str(i) for i in items])

    def confirm(self, message, action='Запустить'):
        if self.ask('confirm', message, [action, 'Отмена']) != 0:
            raise WizardCancelled('Запуск отменён. Ничего не остановлено.')

    def path(self, title, directory):
        while True:
            value = self.ask('folder' if directory else 'file', title)
            path = Path(value.strip().strip('"').strip("'")).expanduser()
            if path.is_absolute() and (path.is_dir() if directory else path.is_file()):
                return path.resolve()
            self.notify('Нужен полный путь к существующей ' + ('папке.' if directory else 'файлу.'))

    def folder(self, title):
        return self.path(title, True)

    def file(self, title):
        return self.path(title, False)

    def on_config(self, config):
        self.write(config=str(config), phase='execution', question=None)


def perform(folder):
    folder = Path(folder)
    command = read(folder/'request.json')
    ui = WebUI(folder)
    try:
        action = command['action']
        config = Path(command['config']) if command.get('config') else None
        if config and digest(config) != command['config_sha256']:
            raise Blocked('Конфигурация изменилась после выбора действия.')
        if action == 'restart':
            ui.write(phase='stopping')
            operator_control.request_stop(config.parent, command['instance'], folder.name)
            deadline = time.monotonic() + 120
            while operator_control.active(config.parent):
                if time.monotonic() >= deadline:
                    raise Blocked('Остановка ещё не подтверждена. Новый контроллер не запущен; проверьте состояние.')
                time.sleep(.25)
            if digest(config) != command['config_sha256']:
                raise Blocked('Конфигурация изменилась во время остановки.')
        if action in ('resume', 'restart'):
            code = bootstrap.resume(ui, config, on_config=ui.on_config)
            ui.write(status='stopped' if code == 130 else 'succeeded', phase='finished')
        elif action == 'new':
            code = bootstrap.launch(ui=ui, new=True, on_config=ui.on_config)
            ui.write(status='stopped' if code == 130 else 'succeeded', phase='finished')
        elif action == 'diagnostics':
            import diagnostics
            ui.write(phase='diagnostics')
            output = diagnostics.collect(config, destination=folder/'diagnostics.txt')
            ui.write(status='succeeded', artifact=str(output), phase='finished',
                     notice='Диагностика содержит локальные данные. Перед отправкой другим людям просмотрите файл. '
                            'Во время работы запуска это наблюдения файлов в разные моменты, не атомарный снимок.')
        elif action == 'export':
            directory = ui.folder('Выберите папку для результата. Введите полный путь на этом компьютере.')
            output = directory/('sdd-result-' + folder.name + '.patch')
            ui.confirm('Экспортировать проверенный результат?\nФайл: ' + str(output), 'Экспортировать')
            runner = Runner(config)
            with runner.locked():
                artifact = runner.export(output)
            ui.write(status='succeeded', artifact=artifact, phase='finished')
        else:
            raise ValueError('Unsupported operation')
    except (Interrupted, WizardCancelled) as exc:
        ui.write(status='stopped', phase='finished', question=None, notice=str(exc))
        return 130
    except (Exception, KeyboardInterrupt) as exc:
        ui.write(status='failed', phase='finished', question=None, error=str(exc))
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', type=Path, required=True)
    folder = parser.parse_args().operation.resolve(strict=True)
    with FileLock(folder/'worker.lock'):
        return perform(folder)


if __name__ == '__main__':
    raise SystemExit(main())
