#!/usr/bin/env python3
"""Local continuation for MCP commit-task resolution; no tracker transport."""
import argparse
import fcntl
from datetime import date
import json
from pathlib import Path
import subprocess

import commit_tasks as tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    quarter = commands.add_parser('quarter-epic')
    quarter.add_argument('--project', type=Path, required=True)
    quarter.add_argument('--quarter', required=True)
    quarter.add_argument('--key')
    quarter.add_argument('--confirmation')
    quarter.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    for name in ('request', 'search-result', 'begin-create', 'created-result', 'select', 'confirm-create', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--state', type=Path, required=True)
        if name == 'request':
            command.add_argument('--owner-type', choices=('delivery', 'quarter'), required=True)
            command.add_argument('--owner-id', required=True)
            command.add_argument('--kind', choices=tuple(tasks.PREFIXES), required=True)
            command.add_argument('--title', required=True)
            command.add_argument('--epic', required=True)
            command.add_argument('--confirmation', required=True)
        if name in ('search-result', 'created-result'):
            command.add_argument('--response', type=Path, required=True)
            command.add_argument('--pointer', default='')
            command.add_argument('--fields', type=Path, required=True,
                                 help='JSON object mapping normalized names to actual JSON Pointers')
            command.add_argument('--step', required=True)
        if name == 'search-result':
            command.add_argument('--complete', action='store_true')
            command.add_argument('--closed-value', action='append', required=True)
        if name == 'begin-create':
            command.add_argument('--tool', required=True)
            command.add_argument('--link-separately', action='store_true')
            command.add_argument('--arguments', type=Path, required=True)
            command.add_argument('--fields', type=Path, required=True)
            command.add_argument('--step', required=True)
        if name == 'confirm-create':
            command.add_argument('--confirmation', required=True)
        if name == 'select':
            command.add_argument('--key', required=True)
            command.add_argument('--confirmation', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'quarter-epic':
            if not tasks.QUARTER.fullmatch(args.quarter):
                raise ValueError('Некорректный квартал')
            from project_layout import safe_path
            path = safe_path(args.project, 'planning/task-bindings.json')
            if args.key:
                git_path = subprocess.run(['git', '-C', str(args.project), 'rev-parse', '--git-path', 'quarter-epics.lock'], capture_output=True, text=True)
                if git_path.returncode:
                    raise ValueError('Для регистрации эпика нужна рабочая Git-ветка')
                lock_path = Path(git_path.stdout.strip())
                if not lock_path.is_absolute():
                    lock_path = args.project / lock_path
                quarter_lock = lock_path.open('a')
                fcntl.flock(quarter_lock, fcntl.LOCK_EX)
            data = json.loads(path.read_text()) if path.exists() else dict(schema_version=1, quarters={}, shared_paths={})
            if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('quarters'), dict):
                raise ValueError('Некорректный реестр квартальных связей')
            epics = data.setdefault('epics', {})
            if not isinstance(epics, dict) or any(not isinstance(value, dict) for value in epics.values()):
                raise ValueError('Некорректный реестр эпиков кварталов')
            if args.key:
                if not tasks.KEY.fullmatch(args.key) or not args.confirmation or not args.confirmation.strip():
                    raise ValueError('Сохрани точный ключ эпика и цитату аналитика')
                branch = subprocess.run(['git', '-C', str(args.project), 'branch', '--show-current'], capture_output=True, text=True)
                if branch.returncode or branch.stdout.strip() in ('', 'main', 'master'):
                    raise ValueError('Связь квартала сохраняется в рабочей ветке, не в main')
                if any(q != args.quarter and value.get('key') == args.key for q, value in epics.items()):
                    raise ValueError('У каждого квартала должен быть свой эпик планирования')
                previous = epics.get(args.quarter, {}).get('key')
                bound = data['quarters'].get(args.quarter, {}).get('epic')
                if any(key and key != args.key for key in (previous, bound)):
                    raise ValueError('Смена эпика требует отдельного решения о переносе уже связанных задач')
                epics[args.quarter] = dict(provider='sbertrek', key=args.key, confirmation=args.confirmation)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                temporary.replace(path)
            current = f'{args.as_of.year}-Q{(args.as_of.month - 1) // 3 + 1}'
            epic = epics.get(args.quarter)
            if epic:
                if (not isinstance(epic, dict) or epic.get('provider') != 'sbertrek'
                        or not tasks.KEY.fullmatch(str(epic.get('key', '')))
                        or not isinstance(epic.get('confirmation'), str) or not epic['confirmation'].strip()):
                    raise ValueError('Нужен подтверждённый эпик квартала')
                result = dict(status='ready', quarter=args.quarter, epic=epic)
            elif args.quarter < current:
                result = dict(status='historical-quarter', quarter=args.quarter,
                              message='Историю не мигрируем автоматически; новая работа требует явного выбора владельца и эпика')
            else:
                result = dict(status='analyst-epic-required', quarter=args.quarter,
                              next_question=f'Какой эпик SberTrack связан с планированием квартала {args.quarter}?')
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        args.state.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = args.state.with_suffix('.lock').open('a')
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        evidence = None
        if args.command == 'request':
            fresh = tasks.request(args.owner_type, args.owner_id, args.kind, args.title, args.epic, args.confirmation)
            if args.state.exists():
                state = json.loads(args.state.read_text())
                if state['spec'] != fresh['spec']:
                    raise ValueError('Незавершённое или сохранённое решение относится к другой работе; не перезаписывай его')
            else:
                state = fresh
        else:
            state = json.loads(args.state.read_text())
        if args.command in ('search-result', 'created-result'):
            raw = json.loads(args.response.read_text())
            fields = json.loads(args.fields.read_text())
            if args.command == 'search-result':
                tasks.ingest_search(state, raw, items_pointer=args.pointer, fields=fields,
                                    closed_values=args.closed_value, complete=args.complete, step=args.step)
            else:
                tasks.ingest_created(state, raw, item_pointer=args.pointer, fields=fields, step=args.step)
            evidence = args.response
        elif args.command == 'begin-create':
            tasks.begin_create(state, args.tool, json.loads(args.arguments.read_text()), args.step, json.loads(args.fields.read_text()), args.link_separately)
        elif args.command == 'confirm-create':
            tasks.confirm_create(state, args.confirmation)
        elif args.command == 'select':
            tasks.select(state, args.key, args.confirmation)
        if args.command != 'status':
            tasks.save_state(args.state, state, evidence)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(json.dumps({'status': 'blocked', 'message': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
