#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from actualization_baseline import baseline_rows
from actual_progress_scope import load_forecast_scope
from role_plan_baselines import RoleBaseline
from workspace_paths import approved_plans_path, team_path
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
DONE_STATUSES = {"done", "closed", "complete", "completed"}
EXCLUDED_STATUSES = {"superseded", "cancelled", "canceled"}
FE_AFTER_BE_OPEN_DAYS = 3
QA_BEFORE_FE_FINISH_OPEN_DAYS = 1


@dataclass
class StoryMap:
    story_id: str
    summary: str
    baseline_start: str
    baseline_duration: int | None
    state: str
    mapping_mode: str
    replaced_by: list[str]
    residual_virtual_tasks: list[str]
    depends_on: list[str]
    baseline_state: str | None = "present"
    follow_up_quarter: str = ""
    role: str = ""
    additional_tasks: list[str] = field(default_factory=list)
    follow_up_notes: list[str] = field(default_factory=list)


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
    completed_by: str = ""


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
    rows = [row for table in parse_tables(path) for row in table if "Story ID" in row]
    result: list[StoryMap] = []
    follow_ups: list[StoryMap] = []
    story_ids: set[str] = set()
    for row in rows:
        story_id = clean_cell(row.get("Story ID", ""))
        if not split_list(story_id):
            raise ValueError(f"{path}: требуется Story ID")
        duration = clean_cell(row.get("Baseline Duration (дн)", ""))
        baseline_start = clean_cell(row.get("Baseline Start", ""))
        follow_up_quarter = ""
        baseline_fields = {"Baseline Start", "Baseline Duration (дн)", "Baseline State"}
        if "Quarter" in row and not baseline_fields.intersection(row):
            required = {"Summary", "Actualization State", "Mapping Mode", "Replaced By"}
            if not required.issubset(row) or not row["Summary"] or row["Mapping Mode"] not in {"explicit", "inferred"}:
                raise ValueError(f"{path}: {story_id}: неполная карта follow-up")
            follow_up_quarter = clean_cell(row["Quarter"])
            if not re.fullmatch(r"\d{4}-Q[1-4]", follow_up_quarter):
                raise ValueError(f"{path}: {story_id}: неверный Quarter в follow-up")
            baseline_state = None
        else:
            if len(split_list(story_id)) != 1:
                raise ValueError(f"{path}: определение должно содержать один Story ID: {story_id}")
            if story_id in story_ids:
                raise ValueError(f"{path}: Duplicate Story ID: {story_id}")
            story_ids.add(story_id)
            baseline_state = clean_cell(row.get("Baseline State", "present")).lower()
            if baseline_state not in {"present", "absent"}:
                raise ValueError(f"{path}: Baseline State должен быть present или absent")
        if baseline_state == "absent" and (baseline_start or duration):
            raise ValueError(f"{path}: absent требует пустых Baseline Start и Baseline Duration (дн)")
        if baseline_state == "present" and not re.fullmatch(r"[1-9]\d*", duration):
            raise ValueError(f"{path}: требуется положительная целая Baseline Duration (дн)")
        if clean_cell(row.get("Actualization State", "")).lower() not in {"virtual", "mixed", "materialized", "done", "real"}:
            raise ValueError(f"{path}: неизвестный Actualization State")
        role = normalize_role(row.get("Role", ""))
        if role and role not in ROLE_COLORS:
            raise ValueError(f"{path}: {story_id}: неизвестный Role")
        (follow_ups if follow_up_quarter else result).append(
            StoryMap(
                story_id=story_id,
                summary=clean_cell(row.get("Summary", story_id)),
                baseline_start=baseline_start,
                baseline_duration=int(duration) if baseline_state == "present" else None,
                state=clean_cell(row.get("Actualization State", "virtual")).lower(),
                mapping_mode=clean_cell(row.get("Mapping Mode", "explicit")).lower(),
                replaced_by=split_list(row.get("Replaced By", "")),
                residual_virtual_tasks=split_list(row.get("Residual Virtual Tasks", "")),
                depends_on=split_list(row.get("Depends On", "")),
                baseline_state=baseline_state,
                follow_up_quarter=follow_up_quarter,
                role=role,
            )
        )
    by_id = {story.story_id: story for story in result}
    for follow_up in follow_ups:
        references = split_list(follow_up.story_id)
        if len(references) == 1 and references[0] not in by_id:
            by_id[follow_up.story_id] = follow_up
            result.append(follow_up)
            continue
        for reference in references:
            if reference not in by_id:
                raise ValueError(f"{path}: follow-up ссылается на неизвестную историю: {reference}")
            story = by_id[reference]
            if story.follow_up_quarter:
                raise ValueError(f"{path}: Duplicate Story ID: {reference}")
            story.additional_tasks = list(dict.fromkeys(story.additional_tasks + follow_up.replaced_by + follow_up.residual_virtual_tasks))
            story.follow_up_notes.append(f"{follow_up.follow_up_quarter}: {follow_up.summary}; tasks={', '.join(follow_up.replaced_by)}")
    return result


