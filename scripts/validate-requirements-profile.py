#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


FORMAT_MARKER = "Формат: **компактная спецификация функциональности**"
REQUIRED_SECTIONS = (
    "Назначение",
    "Границы",
    "Требования",
    "Влияние на соседние функциональности",
    "Источники и открытые вопросы",
)
REQ_DEFINITION_RE = re.compile(
    r"^### (REQ-[A-Z0-9-]+)\.\s+([^\n]+?)\s*$",
    re.MULTILINE,
)
REQ_REFERENCE_RE = re.compile(r"\bREQ-[A-Z0-9-]+\b")
SCENARIO_RE = re.compile(r"^#### (?:[A-Z0-9-]+\.\s+)?Сценарий:\s+\S.+$", re.MULTILINE)
WHEN_RE = re.compile(r"^\s*(?:[-*]\s+)?\*\*Когда\*\*\s*\S", re.MULTILINE)
THEN_RE = re.compile(r"^\s*(?:[-*]\s+)?\*\*Тогда\*\*\s*\S", re.MULTILINE)
NORMATIVE_RE = re.compile(r"\b(?:не\s+)?долж(?:ен|на|но|ны)\b", re.IGNORECASE)
ENGLISH_NORMATIVE_RE = re.compile(
    r"^(?:Purpose|Requirements?|Scenario):|\b(?:WHEN|THEN|AND|SHALL)\b",
    re.MULTILINE,
)


def section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.end():]
    next_heading = re.search(r"^## ", tail, re.MULTILINE)
    return tail[:next_heading.start()] if next_heading else tail


def requirements_region(text: str) -> str:
    start = re.search(r"^## Требования\s*$", text, re.MULTILINE)
    if not start:
        return ""
    tail = text[start.end():]
    end = re.search(
        r"^## Влияние на соседние функциональности\s*$",
        tail,
        re.MULTILINE,
    )
    return tail[:end.start()] if end else tail


def without_fenced_code(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def requirement_bodies(text: str) -> list[tuple[str, str]]:
    requirements = requirements_region(text)
    matches = list(REQ_DEFINITION_RE.finditer(requirements))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(requirements)
        body = requirements[match.end():end]
        next_chapter = re.search(r"^## ", body, re.MULTILINE)
        if next_chapter:
            body = body[:next_chapter.start()]
        result.append((match.group(1), body.strip()))
    return result


def candidates(root: Path, feature: str | None) -> list[Path]:
    if feature:
        path = root / "features" / feature / "requirements.md"
        return [path] if path.is_file() else []
    return sorted(root.glob("features/*/requirements.md"))


def metadata_value(text: str, prefix: str) -> str:
    return next(
        (line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith(prefix)),
        "",
    )


def validate(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    prose = without_fenced_code(text)
    errors: list[str] = []

    headings = set(re.findall(r"^## (.+?)\s*$", text, re.MULTILINE))
    for title in REQUIRED_SECTIONS:
        if title not in headings:
            errors.append(f"отсутствует раздел: {title}")
        elif not section(text, title).strip():
            errors.append(f"раздел не заполнен: {title}")

    metadata = {
        prefix: metadata_value(text, prefix)
        for prefix in ("Редакция:", "Функциональность:")
    }
    for prefix, value in metadata.items():
        if not value:
            errors.append(f"отсутствуют метаданные: {prefix}")
    if "<" in metadata.get("Редакция:", ""):
        errors.append("номер редакции должен быть заполнен")
    if re.search(r"^Статус:\s*", text, re.MULTILINE):
        errors.append(
            "компактный requirements.md не должен содержать Статус: "
            "состояния хранятся в requirements-state.json и manifest.json"
        )

    definitions = requirement_bodies(text)
    identifiers = [identifier for identifier, _ in definitions]
    if not identifiers:
        errors.append("раздел Требования должен содержать хотя бы одно определение REQ-*")
    if len(identifiers) != len(set(identifiers)):
        errors.append("определения REQ-* должны быть уникальными")

    known = set(identifiers)
    for identifier, body in definitions:
        body_prose = without_fenced_code(body)
        before_scenario = body_prose.split("#### ", 1)[0]
        if not NORMATIVE_RE.search(before_scenario):
            errors.append(f"{identifier}: отсутствует явная русская нормативная форма")
        scenarios = list(SCENARIO_RE.finditer(body_prose))
        if not scenarios:
            errors.append(f"{identifier}: отсутствует вложенный сценарий")
            continue
        for index, scenario in enumerate(scenarios, start=1):
            end = scenarios[index].start() if index < len(scenarios) else len(body_prose)
            scenario_body = body_prose[scenario.end():end]
            if not WHEN_RE.search(scenario_body):
                errors.append(f"{identifier}: сценарий {index} не содержит ключевое слово Когда")
            if not THEN_RE.search(scenario_body):
                errors.append(f"{identifier}: сценарий {index} не содержит ключевое слово Тогда")

    for identifier in sorted(set(REQ_REFERENCE_RE.findall(prose)) - known):
        errors.append(f"ссылка на неизвестное требование: {identifier}")

    if ENGLISH_NORMATIVE_RE.search(prose):
        errors.append("нормативные заголовки и ключевые слова должны быть написаны по-русски")
    if "ISO/IEC/IEEE 29148" in text:
        errors.append("документ не должен использовать ISO-подобный профиль")
    if re.search(
        r"(^|/)slices(/|$)|Карточка среза|Порядок срезов",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        errors.append("документ не должен ссылаться на производные срезы")
    if "```mermaid" in text.lower():
        errors.append("диаграммы требований должны использовать PlantUML")
    if text.count("```plantuml") != text.count("@enduml"):
        errors.append("блоки PlantUML должны содержать по одному @enduml")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка компактного профиля требований")
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
    print(f"Компактный профиль проверен: {checked}, прежнего формата пропущено: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
