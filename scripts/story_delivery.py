"""SberTrack business fields and developer instructions for a published task.

Names here are semantic fields, not invented tracker custom-field identifiers.
The agent resolves actual fields/options through the current tracker capabilities.
"""
from urllib.parse import quote

import grooming

CONFIGURATION_ITEM = 'ПКАП1200 АС КОДА (CI07642362)'
REQUIRED_FIELDS = {
    'configuration_item': CONFIGURATION_ITEM,
    'architecture_changes': 'Не влияет на архитектуру',
    'load_testing_required': 'Нет',
}


def task_keys(plan, overrides=None):
    """Allow supplied keys even after publication without rewriting the packet."""
    overrides = {} if overrides is None else overrides
    ids = {t['id'] for t in plan['tasks']}
    if not isinstance(overrides, dict) or set(overrides) - ids:
        raise ValueError('Task key mapping contains unknown local task IDs')
    result = {}
    for task in plan['tasks']:
        key = overrides.get(task['id'], task.get('tracker_key'))
        if key is not None:
            if not grooming.KEY.fullmatch(str(key)):
                raise ValueError('Expected a native SberTrack task key')
            if task.get('tracker_key') and key != task['tracker_key']:
                raise ValueError('Supplied task key conflicts with the published binding')
            if key in result.values():
                raise ValueError('Two grooming tasks cannot use the same SberTrack key')
            result[task['id']] = key
    return result


def markers(request_id):
    return (f'[Начало блока передачи grooming/{request_id}]',
            f'[Конец блока передачи grooming/{request_id}]')


def delivery_content(task, delivery, revision, commit, web_url, request_id):
    packet = f'requirements-exchange/{delivery}'
    root = f'{packet}/revisions/{revision:03d}'
    link = lambda path: web_url.rstrip('/') + '?path=' + quote(path, safe='')
    links = {'revision_url': link(root), 'general_requirements_url': link(root + '/requirements.md'),
             'task_folder_url': link(root + '/tasks/' + task['id'])}
    prompt = (
        f"Начни SDD по задаче {task['id']} «{task['summary']}» поставки {delivery}, редакция {revision:03d}.\n"
        f"Источник: {links['revision_url']}\nКоммит публикации documents: {commit}.\n"
        f"Папка входа: {root}. Прочитай общие requirements.md, tasks/grooming.json, "
        f"tasks/{task['id']}/requirements.md, sdd/package.json и относящиеся к задаче материалы.\n"
        f"Изменения SDD этой задачи: {', '.join(task['sdd_changes']) or 'указаны в документе задачи'}.\n"
        "Сначала прочитай инструкции рабочего кодового репозитория и доступных skills SDD. "
        "Для проверки входа используй инструкции DOCUMENTS-WORKFLOW.md и SDD-WORKFLOW.md "
        f"в requirements-exchange/ и manifest.json в {packet}. "
        f"Параметры sdd-receive.py: --packet <локальный documents>/{packet} --revision {revision}; "
        "code-root определи по рабочему репозиторию. Вначале выполни dry-run.\n"
        "После успешной проверки продолжи штатный процесс SDD этой задачи: приём входа, "
        "технический дизайн и шаги реализации с предусмотренными разработческим процессом подтверждениями. "
        "Готовые proposal/spec не генерируй заново. Сохрани границы задачи и зависимости; "
        "общий пакет не поручает реализацию соседних задач. Приём общего пакета не означает "
        "разрешения изменять чужие changes. Не меняй опубликованную редакцию и не создавай дубли Story. "
        "Для этой передачи не требуются receipt/returns от разработки."
    )
    start, end = markers(request_id)
    description = '\n\n'.join([
        start, task['description'], 'Критерий приёмки: ' + task['acceptance'],
        'Все данные для разработки по фиче находятся в этой папке: ' + links['revision_url'],
        'Общие требования по фиче: ' + links['general_requirements_url'],
        'Требования по текущей задаче находятся в этой папке: ' + links['task_folder_url'],
        'Папка для начала работы SDD: ' + links['revision_url'],
        'Промпт для SDD:\n\n' + prompt, end,
    ])
    return {**links, 'sdd_input_url': links['revision_url'], 'sdd_prompt': prompt,
            # Retain the existing API field for callers that link directly to the task document.
            'requirements_url': link(root + '/tasks/' + task['id'] + '/requirements.md'),
            'description': description}


def merge_description(existing, task, previous=None):
    """Replace only our stable block; preserve independently authored text."""
    if task['description'] in existing:
        return existing
    start, end = markers(task['request_id'])
    if start in existing or end in existing:
        if existing.count(start) != 1 or existing.count(end) != 1 or existing.index(start) > existing.index(end):
            raise ValueError('Ambiguous managed description block; preserve and review it')
        begin = existing.index(start)
        finish = existing.index(end) + len(end)
        return existing[:begin] + task['description'] + existing[finish:]
    if previous and previous.get('description') and previous['description'] in existing:
        return existing.replace(previous['description'], task['description'], 1)
    return existing.rstrip() + '\n\n' + task['description'] if existing.strip() else task['description']


def field_matches(name, actual, expected):
    if name == 'configuration_item':
        return actual in (expected, 'CI07642362')
    if name == 'load_testing_required':
        return actual is False or actual == expected
    return actual == expected
