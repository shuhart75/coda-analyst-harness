"""Console setup for GigaCode SDD on Windows, macOS and Linux; no downloads or service stops."""
from __future__ import annotations
import sys

if sys.version_info < (3, 10):
    if __name__ == '__main__':
        print('Нужен установленный Python 3.10 или новее. Автоматическая установка не выполняется.')
        raise SystemExit(2)
if __name__ == '__main__' and '--check-python' in sys.argv:
    raise SystemExit(0)

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit
import uuid

from controller import Blocked, Runner, digest, initialize, read, save
from workspace import WorkspaceError

NEXUS_HOST = 'nexus-ci.delta.sbrf.ru'
SKIP_DIRS = {'.git', '.workspace-state', '.venv', 'venv', 'node_modules', 'target',
             'dist', 'build', 'logs', 'pgdata', '.m2', '.npm', '__pycache__', 'returns'}


def app_root(home=None, platform=None, environment=None):
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform
    env = os.environ if environment is None else environment
    if platform == 'win32':
        local = Path(env.get('LOCALAPPDATA') or home / 'AppData/Local')
        if not local.is_absolute():
            local = home / 'AppData/Local'
        return local / 'GigaCode SDD'
    if platform == 'darwin':
        return home / 'Library/Application Support/GigaCode SDD'
    state = Path(env.get('XDG_STATE_HOME') or home / '.local/state')
    if not state.is_absolute():
        state = home / '.local/state'
    return state / 'gigacode-sdd'


def new_state_dir(root):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return Path(root) / 'runs' / (stamp + '-' + uuid.uuid4().hex[:8])


def bounded_dirs(roots, depth=4, limit=5000):
    """Bounded search under explicit roots. Never traverse symlinks or HOME itself."""
    seen, count = set(), 0
    for root in roots:
        root = Path(root).expanduser()
        if root.is_symlink() or not root.is_dir():
            continue
        root = root.resolve()
        for current, dirs, _ in os.walk(root, followlinks=False):
            current = Path(current)
            if current in seen:
                dirs[:] = []
                continue
            seen.add(current)
            count += 1
            if count > limit:
                return
            yield current
            relative = current.relative_to(root)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')
                             and not (current / d).is_symlink()) if len(relative.parts) < depth else []


def workspace_roots(home=None, launcher=None):
    home = Path.home() if home is None else Path(home)
    launcher = Path(__file__).resolve() if launcher is None else Path(launcher).resolve()
    roots = [home / 'work']
    for parent in launcher.parents:
        if parent == home or parent == parent.parent:
            break
        if (parent / '.git').exists() or (parent / '.analyst-workspace.json').is_file():
            roots.append(parent)
            break
    return list(dict.fromkeys(roots))


def is_product_repo(path):
    path = Path(path)
    if not (path / '.git').exists() or not all((path / p).is_dir() for p in ('backend', 'frontend')):
        return False
    try:
        root = subprocess.check_output(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                                       stderr=subprocess.DEVNULL, encoding='utf-8', timeout=10).strip()
        return Path(root).resolve() == path.resolve()
    except (OSError, subprocess.SubprocessError):
        return False


def declared_code_roots(directory):
    directory = Path(directory)
    roots = set()
    for config in (directory / '.analyst-workspace.json', directory / '.workspace-state/workspace.json'):
        if not config.is_file() or config.is_symlink() or config.stat().st_size > 1024 * 1024:
            continue
        try:
            data = read(config)
            if config.name == 'workspace.json':
                if data.get('schema_version') != 4:
                    continue
                code = data.get('roles', {}).get('code', {})
            else:
                code = data.get('code', {})
            value = code.get('path') if isinstance(code, dict) else None
            if not isinstance(value, str) or not value:
                continue
            if config.name == 'workspace.json' and not Path(value).is_absolute():
                continue
            root = (directory / value).resolve()
            if is_product_repo(root):
                roots.add(root)
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(roots, key=str)


def discover_repositories(roots):
    candidates, protected = set(), set()
    for directory in bounded_dirs(roots):
        if is_product_repo(directory):
            candidates.add(directory.resolve())
        for code in declared_code_roots(directory):
            candidates.add(code)
            protected.add(code)
    return sorted(candidates, key=str), sorted(protected, key=str)


