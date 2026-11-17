"""Readable system requirements and shared acceptance criteria.

analyst-sdd-receiver:v1

Only the explicit new profile uses this parser; legacy inputs retain their rules.
"""
from __future__ import annotations

import re

FORMAT_MARKER = "Формат: **системная спецификация функциональности**"
LEGACY_MARKER = "Формат: **компактная спецификация функциональности**"
REQ = re.compile(r"\bREQ-[A-Z0-9-]+\b")
HEADING = re.compile(r"^### (REQ-[A-Z0-9-]+)\.\s+(.+)$")
REQUIRED_SECTIONS = (
    "Назначение", "Границы", "Критерии приёмки",
    "Влияние на соседние функциональности", "Источники и открытые вопросы",
)


def prose(text):
    """Exclude examples/comments while preserving diagnostic line numbers."""
    text = re.sub(r"<!--[\s\S]*?-->", lambda m: "\n" * m[0].count("\n"), text)
    return re.sub(r"(?ms)^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[ \t]*$",
                  lambda m: "\n" * m[0].count("\n"), text)


def is_readable(text):
    return FORMAT_MARKER in prose(text).splitlines()


def supported(text):
    return is_readable(text) or LEGACY_MARKER in prose(text).splitlines()


def cells(line):
    return [s.strip().replace(r"\|", "|") for s in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def parse(text):
    lines = prose(text).splitlines()
    definitions, criteria, errors = [], [], []
    section = ""
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip()
        match = HEADING.fullmatch(line)
        row = cells(line) if line.startswith("|") and line.endswith("|") else []
        if match:
            end = i + 1
            while end < len(lines) and not re.match(r"^#{1,3} |^\| REQ-", lines[end]):
                end += 1
            body = "\n".join(lines[i+1:end]).strip()
            definitions.append(dict(id=match[1], title=match[2], body=body,
                                    rule="\n".join(lines[i:end]).rstrip(), line=i+1))
        elif row and REQ.fullmatch(row[0]):
            if len(row) != 3 or not all(row):
                errors.append(f"строка {i+1}: REQ-таблица требует идентификатор, название и правило")
            else:
                definitions.append(dict(id=row[0], title=row[1], body=row[2], line=i+1,
                                        rule="| Требование | Название | Правило |\n|---|---|---|\n" + line))
        if section == "Критерии приёмки" and row and row[0].startswith("AC-"):
            if len(row) != 4 or not re.fullmatch(r"AC-[A-Z0-9-]+", row[0]) or not all(row):
                errors.append(f"строка {i+1}: критерий требует AC-id, условие/действие, результат и REQ-ссылки")
                continue
            refs = REQ.findall(row[3])
            remainder = REQ.sub("", row[3]).strip(" ,;`")
            if not refs or remainder or len(refs) != len(set(refs)):
                errors.append(f"{row[0]}: перечисли полные уникальные REQ без диапазонов")
            criteria.append(dict(id=row[0], when=row[1], then=row[2], refs=refs, line=i+1,
                                 rule="| Критерий | Условие и действие | Ожидаемый результат | Требования |\n|---|---|---|---|\n" + line))
    return definitions, criteria, errors


def validate(text):
    clean = prose(text)
    definitions, criteria, errors = parse(text)
    if not is_readable(text):
        errors.append("отсутствует маркер системной спецификации")
    formats = re.findall(r"^Формат:.*$", clean, re.M)
    if formats != [FORMAT_MARKER]:
        errors.append("требуется ровно один маркер формата")
    for title in REQUIRED_SECTIONS:
        sections = list(re.finditer(rf"^## {re.escape(title)}\s*$", clean, re.M))
        if len(sections) != 1:
            errors.append(f"требуется один раздел: {title}")
        else:
            body = re.split(r"^## ", clean[sections[0].end():], maxsplit=1, flags=re.M)[0]
            if not body.strip():
                errors.append(f"раздел не заполнен: {title}")
    for prefix in ("Редакция", "Функциональность"):
        values = re.findall(rf"^{prefix}:[ \t]*([^\n]+)$", clean, re.M)
        if len(values) != 1 or not values[0].strip() or "<" in values[0]:
            errors.append(f"заполни метаданные: {prefix}")
    if re.search(r"^Статус:", clean, re.M):
        errors.append("requirements.md не должен содержать Статус: используй requirements-state.json")
    ids = [d['id'] for d in definitions]
    if not ids:
        errors.append("отсутствуют определения REQ-*")
    if len(ids) != len(set(ids)):
        errors.append("определения REQ-* должны быть уникальными")
    for d in definitions:
        if not d['body'].strip():
            errors.append(f"{d['id']}: правило не заполнено")
    ac_ids = [c['id'] for c in criteria]
    if not ac_ids:
        errors.append("отсутствуют критерии AC-*")
    if len(ac_ids) != len(set(ac_ids)):
        errors.append("критерии AC-* должны быть уникальными")
    covered = {r for c in criteria for r in c['refs']}
    for identifier in sorted(set(ids) - covered):
        errors.append(f"{identifier}: нет критерия приёмки")
    for identifier in sorted(set(REQ.findall(clean)) - set(ids)):
        errors.append(f"ссылка на неизвестное требование: {identifier}")
    if "```mermaid" in text.lower():
        errors.append("диаграммы требований должны использовать PlantUML")
    if text.count("```plantuml") != text.count("@enduml"):
        errors.append("блоки PlantUML должны содержать по одному @enduml")
    if "ISO/IEC/IEEE 29148" in clean:
        errors.append("документ не должен использовать ISO-подобный профиль")
    if re.search(r"(^|/)slices(/|$)|Карточка среза|Порядок срезов", clean, re.I | re.M):
        errors.append("документ не должен ссылаться на производные срезы")
    return errors


def requirement_blocks(text):
    errors = validate(text)
    if errors:
        raise ValueError("Системная спецификация: " + "; ".join(errors))
    definitions, criteria, _ = parse(text)
    return {d['id']: dict(title=d['title'], rule=d['rule'],
                         scenarios={c['id']: c['rule'] for c in criteria if d['id'] in c['refs']})
            for d in definitions}


def authored_ids(text):
    if is_readable(text):
        return list(requirement_blocks(text))
    # Legacy extraction is deliberately unchanged, including historical fixtures.
    return re.findall(r"^### (REQ-[A-Z0-9-]+)\.\s+.+$", text, re.M)
