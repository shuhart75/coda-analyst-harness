#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from workspace_paths import team_path
import math
import json
import os
import re
import sys
import tempfile


ROLE_COLORS = {
    "AN": "LightGreen",
    "BE": "LightBlue",
    "FE": "LightCoral",
    "QA": "Gold",
}

ROLE_ORDER = {
    "AN": 10,
    "BE": 20,
    "FE": 30,
    "QA": 40,
}

RESOURCE_PREFIX = {
    "AN": "A",
    "BE": "B",
    "FE": "F",
    "QA": "Q",
}

DEFAULT_TEAM_RESOURCES = {
    "AN": ["A1", "A2", "A3"],
    "BE": ["B1", "B2", "B3"],
    "FE": ["F1", "F2"],
    "QA": ["Q1", "Q2", "Q3"],
}

ROLE_ALIASES = {
    "AN": {
        "a",
        "an",
        "analyst",
        "analytics",
        "analitic",
        "аналитик",
        "аналитика",
    },
    "BE": {
        "b",
        "be",
        "back",
        "backend",
        "backender",
        "api",
        "бэк",
        "бек",
        "бэкенд",
        "бекенд",
        "бэкендер",
        "бекендер",
    },
    "FE": {
        "f",
        "fe",
        "front",
        "frontend",
        "frontender",
        "ui",
        "фронт",
        "фронтенд",
        "фронтендер",
    },
    "QA": {
        "q",
        "qa",
        "test",
        "testing",
        "tester",
        "тест",
        "тестирование",
        "тестировщик",
        "qaинженер",
    },
}

NOT_STARTED_STATUSES = {"proposed", "предложен", "planned", "todo", "open", "backlog"}
FE_AFTER_BE_OPEN_DAYS = 3


@dataclass
class StoryMap:
    story_id: str
    summary: str
    baseline_start: str
    baseline_duration: int
    state: str
    mapping_mode: str
    replaced_by: list[str]
    residual_virtual_tasks: list[str]
    depends_on: list[str]


@dataclass
class Task:
    task_id: str
    tracker_key: str
    summary: str
    kind: str
    role: str
    estimate: float
    executor: str
    planned_start: str
    planned_finish: str
    actual_start: str
    actual_finish: str
    status: str
    progress: int
    related_stories: list[str]


@dataclass
class ScheduledTask:
    start: date
    finish: date | None
    assignee: str
    shifted: bool
    resource_note: str = ""


def usage() -> None:
    print("Usage: sync-actual-progress-overlay.py <project-root> <quarter-id> [feature-slug ...]")


def clean_cell(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1]
    return value.strip()


def split_list(value: str) -> list[str]:
    value = clean_cell(value)
    if not value or value in {"-", "—"}:
        return []
    parts = re.split(r"\s*,\s*|\s*;\s*|<br\s*/?>", value)
    return [clean_cell(part) for part in parts if clean_cell(part)]


def to_alias(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value.upper()).strip("_")


def parse_date(value: str) -> date | None:
    value = clean_cell(value)
    if not value:
        return None
    match = re.fullmatch(r"(\d{4})[-/](\d{2})[-/](\d{2})", value)
    if not match:
        return None
    year, month, day = match.groups()
    return date(int(year), int(month), int(day))


def harness_today() -> date:
    override = os.environ.get("HARNESS_TODAY", "").strip()
    if override:
        parsed = parse_date(override)
        if not parsed:
            raise ValueError(f"Invalid HARNESS_TODAY={override!r}; expected YYYY-MM-DD")
        return parsed
    return date.today()


def fmt_date(value: date) -> str:
    return f"{value:%Y/%m/%d}"


def parse_int(value: str, default: int = 0) -> int:
    match = re.search(r"\d+", clean_cell(value))
    return int(match.group(0)) if match else default


def parse_number(value: str, default: float = 0.0) -> float:
    match = re.search(r"\d+(?:[.,]\d+)?", clean_cell(value))
    return float(match.group(0).replace(",", ".")) if match else default


def load_closed_days(project_root: Path, quarter_id: str) -> set[date]:
    path = project_root / "planning" / quarter_id / "gantt/closed-days.txt"
    if not path.exists():
        return set()
    result: set[date] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = parse_date(line)
        if parsed:
            result.add(parsed)
    return result


def is_open_day(value: date, closed_days: set[date]) -> bool:
    return value.weekday() < 5 and value not in closed_days


def next_open_day(value: date, closed_days: set[date]) -> date:
    current = value
    while not is_open_day(current, closed_days):
        current += timedelta(days=1)
    return current


def add_open_days(start: date, days: int, closed_days: set[date]) -> date:
    current = next_open_day(start, closed_days)
    if days <= 1:
        return current
    remaining = days - 1
    while remaining > 0:
        current += timedelta(days=1)
        if is_open_day(current, closed_days):
            remaining -= 1
    return current


def add_open_day_offset(start: date, offset: int, closed_days: set[date]) -> date:
    current = next_open_day(start, closed_days)
    remaining = max(offset, 0)
    while remaining > 0:
        current += timedelta(days=1)
        if is_open_day(current, closed_days):
            remaining -= 1
    return current


