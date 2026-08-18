#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


PROFILE_MARKER = "Профиль требований: **АС КОДА / ISO/IEC/IEEE 29148:2018**"
REQUIRED_SECTIONS = (
    "Назначение и границы",
    "Текущее состояние",
    "Участники и внешние системы",
    "Термины и данные",
    "Функциональные требования",
    "Сценарии",
    "Нефункциональные требования",
    "Доработки затронутых функциональностей",
    "Зависимости и предположения",
    "Критерии завершённости",
    "Трассировка",
    "Открытые вопросы",
)
REQ_RE = re.compile(r"\bREQ-[A-Z0-9-]+\b")
SCN_RE = re.compile(r"\bSCN-[A-Z0-9-]+\b")
NORMATIVE_RE = re.compile(r"\bдолж(?:ен|на|но|ны)\b", re.IGNORECASE)


def section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.end():]
    next_heading = re.search(r"^## ", tail, re.MULTILINE)
    return tail[:next_heading.start()] if next_heading else tail


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
    for prefix in ("Редакция:", "Статус:"):
        value = next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith(prefix)), "")
        metadata[prefix] = value
        if not value:
            errors.append(f"отсутствуют метаданные: {prefix}")
    if not metadata["Редакция:"] or "<" in metadata["Редакция:"]:
        errors.append("номер редакции должен быть заполнен")
    normalized_status = metadata["Статус:"].strip("*` ").lower()
    if normalized_status not in {"черновик", "утверждён"}:
        errors.append("статус должен быть равен `черновик` или `утверждён`")

    requirements = section(text, "Функциональные требования")
    requirement_ids = REQ_RE.findall(requirements)
    if not requirement_ids:
        errors.append("функциональные требования должны содержать хотя бы один REQ-*")
    if len(requirement_ids) != len(set(requirement_ids)):
        errors.append("идентификаторы REQ-* в функциональных требованиях должны быть уникальными")
    for line in requirements.splitlines():
        if REQ_RE.search(line) and not NORMATIVE_RE.search(line):
            errors.append(f"требование должно использовать явную нормативную форму: {REQ_RE.search(line).group(0)}")
    for column in ("Обоснование", "Источник", "Приоритет", "Проверка"):
        if column not in requirements:
            errors.append(f"в таблице функциональных требований нет столбца: {column}")

    non_functional = section(text, "Нефункциональные требования")
    non_functional_ids = REQ_RE.findall(non_functional)
    if len(non_functional_ids) != len(set(non_functional_ids)):
        errors.append("идентификаторы REQ-* в нефункциональных требованиях должны быть уникальными")
    for line in non_functional.splitlines():
        match = REQ_RE.search(line)
        if match and "неприменимо" not in line.lower() and not NORMATIVE_RE.search(line):
            errors.append(f"нефункциональное требование должно использовать явную нормативную форму: {match.group(0)}")
    all_requirement_ids = requirement_ids + non_functional_ids
    if len(all_requirement_ids) != len(set(all_requirement_ids)):
        errors.append("идентификаторы REQ-* должны быть уникальными во всём документе")

    scenarios = section(text, "Сценарии")
    scenario_ids = SCN_RE.findall(scenarios)
    if not scenario_ids:
        errors.append("сценарии должны содержать хотя бы один SCN-*")
    if len(scenario_ids) != len(set(scenario_ids)):
        errors.append("идентификаторы SCN-* в сценариях должны быть уникальными")
    known_requirements = set(all_requirement_ids)
    for line in scenarios.splitlines():
        scenario = SCN_RE.search(line)
        if not scenario:
            continue
        covered = set(REQ_RE.findall(line))
        if not covered:
            errors.append(f"сценарий не связан с требованиями: {scenario.group(0)}")
        for requirement_id in sorted(covered - known_requirements):
            errors.append(f"сценарий {scenario.group(0)} ссылается на неизвестное требование: {requirement_id}")

    trace = section(text, "Трассировка")
    for item_id in sorted(set(requirement_ids + non_functional_ids + scenario_ids)):
        if item_id not in trace:
            errors.append(f"в трассировке отсутствует идентификатор: {item_id}")

    if normalized_status == "утверждён":
        for prefix in ("Утвердил:", "Дата утверждения:"):
            value = next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith(prefix)), "")
            if not value or "<" in value:
                errors.append(f"для утверждённых требований нужны метаданные: {prefix}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка профиля требований АС КОДА")
    parser.add_argument("project")
    parser.add_argument("--feature")
    args = parser.parse_args()
    root = Path(args.project).resolve()
    files = candidates(root, args.feature)
    profiled = 0
    skipped = 0
    failed = False
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if PROFILE_MARKER not in text:
            skipped += 1
            continue
        profiled += 1
        for error in validate(path):
            failed = True
            print(f"{path.relative_to(root)}: {error}")
    if failed:
        return 1
    print(f"Профиль требований проверен: {profiled} по профилю, {skipped} прежнего формата пропущено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
