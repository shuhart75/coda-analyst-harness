#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


FORMAT_MARKER = "Формат: **компактная спецификация функциональности**"
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
REQ_HEADING_RE = re.compile(r"^### (REQ-[A-Z0-9-]+)\.", re.MULTILINE)


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern[str]
    message: str


RULES = (
    Rule(
        "неопределённая-роль",
        re.compile(r"\bлюб\w*\s+(?:\w+\s+){0,2}рол\w*", re.IGNORECASE),
        "замени «любая роль» точным количеством и явно названным набором ролей",
    ),
    Rule(
        "неявная-ссылка",
        re.compile(
            r"\bсоответствующ\w*\s+(?:событи\w*|сценари\w*|действи\w*|услови\w*|рол\w*|результат\w*)",
            re.IGNORECASE,
        ),
        "назови событие, сценарий, действие, условие, роль или результат явно",
    ),
    Rule(
        "неопределённый-алгоритм",
        re.compile(
            r"\b(?:штатн\w*|обычн\w*|корректн\w*)\s+(?:правил\w*|алгоритм\w*|механизм\w*|логик\w*)",
            re.IGNORECASE,
        ),
        "укажи точное правило, алгоритм, механизм или проверяемый критерий",
    ),
    Rule(
        "открытый-перечень",
        re.compile(r"\b(?:и\s+т\.\s*[дп]\.|и\s+проч(?:ее|ие))\b", re.IGNORECASE),
        "замени открытый перечень полным набором или явно определённой областью",
    ),
)

WHEN_PLACEHOLDER_RE = re.compile(
    r"^\s*\*\*Когда\*\*.*(?:"
    r"соответствующ\w+|"
    r"основн\w+\s+сценари\w+|"
    r"запрашивает\s+доступ\s+к\s+данным\s+или\s+действию|"
    r"открывает\s+карточку\s+заявки\s+или\s+действует\s+в\s+ней|"
    r"открывает\s+заключение\s+или\s+эксперт\s+сохраняет\s+его|"
    r"использует\s+данные\s+или\s+поведение\s+соседн\w+\s+подсистем\w+"
    r")",
    re.IGNORECASE,
)
THEN_NORMATIVE_RE = re.compile(
    r"^\s*\*\*Тогда\*\*.*\b(?:не\s+)?долж(?:ен|на|но|ны)\b",
    re.IGNORECASE,
)


def without_fenced_code(text: str) -> str:
    return FENCE_RE.sub(lambda match: "\n" * match.group(0).count("\n"), text)


def requirement_files(root: Path, feature: str | None) -> list[Path]:
    if feature:
        path = root / "features" / feature / "requirements.md"
        return [path] if path.is_file() else []
    return sorted(root.glob("features/*/requirements.md"))


def current_requirement(line_number: int, headings: list[tuple[int, str]]) -> str | None:
    identifier: str | None = None
    for heading_line, heading_id in headings:
        if heading_line > line_number:
            break
        identifier = heading_id
    return identifier


def validate(path: Path) -> list[tuple[int, str, str, str | None]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if FORMAT_MARKER not in text:
        return []
    prose = without_fenced_code(text)
    headings = [
        (prose.count("\n", 0, match.start()) + 1, match.group(1))
        for match in REQ_HEADING_RE.finditer(prose)
    ]
    findings: list[tuple[int, str, str, str | None]] = []
    for line_number, raw_line in enumerate(prose.splitlines(), start=1):
        line = INLINE_CODE_RE.sub("", raw_line)
        if not line.strip():
            continue
        requirement = current_requirement(line_number, headings)
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append((line_number, rule.code, rule.message, requirement))
        if WHEN_PLACEHOLDER_RE.search(line):
            findings.append((
                line_number,
                "непроверяемое-условие",
                "в Когда укажи конкретное начальное состояние и событие",
                requirement,
            ))
        if THEN_NORMATIVE_RE.search(line):
            findings.append((
                line_number,
                "нормативный-результат",
                "в Тогда опиши наблюдаемый результат без повторения «система должна»",
                requirement,
            ))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка однозначности формулировок требований")
    parser.add_argument("project")
    parser.add_argument("--feature")
    args = parser.parse_args()
    root = Path(args.project).expanduser().resolve()
    files = requirement_files(root, args.feature)
    checked = 0
    findings: list[tuple[Path, int, str, str, str | None]] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if FORMAT_MARKER not in text:
            continue
        checked += 1
        findings.extend(
            (path.relative_to(root), line, code, message, requirement)
            for line, code, message, requirement in validate(path)
        )
    if findings:
        print(f"Неоднозначные формулировки: {len(findings)}")
        for path, line, code, message, requirement in findings:
            owner = f" [{requirement}]" if requirement else ""
            print(f"- {path}:{line}{owner}: {code}: {message}")
        print("Содержательный вариант не выбирается автоматически; при сомнении задай аналитику один вопрос.")
        return 1
    print(f"Формулировки требований проверены: {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
