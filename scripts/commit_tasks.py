"""Commit-task identities and guarded MCP search/create continuation.

This module never calls a tracker or guesses an MCP endpoint. The main agent
records complete responses from the currently available tools.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import uuid

PREFIXES = {'delivery': '[SA][PR]', 'planning': '[PLANNING_QUARTER]',
            'baseline': '[BASELINE]', 'context': '[CONTEXT]'}
KEY = re.compile(r'[A-Z][A-Z0-9_]*-[1-9][0-9]*')
QUARTER = re.compile(r'\d{4}-Q[1-4]')
WORK_BINDINGS = 'work-task-bindings.json'


def expected_title(kind, title):
    if kind not in PREFIXES or not isinstance(title, str) or not title.strip() or any(char in title for char in '\r\n'):
        raise ValueError('Укажи вид работы и название владельца')
    if kind == 'planning' and not QUARTER.fullmatch(title):
        raise ValueError('Название квартального планирования должно иметь вид YYYY-QN')
    return PREFIXES[kind] + ' ' + title


def validate_task(value, expected_kind=None):
    if (not isinstance(value, dict) or value.get('schema_version') != 2
            or value.get('provider') != 'sbertrek' or value.get('suit') != 'task'
            or value.get('kind') not in PREFIXES
            or not isinstance(value.get('key'), str) or not KEY.fullmatch(value['key'])
            or not isinstance(value.get('epic'), str) or not KEY.fullmatch(value['epic'])
            or not isinstance(value.get('confirmation'), str) or not value['confirmation'].strip()):
        raise ValueError('Нужна проверенная задача suit="task" нужного вида в подтверждённом эпике; выполни core/commit-task-resolution.md')
    if expected_kind and value['kind'] != expected_kind:
        raise ValueError('Задача относится к другому виду работы: ' + expected_kind)
    summary = value.get('summary', '')
    prefix = PREFIXES[value['kind']] + ' '
    if not isinstance(summary, str) or any(char in summary for char in '\r\n') or not summary.startswith(prefix) or not summary[len(prefix):].strip():
        raise ValueError('Название задачи не соответствует префиксу вида работы')
    if value['kind'] == 'planning' and not QUARTER.fullmatch(summary[len(prefix):]):
        raise ValueError('Задача планирования должна называться [PLANNING_QUARTER] YYYY-QN')
    owner = value.get('owner', {})
    if (not isinstance(owner, dict) or owner.get('type') not in ('delivery', 'quarter')
            or not isinstance(owner.get('id'), str) or not owner['id']):
        raise ValueError('У задачи не указан владелец: поставка или квартал')
    if owner['type'] == 'quarter' and not QUARTER.fullmatch(owner['id']):
        raise ValueError('Некорректный квартал владельца задачи')
    if value['kind'] == 'planning' and (owner['type'] != 'quarter' or summary != expected_title('planning', owner['id'])):
        raise ValueError('Задача планирования относится к другому кварталу')
    if value['kind'] == 'delivery' and owner['type'] != 'delivery':
        raise ValueError('[SA][PR] относится только к поставке')
    return value


def require_owner_epic(task, read, delivery_root=None):
    owner = task['owner']
    expected = None
    if owner['type'] == 'quarter':
        raw = read('planning/task-bindings.json')
        planning = json.loads(raw) if raw else {}
        expected = (planning.get('epics', {}).get(owner['id'], {}).get('key')
                    or planning.get('quarters', {}).get(owner['id'], {}).get('epic'))
    else:
        if delivery_root is None:
            raw = read('delivery-index.json')
            index = json.loads(raw) if raw else None
            delivery_root = (index.get('deliveries', {}).get(owner['id'], {}).get('path')
                             if index else 'features/' + owner['id'])
        raw = read(delivery_root + '/analysis-task.json') if delivery_root else None
        if raw:
            expected = json.loads(raw).get('epic')
    if expected and task['epic'] != expected:
        raise ValueError('Задача должна находиться в том же эпике, что и её владелец')


def work_keys(path, read, expected_kind=None, expected_owner_type=None):
    """Explicit path ownership for shared baseline/context and other work."""
    raw = read(WORK_BINDINGS)
    registry = json.loads(raw) if raw else {}
    if not isinstance(registry, dict) or (registry and (registry.get('schema_version') != 1 or not isinstance(registry.get('paths'), dict)
                     or not isinstance(registry.get('tasks'), dict))):
        raise ValueError('Исправь ' + WORK_BINDINGS)
    mappings = registry.get('paths', {})
    matches = [prefix for prefix in mappings if path == prefix or
               (prefix.endswith('/') and path.startswith(prefix))]
    if path == WORK_BINDINGS:
        ids = list(registry.get('tasks', {}))
    elif matches:
        ids = mappings[max(matches, key=len)]
    else:
        return None
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
        raise ValueError('Укажи задачи всех владельцев для ' + path)
    result = set()
    for task_id in ids:
        task = validate_task(registry.get('tasks', {}).get(task_id), expected_kind)
        require_owner_epic(task, read)
        if expected_owner_type and task['owner']['type'] != expected_owner_type:
            raise ValueError('Работа внутри поставки должна использовать эпик поставки, не квартальный')
        if path.startswith(('baseline/', 'releases/')) and task['kind'] != 'baseline':
            raise ValueError('Для baseline и закрытия релиза нужна задача [BASELINE]')
        if path.startswith('context/') and task['kind'] != 'context':
            raise ValueError('Для контекста нужна задача [CONTEXT]')
        result.add(task['key'])
    return result


def pointer(value, path):
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Пути полей ответа MCP должны быть JSON Pointer')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def request(owner_type, owner_id, kind, title, epic, confirmation):
    if not KEY.fullmatch(epic) or not confirmation.strip():
        raise ValueError('Сначала запроси и сохрани эпик у аналитика')
    spec = dict(schema_version=2, provider='sbertrek', suit='task', kind=kind,
                summary=expected_title(kind, title), epic=epic,
                owner={'type': owner_type, 'id': owner_id}, confirmation=confirmation)
    validate_task({**spec, 'key': 'VALIDATION-1'})
    return dict(schema_version=1, spec=spec, status='search-required',
                step_id=uuid.uuid4().hex, attempts=[], evidence=[])


def ingest_search(state, raw, *, items_pointer, fields, closed_values, complete, step):
    if state['step_id'] != step or state['status'] not in ('search-required', 'create-outcome-unknown'):
        raise ValueError('Ответ не соответствует ожидаемому поиску')
    items = pointer(raw, items_pointer)
    if not isinstance(items, list) or not complete:
        raise ValueError('Нужен полный результат поиска в эпике без усечения и непрочитанных страниц')
    if not closed_values or not isinstance(closed_values, list):
        raise ValueError('Укажи закрытые состояния из реальных метаданных трекера')
    spec = state['spec']
    candidates = {}
    for item in items:
        card = {name: pointer(item, fields[name]) for name in ('key', 'summary', 'suit', 'epic', 'status')}
        if not isinstance(card['status'], str) or not card['status']:
            raise ValueError('Состояние найденной задачи неизвестно')
        if card['epic'] != spec['epic'] or card['suit'] != 'task':
            continue
        if not isinstance(card['summary'], str) or not card['summary'].startswith(PREFIXES[spec['kind']] + ' '):
            continue
        if not isinstance(card['key'], str) or not KEY.fullmatch(card['key']):
            raise ValueError('Некорректный собственный ключ SberTrack')
        card['closed'] = card['status'] in closed_values
        if card['key'] in candidates and candidates[card['key']] != card:
            raise ValueError('Поиск вернул противоречивые карточки одной задачи')
        candidates[card['key']] = card
    previous_unknown = state['status'] == 'create-outcome-unknown'
    state['candidates'] = list(candidates.values())
    exact = [card for card in candidates.values() if card['summary'] == spec['summary']]
    if len(exact) == 1 and not exact[0]['closed']:
        state.update(status='resolved', binding={**spec, 'key': exact[0]['key']})
    elif candidates:
        state.update(status='analyst-selection-required',
                     next_question='Какую из найденных задач связать с этой работой? Закрытую задачу можно использовать только по явному решению.')
    elif previous_unknown:
        state.update(status='creation-reconciliation-required',
                     next_question='Результат создания неизвестен, повторный поиск не нашёл задачу. Проверь результат операции перед повторным созданием.')
    else:
        state['status'] = 'create-ready'
    state['step_id'] = uuid.uuid4().hex
    return state


def select(state, key, confirmation):
    if state['status'] != 'analyst-selection-required' or not confirmation.strip():
        raise ValueError('Выбор задачи требует явного решения аналитика')
    selected = next((item for item in state['candidates'] if item['key'] == key), None)
    if selected is None:
        raise ValueError('Выбирать можно только проверенную задачу из результата поиска')
    # Existing titles can differ after an analyst-approved rename/ownership choice.
    state.update(status='resolved', binding={**state['spec'], 'key': key,
                 'summary': selected['summary'], 'confirmation': confirmation})
    validate_task(state['binding'])
    return state


def confirm_create(state, confirmation):
    if state['status'] not in ('analyst-selection-required', 'creation-reconciliation-required') or not confirmation.strip():
        raise ValueError('Новое создание требует явного решения после разбора найденных задач или результата прошлой попытки')
    state.setdefault('decisions', []).append({'action': 'confirm-create', 'confirmation': confirmation})
    state.update(status='create-ready', step_id=uuid.uuid4().hex)
    return state


def begin_create(state, tool, arguments, step, fields, link_separately=False):
    if state['status'] != 'create-ready' or state['step_id'] != step:
        raise ValueError('Создание допустимо только после полного поиска без подходящей задачи')
    if not tool or not isinstance(arguments, dict):
        raise ValueError('Запиши фактический инструмент MCP и его аргументы')
    for name in (('summary', 'suit') if link_separately else ('summary', 'suit', 'epic')):
        if pointer(arguments, fields[name]) != state['spec'][name]:
            raise ValueError('Аргументы создания не соответствуют согласованной задаче: ' + name)
    attempt = dict(tool=tool, arguments=arguments, fields=fields, link_separately=link_separately, step_id=uuid.uuid4().hex)
    state['attempts'].append(attempt)
    # Persist BEFORE invoking MCP. A crash or timeout must trigger search, not retry.
    state.update(status='create-outcome-unknown', step_id=attempt['step_id'])
    return state


def ingest_created(state, raw, *, item_pointer, fields, step):
    if state['status'] != 'create-outcome-unknown' or state['step_id'] != step:
        raise ValueError('Ответ не соответствует ожидаемому созданию')
    item = pointer(raw, item_pointer)
    card = {name: pointer(item, fields[name]) for name in ('key', 'summary', 'suit', 'epic')}
    spec = state['spec']
    if any(card[name] != spec[name] for name in ('summary', 'suit', 'epic')):
        raise ValueError('Созданная задача не подтверждает нужные название, тип и связь с эпиком; выполни проверочное чтение')
    binding = validate_task({**spec, 'key': card['key']})
    state.update(status='resolved', binding=binding)
    return state


def save_state(path, state, evidence=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if evidence is not None:
        data = Path(evidence).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        archive = path.parent / (path.stem + '-responses') / (digest + '.json')
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        state['evidence'].append({'path': str(archive), 'sha256': digest})
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
