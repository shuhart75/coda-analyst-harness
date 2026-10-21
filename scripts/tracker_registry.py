from __future__ import annotations

from importlib import import_module
from pathlib import Path
import re


LEGACY_NO_TASKS = re.compile(
    r"\s*# Implementation tasks\s+"
    r"Этот slice зафиксирован как imported existing coverage\.\s+"
    r"- Активных implementation tasks в рамках текущего harness не заведено\."
    r"(?:\s+- Исторические task docs смотри в `../../references\.md` и raw legacy snapshot\.)?\s*"
)


def registry_headers(headers: list[str]) -> bool:
    return bool({"Task ID", "SberTrek"} & set(headers)) or (
        {"Jira", "Summary", "Kind", "Role"} <= set(headers)
    )


def read_registry(path: Path, *, identity_only: bool = False) -> tuple[list[list[dict[str, str]]], str | None]:
    overlay = import_module("sync-actual-progress-overlay")
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        headers = [overlay.clean_cell(cell) for cell in line.strip().strip("|").split("|")]
        if not registry_headers(headers):
            continue
        if not identity_only and any(re.fullmatch(r"Estimate\s+(AN|BE|FE|QA)(?:\s.*)?", header) for header in headers):
            raise ValueError(f"Ролевые оценки допустимы в отчёте, не вместо Estimate (дн) в реестре: {path}")
        separator = lines[index + 1].strip().strip("|").split("|") if index + 1 < len(lines) else []
        if (not separator or len(separator) != len(headers) or len(set(headers)) != len(headers)
                or not overlay.is_separator_row(separator)):
            raise ValueError(f"Повреждён заголовок реестра: {path}")
        for row in lines[index + 2:]:
            if not row.strip().startswith("|"):
                break
            if len(row.strip().strip("|").split("|")) != len(headers):
                raise ValueError(f"Повреждена строка реестра: {path}")
    tables, legacy = [], False
    for headers, rows in overlay.parse_table_blocks(path):
        if not registry_headers(headers):
            continue
        if "Jira" in headers and "Task ID" not in headers:
            # Historical column was also used for virtual/local IDs. Never treat
            # its values as SberTrek keys, even when their spelling matches one.
            rows = [{**row, "Task ID": row.get("Jira", "")} for row in rows]
            legacy = True
        tables.append(rows)
    if tables:
        return tables, "legacy-local-identities" if legacy else None if any(tables) else "empty-registry"
    if LEGACY_NO_TASKS.fullmatch(path.read_text(encoding="utf-8")):
        return [], "legacy-no-active-tasks"
    raise ValueError(f"Нет читаемого реестра Task ID/SberTrek: {path}")