def is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def parse_tables(path: Path) -> list[list[dict[str, str]]]:
    tables: list[list[dict[str, str]]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("|"):
            i += 1
            continue
        headers = [clean_cell(cell) for cell in line.strip("|").split("|")]
        if i + 1 >= len(lines):
            break
        separator = [cell.strip() for cell in lines[i + 1].strip().strip("|").split("|")]
        if not is_separator_row(separator):
            i += 1
            continue
        i += 2
        rows: list[dict[str, str]] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = [clean_cell(cell) for cell in lines[i].strip().strip("|").split("|")]
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(dict(zip(headers, cells)))
            i += 1
        tables.append(rows)
    return tables


def first_table_with(path: Path, required_header: str) -> list[dict[str, str]]:
    for table in parse_tables(path):
        if table and required_header in table[0]:
            return table
    return []


def load_team_resources(project_root: Path) -> dict[str, list[str]]:
    resources = {role: list(values) for role, values in DEFAULT_TEAM_RESOURCES.items()}
    path = team_path(project_root)
    if not path.exists():
        return resources

    rows = first_table_with(path, "Role")
    for row in rows:
        role = normalize_role(row.get("Role", ""))
        if role not in DEFAULT_TEAM_RESOURCES:
            continue
        raw_resources = split_list(row.get("Resources", ""))
        normalized = [normalize_executor(item) for item in raw_resources]
        normalized = [item for item in normalized if resource_role(item) == role]
        if normalized:
            resources[role] = normalized
    return resources


def load_story_map(feature_dir: Path) -> list[StoryMap]:
    path = feature_dir / "planning/actualization.md"
    if not path.exists():
        return []
    rows = first_table_with(path, "Story ID")
    result: list[StoryMap] = []
    for row in rows:
        story_id = clean_cell(row.get("Story ID", ""))
        if not story_id:
            raise ValueError(f"{path}: требуется Story ID")
        duration = clean_cell(row.get("Baseline Duration (дн)", ""))
        if not re.fullmatch(r"[1-9]\d*", duration):
            raise ValueError(f"{path}: требуется положительная целая Baseline Duration (дн)")
        if clean_cell(row.get("Actualization State", "")).lower() not in {"virtual", "mixed", "materialized", "done", "real"}:
            raise ValueError(f"{path}: неизвестный Actualization State")
        result.append(
            StoryMap(
                story_id=story_id,
                summary=clean_cell(row.get("Summary", story_id)),
                baseline_start=clean_cell(row.get("Baseline Start", "")),
                baseline_duration=parse_int(row.get("Baseline Duration (дн)", ""), 1),
                state=clean_cell(row.get("Actualization State", "virtual")).lower(),
                mapping_mode=clean_cell(row.get("Mapping Mode", "explicit")).lower(),
                replaced_by=split_list(row.get("Replaced By", "")),
                residual_virtual_tasks=split_list(row.get("Residual Virtual Tasks", "")),
                depends_on=split_list(row.get("Depends On", "")),
            )
        )
    return result


def progress_from_status(status: str) -> int:
    status = status.lower()
    if status in {"done", "closed", "complete", "completed"}:
        return 100
    if status in {"planned", "todo", "open", "backlog", "superseded"}:
        return 0
    if status in {"in_progress", "in progress", "doing"}:
        return 50
    return 0


def normalized_token(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", value.lower().replace("ё", "е"))


def role_alias_matches(token: str, alias: str) -> bool:
    alias_token = normalized_token(alias)
    if not alias_token:
        return False
    if token == alias_token:
        return True
    if len(alias_token) == 1:
        return bool(re.fullmatch(rf"{re.escape(alias_token)}\d+", token))
    return token.startswith(alias_token)


def normalize_role(value: str) -> str:
    token = normalized_token(clean_cell(value))
    if not token:
        return ""
    for role, aliases in ROLE_ALIASES.items():
        for alias in aliases:
            if role_alias_matches(token, alias):
                return role
    return clean_cell(value).upper()


def infer_role_from_task_id(value: str) -> str:
    raw = clean_cell(value).upper()
    for role, prefix in RESOURCE_PREFIX.items():
        if re.match(rf"^{role}(?:[-_]\w|\d)", raw) or re.match(rf"^{prefix}\d+", raw):
            return role
    return ""


def infer_role_from_summary(value: str) -> str:
    text = clean_cell(value).lower().replace("ё", "е")
    if any(keyword in text for keyword in ("frontend", "front", "ui", "фронт", "фронтенд")):
        return "FE"
    if any(keyword in text for keyword in ("backend", "back", "api", "бэк", "бек", "бэкенд", "бекенд")):
        return "BE"
    if any(keyword in text for keyword in ("qa", "test", "тест", "тестирование")):
        return "QA"
    if any(keyword in text for keyword in ("analyst", "аналитик", "аналитика")):
        return "AN"
    return ""


def role_for_task(task: Task) -> str:
    role = normalize_role(task.role)
    if role in ROLE_COLORS:
        return role
    id_role = infer_role_from_task_id(task.task_id)
    if id_role:
        return id_role
    executor_role = infer_role_from_executor(task.executor)
    if executor_role:
        return executor_role
    summary_role = infer_role_from_summary(task.summary)
    return summary_role or role


def resource_role(value: str) -> str:
    token = normalized_token(clean_cell(value))
    for role, prefix in RESOURCE_PREFIX.items():
        if re.fullmatch(rf"{normalized_token(prefix)}\d+", token):
            return role
    if token.startswith("tbd"):
        return infer_role_from_executor(value)
    return ""


def infer_role_from_executor(value: str) -> str:
    token = normalized_token(clean_cell(value))
    if not token:
        return ""
    token = re.sub(r"^(tbd|todo|unknown)", "", token)
    for role, aliases in ROLE_ALIASES.items():
        for alias in aliases:
            if role_alias_matches(token, alias):
                return role
    return ""


def normalize_executor(value: str) -> str:
    raw = clean_cell(value).strip("{} ")
    if not raw or raw in {"-", "—"}:
        return ""

    lower_raw = raw.lower().replace("ё", "е")
    role = infer_role_from_executor(raw)
    number_match = re.search(r"(\d+)\s*$", raw)
    number = number_match.group(1) if number_match else ""
    is_tbd = any(marker in lower_raw for marker in ("tbd", "todo", "unknown", "не назнач", "не определ"))

    if role:
        prefix = RESOURCE_PREFIX[role]
        if is_tbd:
            return f"TBD_{prefix}"
        return f"{prefix}{number}" if number else prefix

    # Keep real human names usable as PlantUML resources without inventing a role.
    return re.sub(r"[^0-9A-Za-zА-Яа-я_]+", "_", raw).strip("_")


def is_tbd_executor(value: str) -> bool:
    normalized = normalize_executor(value)
    return normalized.startswith("TBD_")


def explicit_executor(task: Task, team_resources: dict[str, list[str]]) -> str:
    normalized = normalize_executor(task.executor)
    if not normalized or normalized.startswith("TBD_"):
        return ""
    role = resource_role(normalized)
    task_role = role_for_task(task)
    if role and task_role in ROLE_COLORS and role != task_role:
        return ""
    if role and normalized not in team_resources.get(role, []):
        return ""
    return normalized


def validate_task_row(row: dict[str, str], path: Path) -> None:
    for column in ("Summary", "Kind", "Role", "Estimate (дн)", "Status", "Progress %"):
        if not clean_cell(row.get(column, "")):
            raise ValueError(f"{path}: требуется заполненное поле {column}")
    if normalize_role(row["Role"]) not in ROLE_COLORS or row["Kind"].lower() not in {"real", "virtual"}:
        raise ValueError(f"{path}: неизвестный Role или Kind")
    estimate = clean_cell(row["Estimate (дн)"]).replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", estimate) or not math.isfinite(float(estimate)) or float(estimate) <= 0:
        raise ValueError(f"{path}: Estimate (дн) должна быть положительным числом")
    progress = clean_cell(row["Progress %"])
    if not re.fullmatch(r"\d+", progress) or not 0 <= int(progress) <= 100:
        raise ValueError(f"{path}: Progress % должен быть целым числом от 0 до 100")
    for column in ("Planned Start", "Planned Finish", "Actual Start", "Actual Finish"):
        value = clean_cell(row.get(column, ""))
        if value and not parse_date(value):
            raise ValueError(f"{path}: неверная дата {column}")
    for prefix in ("Planned", "Actual"):
        start = parse_date(row.get(f"{prefix} Start", ""))
        finish = parse_date(row.get(f"{prefix} Finish", ""))
        if start and finish and finish < start:
            raise ValueError(f"{path}: окончание {prefix} раньше начала")
    if int(progress) > 0 and not (parse_date(row.get("Actual Start", "")) or parse_date(row.get("Planned Start", ""))):
        raise ValueError(f"{path}: для начатой задачи требуется дата начала из источника")


def load_tasks(feature_dir: Path) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    tracker_roles: set[tuple[str, str]] = set()
    registry = feature_dir / "execution/tasks.md"
    paths = ([registry] if registry.exists() else []) + sorted(feature_dir.glob("slices/*/execution/tasks.md"))
    for path in paths:
        rows = first_table_with(path, "Task ID") or first_table_with(path, "Jira")
        if not rows:
            raise ValueError(f"{path}: нет непустого реестра Task ID/Jira; Гант сохранён")
        for row in rows:
            tracker_key = clean_cell(row.get("Jira", ""))
            if tracker_key in {"-", "—"}:
                tracker_key = ""
            explicit_id = clean_cell(row.get("Task ID", ""))
            if explicit_id in {"-", "—"}:
                explicit_id = ""
            if not tracker_key and not explicit_id:
                raise ValueError(f"{path}: требуется Task ID или подтверждённый Jira")
            if path == registry or "Task ID" in row:
                validate_task_row(row, path)
            status = clean_cell(row.get("Status", "planned"))
            progress_value = row.get("Progress %", "")
            progress = parse_int(progress_value, progress_from_status(status)) if progress_value else progress_from_status(status)
            kind = clean_cell(row.get("Kind", "virtual")).lower()
            role = clean_cell(row.get("Role", ""))
            normalized_role = normalize_role(role)
            task_id = explicit_id or (f"{tracker_key}/{normalized_role}" if kind == "real" and normalized_role in ROLE_COLORS else tracker_key)
            if task_id in tasks:
                raise ValueError(f"Duplicate execution work item: {task_id}")
            pair = (tracker_key, normalized_role)
            if tracker_key and pair in tracker_roles:
                raise ValueError(f"Duplicate tracker role: {tracker_key}/{normalized_role}")
            if tracker_key:
                tracker_roles.add(pair)
            tasks[task_id] = Task(
                task_id=task_id,
                tracker_key=tracker_key,
                summary=clean_cell(row.get("Summary", tracker_key)),
                kind=kind,
                role=role,
                estimate=parse_number(row.get("Estimate (дн)", ""), 1.0),
                executor=clean_cell(row.get("Executor", "")),
                planned_start=clean_cell(row.get("Planned Start", "")),
                planned_finish=clean_cell(row.get("Planned Finish", "")),
                actual_start=clean_cell(row.get("Actual Start", "")),
                actual_finish=clean_cell(row.get("Actual Finish", "")),
                status=status,
                progress=progress,
                related_stories=split_list(row.get("Related Stories", "")),
            )
    candidates = feature_dir / "execution/task-candidates.md"
    for path in ([candidates] if candidates.exists() else []) + sorted(feature_dir.glob("slices/*/execution/task-candidates.md")):
        rows = first_table_with(path, "Candidate ID")
        russian = False
        if not rows:
            rows = first_table_with(path, "Идентификатор")
            russian = True
        for row in rows:
            task_id = clean_cell(row.get("Идентификатор" if russian else "Candidate ID", ""))
            if not task_id:
                continue
            if task_id in tasks:
                raise ValueError(f"Duplicate execution work item: {task_id}")
            status = clean_cell(row.get("Статус" if russian else "Status", "proposed"))
            tasks[task_id] = Task(
                task_id=task_id,
                tracker_key=task_id,
                summary=clean_cell(row.get("Краткое описание" if russian else "Summary", task_id)),
                kind="candidate",
                role=clean_cell(row.get("Роль" if russian else "Role", "")),
                estimate=parse_number(row.get("Оценка (дн)" if russian else "Estimate (дн)", ""), 1.0),
                executor="",
                planned_start="",
                planned_finish="",
                actual_start="",
                actual_finish="",
                status=status,
                progress=0,
                related_stories=split_list(row.get("Связанная плановая история" if russian else "Related Story", "")),
            )
    return tasks


def task_start(task: Task) -> date | None:
    return parse_date(task.actual_start) or parse_date(task.planned_start)


def task_finish(task: Task) -> date | None:
    finish = parse_date(task.actual_finish) or parse_date(task.planned_finish)
    if finish:
        return finish
    start = task_start(task)
    if not start:
        return None
    return start + timedelta(days=task_duration(task) - 1)


def is_not_started(task: Task) -> bool:
    status = task.status.lower()
    return (
        task.progress == 0
        and not parse_date(task.actual_start)
        and not parse_date(task.actual_finish)
        and status in NOT_STARTED_STATUSES
    )


def task_duration(task: Task) -> int:
    return max(math.ceil(task.estimate), 1)


def open_days_between(start: date, finish: date, closed_days: set[date]) -> list[date]:
    if finish < start:
        return []
    current = start
    result: list[date] = []
    while current <= finish:
        if is_open_day(current, closed_days):
            result.append(current)
        current += timedelta(days=1)
    return result


def task_open_days(start: date, duration: int, closed_days: set[date]) -> list[date]:
    first = next_open_day(start, closed_days)
    finish = add_open_days(first, duration, closed_days)
    return open_days_between(first, finish, closed_days)


def reserve_resource(
    occupied: dict[str, set[date]],
    resource: str,
    start: date,
    finish: date | None,
    duration: int,
    closed_days: set[date],
) -> None:
    if not resource:
        return
    if finish and finish >= start:
        days = open_days_between(start, finish, closed_days)
    else:
        days = task_open_days(start, duration, closed_days)
    occupied.setdefault(resource, set()).update(days)


def find_resource_slot(
    occupied: dict[str, set[date]],
    resource: str,
    earliest: date,
    duration: int,
    closed_days: set[date],
) -> tuple[date, date]:
    current = next_open_day(earliest, closed_days)
    for _ in range(370):
        days = task_open_days(current, duration, closed_days)
        busy = occupied.get(resource, set())
        if all(day not in busy for day in days):
            return days[0], days[-1]
        current = next_open_day(current + timedelta(days=1), closed_days)
    raise ValueError(f"Cannot find a free {duration}-day slot for {resource} after {earliest}")


def earliest_task_start(task: Task, closed_days: set[date], today: date) -> tuple[date | None, bool]:
    raw_start = task_start(task)
    shifted = False

    if is_not_started(task):
        min_start = next_open_day(today, closed_days)
        if raw_start is None or raw_start < min_start:
            raw_start = min_start
            shifted = True
        else:
            open_start = next_open_day(raw_start, closed_days)
            shifted = shifted or open_start != raw_start
            raw_start = open_start

    return raw_start, shifted


def fixed_task_schedule(
    task: Task,
    closed_days: set[date],
    today: date,
    team_resources: dict[str, list[str]],
) -> ScheduledTask | None:
    start, shifted = earliest_task_start(task, closed_days, today)
    if not start:
        return None
    finish = parse_date(task.actual_finish) or parse_date(task.planned_finish)
    if not finish or finish < start:
        finish = add_open_days(start, task_duration(task), closed_days)
    return ScheduledTask(start, finish, explicit_executor(task, team_resources), shifted)


def candidate_resources(task: Task, team_resources: dict[str, list[str]]) -> tuple[list[str], str]:
    explicit = explicit_executor(task, team_resources)
    if explicit:
        return [explicit], ""

    role = role_for_task(task)
    resources = team_resources.get(role, [])
    if resources:
        source = normalize_executor(task.executor) or role
        if is_tbd_executor(task.executor):
            source = normalize_executor(task.executor)
        return resources, f"auto from {source}"

    fallback = normalize_executor(task.executor)
    return ([fallback] if fallback and not fallback.startswith("TBD_") else []), ""


def schedule_not_started_task(
    task: Task,
    earliest: date,
    shifted: bool,
    occupied: dict[str, set[date]],
    closed_days: set[date],
    team_resources: dict[str, list[str]],
) -> ScheduledTask:
    duration = task_duration(task)
    resources, note_source = candidate_resources(task, team_resources)
    if not resources:
        finish = add_open_days(earliest, duration, closed_days)
        return ScheduledTask(earliest, finish, "", shifted)

    best: tuple[date, date, int, str] | None = None
    for resource in resources:
        start, finish = find_resource_slot(occupied, resource, earliest, duration, closed_days)
        load = len(occupied.get(resource, set()))
        candidate = (start, finish, load, resource)
        if best is None or candidate < best:
            best = candidate

    assert best is not None
    start, finish, _, resource = best
    reserve_resource(occupied, resource, start, finish, duration, closed_days)
    resource_note = f"Auto-assigned {resource} ({note_source})" if note_source else ""
    return ScheduledTask(start, finish, resource, shifted or start != earliest, resource_note)


def task_schedules(
    tasks: dict[str, Task],
    closed_days: set[date],
    today: date,
    team_resources: dict[str, list[str]],
) -> dict[str, ScheduledTask]:
    schedules: dict[str, ScheduledTask] = {}
    occupied: dict[str, set[date]] = {}

    def task_scope(task_key: str) -> str:
        return task_key.split("/", 1)[0] if "/" in task_key else ""

    for task_id, task in tasks.items():
        if is_not_started(task):
            continue
        scheduled = fixed_task_schedule(task, closed_days, today, team_resources)
        if scheduled:
            schedules[task_id] = scheduled
            reserve_resource(
                occupied,
                scheduled.assignee,
                scheduled.start,
                scheduled.finish,
                task_duration(task),
                closed_days,
            )

    not_started = [
        (task_id, task, *earliest_task_start(task, closed_days, today))
        for task_id, task in tasks.items()
        if is_not_started(task)
    ]
    not_started = [(task_id, task, start, shifted) for task_id, task, start, shifted in not_started if start]

    def schedule_phase(roles: set[str]) -> None:
        phase_items = [
            item
            for item in not_started
            if item[0] not in schedules and (role_for_task(item[1]) in roles if roles else True)
        ]
        for task_id, task, start, shifted in sorted(
            phase_items,
            key=lambda item: (item[2] or date.max, ROLE_ORDER.get(role_for_task(item[1]), 90), item[0]),
        ):
            schedules[task_id] = schedule_not_started_task(
                task,
                start,
                shifted,
                occupied,
                closed_days,
                team_resources,
            )

    schedule_phase({"AN", "BE"})

    be_starts_by_scope: dict[str, list[date]] = {}
    for task_id, task in tasks.items():
        if task_id in schedules and is_not_started(task) and role_for_task(task) == "BE":
            be_starts_by_scope.setdefault(task_scope(task_id), []).append(schedules[task_id].start)

    for task_id, task, start, shifted in sorted(
        [item for item in not_started if item[0] not in schedules and role_for_task(item[1]) == "FE"],
        key=lambda item: (item[2] or date.max, item[0]),
    ):
        be_starts = be_starts_by_scope.get(task_scope(task_id), [])
        fe_min_start = add_open_day_offset(min(be_starts), FE_AFTER_BE_OPEN_DAYS, closed_days) if be_starts else None
        earliest = max(start, fe_min_start) if fe_min_start else start
        schedules[task_id] = schedule_not_started_task(
            task,
            earliest,
            shifted or earliest != start,
            occupied,
            closed_days,
            team_resources,
        )

    schedule_phase({"QA"})
    for task_id, task, start, shifted in sorted(
        [item for item in not_started if item[0] not in schedules],
        key=lambda item: (item[2] or date.max, ROLE_ORDER.get(role_for_task(item[1]), 90), item[0]),
    ):
        schedules[task_id] = schedule_not_started_task(
            task,
            start,
            shifted,
            occupied,
            closed_days,
            team_resources,
            )

    return schedules


def role_color(role: str) -> str:
    return ROLE_COLORS.get(normalize_role(role), "Wheat")


def plantuml_label(value: str) -> str:
    return " ".join(value.replace("[", "").replace("]", "").split())


def role_prefixed_summary(task: Task) -> str:
    role = role_for_task(task)
    summary = task.summary.strip()
    if role not in ROLE_COLORS:
        return plantuml_label(summary)
    bracketed = re.match(r"^\s*\[\s*([^\]]+)\s*\]\s*", summary)
    if bracketed and normalize_role(bracketed.group(1)) == role:
        summary = summary[bracketed.end():].strip()
    else:
        plain = re.match(r"^\s*([^\s:_/\-]+)(?=[\s:_/\-])\s*[:_\-/]?\s*", summary)
        if plain and normalize_role(plain.group(1)) == role:
            summary = summary[plain.end():].strip()
    return plantuml_label(f"{role} {summary}")


def story_type(story: StoryMap, tasks: dict[str, Task]) -> str:
    summary = story.summary.lower()
    fe_keywords = [
        "страница",
        "список",
        "форма",
        "detail",
        "деталь",
        "детальная",
        "ui",
        "frontend",
        "workspace",
        "view",
    ]
    be_keywords = [
        "backend",
        "core",
        "жизненный цикл",
        "жц",
        "api",
        "бд",
        "integration",
        "интеграция",
        "model",
        "модель",
        "контракты",
        "logic",
        "логика",
    ]
    if any(keyword in summary for keyword in fe_keywords):
        return "FE"
    if any(keyword in summary for keyword in be_keywords):
        return "BE"

    counts: dict[str, int] = {}
    for task_id in mapped_task_ids(story, tasks):
        role = role_for_task(tasks[task_id])
        if not role:
            continue
        counts[role] = counts.get(role, 0) + 1
    if counts:
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ordered[0][0]
    return "GEN"


def render_task(task: Task, schedules: dict[str, ScheduledTask]) -> list[str]:
    alias = f"TASK_{to_alias(task.task_id)}"
    scheduled = schedules.get(task.task_id)
    if not scheduled:
        return [f"' Skip task without start date: {task.task_id}"]
    assignee = scheduled.assignee
    assignee_part = f" on {{{assignee}}}" if assignee else ""
    lines = [
        f"[{role_prefixed_summary(task)}] as [{alias}]{assignee_part} starts {fmt_date(scheduled.start)}",
        f"[{alias}] ends {fmt_date(scheduled.finish)}" if scheduled.finish else f"[{alias}] lasts {task_duration(task)} days",
        f"[{alias}] is colored in {role_color(role_for_task(task))}",
        f"[{alias}] is {max(0, min(task.progress, 100))}% completed",
    ]
    if scheduled.shifted:
        lines.append(f"' Shifted not-started task from stale/non-open plan: {task.task_id}")
    if scheduled.resource_note:
        lines.append(f"' {scheduled.resource_note}: {task.task_id}")
    return lines


def mapped_task_ids(story: StoryMap, tasks: dict[str, Task]) -> list[str]:
    references = list(story.replaced_by)
    if story.state == "mixed":
        references.extend(story.residual_virtual_tasks)
    ids: list[str] = []
    for reference in references:
        if reference in tasks:
            ids.append(reference)
        ids.extend(task_id for task_id, task in tasks.items() if task.tracker_key == reference)
    if story.state == "virtual" and not ids:
        ids.extend(task_id for task_id, task in tasks.items() if story.story_id in task.related_stories)
    unique: list[str] = []
    for task_id in ids:
        if task_id not in unique and task_id in tasks and tasks[task_id].status.lower() != "superseded":
            unique.append(task_id)
    return unique


def story_progress(story: StoryMap, tasks: dict[str, Task]) -> int:
    task_ids = mapped_task_ids(story, tasks)
    if not task_ids:
        return 0
    total = 0
    weighted = 0
    for task_id in task_ids:
        task = tasks[task_id]
        estimate = max(task.estimate, 1.0)
        total += estimate
        weighted += estimate * max(0, min(task.progress, 100))
    return round(weighted / total) if total else 0


def story_dates(
    story: StoryMap,
    tasks: dict[str, Task],
    schedules: dict[str, ScheduledTask],
    story_ends: dict[str, date],
    closed_days: set[date],
) -> tuple[date | None, date | None]:
    baseline_start = parse_date(story.baseline_start)
    baseline_finish = add_open_days(baseline_start, max(story.baseline_duration, 1), closed_days) if baseline_start else None
    task_ids = mapped_task_ids(story, tasks)
    starts = [schedules[task_id].start for task_id in task_ids if task_id in schedules]
    finishes = [schedules[task_id].finish for task_id in task_ids if task_id in schedules]
    starts = [item for item in starts if item]
    finishes = [item for item in finishes if item]

    if finishes:
        start_candidates = list(starts)
        if baseline_start:
            start_candidates.append(baseline_start)
        return min(start_candidates) if start_candidates else baseline_start, max(finishes)

    if story.depends_on:
        dep_finishes = [story_ends[dep] for dep in story.depends_on if dep in story_ends]
        if dep_finishes:
            start = next_open_day(max(dep_finishes) + timedelta(days=1), closed_days)
            return start, add_open_days(start, max(story.baseline_duration, 1), closed_days)

    return baseline_start, baseline_finish


def render_story(
    story: StoryMap,
    tasks: dict[str, Task],
    schedules: dict[str, ScheduledTask],
    story_ends: dict[str, date],
    closed_days: set[date],
) -> tuple[list[str], date | None]:
    start, finish = story_dates(story, tasks, schedules, story_ends, closed_days)
    if not start:
        return [f"' Skip story without start date: {story.story_id}"], None
    alias = f"STORY_{to_alias(story.story_id)}"
    label = plantuml_label(f"PLAN {story_type(story, tasks)} {story.summary}")
    progress = story_progress(story, tasks)
    color = "LightSteelBlue" if story.state == "virtual" else "Gainsboro"
    lines = [
        f"[{label}] as [{alias}] starts {fmt_date(start)}",
    ]
    if finish and finish >= start:
        lines.append(f"[{alias}] ends {fmt_date(finish)}")
    else:
        lines.append(f"[{alias}] lasts {max(story.baseline_duration, 1)} days")
    lines.extend(
        [
            f"[{alias}] is colored in {color}",
            f"[{alias}] is {progress}% completed",
        ]
    )
    return lines, finish


def render_feature(
    feature_dir: Path,
    feature_slug: str,
    closed_days: set[date],
    tasks: dict[str, Task] | None = None,
    schedules: dict[str, ScheduledTask] | None = None,
) -> str | None:
    stories = load_story_map(feature_dir)
    tasks = tasks if tasks is not None else load_tasks(feature_dir)
    if not stories:
        return None
    schedules = schedules or {}

    lines = [
        f"' FEATURE: {feature_slug}",
        "' Actual-progress overlay:",
        "' - STORY rows are commander-plan stories actualized by linked execution tasks",
        "' - TASK rows are current execution tasks rendered once, with many-to-many links kept in markdown",
        "",
        "' Story layer",
    ]

    story_ends: dict[str, date] = {}
    for story in stories:
        rendered, finish = render_story(story, tasks, schedules, story_ends, closed_days)
        lines.append(f"' Story {story.story_id}: {story.state}, mapping={story.mapping_mode}")
        lines.extend(rendered)
        lines.append("")
        if finish:
            story_ends[story.story_id] = finish

    active_tasks = [task for task in tasks.values() if task.status.lower() != "superseded"]
    if active_tasks:
        task_order = {task_id: index for index, task_id in enumerate(tasks)}
        lines.append("' Execution task layer")
        for task in sorted(
            active_tasks,
            key=lambda item: (
                schedules[item.task_id].start if item.task_id in schedules else date.max,
                ROLE_ORDER.get(role_for_task(item), 90),
                task_order.get(item.task_id, 0),
            ),
        ):
            lines.extend(render_task(task, schedules))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def prepare_outputs(project_root: Path, quarter_id: str, feature_slugs: list[str] | None = None) -> dict[Path, str]:
    project_root = project_root.resolve()
    if not re.fullmatch(r"\d{4}-Q[1-4]", quarter_id):
        raise ValueError("Неверный идентификатор квартала")
    target_dir = project_root / "planning" / quarter_id / "gantt/includes/actual-progress"
    mapping_path = target_dir.parent.parent / "actual-progress-features.json"
    feature_map = {}
    if mapping_path.exists():
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("features"), dict):
            raise ValueError(f"{mapping_path}: ожидается schema_version=1 и объект features")
        feature_map = payload["features"]
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) for pair in feature_map.items() for value in pair):
            raise ValueError(f"{mapping_path}: неверный slug")
    if feature_slugs is None:
        feature_slugs = sorted({
            path.name for path in (project_root / "features").iterdir()
            if path.is_dir() and (
                (path / "planning/actualization.md").exists() or (path / "execution").exists()
                or list(path.glob("slices/*/execution/tasks.md"))
            )
        } | {path.stem.removeprefix("FEATURE-") for path in target_dir.glob("FEATURE-*.puml")})
        feature_slugs = [slug for slug in feature_slugs if slug not in feature_map.values() or slug in feature_map]
        feature_slugs = sorted(set(feature_slugs) | set(feature_map))
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) for slug in feature_slugs):
        raise ValueError("Неверный slug функциональности")
    sources = [feature_map.get(slug, slug) for slug in feature_slugs]
    if len(set(sources)) != len(sources):
        raise ValueError("Одна функциональность назначена нескольким файлам Ганта")
    closed_days = load_closed_days(project_root, quarter_id)
    team_resources = load_team_resources(project_root)
    feature_tasks: dict[str, dict[str, Task]] = {}
    scoped_tasks: dict[str, Task] = {}
    aliases: set[str] = set()
    for feature_slug in feature_slugs:
        feature_dir = project_root / "features" / feature_map.get(feature_slug, feature_slug)
        if not feature_dir.exists():
            raise ValueError(f"{feature_dir}: функциональность не найдена; проверь actual-progress-features.json")
        stories = load_story_map(feature_dir)
        if not stories:
            raise ValueError(f"{feature_dir}: нет непустой planning/actualization.md; существующий Гант сохранён")
        tasks = load_tasks(feature_dir)
        story_ids = {story.story_id for story in stories}
        for story in stories:
            alias = f"STORY_{to_alias(story.story_id)}"
            if alias in aliases:
                raise ValueError(f"Повторяющийся идентификатор PlantUML: {alias}")
            aliases.add(alias)
            if not parse_date(story.baseline_start) or story.baseline_duration <= 0:
                raise ValueError(f"{feature_dir}: неверные исходные даты истории {story.story_id}")
            references = story.replaced_by + story.residual_virtual_tasks
            for reference in references:
                if reference in tasks and any(task.task_id != reference and task.tracker_key == reference for task in tasks.values()):
                    raise ValueError(f"{feature_dir}: неоднозначная ссылка Task ID/Jira {reference}")
                if reference not in tasks and not any(task.tracker_key == reference for task in tasks.values()):
                    raise ValueError(f"{feature_dir}: история {story.story_id} ссылается на отсутствующую задачу {reference}")
            if story.state in {"materialized", "mixed", "done", "real"} and not mapped_task_ids(story, tasks):
                raise ValueError(f"{feature_dir}: отсутствует состав истории {story.story_id}")
            if any(dependency not in story_ids for dependency in story.depends_on):
                raise ValueError(f"{feature_dir}: неизвестная зависимость истории {story.story_id}")
        if not tasks and ((feature_dir / "execution/actual-progress.md").exists() or list(feature_dir.glob("execution/tasks/*.md"))):
            raise ValueError(f"{feature_dir}: индивидуальные карточки и сводка не заменяют execution/tasks.md")
        for task in tasks.values():
            alias = f"TASK_{to_alias(task.task_id)}"
            if not to_alias(task.task_id) or alias in aliases:
                raise ValueError(f"Повторяющийся или пустой идентификатор PlantUML: {alias}")
            aliases.add(alias)
            if any(story_id not in story_ids for story_id in task.related_stories):
                raise ValueError(f"{feature_dir}: задача {task.task_id} ссылается на неизвестную историю")
        feature_tasks[feature_slug] = tasks
        for task_id, task in tasks.items():
            scoped_tasks[f"{feature_slug}/{task_id}"] = task

    scoped_schedules = task_schedules(scoped_tasks, closed_days, harness_today(), team_resources)

    outputs: dict[Path, str] = {}
    for feature_slug in feature_slugs:
        feature_dir = project_root / "features" / feature_map.get(feature_slug, feature_slug)
        target = target_dir / f"FEATURE-{feature_slug}.puml"
        tasks = feature_tasks.get(feature_slug, {})
        schedules = {
            task_id: scoped_schedules[f"{feature_slug}/{task_id}"]
            for task_id in tasks
            if f"{feature_slug}/{task_id}" in scoped_schedules
        }
        if any(task.status.lower() != "superseded" and task_id not in schedules for task_id, task in tasks.items()):
            raise ValueError(f"{feature_dir}: не для каждой задачи определена дата начала; Гант сохранён")
        content = render_feature(feature_dir, feature_slug, closed_days, tasks, schedules)
        if content is None:
            raise ValueError(f"{feature_dir}: источники изменились во время генерации")
        outputs[target] = content
    return outputs


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_outputs(outputs: dict[Path, str]) -> None:
    previous = {path: path.read_bytes() if path.exists() else None for path in outputs}
    written: list[Path] = []
    try:
        for path, content in outputs.items():
            if previous[path] == content.encode("utf-8"):
                continue
            atomic_write(path, content.encode("utf-8"))
            written.append(path)
    except OSError:
        for path in reversed(written):
            if previous[path] is None:
                path.unlink()
            else:
                atomic_write(path, previous[path])
        raise


def main() -> int:
    if len(sys.argv) < 3:
        usage()
        return 1
    outputs = prepare_outputs(Path(sys.argv[1]), sys.argv[2], sys.argv[3:] or None)
    publish_outputs(outputs)
    for path in outputs:
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
