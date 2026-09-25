"""Persistent synthetic agent trial; never operates on configured real analytics."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from test_release_actualization_e2e import FIELDS, ROOT, ReleaseActualizationEndToEndTests


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def create(destination):
    if destination.is_symlink() or destination.exists():
        raise ValueError('Destination must not exist; choose a new local directory')
    parent = destination.parent.resolve(strict=True)
    if subprocess.run(['git', '-C', str(parent), 'rev-parse', '--show-toplevel'],
                      capture_output=True).returncode == 0:
        raise ValueError('Create the lab outside existing Git repositories')
    destination = parent / destination.name
    destination.mkdir()
    fixture = ReleaseActualizationEndToEndTests()
    try:
        fixture.prepare_lab(destination)
        fixture.collect()
        fixture.review('initial')
        dump(fixture.inputs / 'expected.json', fixture.expected)
        immutable = {}
        for directory in (fixture.inputs, fixture.state / 'tracker-runs'):
            for path in directory.rglob('*'):
                if path.is_file():
                    immutable[str(path.relative_to(destination))] = digest(path)
        for path in fixture.state.glob('*-response.json'):
            immutable[str(path.relative_to(destination))] = digest(path)
        immutable[str((fixture.state / 'links.json').relative_to(destination))] = digest(fixture.state / 'links.json')
        mutable = {'features/registry/execution/tasks.md', 'features/registry/execution/qa-groups.json',
                   'planning/2026-Q3/gantt/actual-progress.puml',
                   'planning/2026-Q3/gantt/actual-progress-confluence.puml',
                   'planning/2026-Q3/gantt/includes/actual-progress/FEATURE-registry.puml',
                   'planning/2026-Q3/gantt/includes/actual-progress/FEATURE-other.puml'}
        project_files = fixture.snapshot(fixture.project)
        for relative in project_files:
            if relative not in mutable:
                path = fixture.project / relative
                immutable[str(path.relative_to(destination))] = digest(path)
        expected_rows = {task: dict(row) for task, row in fixture.rows.items()}
        for task in fixture.expected['members']:
            expected_rows[task].update(fixture.expected['development'][
                'finish_only' if task == 'LAB-112/BE' else 'regular'])
        for task, fields in fixture.expected['qa_fields'].items():
            expected_rows[task].update(fields)
        environment = {key: value for key, value in fixture.environment.items()
                       if key.startswith(('GIT_', 'ANALYST_HARNESS_', 'CODA_ANALYST_'))
                       or key in {'HOME', 'XDG_CONFIG_HOME', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE', 'HARNESS_TODAY'}}
        metadata = dict(schema_version=1, root=str(destination), harness=str(ROOT),
                        workspace=str(fixture.workspace), project=str(fixture.project),
                        remote=str(fixture.remote), state=str(fixture.state), run_id=fixture.run_id,
                        head=fixture.initial_head, main=fixture.initial_main,
                        branch=fixture.git(fixture.project, 'branch', '--show-current'),
                        environment=environment, immutable=immutable, rows=expected_rows,
                        groups=fixture.expected['groups'], mutable=sorted(mutable),
                        project_files=sorted(project_files))
        dump(destination / 'lab.json', metadata)
        prompt = f'''# Испытание агента: продолжение актуализации

Работай только в синтетическом стенде {destination}.
HARNESS_ROOT (только чтение): {ROOT}
Локальный workspace: {fixture.workspace}
PROJECT_ROOT: {fixture.project}
Run: {fixture.run_id}. Сбор уже закончен; новый run не создавай.
История и исходный манифест: {fixture.inputs}; начальный review уже записан.

Актуализируй REL-1, фича registry, квартал 2026-Q3 до успешного save-preview.
Прочитай правила execution-update и tracker-release в HARNESS_ROOT.
Команды запускай через:
python3 {ROOT}/tests/release_actualization_lab.py run {destination} TOOL [ARGS...]
TOOL: trackerctl.py, collaboration.py, sync-quarter-gantt.py, workspace.py.
Обёртка задаёт локальные root/state и фиксированное today; --root не переопределяй.
Файлы редактируй только в стенде. MCP, сеть, реальные репозитории запрещены.
Не меняй обвязку, lab.json, исходные ответы, expected.json и старые review.
Не запускай unittest и не используй функции теста для применения результата.

Решения аналитика уже приняты:
- LAB-112 без префикса относится к BE. CREATE с актором не доказывает начало.
- Принять даты разработки из назначений; для LAB-112 начало и оценка неизвестны.
- Полный принятый набор полей: inputs/expected.json, development и qa_fields.
- QA: 12 участников релиза / 7.2 дня и 3 остатка / 1.8 дня, всего 9.
- LAB-201/BE, LAB-202/FE, LAB-203/FE защищены: строки не менять, даже при cancelled в снимке.
- REST меняется только в составе и доле оценки, не в факте.
- Соседняя other: исходники и утверждённые планы не менять; производный прогноз допустим.
- Колонку Completed By не добавлять. Не выдумывать оценки, даты и терминальные коды.

Пройди preflight, новый history-review с подтверждениями, применение, QA-проверку,
actual-only и save-preview. Новые манифесты сохраняй отдельными файлами.
Никаких save, commit, push, merge. При реальном блокере остановись с диагнозом.
В конце укажи абсолютный путь финального review и краткий отчёт: вопросы,
ошибки команд, вмешательства аналитика, что проверено. Не заявляй об ускорении без замера.
'''
        (destination / 'AGENT-TASK.md').write_text(prompt, encoding='utf-8')
        return metadata
    finally:
        fixture.doCleanups()


def load(destination):
    destination = destination.resolve(strict=True)
    metadata = json.loads((destination / 'lab.json').read_text(encoding='utf-8'))
    if metadata['root'] != str(destination) or metadata['harness'] != str(ROOT):
        raise ValueError('Lab was moved or belongs to a different harness')
    for key in ('workspace', 'project', 'state', 'remote'):
        Path(metadata[key]).resolve(strict=True).relative_to(destination)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('GIT_', 'ANALYST_HARNESS_', 'CODA_ANALYST_'))}
    environment.update(metadata['environment'])
    return metadata, environment


def execute(metadata, environment, tool, arguments):
    if tool not in {'trackerctl.py', 'collaboration.py', 'sync-quarter-gantt.py', 'workspace.py'}:
        raise ValueError('Unsupported lab tool')
    if any(arg == '--root' or arg.startswith('--root=') for arg in arguments):
        raise ValueError('Workspace root is fixed by the lab')
    if tool == 'collaboration.py' and (not arguments or arguments[0] not in {
            'status', 'save-preview'}):
        raise ValueError('Only collaboration status/save-preview are allowed in the trial')
    if tool == 'workspace.py' and arguments != ['project-root']:
        raise ValueError('Only workspace project-root is allowed in the trial')
    prefix = ['--root', metadata['workspace']] if tool in {'workspace.py', 'collaboration.py'} else []
    return subprocess.run([sys.executable, str(ROOT / 'scripts' / tool), *prefix, *arguments],
                          cwd=metadata['root'], env=environment, text=True, capture_output=True, timeout=180)


def verify(destination, review):
    metadata, environment = load(destination)
    project = Path(metadata['project'])
    for relative, checksum in metadata['immutable'].items():
        if digest(destination / relative) != checksum:
            raise ValueError(f'Protected file changed: {relative}')
    from tracker_registry import read_registry
    registry = project / 'features/registry/execution/tasks.md'
    tables, _ = read_registry(registry)
    rows = [row for table in tables for row in table]
    if any(set(row) != set(FIELDS) for row in rows):
        raise ValueError('Registry columns changed')
    actual = {row['Task ID']: {field: row.get(field, '') for field in FIELDS} for row in rows}
    if len(rows) != len(actual) or actual != metadata['rows']:
        raise ValueError('Registry fields do not match independent expected values')
    if json.loads(registry.with_name('qa-groups.json').read_text()) != metadata['groups']:
        raise ValueError('QA partition mismatch')
    current_files = set(ReleaseActualizationEndToEndTests().snapshot(project))
    if current_files - set(metadata['project_files']) - set(metadata['mutable']):
        raise ValueError('Unexpected project files')
    def git(repository, *arguments):
        return subprocess.check_output(['git', '-C', str(repository), *arguments],
                                       env=environment, text=True).strip()
    for actual_value, expected in ((git(project, 'rev-parse', 'HEAD'), metadata['head']),
                                   (git(project, 'branch', '--show-current'), metadata['branch']),
                                   (git(metadata['remote'], 'rev-parse', 'main'), metadata['main']),
                                   (git(project, 'diff', '--cached', '--name-only'), '')):
        if actual_value != expected:
            raise ValueError('Git state changed beyond trial scope')
    runs = Path(metadata['state']) / 'tracker-runs'
    if {path.name for path in runs.iterdir() if path.is_dir()} != {metadata['run_id']}:
        raise ValueError('Unexpected tracker run')
    review = review.resolve(strict=True)
    review.relative_to(runs / metadata['run_id'] / 'history')
    before = ReleaseActualizationEndToEndTests().snapshot(project)
    for tool, arguments in (
        ('trackerctl.py', ['qa-application-check', '--project-root', str(project), '--review-file', str(review)]),
        ('collaboration.py', ['save-preview', '--review-file', str(review)]),
    ):
        result = execute(metadata, environment, tool, arguments)
        if result.returncode:
            raise ValueError(result.stdout + result.stderr)
    if before != ReleaseActualizationEndToEndTests().snapshot(project):
        raise ValueError('Verification modified project files')
    return {'status': 'agent-artifacts-verified', 'run_id': metadata['run_id'],
            'conversation_quality': 'requires transcript review', 'generation_replayed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'run', 'verify'))
    parser.add_argument('destination', type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == 'create':
            result = create(args.destination)
            print(json.dumps({key: result[key] for key in ('root', 'workspace', 'project', 'run_id')}, indent=2))
            print('Agent task:', Path(result['root']) / 'AGENT-TASK.md')
        elif args.action == 'verify':
            if len(args.arguments) != 1:
                raise ValueError('verify requires the final review path')
            print(json.dumps(verify(args.destination.resolve(), Path(args.arguments[0])), indent=2))
        else:
            if not args.arguments:
                raise ValueError('run requires a tool name and arguments')
            result = execute(*load(args.destination), args.arguments[0], args.arguments[1:])
            print(result.stdout, end='')
            print(result.stderr, end='', file=sys.stderr)
            return result.returncode
        return 0
    except (ValueError, OSError, AssertionError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
