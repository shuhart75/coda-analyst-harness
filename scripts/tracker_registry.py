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


def read_registry_blocks(path: Path, *, identity_only: bool = False) -> tuple[list[tuple[list[str], list[dict[str, str]]]], str | None]:
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
        tables.append((headers, rows))
    if tables:
        return tables, "legacy-local-identities" if legacy else None if any(rows for _, rows in tables) else "empty-registry"
    if LEGACY_NO_TASKS.fullmatch(path.read_text(encoding="utf-8")):
        return [], "legacy-no-active-tasks"
    raise ValueError(f"Нет читаемого реестра Task ID/SberTrek: {path}")


def read_registry(path: Path, *, identity_only: bool = False) -> tuple[list[list[dict[str, str]]], str | None]:
    blocks, note = read_registry_blocks(path, identity_only=identity_only)
    return [rows for _, rows in blocks], note


def require_new_registry_format(project: Path, path: Path) -> None:
    """Compatibility reads do not authorize creating another legacy registry."""
    from tracker_execution import git

    project, path = project.resolve(), path.resolve()
    if not path.is_relative_to(project):
        raise ValueError('Registry is outside project')
    relative = path.relative_to(project).as_posix()
    # Compare to HEAD, not the index: staging a new file must not bypass this gate.
    if git(project, 'ls-tree', '--name-only', '-z', 'HEAD', '--', relative).strip('\0\n'):
        return
    overlay = import_module('sync-actual-progress-overlay')
    tables = [(headers, rows) for headers, rows in overlay.parse_table_blocks(path)
              if registry_headers(headers)]
    if (not tables or any(not {'Task ID', 'SberTrek'} <= set(headers) or 'Jira' in headers
                          or any(row.get('Task ID', '').strip() in {'', '-', '—'} for row in rows)
                          for headers, rows in tables)):
        raise ValueError(f'New registry requires Task ID and SberTrek columns, nonempty local Task ID, and no Jira column: {relative}')


def check_protected_rows(project: Path, protected_rows: list[dict]) -> list[dict]:
    """Match old identity-less rows by content and multiplicity, never by Jira."""
    from collections import Counter, defaultdict

    grouped = defaultdict(list)
    for reference in protected_rows:
        grouped[reference['registry']].append(reference)
    errors = []

    def identity(row):
        value = row.get('Task ID') or row.get('SberTrek') or ''
        return '' if value.strip() in {'', '-', '—'} else value

    def fields(row, allowed):
        return tuple(sorted((key, value) for key, value in row.items() if key not in allowed))

    for relative, references in grouped.items():
        registry = (project / relative).resolve()
        if not registry.is_relative_to(project):
            raise ValueError('Protected registry is outside project')
        tables, _ = read_registry(registry) if registry.is_file() else ([], None)
        rows = [row for table in tables for row in table]
        anonymous = defaultdict(list)
        for reference in references:
            allowed = frozenset(reference.get('allowed_fields', []))
            if not identity({'Task ID': reference['task_id']}):
                anonymous[allowed].append(reference)
                continue
            matches = [row for row in rows if identity(row) == reference['task_id']]
            if len(matches) != 1 or fields(matches[0], allowed) != fields(reference['registry_fields'], allowed):
                errors.append({'task_id': reference['task_id'], 'registry': relative,
                               'reason': 'Task outside release changed or was removed'})
        for allowed, items in anonymous.items():
            expected = Counter(fields(item['registry_fields'], allowed) for item in items)
            actual = Counter(fields(row, allowed) for row in rows if not identity(row))
            if any(actual[value] != count for value, count in expected.items()):
                errors.append({'task_id': '', 'registry': relative,
                               'reason': 'Protected rows without identity changed or their multiplicity differs'})
    return errors