def progress_from_status(status: str) -> int:
    status = status.lower()
    if status in DONE_STATUSES:
        return 100
    if status in {"planned", "todo", "open", "backlog"} | EXCLUDED_STATUSES:
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


def validate_estimate(value: str, path: Path, task_id: str) -> None:
    estimate = clean_cell(value).replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", estimate) or not math.isfinite(float(estimate)) or float(estimate) <= 0:
        raise ValueError(f"{path}: {task_id}: требуется положительная числовая оценка; уточни оценку у аналитика")


def validate_task_row(row: dict[str, str], path: Path) -> None:
    for column in ("Summary", "Kind", "Role", "Status", "Progress %"):
        if not clean_cell(row.get(column, "")):
            raise ValueError(f"{path}: требуется заполненное поле {column}")
    if normalize_role(row["Role"]) not in ROLE_COLORS or row["Kind"].lower() not in {"real", "virtual"}:
        raise ValueError(f"{path}: неизвестный Role или Kind")
    task_id = clean_cell(row.get("Task ID", "")) or f"{row.get('Jira', '')}/{row.get('Role', '')}"
    validate_estimate(row.get("Estimate (дн)", ""), path, task_id)
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
    if int(progress) > 0 and not (parse_date(row.get("Actual Start", "")) or parse_date(row.get("Planned Start", "")) or row.get("Completed By", "")):
        raise ValueError(f"{path}: для начатой задачи требуется дата начала из источника")


def validate_completion_bound(row: dict[str, str], path: Path) -> str:
    value = clean_cell(row.get("Completed By", ""))
    if not value:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{path}: Completed By требует дату YYYY-MM-DD")
    boundary = parse_date(value)
    if not boundary or clean_cell(row.get("Status", "")).lower() not in DONE_STATUSES or clean_cell(row.get("Progress %", "")) != "100":
        raise ValueError(f"{path}: Completed By требует дату YYYY-MM-DD и завершённый Status / 100%")
    for column in ("Actual Start", "Actual Finish"):
        raw = clean_cell(row.get(column, ""))
        actual = parse_date(raw)
        if raw and not actual:
            raise ValueError(f"{path}: неверная дата {column}")
        if actual and actual > boundary:
            raise ValueError(f"{path}: {column} позже Completed By")
    start = parse_date(row.get("Actual Start", ""))
    finish = parse_date(row.get("Actual Finish", ""))
    if start and finish and finish < start:
        raise ValueError(f"{path}: окончание Actual раньше начала")
    return value


