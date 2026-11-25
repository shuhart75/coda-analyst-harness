"""Readable decisions; machine evidence stays in the durable mailbox file."""
from pathlib import Path
import re
import shlex


def brief(value, limit=700):
    """Keep complete prose only. Never cut a sentence or print a nested object."""
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split())
    if len(text) <= limit:
        return text
    sentences = re.split(r'(?<=[.!?])\s+(?=[А-ЯA-Z])', text)
    out = []
    for sentence in sentences:
        if len(' '.join(out + [sentence])) > limit:
            break
        out.append(sentence)
    return ' '.join(out)


def scope_reason(reason):
    if reason.startswith('No accepted predecessor owns'):
        return 'Файл не закреплён за принятой предшествующей задачей в сохранённом плане.'
    if reason.startswith('Repair overlaps an unfinished or independent owner:'):
        return 'Файл закреплён за другой независимой или незавершённой задачей: ' + reason.split(': ', 1)[1]
    if reason.startswith(('Repair file differs', 'Repair target changed')):
        return 'Исходная версия файла изменилась; сначала нужно сверить сохранённую работу.'
    if reason.startswith('Repair path is missing or ambiguous'):
        return 'Указанный файл не найден однозначно; нужно уточнить путь.'
    return 'Контроллер не смог безопасно подготовить разрешение. Причина сохранена в файле подробностей.'


def render_question(root, question):
    lines = ['\nЗадача: ' + question['task']]
    action = question.get('action') or {}
    details = [r.get('details') for r in question.get('requests', []) if r.get('details')]
    if question.get('manual_check'):
        check = question['manual_check']
        lines.extend([question['question'], 'Откройте в браузере: ' + check['url'],
                      'Проверяемая сборка: ' + check['artifact'],
                      'Требования: ' + ', '.join(check['requirements']),
                      'Перед проверкой: ' + check['preconditions']])
        for index, step in enumerate(check['steps'], 1):
            lines.append(str(index) + '. ' + step['action'])
            lines.append('   Ожидаемый результат: ' + step['expected'])
        lines.append('Совпали ли все результаты? При расхождении укажите номер шага и что произошло.')
        lines.append('Если пока не проверяли, выберите «Ответить позже». Это не подтверждает проверку.')
    elif action.get('kind') == 'scope-repair-v1':
        lines.append('Разрешить исправить следующие файлы за пределами текущей задачи?')
        for item in action['files']:
            lines.append('Файл: ' + item['path'])
            lines.append('В плане: ' + ('ранее принятая задача ' + ', '.join(item['owners'])
                                       if item['owners'] else 'не закреплён ни за одной задачей.'))
        reason = next((brief(d.get('reason')) for d in details if isinstance(d, dict) and brief(d.get('reason'))), '')
        if reason:
            lines.append('Причина по отчёту модели: ' + reason)
        lines.append('При согласии: модель исправит только эти файлы дополнительно к своей задаче; затем пройдут проверки и независимое ревью.')
        lines.append('При отказе: эти файлы останутся вне разрешённой области.')
    elif question['topic'].startswith('scope-unavailable-'):
        data = next((d for d in details if isinstance(d, dict) and isinstance(d.get('model_request'), dict)), {})
        request = data.get('model_request', {})
        proposed = request.get('question') or request.get('request') or request.get('reason')
        lines.append('Модель просит: ' + (brief(proposed) or 'разрешение на исправление файла за пределами своей задачи.'))
        reason = brief(request.get('diagnosis'))
        if reason:
            lines.append('Причина по отчёту модели: ' + reason)
        lines.append('Почему разрешение пока недоступно: ' + scope_reason(str(data.get('reason', ''))))
        lines.append('Ответ «да» не расширит область записи. Можно отложить ответ пустой строкой или сообщить уточнение.')
    else:
        lines.append(question['question'])
        seen = set()
        for detail in details:
            if question['topic'].startswith('service:') and isinstance(detail, dict):
                # These fields define what the user is authorizing, so retain them.
                lines.extend(['Назначение: ' + str(detail.get('purpose', '')),
                    'Команда сервиса: ' + shlex.join(detail.get('command', [])),
                    'Рабочий каталог: ' + str(detail.get('cwd', '')),
                    'Порты: ' + ', '.join(map(str, detail.get('ports', [])))])
                continue
            values = detail if isinstance(detail, list) else [detail]
            for value in values:
                if isinstance(value, dict):
                    value = value.get('summary') or value.get('reason') or value.get('message')
                text = brief(value)
                if text and text not in seen and len(seen) < 3:
                    lines.append('Контекст: ' + text)
                    seen.add(text)
    lines.append('Подробности: ' + str(Path(root).resolve()/'operator-questions'/(question['id'] + '.json')))
    return '\n'.join(lines)