def requirement_candidate(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.name != 'requirements.md':
        return None
    if path.stat().st_size > 8 * 1024 * 1024:
        return None
    try:
        content = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return None
    if not re.search(r'\bREQ-[A-Za-z0-9]', content):
        return None
    path = path.resolve()
    heading = next((line.lstrip('#').strip() for line in content.splitlines() if line.startswith('# ')), '')
    meta = {}
    manifest = path.parent / 'manifest.json'
    if manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size < 1024 * 1024:
        try:
            data = read(manifest)
            for key in ('feature', 'feature_id', 'revision', 'stage_id', 'title'):
                value = data.get(key)
                if isinstance(value, (str, int)):
                    meta[key] = str(value)
        except (OSError, ValueError, AttributeError):
            pass
    label = ' · '.join(x for x in (meta.get('title') or heading or path.parent.name,
                                  'редакция ' + meta['revision'] if 'revision' in meta else '', str(path)) if x)
    # Display data never becomes code or a shell argument expression.
    label = ' '.join(label.split())
    return {'file': str(path), 'packet': str(path.parent), 'label': label, 'metadata': meta}


def discover_requirements(repo, roots):
    candidates = {}
    for directory in bounded_dirs([Path(repo) / 'requirements-exchange', *roots], depth=7):
        candidate = requirement_candidate(directory / 'requirements.md')
        if candidate:
            candidates[candidate['file']] = candidate
    return [candidates[key] for key in sorted(candidates)]


def nexus_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and parsed.hostname == NEXUS_HOST
                and parsed.username is None and parsed.password is None and '${' not in value)
    except ValueError:
        return False


def infer_nexus(repo, home=None, environment=None):
    """Choose explicit local endpoints; never turn public configuration into approval."""
    home = Path.home() if home is None else Path(home)
    env = os.environ if environment is None else environment
    result = {'nexus_host': [NEXUS_HOST], 'npm_registry': None, 'pip_index_url': None}
    for names, key in ((('NPM_CONFIG_REGISTRY', 'npm_config_registry'), 'npm_registry'),
                       (('PIP_INDEX_URL', 'pip_index_url'), 'pip_index_url')):
        value = next((env[n] for n in names if env.get(n)), None)
        if value:
            if not nexus_url(value):
                raise Blocked('В окружении задан внешний или неподтверждённый реестр. Нужен корпоративный профиль Nexus; секреты не показаны.')
            result[key] = value
    if result['npm_registry'] is None:
        for path in (Path(repo) / 'frontend/.npmrc', Path(repo) / '.npmrc', home / '.npmrc'):
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                continue
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                match = re.fullmatch(r'\s*registry\s*=\s*(.*?)\s*', line)
                if match:
                    value = match[1].strip('"\'')
                    if not nexus_url(value):
                        raise Blocked('В локальном .npmrc указан внешний или неподтверждённый registry. Нужен корпоративный профиль Nexus; URL не показан.')
                    result['npm_registry'] = value
                    break
            if result['npm_registry']:
                break
    return result


def find_cli(home=None):
    home = Path.home() if home is None else Path(home)
    found = shutil.which('gigacode')
    candidates = ([Path(found)] if found else []) + [
        Path('/opt/homebrew/bin/gigacode'), Path('/usr/local/bin/gigacode'),
        home / '.local/bin/gigacode', home / '.npm-global/bin/gigacode',
        home / '.gigacode/bin/gigacode']
    if sys.platform == 'win32':
        appdata = Path(os.environ.get('APPDATA') or home / 'AppData/Roaming')
        candidates.extend(appdata / 'npm' / name for name in ('gigacode.cmd', 'gigacode.exe'))
        candidates.extend(home / '.gigacode/bin' / name for name in ('gigacode.exe', 'gigacode.cmd'))
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