def completion_bound_only(task: Task) -> bool:
    return bool(task.completed_by and not (task.actual_start and task.actual_finish))


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
            completed_by = validate_completion_bound(row, path)
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
            legacy_key = re.fullmatch(r"([^/]+)/(AN|BE|FE|QA)", tracker_key, re.IGNORECASE)
            if legacy_key:
                if legacy_key.group(2).upper() != normalized_role:
                    raise ValueError(f"{path}: роль в Jira {tracker_key} не совпадает с Role {role}")
                tracker_key = legacy_key.group(1)
            task_id = explicit_id or (f"{tracker_key}/{normalized_role}" if (kind == "real" or legacy_key) and normalized_role in ROLE_COLORS else tracker_key)
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
                completed_by=completed_by,
            )
            if role_for_task(tasks[task_id]) == "QA":
                validate_estimate(row.get("Estimate (дн)", ""), path, task_id)
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
            if role_for_task(tasks[task_id]) == "QA":
                validate_estimate(row.get("Оценка (дн)" if russian else "Estimate (дн)", ""), path, task_id)
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
    tasks = {task_id: task for task_id, task in tasks.items()
             if task.kind != "candidate" and task.status.lower() not in EXCLUDED_STATUSES
             and not completion_bound_only(task)}
    schedules: dict[str, ScheduledTask] = {}
    occupied: dict[str, set[date]] = {}

    def task_scope(task_key: str) -> str:
        return task_key.removesuffix(tasks[task_key].task_id).rstrip("/")

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

    for task_id, task, start, shifted in sorted(
        [item for item in not_started if item[0] not in schedules and role_for_task(item[1]) == "QA"],
        key=lambda item: (item[2] or date.max, item[0]),
    ):
        feature_schedules = [
            (item, schedules[item_id]) for item_id, item in tasks.items()
            if item_id in schedules and task_scope(item_id) == task_scope(task_id)
            and item.status.lower() not in EXCLUDED_STATUSES and role_for_task(item) != "QA"
        ]
        frontend = [scheduled for item, scheduled in feature_schedules if role_for_task(item) == "FE" and scheduled.finish]
        if frontend:
            first = min(frontend, key=lambda scheduled: scheduled.finish)
            target = first.finish
            for _ in range(QA_BEFORE_FE_FINISH_OPEN_DAYS):
                target -= timedelta(days=1)
                while not is_open_day(target, closed_days):
                    target -= timedelta(days=1)
            target = max(first.start, target)
        else:
            finishes = [scheduled.finish for _, scheduled in feature_schedules if scheduled.finish]
            if not finishes:
                continue
            target = next_open_day(min(finishes) + timedelta(days=1), closed_days)
        earliest = max(start, target)
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
    if role == "QA":
        summary = plantuml_label(summary)
        summary = re.sub(r"^QA\s+", "", summary, flags=re.IGNORECASE)
        if task.tracker_key:
            summary = re.sub(
                rf"^{re.escape(task.tracker_key)}(?:/(?:AN|BE|FE|QA))?(?:\s*[:\-]\s*|\s+)",
                "", summary, flags=re.IGNORECASE,
            )
    if role not in ROLE_COLORS:
        return plantuml_label(summary)
    prefix_roles = set(ROLE_COLORS) if role == "QA" else {role}
    bracketed = re.match(r"^\s*\[\s*([^\]]+)\s*\]\s*", summary)
    if bracketed and normalize_role(bracketed.group(1)) in prefix_roles:
        summary = summary[bracketed.end():].strip()
    else:
        plain = re.match(r"^\s*([^\s:_/\-]+)(?=[\s:_/\-])\s*[:_\-/]?\s*", summary)
        if plain and normalize_role(plain.group(1)) in prefix_roles:
            summary = summary[plain.end():].strip()
    return plantuml_label(f"{role} {summary}")


def story_type(story: StoryMap, tasks: dict[str, Task]) -> str:
    if story.role:
        return story.role
    suffix = re.search(r"(?:^|[-_/])(AN|BE|FE|QA)$", story.story_id, re.IGNORECASE)
    if suffix:
        return suffix.group(1).upper()
    prefix = normalize_role(re.split(r"[\s:]+", story.summary.strip("[] "), maxsplit=1)[0])
    if prefix in ROLE_COLORS:
        return prefix
    roles = {role_for_task(tasks[task_id]) for task_id in mapped_task_ids(story, tasks)
             if tasks[task_id].kind != "candidate"}
    if len(roles) > 1:
        roles.discard("QA")
    if len(roles) == 1 and roles <= ROLE_COLORS.keys():
        return next(iter(roles))
    return "GEN"


