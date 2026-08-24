#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


FORMAT_MARKER = "Формат: **последовательный человекочитаемый**"
REQUIRED_SECTIONS = (
    "Кратко о функциональности",
    "Цель и ожидаемый результат",
    "Границы",
    "Текущее и требуемое состояние",
    "Участники, внешние системы и данные",
    "Общие правила",
    "Ошибки и пограничные случаи",
    "Нефункциональные требования",
    "Доработки затронутых функциональностей",
    "Подчистка устаревшего поведения",
    "Сводная трассировка",
    "Открытые вопросы",
)
REQ_DEFINITION_RE = re.compile(r"^\*\*(REQ-[A-Z0-9-]+)\.\s+.+?\*\*\s*$", re.MULTILINE)
REQ_REFERENCE_RE = re.compile(r"\bREQ-[A-Z0-9-]+\b")
AC_DEFINITION_RE = re.compile(r"^\*\*(AC-[A-Z0-9-]+)\.\s+.+?\*\*\s*$", re.MULTILINE)
NORMATIVE_RE = re.compile(r"\bдолж(?:ен|на|но|ны)\b", re.IGNORECASE)


def section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.end():]
    next_heading = re.search(r"^## ", tail, re.MULTILINE)
    return tail[:next_heading.start()] if next_heading else tail


def definition_bodies(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    matches = list(pattern.finditer(text))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = re.search(r"^#{1,6}\s+", text[match.end():end], re.MULTILINE)
        body_end = match.end() + heading.start() if heading else end
        result.append((match.group(1), text[match.end():body_end].strip()))
    return result


def candidates(root: Path, feature: str | None) -> list[Path]:
    if feature:
        path = root / "features" / feature / "requirements.md"
        return [path] if path.is_file() else []
    return sorted(root.glob("features/*/requirements.md"))


def validate(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    errors: list[str] = []
    headings = set(re.findall(r"^## (.+?)\s*$", text, re.MULTILINE))
    for title in REQUIRED_SECTIONS:
        if title not in headings:
            errors.append(f"отсутствует раздел: {title}")
    metadata: dict[str, str] = {}
    for prefix in ("Редакция:", "Статус:", "Функциональность:"):
        value = next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith(prefix)), "")
        metadata[prefix] = value
        if not value:
            errors.append(f"отсутствуют метаданные: {prefix}")
    if "<" in metadata.get("Редакция:", ""):
        errors.append("номер редакции должен быть заполнен")
    status = metadata.get("Статус:", "").strip("*\u0060 ").lower()
    if status not in {"черновик", "утверждён"}:
        errors.append("статус должен быть равен черновик или утверждён")
    definitions = definition_bodies(text, REQ_DEFINITION_RE)
    identifiers = [identifier for identifier, _ in definitions]
    if not identifiers:
        errors.append("документ должен содержать хотя бы одно оформленное требование REQ-*")
    if len(identifiers) != len(set(identifiers)):
        errors.append("определения REQ-* должны быть уникальными")
    for identifier, body in definitions:
        if not NORMATIVE_RE.search(body):
            errors.append(f"{identifier}: отсутствует явная нормативная форма")
    known = set(identifiers)
    for identifier in sorted(set(REQ_REFERENCE_RE.findall(text)) - known):
        errors.append(f"ссылка на неизвестное требование: {identifier}")
    acceptance = definition_bodies(text, AC_DEFINITION_RE)
    acceptance_ids = [identifier for identifier, _ in acceptance]
    if len(acceptance_ids) != len(set(acceptance_ids)):
        errors.append("определения AC-* должны быть уникальными")
    for identifier, body in acceptance:
        references = set(REQ_REFERENCE_RE.findall(body))
        if not references:
            errors.append(f"{identifier}: пример приёмки не связан с требованиями")
        for requirement in sorted(references - known):
            errors.append(f"{identifier}: ссылка на неизвестное требование {requirement}")
    traceability = section(text, "Сводная трассировка")
    for identifier in identifiers:
        if identifier not in traceability:
            errors.append(f"в сводной трассировке отсутствует {identifier}")
    if "ISO/IEC/IEEE 29148" in text:
        errors.append("документ не должен использовать ISO-подобный профиль")
    if re.search(r"(^|/)slices(/|$)|Карточка среза|Порядок срезов", text, re.IGNORECASE | re.MULTILINE):
        errors.append("документ не должен ссылаться на производные срезы")
    if "\u0060\u0060\u0060mermaid" in text.lower():
        errors.append("диаграммы требований должны использовать PlantUML")
    if text.count("\u0060\u0060\u0060plantuml") != text.count("@enduml"):
        errors.append("блоки PlantUML должны содержать по одному @enduml")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка человекочитаемого профиля требований")
    parser.add_argument("project")
    parser.add_argument("--feature")
    args = parser.parse_args()
    root = Path(args.project).resolve()
    files = candidates(root, args.feature)
    checked = 0
    skipped = 0
    failed = False
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if FORMAT_MARKER not in text:
            skipped += 1
            continue
        checked += 1
        for error in validate(path):
            failed = True
            print(f"{path.relative_to(root)}: {error}")
    if failed:
        return 1
    print(f"Человекочитаемый профиль проверен: {checked}, прежнего формата пропущено: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
