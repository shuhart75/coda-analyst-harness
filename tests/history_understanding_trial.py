"""Standalone history interpretation trial. No tracker, Git, or project imports."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


CASES = Path(__file__).parent / 'fixtures/history-understanding/cases.json'
RULES = '''# Правила испытания

Это синтетические карточки, не реальные задачи. developer — разработчик,
tester — тестировщик. Автор события не является назначенным исполнителем.
Разработка начинается при явном назначении разработчику. Первая передача
developer -> tester завершает разработку и начинает QA. Возвраты tester -> developer
считаются доработкой в QA: не переоткрывают разработку и не сдвигают её завершение.
Код done завершает QA; код cancelled означает отмену, не выполнение.
Код development в снимке означает активную разработку. Если это единственное
свидетельство, начало известно лишь как «не позднее времени снимка».
Передача на QA при неизвестном начале также доказывает начало не позднее передачи.
Снимок не доказывает, что QA никогда не начиналось: без свидетельств QA = unknown.
При отмене приоритет имеет cancelled; не выводи начало из текущего назначения.
Создание карточки, created и updated не являются датами начала/завершения работы.
Если известна точная дата, запиши её в *_at, соответствующий *_by оставь null.
*_by — временная граница «не позднее», не имя человека и не точная дата.
Для неизвестных дат используй JSON null. Не додумывай оценки или события.

Для каждой карточки верни id, facts, evidence, explanation.
facts содержит ровно поля:
dev_state, dev_started_at, dev_finished_at, dev_started_by, dev_completed_by,
qa_state, qa_started_at, qa_finished_at, cancelled_at.
Состояния: unknown, not-started, in-progress, completed, cancelled.
Даты: ISO 8601 с часовым поясом либо null; эквивалентные часовые пояса допустимы.
evidence — объект: имя поля -> список дословных цитат из истории этой карточки.
Для каждой ненулевой даты/границы обязательна цитата события или снимка-основания.
Цитата должна включать время и содержательное описание соответствующего события.
explanation — краткое объяснение вывода, особенно неизвестных значений и границ.
Результат: один JSON-объект {"schema_version": 1, "cases": [ ... ]} в answer.json.
'''


def cases():
    return json.loads(CASES.read_text(encoding='utf-8'))


def create(destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError('Каталог уже существует. Выберите новое имя; старый прогон не изменён.')
    destination = destination.parent.resolve(strict=True) / destination.name
    destination.mkdir()
    (destination / 'RULES.md').write_text(RULES, encoding='utf-8')
    inputs = [{'id': case['id'], 'history': case['text']} for case in cases()]
    (destination / 'histories.json').write_text(
        json.dumps(inputs, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (destination / 'AGENT-TASK.md').write_text('''# Разбор текстовой истории

Прочитай RULES.md и histories.json только в этом каталоге. Самостоятельно выведи
факты и запиши answer.json. Не обращайся к MCP, сети, рабочей аналитике, обвязке
или исходникам теста. Не изменяй входные файлы. Можно написать локальный скрипт
для формирования JSON, но смысл событий определи самостоятельно.
Ничего не устанавливай и не создавай tracker-run, репозитории или манифесты review.
Не спрашивай о regex и форматировании: бизнес-правила уже даны в RULES.md.
При реальной неоднозначности явно укажи её в explanation, не выдумывай факт.
В конце сообщи путь answer.json, вопросы и ошибки. Проверку выполнит аналитик
отдельно: эталон и код проверяющей программы не читай. MCP отключать не требуется,
запрещены именно вызовы. Это инструкция, а не системная песочница.
''', encoding='utf-8')
    return destination


def instant(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('ожидается дата-строка или null')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('у даты отсутствует часовой пояс')
    return parsed.astimezone(timezone.utc)


def check(answer):
    errors = []
    if not isinstance(answer, dict) or answer.get('schema_version') != 1:
        return ['Ожидается JSON-объект с schema_version=1.']
    rows = answer.get('cases')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return ['cases должен быть списком объектов.']
    expected_cases = cases()
    identifiers = [row.get('id') for row in rows]
    expected_ids = [case['id'] for case in expected_cases]
    if sorted(str(value) for value in identifiers) != sorted(expected_ids):
        return [f'Нужна каждая карточка ровно один раз: {expected_ids}; получено {identifiers}.']
    by_id = {row['id']: row for row in rows}
    for case in expected_cases:
        identifier = case['id']
        row = by_id[identifier]
        facts = row.get('facts')
        if not isinstance(facts, dict):
            errors.append(f'{identifier}: отсутствует объект facts.')
            continue
        if set(facts) != set(case['facts']):
            errors.append(f'{identifier}: неверный набор полей facts.')
        for field, expected in case['facts'].items():
            actual = facts.get(field)
            try:
                same = actual == expected if field.endswith('_state') else instant(actual) == instant(expected)
            except ValueError as error:
                errors.append(f'{identifier}.{field}: {error}.')
                continue
            if not same:
                errors.append(f'{identifier}.{field}: ожидалось {expected!r}, получено {actual!r}.')
        evidence = row.get('evidence')
        if not isinstance(evidence, dict):
            errors.append(f'{identifier}: evidence должен быть объектом с цитатами.')
            evidence = {}
        for field, quotes in evidence.items():
            if (field not in case['facts'] or not isinstance(quotes, list) or not quotes
                    or any(not isinstance(quote, str) or not quote.strip() or quote not in case['text']
                           for quote in quotes)):
                errors.append(f'{identifier}.{field}: цитаты отсутствуют в исходной истории или имеют неверный формат.')
        for field, required in case['support'].items():
            quotes = evidence.get(field, [])
            if not isinstance(quotes, list) or not any(
                    isinstance(quote, str) and quote in case['text'] and required in quote
                    and any(token.startswith('2026-') for token in quote.replace('[', ' ').split())
                    for quote in quotes):
                errors.append(f'{identifier}.{field}: нет цитаты с датой и событием-основанием.')
        if not isinstance(row.get('explanation'), str) or not row['explanation'].strip():
            errors.append(f'{identifier}: нужно объяснение вывода.')
    return errors


def verify(answer_file):
    errors = check(json.loads(answer_file.read_text(encoding='utf-8')))
    return {'status': 'history-understanding-failed' if errors else 'history-understanding-passed',
            'errors': errors, 'dialogue_review_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'verify'))
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'create':
            print(create(args.path))
            return 0
        result = verify(args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['errors'] else 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
