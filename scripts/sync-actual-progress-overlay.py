#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import os
import re
import sys


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
    summary: str
    kind: str
    role: str
    estimate: int
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
    path = project_root / ".workflow/team.md"
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
            continue
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
    if role and normalized not in team_resources.get(role, []):
        return ""
    return normalized


def load_tasks(feature_dir: Path) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    for path in sorted(feature_dir.glob("slices/*/execution/tasks.md")):
        rows = first_table_with(path, "Jira")
        for row in rows:
            task_id = clean_cell(row.get("Jira", ""))
            if not task_id:
                continue
            status = clean_cell(row.get("Status", "planned"))
            progress_value = row.get("Progress %", "")
            progress = parse_int(progress_value, progress_from_status(status)) if progress_value else progress_from_status(status)
            tasks[task_id] = Task(
                task_id=task_id,
                summary=clean_cell(row.get("Summary", task_id)),
                kind=clean_cell(row.get("Kind", "virtual")).lower(),
                role=clean_cell(row.get("Role", "")),
                estimate=parse_int(row.get("Estimate (дн)", ""), 1),
                executor=clean_cell(row.get("Executor", "")),
                planned_start=clean_cell(row.get("Planned Start", "")),
                planned_finish=clean_cell(row.get("Planned Finish", "")),
                actual_start=clean_cell(row.get("Actual Start", "")),
                actual_finish=clean_cell(row.get("Actual Finish", "")),
                status=status,
                progress=progress,
                related_stories=split_list(row.get("Related Stories", "")),
            )
    for path in sorted(feature_dir.glob("slices/*/execution/task-candidates.md")):
        rows = first_table_with(path, "Candidate ID")
        russian = False
        if not rows:
            rows = first_table_with(path, "Идентификатор")
            russian = True
        for row in rows:
            task_id = clean_cell(row.get("Идентификатор" if russian else "Candidate ID", ""))
            if not task_id:
                continue
            status = clean_cell(row.get("Статус" if russian else "Status", "proposed"))
            tasks[task_id] = Task(
                task_id=task_id,
                summary=clean_cell(row.get("Краткое описание" if russian else "Summary", task_id)),
                kind="candidate",
                role=clean_cell(row.get("Роль" if russian else "Role", "")),
                estimate=parse_int(row.get("Оценка (дн)" if russian else "Estimate (дн)", ""), 1),
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
    return start + timedelta(days=max(task.estimate - 1, 0))


def is_not_started(task: Task) -> bool:
    status = task.status.lower()
    return (
        task.progress == 0
        and not parse_date(task.actual_start)
        and not parse_date(task.actual_finish)
        and status in NOT_STARTED_STATUSES
    )


def task_duration(task: Task) -> int:
    return max(task.estimate, 1)


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
        f"[{task.summary}] as [{alias}]{assignee_part} starts {fmt_date(scheduled.start)}",
        f"[{alias}] ends {fmt_date(scheduled.finish)}" if scheduled.finish else f"[{alias}] lasts {max(task.estimate, 1)} days",
        f"[{alias}] is colored in {role_color(role_for_task(task))}",
        f"[{alias}] is {max(0, min(task.progress, 100))}% completed",
    ]
    if scheduled.shifted:
        lines.append(f"' Shifted not-started task from stale/non-open plan: {task.task_id}")
    if scheduled.resource_note:
        lines.append(f"' {scheduled.resource_note}: {task.task_id}")
    return lines


def mapped_task_ids(story: StoryMap, tasks: dict[str, Task]) -> list[str]:
    ids = list(story.replaced_by)
    if story.state == "mixed":
        ids.extend(story.residual_virtual_tasks)
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
        estimate = max(task.estimate, 1)
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
    label = f"PLAN {story_type(story, tasks)} {story.summary}"
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


def main() -> int:
    if len(sys.argv) < 3:
        usage()
        return 1

    project_root = Path(sys.argv[1])
    quarter_id = sys.argv[2]
    closed_days = load_closed_days(project_root, quarter_id)
    team_resources = load_team_resources(project_root)
    if len(sys.argv) > 3:
        feature_slugs = sys.argv[3:]
    else:
        feature_slugs = [path.name for path in sorted((project_root / "features").iterdir()) if path.is_dir()]

    feature_tasks: dict[str, dict[str, Task]] = {}
    scoped_tasks: dict[str, Task] = {}
    for feature_slug in feature_slugs:
        feature_dir = project_root / "features" / feature_slug
        if not feature_dir.exists():
            continue
        tasks = load_tasks(feature_dir)
        feature_tasks[feature_slug] = tasks
        for task_id, task in tasks.items():
            scoped_tasks[f"{feature_slug}/{task_id}"] = task

    scoped_schedules = task_schedules(scoped_tasks, closed_days, harness_today(), team_resources)

    target_dir = project_root / "planning" / quarter_id / "gantt/includes/actual-progress"
    target_dir.mkdir(parents=True, exist_ok=True)

    for feature_slug in feature_slugs:
        feature_dir = project_root / "features" / feature_slug
        if not feature_dir.exists():
            print(f"Skip missing feature: {feature_slug}")
            continue
        target = target_dir / f"FEATURE-{feature_slug}.puml"
        tasks = feature_tasks.get(feature_slug, {})
        schedules = {
            task_id: scoped_schedules[f"{feature_slug}/{task_id}"]
            for task_id in tasks
            if f"{feature_slug}/{task_id}" in scoped_schedules
        }
        content = render_feature(feature_dir, feature_slug, closed_days, tasks, schedules)
        if content is None:
            if target.exists():
                target.unlink()
                print(f"Removed stale {target}")
            else:
                print(f"Skip feature without actualization map: {feature_slug}")
            continue
        target.write_text(content, encoding="utf-8")
        print(f"Wrote {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
