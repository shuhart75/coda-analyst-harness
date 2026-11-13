#!/usr/bin/env python3
"""SberTrack analysis-task traceability for commit messages."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from functools import cache
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


def validate_binding(value: dict, expected_kind=None) -> dict:
    from commit_tasks import validate_task
    return validate_task(value, expected_kind)


def delivery_analysis_task(project: Path, delivery: str) -> dict:
    from project_layout import feature_root
    path = feature_root(project, delivery) / BINDING_FILE
    try:
        return validate_binding(json.loads(path.read_text(encoding='utf-8')), 'delivery')
    except (OSError, ValueError) as exc:
        raise ValueError(f'Разреши задачу [SA][PR] в эпике поставки {delivery}. До разрешения задачи коммит запрещён ({path}).') from exc


def delivery_message(project: Path, deliveries, description: str) -> str:
    keys = sorted({delivery_analysis_task(project, key)['key'] for key in deliveries})
    message = ', '.join(keys) + ': ' + description
    require_valid_commit_message(message, keys)
    return message


def keys_for_paths(paths, read) -> set[str]:
    """Resolve delivery and quarterly planning ownership from one snapshot."""
    # Scope to this validation only, including missing files. Never reuse reads
    # across changes to the index, worktree or receiver decisions.
    read = cache(read)
    paths = list(paths)
    index_text = read('delivery-index.json')
    index = json.loads(index_text) if index_text else None
    entries = index['deliveries'] if index else {}
    roots = {entry['path']: entry for entry in entries.values()}
    owners, quarters = set(), set()
    work_task_keys = set()
    planning_raw = read(PLANNING_BINDINGS)
    planning = json.loads(planning_raw) if planning_raw else {}
    if not isinstance(planning, dict) or (planning and (planning.get('schema_version') != 1
                     or not isinstance(planning.get('quarters'), dict)
                     or not isinstance(planning.get('shared_paths', {}), dict)
                     or not isinstance(planning.get('epics', {}), dict))):
        raise ValueError('Исправь реестр задач квартального планирования: ' + PLANNING_BINDINGS)
    associations = planning.get('quarters', {})
    if any(not isinstance(value, dict) for value in [*associations.values(), *planning.get('epics', {}).values()]):
        raise ValueError('Некорректные связи задач или эпиков квартального планирования')
    epic_owners = {}
    for quarter in set(associations) | set(planning.get('epics', {})):
        epic = planning.get('epics', {}).get(quarter, {}).get('key') or associations.get(quarter, {}).get('epic')
        if epic:
            if epic in epic_owners and epic_owners[epic] != quarter:
                raise ValueError('У каждого квартала должен быть свой эпик планирования')
            epic_owners[epic] = quarter
    shared = planning.get('shared_paths', {})
    for path in paths:
        parts = Path(path).parts
        if path == PLANNING_BINDINGS:
            quarters.update(associations)
            quarters.update(shared.values())
            quarters.update(planning.get('epics', {}))
            continue
        matched = next((entry for root, entry in roots.items()
                        if path == root or path.startswith(root + '/')), None)
        legacy_owner = '/'.join(parts[:2]) if len(parts) >= 3 and parts[0] == 'features' and not index else None
        relative = path[len(matched['path']) + 1:] if matched else (path[len(legacy_owner) + 1:] if legacy_owner else '')
        legacy_planning = len(parts) >= 4 and parts[0] == 'features' and parts[2] == 'planning'
        quarter_path = (len(parts) >= 3 and parts[0] in {'quarters', 'planning'}
                        and QUARTER_RE.fullmatch(parts[1]) and not matched
                        and not (len(parts) >= 6 and parts[2] == 'features' and parts[4] == 'deliveries'))
        is_planning = (quarter_path or path.startswith('planning/')
                       or re.match(r'(?:slices/[^/]+/)?(?:planning/|execution/|execution-context\.md$)', relative)
                       or legacy_planning)
        if is_planning:
            named = QUARTER_RE.findall(Path(path).name)
            quarter = (parts[1] if quarter_path else
                       (named[0] if len(set(named)) == 1 else (matched.get('quarter') if matched else None)))
            if quarter is None:
                named = QUARTER_RE.findall(Path(path).name)
                quarter = named[0] if len(set(named)) == 1 else shared.get(path)
            if not isinstance(quarter, str) or not QUARTER_RE.fullmatch(quarter):
                raise ValueError(f'К какому кварталу относится планирование в {path}? '
                                 f'До разрешения задачи коммит запрещён; сохрани квартал в {PLANNING_BINDINGS}/shared_paths.')
            quarters.add(quarter)
            continue
        from commit_tasks import work_keys, require_owner_epic
        work_kind = ('baseline' if path.startswith(('baseline/', 'releases/')) or relative.startswith('baseline/')
                     else 'context' if path.startswith('context/') or relative.startswith('context/') else None)
        explicit = work_keys(path, read, work_kind or ('delivery' if matched or legacy_owner else None),
                             'delivery' if matched or legacy_owner else None)
        if explicit is not None:
            work_task_keys.update(explicit)
            continue
        if work_kind:
            owner_root = matched['path'] if matched else legacy_owner
            default_path = ((owner_root + '/' + work_kind) if owner_root else work_kind) + '/analysis-task.json'
            raw = read(default_path)
            if not raw:
                raise ValueError('Укажи поставку (поставки) или квартал для ' + path +
                                 '; найди или создай задачу [' + work_kind.upper() + '] в эпике владельца по core/commit-task-resolution.md')
            binding = validate_binding(json.loads(raw), work_kind)
            if owner_root and binding['owner']['type'] != 'delivery':
                raise ValueError('Работа внутри поставки должна использовать эпик поставки, не квартальный')
            require_owner_epic(binding, read, owner_root)
            work_task_keys.add(binding['key'])
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
    keys = set(work_task_keys)
    for quarter in sorted(quarters):
        if not isinstance(quarter, str) or not QUARTER_RE.fullmatch(quarter):
            raise ValueError('Некорректный квартал в ' + PLANNING_BINDINGS)
        try:
            binding = validate_binding(associations.get(quarter), 'planning')
            if binding['owner']['id'] != quarter:
                raise ValueError('Задача другого квартала')
            epic = planning.get('epics', {}).get(quarter)
            if epic and binding['epic'] != epic.get('key'):
                raise ValueError('Задача относится к другому эпику квартала')
        except ValueError as exc:
            raise ValueError(f'Разреши задачу [PLANNING_QUARTER] в эпике планирования квартала {quarter}. '
                             f'Нужна одна задача на весь квартал. До разрешения задачи коммит запрещён ({PLANNING_BINDINGS}).') from exc
        keys.add(binding['key'])
    legacy_packets = False
    for owner in sorted(owners):
        raw = read(owner)
        if raw is None:
            raise ValueError(f'Разреши задачу [SA][PR] в эпике поставки {owner.rsplit("/", 1)[0]}? '
                             f'До разрешения задачи коммит запрещён ({owner}).')
        value = json.loads(raw)
        if owner.endswith('/manifest.json') and ('analysis_task' not in value
                or (isinstance(value.get('analysis_task'), dict) and value['analysis_task'].get('schema_version') == 1)):
            # Immutable historical packages are never rewritten. The registered
            # publisher/cleanup checks the current delivery binding before Git.
            legacy_packets = True
            continue
        binding = value.get('analysis_task') if owner.endswith('/manifest.json') else value
        try:
            keys.add(validate_binding(binding, 'delivery')['key'])
        except ValueError as exc:
            raise ValueError(f'Разреши задачу [SA][PR] в эпике поставки {owner.rsplit("/", 1)[0]}? '
                             'Выполни поиск/создание по core/commit-task-resolution.md перед коммитом.') from exc
    return keys


def require_preserved_planning_bindings(current, previous) -> None:
    if previous is None:
        return
    old_data = json.loads(previous)
    new_data = json.loads(current) if current else {}
    old = set(old_data.get('quarters', {})) | set(old_data.get('epics', {}))
    new = set(new_data.get('quarters', {})) | set(new_data.get('epics', {}))
    missing = old - new
    removed_epics = set(old_data.get('epics', {})) - set(new_data.get('epics', {}))
    missing |= removed_epics
    if missing:
        quarter = sorted(missing)[0]
        raise ValueError(f'Разреши задачу [PLANNING_QUARTER] в эпике планирования квартала {quarter}. '
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