class ConsoleUI:
    """Numbered choices; all entered values remain data, never shell source."""
    def __init__(self, input_fn=None, output_fn=None):
        self.input = input if input_fn is None else input_fn
        self.output = print if output_fn is None else output_fn

    def _read(self, prompt):
        try:
            value = self.input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise Blocked('Выбор отменён. Ничего не остановлено.') from None
        if value.casefold() in ('q', 'quit', 'отмена'):
            raise Blocked('Выбор отменён. Ничего не остановлено.')
        return value

    def choose(self, title, items):
        if not items:
            raise ValueError('Empty chooser')
        self.output(title)
        for number, item in enumerate(items, 1):
            self.output(str(number) + '. ' + str(item))
        while True:
            selected = self._read('Номер варианта (q: отмена): ')
            if selected.isascii() and selected.isdigit() and 1 <= int(selected) <= len(items):
                return int(selected) - 1
            self.output('Введите номер от 1 до ' + str(len(items)) + '.')

    def _path(self, prompt, is_folder):
        while True:
            value = self._read(prompt + '\nПолный путь (q: отмена): ')
            # Accept paths copied with surrounding quotes; do not expand shell variables.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if not value:
                raise Blocked('Путь не выбран. Ничего не остановлено.')
            path = Path(value).expanduser()
            if path.is_absolute() and (path.is_dir() if is_folder else path.is_file()):
                return path.resolve()
            self.output('Нужен полный путь ' + ('к существующей папке.' if is_folder else 'к существующему файлу.'))

    def folder(self, prompt):
        return self._path(prompt, True)

    def file(self, prompt):
        return self._path(prompt, False)

    def confirm(self, message, action='Запустить'):
        self.output(message)
        if self._read(action + '? [да/Нет]: ').casefold() not in ('да', 'yes', 'y'):
            raise Blocked('Запуск отменён. Ничего не остановлено.')

    def notify(self, message):
        self.output(message)


def choose_repository(ui, candidates):
    labels = [str(p) for p in candidates] + ['Выбрать другую папку…']
    choice = ui.choose('Выберите исходный PoC. Runner сохранит его и будет работать в отдельной копии.', labels) if candidates else 0
    repo = candidates[choice] if candidates and choice < len(candidates) else ui.folder('Выберите Git-каталог PoC с backend и frontend')
    repo = Path(repo).resolve()
    if not is_product_repo(repo):
        raise Blocked('Выбранная папка не является корнем Git-проекта с backend и frontend. Другой каталог не подставлен.')
    return repo


def choose_requirements(ui, candidates):
    labels = [item['label'] for item in candidates] + ['Выбрать другую папку пакета…']
    choice = ui.choose('Выберите переданный пакет требований. Новейшая дата сама по себе не определяет нужную редакцию.', labels) if candidates else 0
    if candidates and choice < len(candidates):
        return candidates[choice]
    folder = ui.folder('Выберите каталог переданного пакета с requirements.md')
    direct = requirement_candidate(folder / 'requirements.md')
    if direct:
        return direct
    found = discover_requirements(folder, [folder])
    if not found:
        raise Blocked('В выбранном каталоге не найден requirements.md с REQ-идентификаторами. Пакет не выбран.')
    return found[ui.choose('В пакете найдено несколько документов. Выберите основной.', [x['label'] for x in found])]


def known_runs(root):
    runs = []
    record = Path(root) / 'last-run.json'
    if not record.is_file():
        return runs
    try:
        config = Path(read(record)['config']).resolve()
        if not config.is_relative_to(Path(root).resolve() / 'runs') or not config.is_file():
            return runs
        data = read(config)
        state_dir = Path(data['state_dir']).resolve()
        if state_dir != config.parent or not (state_dir / 'state.json').is_file():
            return runs
        state = read(state_dir / 'state.json')
        runs.append({'config': str(config), 'source': data['source'],
                     'requirements_sha256': data['requirements_sha256'], 'status': state['status']})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return runs


def choose_prior_history(ui, repo):
    try:
        from prior_context import discover_prior_context
    except ImportError:
        return None
    found = discover_prior_context(repo)
    candidates = found.get('candidates', [])
    labels = ['Без журнала'] + [str(item['path']) for item in candidates] + ['Выбрать файл журнала…']
    choice = ui.choose('Можно использовать прежний диалог как справку. Требования и фактический код имеют приоритет.', labels)
    if choice == 0:
        return None
    if choice <= len(candidates):
        return str(Path(candidates[choice - 1]['path']).resolve())
    path = ui.file('Выберите экспорт прежнего диалога в формате JSONL')
    if path.suffix.lower() != '.jsonl' or not path.is_file():
        raise Blocked('Выбранный журнал должен быть существующим файлом JSONL.')
    return str(path)