def render_task(task: Task, schedules: dict[str, ScheduledTask]) -> list[str]:
    if task.status.lower() in EXCLUDED_STATUSES:
        return [f"' Excluded task: {task.task_id}; status={task.status}; {plantuml_label(task.summary)}"]
    if task.kind == "candidate":
        return [f"' Candidate {task.task_id}: {task.status}; not scheduled"]
    alias = f"TASK_{to_alias(task.task_id)}"
    if completion_bound_only(task):
        label = plantuml_label(f"{role_prefixed_summary(task)} (100%; завершено к {task.completed_by}; точный интервал неизвестен)")
        return [
            f"' Completion bound, not an actual date: {task.task_id}; completed_by={task.completed_by}",
            f"[{label}] as [{alias}] happens at {fmt_date(parse_date(task.completed_by))}",
            f"[{alias}] is colored in {role_color(role_for_task(task))}",
        ]
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


def mapped_task_ids(story: StoryMap, tasks: dict[str, Task], include_excluded: bool = False) -> list[str]:
    references = story.replaced_by + story.additional_tasks
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
        if task_id not in unique and task_id in tasks and (include_excluded or tasks[task_id].status.lower() not in EXCLUDED_STATUSES):
            unique.append(task_id)
    return unique


def role_task_ids(role: str, tasks: dict[str, Task]) -> list[str]:
    return [task_id for task_id, task in tasks.items() if role_for_task(task) == role
            and task.kind != "candidate" and task.status.lower() not in EXCLUDED_STATUSES]


def plan_task_ids(story: StoryMap, tasks: dict[str, Task]) -> list[str]:
    role = story_type(story, tasks)
    if role in ROLE_COLORS:
        return role_task_ids(role, tasks)
    return [task_id for task_id in mapped_task_ids(story, tasks)
            if tasks[task_id].kind != "candidate" and role_for_task(tasks[task_id]) != "QA"]


def story_progress(story: StoryMap, tasks: dict[str, Task]) -> int:
    return task_progress(plan_task_ids(story, tasks), tasks)


def task_progress(task_ids: list[str], tasks: dict[str, Task]) -> int:
    if not task_ids:
        return 0
    total = 0
    weighted = 0
    for task_id in task_ids:
        task = tasks[task_id]
        estimate = task.estimate
        if not math.isfinite(estimate) or estimate <= 0:
            raise ValueError(f"{task_id}: требуется положительная оценка для расчёта прогресса")
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
    if story.baseline_duration is None:
        return None, None
    task_ids = plan_task_ids(story, tasks)
    actual_starts = [parse_date(tasks[task_id].actual_start) for task_id in task_ids if tasks[task_id].actual_start]
    starts = [schedules[task_id].start for task_id in task_ids if task_id in schedules]
    start = min(actual_starts or starts, default=baseline_start)
    return start, add_open_days(start, story.baseline_duration, closed_days) if start else None


def render_story(
    story: StoryMap,
    tasks: dict[str, Task],
    schedules: dict[str, ScheduledTask],
    story_ends: dict[str, date],
    closed_days: set[date],
) -> tuple[list[str], date | None]:
    if story.baseline_state != "present":
        task_ids = plan_task_ids(story, tasks)
        finishes = [schedules[task_id].finish for task_id in task_ids if task_id in schedules and schedules[task_id].finish]
        label = f"Follow-up {story.follow_up_quarter}; baseline not declared" if story.follow_up_quarter else "No approved baseline"
        return [
            f"' {label}: {story.story_id}; progress={story_progress(story, tasks)}%; tasks={', '.join(task_ids)}",
        ], max(finishes, default=None)
    start, finish = story_dates(story, tasks, schedules, story_ends, closed_days)
    if not start:
        return [f"' Skip story without start date: {story.story_id}"], None
    alias = f"STORY_{to_alias(story.story_id)}"
    label = plantuml_label(f"PLAN {story_type(story, tasks)} {story.summary}")
    progress = story_progress(story, tasks)
    color = "Gainsboro"
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


