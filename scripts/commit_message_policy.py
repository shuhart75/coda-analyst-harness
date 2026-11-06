#!/usr/bin/env python3
"""SberTrack analysis-task traceability for commit messages."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TRACKER_IDENTIFIER_RE = re.compile(r'(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]*-[1-9][0-9]*(?![A-Za-z0-9_])')
HOOK_MARKER = 'analyst-harness-commit-message-policy:v1'
BINDING_FILE = 'analysis-task.json'
PLANNING_BINDINGS = 'planning/task-bindings.json'
QUARTER_RE = re.compile(r'\d{4}-Q[1-4]')
ERROR_MESSAGE = 'Сообщение коммита отклонено: укажи ключ задачи SberTrack на аналитику в заголовке вместе со смысловым описанием изменения.'


def has_tracker_identifier(message: str) -> bool:
    return TRACKER_IDENTIFIER_RE.search(message) is not None


def require_valid_commit_message(message: str, required_keys=()) -> None:
    title = message.split('\n', 1)[0]
    keys = set(TRACKER_IDENTIFIER_RE.findall(title))
    if not keys or not re.search(r'[A-Za-zА-Яа-яЁё]', TRACKER_IDENTIFIER_RE.sub('', title)):
        raise ValueError(ERROR_MESSAGE)
    missing = set(required_keys) - keys
    if missing:
        raise ValueError('Сообщение коммита отклонено: в заголовке отсутствуют обязательные задачи: ' + ', '.join(sorted(missing)))


def validate_binding(value: dict) -> dict:
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('provider') != 'sbertrek' or value.get('role') != 'AN'
            or not isinstance(value.get('key'), str)
            or not TRACKER_IDENTIFIER_RE.fullmatch(value['key'])
            or not isinstance(value.get('confirmation'), str) or not value['confirmation'].strip()):
        raise ValueError('Требуется подтверждённая связь поставки с задачей SberTrack на аналитику: analysis-task.json')
    return value


def delivery_analysis_task(project: Path, delivery: str) -> dict:
    from project_layout import feature_root
    path = feature_root(project, delivery) / BINDING_FILE
    try:
        return validate_binding(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Какую задачу SberTrack на аналитику связать с поставкой {delivery}? До ответа коммит запрещён ({path}).') from exc


def delivery_message(project: Path, deliveries, description: str) -> str:
    keys = sorted({delivery_analysis_task(project, key)['key'] for key in deliveries})
    message = ', '.join(keys) + ': ' + description
    require_valid_commit_message(message, keys)
    return message


def keys_for_paths(paths, read) -> set[str]:
    """Resolve delivery and quarterly planning ownership from one snapshot."""
    paths = list(paths)
    index_text = read('delivery-index.json')
    index = json.loads(index_text) if index_text else None
    entries = index['deliveries'] if index else {}
    roots = {entry['path']: entry for entry in entries.values()}
    owners, quarters = set(), set()
    planning_raw = read(PLANNING_BINDINGS)
    planning = json.loads(planning_raw) if planning_raw else {}
    if not isinstance(planning, dict) or (planning and (planning.get('schema_version') != 1
                     or not isinstance(planning.get('quarters'), dict)
                     or not isinstance(planning.get('shared_paths', {}), dict))):
        raise ValueError('Исправь реестр задач квартального планирования: ' + PLANNING_BINDINGS)
    associations = planning.get('quarters', {})
    shared = planning.get('shared_paths', {})
    for path in paths:
        parts = Path(path).parts
        if path == PLANNING_BINDINGS:
            quarters.update(associations)
            quarters.update(shared.values())
            continue
        matched = next((entry for root, entry in roots.items()
                        if path == root or path.startswith(root + '/')), None)
        relative = path[len(matched['path']) + 1:] if matched else ''
        legacy_planning = len(parts) >= 4 and parts[0] == 'features' and parts[2] == 'planning'
        quarter_path = (len(parts) >= 3 and parts[0] in {'quarters', 'planning'}
                        and QUARTER_RE.fullmatch(parts[1]) and not matched
                        and not (len(parts) >= 6 and parts[2] == 'features' and parts[4] == 'deliveries'))
        is_planning = (quarter_path or path.startswith('planning/')
                       or relative.startswith('planning/') or legacy_planning)
        if is_planning:
            named = QUARTER_RE.findall(Path(path).name)
            quarter = (parts[1] if quarter_path else
                       (named[0] if len(set(named)) == 1 else (matched.get('quarter') if matched else None)))
            if quarter is None:
                named = QUARTER_RE.findall(Path(path).name)
                quarter = named[0] if len(set(named)) == 1 else shared.get(path)
            if not isinstance(quarter, str) or not QUARTER_RE.fullmatch(quarter):
                raise ValueError(f'К какому кварталу относится планирование в {path}? '
                                 f'До ответа коммит запрещён; сохрани квартал в {PLANNING_BINDINGS}/shared_paths.')
            quarters.add(quarter)
            continue
        if index:
            for root in roots:
                if path == 'delivery-index.json' or path == root or path.startswith(root + '/'):
                    owners.add(root + '/' + BINDING_FILE)
        elif path.startswith('features/') and len(parts) >= 3:
            owners.add('/'.join(parts[:2]) + '/' + BINDING_FILE)
        if len(parts) >= 7 and parts[0] == 'quarters' and parts[2] == 'features' and parts[4] == 'deliveries':
            owners.add('/'.join(parts[:6]) + '/' + BINDING_FILE)
        if len(parts) >= 3 and parts[0] == 'requirements-exchange':
            owners.add('/'.join(parts[:2]) + '/manifest.json')
    keys = set()
    for quarter in sorted(quarters):
        if not isinstance(quarter, str) or not QUARTER_RE.fullmatch(quarter):
            raise ValueError('Некорректный квартал в ' + PLANNING_BINDINGS)
        try:
            binding = validate_binding(associations.get(quarter))
        except ValueError as exc:
            raise ValueError(f'Какую задачу SberTrack связать с планированием квартала {quarter}? '
                             f'Нужна одна задача на весь квартал. До ответа коммит запрещён ({PLANNING_BINDINGS}).') from exc
        keys.add(binding['key'])
    for owner in sorted(owners):
        raw = read(owner)
        if raw is None:
            raise ValueError(f'Какую задачу SberTrack на аналитику связать с поставкой {owner.rsplit("/", 1)[0]}? '
                             f'До ответа коммит запрещён ({owner}).')
        value = json.loads(raw)
        if owner.endswith('/manifest.json') and 'analysis_task' not in value:
            # Immutable legacy packets predate the binding; publisher checks the current delivery.
            continue
        binding = value.get('analysis_task') if owner.endswith('/manifest.json') else value
        try:
            keys.add(validate_binding(binding)['key'])
        except ValueError as exc:
            raise ValueError(f'Какую задачу SberTrack на аналитику связать с поставкой {owner.rsplit("/", 1)[0]}? '
                             'Подтверди корректную связь перед коммитом.') from exc
    return keys


def require_preserved_planning_bindings(current, previous) -> None:
    if previous is None:
        return
    old = json.loads(previous).get('quarters', {})
    new = json.loads(current).get('quarters', {}) if current else {}
    missing = set(old) - set(new)
    if missing:
        quarter = sorted(missing)[0]
        raise ValueError(f'Какую задачу SberTrack связать с планированием квартала {quarter}? '
                         'Нельзя удалить прежнюю связь без замены; до ответа коммит запрещён.')


def working_keys(project: Path, paths) -> set[str]:
    paths = list(paths)
    def read(path):
        from project_layout import safe_path
        candidate = safe_path(project, path)
        if candidate.is_file():
            return candidate.read_text(encoding='utf-8')
        if not candidate.parent.exists():
            return git_output(project, 'show', 'HEAD:' + path)
        return None
    if PLANNING_BINDINGS in paths:
        require_preserved_planning_bindings(read(PLANNING_BINDINGS),
                                           git_output(project, 'show', 'HEAD:' + PLANNING_BINDINGS))
    return keys_for_paths(paths, read)


def git_output(repository: Path, *args) -> str | None:
    result = subprocess.run(['git', '-C', str(repository), *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def staged_keys(repository: Path) -> set[str]:
    paths = git_output(repository, 'diff', '--cached', '--name-only', '--no-renames', '-z')
    if paths is None:
        raise ValueError('Не удалось прочитать индекс для проверки задач на аналитику')
    deleted = set((git_output(repository, 'diff', '--cached', '--name-only', '--diff-filter=D', '-z') or '').split('\0'))
    def read(path):
        value = git_output(repository, 'show', ':' + path)
        # A deleted package retains its archived binding, never an unstaged substitute.
        if value is None and path in deleted:
            if path == PLANNING_BINDINGS:
                raise ValueError('Нельзя удалить реестр задач квартального планирования перед коммитом')
            if path.endswith('/' + BINDING_FILE) or path.endswith('/manifest.json'):
                remaining = git_output(repository, 'ls-files', '-z', '--', path.rsplit('/', 1)[0] + '/')
                if remaining:
                    raise ValueError('Нельзя удалить связь с задачей, сохранив материалы поставки: ' + path)
            return git_output(repository, 'show', 'HEAD:' + path)
        return value
    if PLANNING_BINDINGS in paths.split('\0'):
        require_preserved_planning_bindings(read(PLANNING_BINDINGS),
                                           git_output(repository, 'show', 'HEAD:' + PLANNING_BINDINGS))
    return keys_for_paths(filter(None, paths.split('\0')), read)


def validate_file(path: Path, repository: Path | None = None) -> None:
    try:
        message = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise ValueError(f'Не удалось прочитать сообщение коммита: {exc}') from exc
    if repository is not None and is_harness_repository(repository):
        if not message.split('\n', 1)[0].strip():
            raise ValueError('Заголовок коммита обвязки не должен быть пустым')
        return
    required = staged_keys(repository) if repository is not None else ()
    require_valid_commit_message(message, required)


def is_harness_repository(repository: Path) -> bool:
    """Exempt only this harness Git repository, including its linked worktrees."""
    harness = Path(__file__).resolve().parents[1]
    expected = git_output(harness, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    actual = git_output(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return bool(expected and actual and Path(expected.strip()).resolve() == Path(actual.strip()).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description='Проверяет задачи SberTrack на аналитику в заголовках коммитов')
    parser.add_argument('message_file', type=Path)
    parser.add_argument('--repository', type=Path)
    args = parser.parse_args()
    try:
        repository = args.repository
        if repository is None and git_output(Path.cwd(), 'rev-parse', '--show-toplevel') is not None:
            repository = Path.cwd()
        validate_file(args.message_file, repository)
    except (ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
