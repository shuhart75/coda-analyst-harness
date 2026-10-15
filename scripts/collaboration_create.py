"""Create a quarter delivery and its collaboration session, with guarded resume."""
from __future__ import annotations

import json
import re
from pathlib import Path

from project_layout import REGISTRY, safe_path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def create(args, api):
    root = api.root_path(args.root)
    repository, repository_id = api.analytics_repository(root)
    state = api.load_state(root)
    identity = {
        'feature_id': api.validate_slug(args.feature, 'Фича'),
        'delivery_id': api.validate_slug(args.delivery, 'Поставка'),
        'delivery_key': api.validate_slug(args.key, 'Ключ поставки'),
        'quarter': args.quarter,
        'title': args.title,
    }
    if not args.analyst_confirmed:
        raise ValueError('Требуется решение аналитика о фиче, поставке и квартале')
    if not re.fullmatch(r'\d{4}-Q[1-4]', args.quarter):
        raise ValueError('Укажи один квартал YYYY-QN')
    if not args.title.strip() or '\n' in args.title or '\r' in args.title:
        raise ValueError('Название должно быть непустой одной строкой')
    if api.active_merge(repository):
        raise ValueError('Сначала заверши слияние')
    for marker in ('CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        location = api.git(repository, 'rev-parse', '--path-format=absolute', '--git-path', marker)
        if location.returncode or Path(location.stdout.strip()).exists():
            raise ValueError('Сначала заверши текущую Git-операцию')
    work = state.get('active_work')
    if work:
        if work.get('status') != 'creation-pending' or work.get('creation', {}).get('identity') != identity:
            raise ValueError('Уже есть активная работа; creation-pending возобновляется только с теми же аргументами')
        journal = work['creation']
    else:
        api.require_clean(repository)
        if api.current_branch(repository) != api.BRANCH:
            raise ValueError('Новую фичу создавай из принятой main без активной сессии')
        remote = api.fetch_main(repository)
        if api.head(repository) != remote:
            raise ValueError('Локальная main не равна origin/main; сначала синхронизируй принятую main')
        index_path = safe_path(repository, REGISTRY)
        before = index_path.read_text(encoding='utf-8')
        index = json.loads(before)
        if index.get('schema_version') != 1:
            raise ValueError('Нужен квартальный delivery-index схемы 1')
        if args.key in index['deliveries'] or any(d['feature_id'] == args.feature for d in index['deliveries'].values()):
            raise ValueError('Фича или ключ уже существует; используй start для существующей поставки')
        if safe_path(repository, f'features/{args.feature}').exists():
            raise ValueError('Паспорт фичи уже существует')
        relative = f'quarters/{args.quarter}/features/{args.feature}/deliveries/{args.delivery}'
        target = safe_path(repository, relative)
        if target.exists():
            raise ValueError('Каталог поставки уже существует')
        index['deliveries'][args.key] = {
            'feature_id': args.feature, 'delivery_id': args.delivery,
            'quarter': args.quarter, 'path': relative,
        }
        journal = {
            'identity': identity, 'original_branch': api.BRANCH, 'head': remote,
            'files': {
                REGISTRY: {'before': before, 'after': encoded(index)},
                relative + '/README.md': {'before': None, 'after': f'# {args.title}\n\nФича: `{args.feature}`. Поставка: `{args.delivery}`. Квартал: `{args.quarter}`.\n\nЗарегистрировано намерение аналитика. Требования, этап и передача оформляются отдельно.\n'},
            },
        }
        branch = api.next_feature_branch(repository, args.key, state['analyst_id'])
        work = {'feature': args.key, 'branch': branch, 'status': 'creation-pending',
                'started_at': api.utc_now(), 'started_from': remote,
                'origin_main_at_start': remote, 'migration': False, 'creation': journal}
        state['active_work'] = work
        api.write_state(root, state)  # Persist intent before switching or writing project files.
    branch = api.current_branch(repository)
    if branch not in {journal['original_branch'], work['branch']} or api.head(repository) != journal['head']:
        raise ValueError('Ветка или HEAD изменились; сохранённый запуск не применяется')
    if api.git(repository, 'diff', '--cached', '--name-only').stdout.strip():
        raise ValueError('Индекс изменён; сохрани pending и разбери чужие изменения')
    changed = api.changed_paths(repository)
    if changed - set(journal['files']):
        raise ValueError('Появились посторонние изменения; pending сохранён')
    for relative, content in journal['files'].items():
        path = safe_path(repository, relative)
        if path.exists() and not path.is_file():
            raise ValueError('Путь занят не файлом: ' + relative)
        actual = path.read_text(encoding='utf-8') if path.exists() else None
        if actual not in (content['before'], content['after']):
            raise ValueError('Содержимое изменено вне операции: ' + relative)
        if branch == journal['original_branch'] and actual != content['before']:
            raise ValueError('Файлы появились до переключения ветки; pending сохранён')
    if branch != work['branch']:
        result = api.git(repository, 'switch', '--no-track', '-c', work['branch'], journal['head'])
        if result.returncode:
            raise ValueError('Не удалось создать ветку: ' + result.stderr.strip())
    for relative, content in journal['files'].items():
        path = safe_path(repository, relative)
        if path.exists() and path.read_text(encoding='utf-8') == content['after']:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement; an interrupted write cannot leave a partial registry.
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root / '.workspace-state', delete=False) as handle:
            handle.write(content['after'])
            temporary = Path(handle.name)
        try:
            # Both locations are normally on the same filesystem. Cross-device replace
            # fails without modifying the target and leaves the session resumable.
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    work['status'] = 'active'
    work['created_delivery'] = identity
    del work['creation']
    api.write_state(root, state)
    print(encoded({'status': 'feature-created', 'analytics_repository': repository_id,
                   'branch': work['branch'], **identity,
                   'path': index_path_for(repository, args.key)}))
    return 0


def index_path_for(repository, key):
    return json.loads((repository / REGISTRY).read_text())['deliveries'][key]['path']