def confirm_history_scope(ui, history, repo):
    if history is None:
        return None, False, None
    from prior_context import inspect_history_scope
    scope = inspect_history_scope(history, repo)
    if scope['matches'] or scope['unknown']:
        return history, False, scope['source_sha256']
    choice = ui.choose('Журнал был начат в другом каталоге:\n' + '\n'.join(scope['cwds'])
                       + '\n\nВыбранный проект:\n' + str(Path(repo).resolve())
                       + '\n\nПодтвердите связь, только если этот диалог относится к работе над выбранным проектом. '
                       'Это не подтверждает совпадение версии кода; история останется справкой.',
                       ['Без журнала', 'Подтверждаю связь журнала с выбранным проектом'])
    if choice == 0:
        return None, False, None
    return history, True, scope['source_sha256']


def launcher_dependency(inventory):
    """Check observed GigaCode ancestry even when its cwd is another project."""
    agents = {p['pid']: p for p in inventory.get('agents', []) if isinstance(p.get('pid'), int)}
    if not agents:
        return {'complete': True, 'agent_pids': []}
    records = inventory.get('processes', [])
    table = {p['pid']: p for p in records if isinstance(p.get('pid'), int)
             and isinstance(p.get('ppid'), int)}
    if not table:
        return {'complete': False, 'agent_pids': []}
    pid = os.getpid()
    seen, found = set(), set()
    complete = True
    while pid > 1:
        if pid in seen or pid not in table or len(seen) > 128:
            complete = False
            break
        seen.add(pid)
        if pid in agents:
            found.add(pid)
        pid = table[pid]['ppid']
    group = table.get(os.getpid(), {}).get('pgid')
    if group is not None and group > 1:
        found.update(pid for pid, agent in agents.items() if agent.get('pgid') == group)
    return {'complete': complete, 'agent_pids': sorted(found)}