def render_role_baseline(baseline: RoleBaseline, feature_slug: str, tasks: dict[str, Task],
                         schedules: dict[str, ScheduledTask], closed_days: set[date]) -> list[str]:
    task_ids = role_task_ids(baseline.role, tasks)
    actual_starts = [parse_date(tasks[task_id].actual_start) for task_id in task_ids if tasks[task_id].actual_start]
    forecast_starts = [schedules[task_id].start for task_id in task_ids if task_id in schedules]
    starts = actual_starts or forecast_starts
    start = min(starts) if starts else baseline.start
    finish = add_open_days(start, baseline.duration, closed_days) if starts else baseline.finish
    alias = f"PLAN_{to_alias(feature_slug)}_{baseline.role}"
    source_label = "квартальный план" if baseline.view == "quarter-plan" else "командирский план"
    label = plantuml_label(f"PLAN {baseline.role} {feature_slug} ({source_label})")
    return [
        f"' Role baseline: {baseline.path}#{baseline.alias}; duration={baseline.duration} working days",
        f"' Decision source: {baseline.decision_source}; tasks={', '.join(task_ids)}",
        f"[{label}] as [{alias}] starts {fmt_date(start)}",
        f"[{alias}] ends {fmt_date(finish)}",
        f"[{alias}] is colored in Gainsboro",
        f"[{alias}] is {task_progress(task_ids, tasks)}% completed",
        "",
    ]


