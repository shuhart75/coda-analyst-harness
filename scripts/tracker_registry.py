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


def read_registry(path: Path) -> tuple[list[list[dict[str, str]]], str | None]:
    overlay = import_module("sync-actual-progress-overlay")
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        headers = [overlay.clean_cell(cell) for cell in line.strip().strip("|").split("|")]
        if not ({"Task ID", "Jira"} & set(headers)):
            continue
        separator = lines[index + 1].strip().strip("|").split("|") if index + 1 < len(lines) else []
        if (not separator or len(separator) != len(headers) or len(set(headers)) != len(headers)
                or not overlay.is_separator_row(separator)):
            raise ValueError(f"Повреждён заголовок реестра: {path}")
        for row in lines[index + 2:]:
            if not row.strip().startswith("|"):
                break
            if len(row.strip().strip("|").split("|")) != len(headers):
                raise ValueError(f"Повреждена строка реестра: {path}")
    tables = [rows for headers, rows in overlay.parse_table_blocks(path)
              if "Task ID" in headers or "Jira" in headers]
    if tables:
        return tables, None if any(tables) else "empty-registry"
    if LEGACY_NO_TASKS.fullmatch(path.read_text(encoding="utf-8")):
        return [], "legacy-no-active-tasks"
    raise ValueError(f"Нет читаемого реестра Task ID/Jira: {path}")