def diagnostic(root, repo=None):
    try:
        from service_inventory import inspect_services, assess_handoff
    except ImportError:
        raise Blocked('Пакет неполон: нет service_inventory.py. Запуск запрещён без проверки сервисов.') from None
    inventory = inspect_services()
    assessment = assess_handoff(Path(repo), inventory) if repo is not None else None
    dependency = launcher_dependency(inventory)
    if assessment is not None and (dependency['agent_pids'] or not dependency['complete']):
        assessment = {**assessment, 'blockers': list(assessment.get('blockers', [])), 'blocked': True}
        assessment['blockers'].append('Контроллер связан с GigaCode или его независимый запуск не подтверждён. '
                                      'Запустите bootstrap.py установленным Python в отдельном терминале вне агента; '
                                      'текущую сессию не завершайте.')
    report = {'inventory': inventory, 'handoff': assessment, 'launcher_dependency': dependency,
              'source': str(repo) if repo is not None else None}
    output = Path(root) / 'diagnostics' / (datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
    save(output, report)
    return report, output


def require_handoff(root, repo):
    report, output = diagnostic(root, repo)
    assessment = report['handoff']
    if assessment.get('blocked') or not report['inventory'].get('complete'):
        details = assessment.get('blockers', []) or report['inventory'].get('limitations', [])
        raise Blocked('Передача пока не подтверждена. Текущую сессию и сервисы не завершайте. '
                      'Отчёт: ' + str(output) + '\n' + '\n'.join(str(x) for x in details))
    return output


def launch(ui=None, home=None, diagnose_only=False, repo_arg=None, dry_run=False):
    ui = ConsoleUI() if ui is None else ui
    home = Path.home() if home is None else Path(home)
    root = app_root(home)
    if diagnose_only:
        report, output = diagnostic(root, Path(repo_arg).resolve() if repo_arg else None)
        print('Диагностика сохранена: ' + str(output))
        print('Проверка ничего не останавливает и не подтверждает возможность выключить старую сессию.')
        if report['handoff']:
            print(json.dumps(report['handoff'], ensure_ascii=False, indent=2))
        return 0
    previous = known_runs(root)
    if previous:
        mode = ui.choose('Что сделать?', ['Продолжить сохранённую работу (' + previous[0]['status'] + ')',
                                         'Новая работа', 'Только диагностика'])
        if mode == 2:
            return launch(ui, home, diagnose_only=True, repo_arg=previous[0]['source'])
        if mode == 0:
            saved = previous[0]
            require_handoff(root, saved['source'])
            ui.confirm('Продолжить ровно этот запуск?\nПроект: ' + saved['source']
                       + '\nSHA256 требований: ' + saved['requirements_sha256']
                       + '\nСостояние: ' + saved['status']
                       + '\nИсходный PoC и работающие сервисы не переключаются.', 'Продолжить')
            if dry_run:
                print('Подготовлен прежний запуск: ' + saved['config'])
                return 0
            runner = Runner(saved['config'])
            with runner.locked():
                runner.start(retry_blocked=saved['status'] == 'blocked')
            print('Состояние: ' + runner.state['status'] + '\nРезультат: ' + str(runner.main))
            return 130 if runner.stop.is_set() else 0
    roots = workspace_roots(home)
    candidates, protected = discover_repositories(roots)
    repo = Path(repo_arg).resolve() if repo_arg else choose_repository(ui, candidates)
    if not is_product_repo(repo):
        raise Blocked('Не найден корень Git-проекта с backend и frontend.')
    report_path = require_handoff(root, repo)
    packets = discover_requirements(repo, [home / 'Downloads', *roots])
    selected = choose_requirements(ui, packets)
    history = choose_prior_history(ui, repo)
    history, history_mapping, history_sha256 = confirm_history_scope(ui, history, repo)
    req = Path(selected['file'])
    checksum = digest(req)
    nexus = infer_nexus(repo, home)
    cli = find_cli(home)
    if cli is None:
        raise Blocked('GigaCode CLI не найден. Нужна корпоративная установка; скачивание не выполняется.')
    state = new_state_dir(root)
    args = argparse.Namespace(repo=str(repo), packet=selected['packet'], requirements_file=req.name,
                              requirements_sha256=checksum, state_dir=str(state),
                              protected_root=[str(p) for p in protected if p != repo],
                              commit_prefix='SDD PoC', jobs=8, cli=cli, session_timeout=3600,
                              history_file=history, history_user_selected=bool(history),
                              history_project_mapping_confirmed=history_mapping,
                              history_expected_sha256=history_sha256, **nexus)
    ui.confirm('Начать работу с выбранными данными?\nПроект: ' + str(repo)
               + '\nПакет: ' + selected['label'] + '\nSHA256: ' + checksum
               + ('\nСправочный журнал: ' + history if history else '\nПрежний журнал не используется.')
               + '\nРабочая копия будет создана автоматически. Исходный PoC и сервисы сохраняются.'
               + '\nОтчёт о процессах: ' + str(report_path))
    if dry_run:
        print('Выбор проверен; рабочая копия и сессии не созданы.')
        return 0
    try:
        config = initialize(args)
    except WorkspaceError:
        raise Blocked('Не удалось сохранить исходный снимок с действующими правилами Git. '
                      'Возможная причина: hook требует связь с задачей или настройку репозитория. '
                      'Исходный проект не изменён. Сохранённая рабочая копия: ' + str(state / 'work/main')
                      + '. Не обходите hook; его вывод может содержать секреты и здесь не показан.') from None
    save(root / 'last-run.json', {'config': config})
    runner = Runner(config)
    with runner.locked():
        runner.start()
    print('Состояние: ' + runner.state['status'] + '\nРезультат: ' + str(runner.main))
    return 130 if runner.stop.is_set() else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnose', action='store_true', help='Read-only process/service inventory; no GUI or agent launch')
    parser.add_argument('--repo', help='Optional exact source root for diagnosis/advanced use')
    parser.add_argument('--dry-run', action='store_true', help='Select and validate without initializing or starting agents')
    args = parser.parse_args(argv)
    try:
        return launch(diagnose_only=args.diagnose, repo_arg=args.repo, dry_run=args.dry_run)
    except (Blocked, OSError, ValueError, RuntimeError) as exc:
        message = 'Запуск остановлен. ' + str(exc)
        print(message)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