def render_feature(
    feature_dir: Path,
    feature_slug: str,
    closed_days: set[date],
    tasks: dict[str, Task] | None = None,
    schedules: dict[str, ScheduledTask] | None = None,
    role_baselines: list[RoleBaseline] | None = None,
) -> str | None:
    stories = load_story_map(feature_dir)
    tasks = tasks if tasks is not None else load_tasks(feature_dir)
    if not stories:
        return None
    schedules = schedules or {}

    lines = [
        f"' FEATURE: {feature_slug}",
        "' Actual-progress overlay:",
        "' - STORY bars require a recorded baseline; absent baselines keep task links and progress in comments only",
        "' - TASK rows are current execution tasks rendered once, with many-to-many links kept in markdown",
        "",
        "' Story layer",
    ]

    story_ends: dict[str, date] = {}
    for story in stories:
        lines.append(f"' Story {story.story_id}: {story.state}, mapping={story.mapping_mode}")
        lines.extend(f"' Follow-up link: {note}" for note in story.follow_up_notes)
        if role_baselines:
            lines.append(f"' Legacy mapping retained; PLAN replaced by explicit feature-role comparison; tasks={', '.join(mapped_task_ids(story, tasks, include_excluded=True))}")
            continue
        rendered, finish = render_story(story, tasks, schedules, story_ends, closed_days)
        if story_type(story, tasks) == "GEN":
            lines.append("' Legacy mixed-role baseline: role is unresolved; only recorded non-QA links are used")
        lines.extend(rendered)
        lines.append("")
        if finish:
            story_ends[story.story_id] = finish

    if role_baselines:
        lines.extend(["", "' Feature-role comparison layer"])
        for baseline in role_baselines:
            lines.extend(render_role_baseline(baseline, feature_slug, tasks, schedules, closed_days))

    active_tasks = list(tasks.values())
    if active_tasks:
        task_order = {task_id: index for index, task_id in enumerate(tasks)}
        lines.append("' Execution task layer")
        sorted_tasks = sorted(
            active_tasks,
            key=lambda item: (
                schedules[item.task_id].start if item.task_id in schedules else date.max,
                ROLE_ORDER.get(role_for_task(item), 90),
                task_order.get(item.task_id, 0),
            ),
        )
        for task in sorted_tasks:
            lines.extend(render_task(task, schedules))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def prepare_outputs(project_root: Path, quarter_id: str, feature_slugs: list[str] | None = None) -> dict[Path, str]:
    project_root = project_root.resolve()
    if not re.fullmatch(r"\d{4}-Q[1-4]", quarter_id):
        raise ValueError("Неверный идентификатор квартала")
    target_dir = project_root / "planning" / quarter_id / "gantt/includes/actual-progress"
    scope = load_forecast_scope(project_root, quarter_id)
    feature_map = scope.features
    if feature_slugs is None:
        feature_slugs = sorted({
            path.name for path in (project_root / "features").iterdir()
            if path.is_dir() and (
                (path / "planning/actualization.md").exists() or (path / "execution").exists()
                or list(path.glob("slices/*/execution/tasks.md"))
            )
        } | {path.stem.removeprefix("FEATURE-") for path in target_dir.glob("FEATURE-*.puml")})
        feature_slugs = [slug for slug in feature_slugs if slug not in feature_map.values() or slug in feature_map]
    feature_slugs = sorted(set(feature_slugs) | set(feature_map) | set(scope.exclusions) | set(scope.preserved) | set(scope.role_baselines))
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) for slug in feature_slugs):
        raise ValueError("Неверный slug функциональности")
    sources = [feature_map.get(slug, slug) for slug in feature_slugs]
    if len(set(sources)) != len(sources):
        raise ValueError("Одна функциональность назначена нескольким файлам Ганта")
    feature_slugs = [slug for slug in feature_slugs if slug not in scope.exclusions and slug not in scope.preserved]
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
        actualization_path = feature_dir / "planning/actualization.md"
        relative_map = actualization_path.relative_to(project_root).as_posix()
        for snapshot_path in approved_plans_path(project_root).glob("*.json"):
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            expected = snapshot.get("actualization_baseline", {}).get(relative_map)
            if expected is not None and baseline_rows(actualization_path) != expected:
                raise ValueError(f"{actualization_path}: approved actualization baseline was modified")
        tasks = load_tasks(feature_dir)
        for baseline in scope.role_baselines.get(feature_slug, []):
            alias = f"PLAN_{to_alias(feature_slug)}_{baseline.role}"
            if alias in aliases:
                raise ValueError(f"Повторяющийся идентификатор PlantUML: {alias}")
            aliases.add(alias)
        story_ids = {story.story_id for story in stories}
        for story in stories:
            alias = f"STORY_{to_alias(story.story_id)}"
            if alias in aliases:
                raise ValueError(f"Повторяющийся идентификатор PlantUML: {alias}")
            aliases.add(alias)
            if story.baseline_state == "present" and (not parse_date(story.baseline_start) or not story.baseline_duration):
                raise ValueError(f"{feature_dir}: неверные исходные даты истории {story.story_id}")
            references = story.replaced_by + story.residual_virtual_tasks + story.additional_tasks
            for reference in references:
                if reference in tasks and any(task.task_id != reference and task.tracker_key == reference for task in tasks.values()):
                    raise ValueError(f"{feature_dir}: неоднозначная ссылка Task ID/Jira {reference}")
                if reference not in tasks and not any(task.tracker_key == reference for task in tasks.values()):
                    raise ValueError(f"{feature_dir}: история {story.story_id} ссылается на отсутствующую задачу {reference}")
            known_excluded = any(tasks[task_id].status.lower() in EXCLUDED_STATUSES
                                 for task_id in mapped_task_ids(story, tasks, include_excluded=True))
            if (story.baseline_state != "present" or story.state in {"materialized", "mixed", "done", "real"}) and not plan_task_ids(story, tasks) and not known_excluded:
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
            unknown_stories = [story_id for story_id in task.related_stories if story_id not in story_ids]
            if unknown_stories:
                raise ValueError(f"{feature_dir}: задача {task.task_id} ссылается на неизвестную историю: {', '.join(unknown_stories)}")
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
        if any(task.kind != "candidate" and task.status.lower() not in EXCLUDED_STATUSES and not completion_bound_only(task)
               and task_id not in schedules for task_id, task in tasks.items()):
            raise ValueError(f"{feature_dir}: не для каждой задачи определена дата начала; Гант сохранён")
        content = render_feature(feature_dir, feature_slug, closed_days, tasks, schedules, scope.role_baselines.get(feature_slug))
        if content is None:
            raise ValueError(f"{feature_dir}: источники изменились во время генерации")
        outputs[target] = content
    if scope.role_baselines and load_forecast_scope(project_root, quarter_id) != scope:
        raise ValueError("Ролевой PLAN изменился во время генерации")
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
