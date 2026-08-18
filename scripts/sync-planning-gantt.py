#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from workspace_paths import team_path


ROLE_COLORS = {"AN": "LightGreen", "BE": "LightBlue", "FE": "LightCoral", "QA": "Gold"}
ROLE_DEFAULT_EFFICIENCY = {"AN": 0.80, "BE": 0.70, "FE": 0.65, "QA": 0.80}
ROLE_ORDER = ("AN", "BE", "FE", "QA")
QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
INTERVAL_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


@dataclass
class Story:
    story_id: str
    role: str
    summary: str
    effort: float
    max_parallelism: int
    efficiency: float
    depends_on: list[str]
    not_before: date | None


@dataclass
class Scheduled:
    story: Story
    start: date
    finish: date
    resources: list[str]
    duration: int


def clean(value: str) -> str:
    return value.strip().strip("`")


def parse_date(value: str) -> date | None:
    value = clean(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def quarter_bounds(quarter_id: str) -> tuple[date, date]:
    match = QUARTER_RE.match(quarter_id)
    if not match:
        raise ValueError(f"Invalid quarter id: {quarter_id}")
    year, quarter = int(match.group(1)), int(match.group(2))
    start_month = (quarter - 1) * 3 + 1
    start = date(year, start_month, 1)
    end = date(year + 1, 1, 1) - timedelta(days=1) if quarter == 4 else date(year, start_month + 3, 1) - timedelta(days=1)
    return start, end


def markdown_table(path: Path, required: set[str]) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = [clean(cell) for cell in line.strip().strip("|").split("|")]
        if not required.issubset(set(header)):
            continue
        rows: list[dict[str, str]] = []
        for row_line in lines[index + 2 :]:
            if not row_line.lstrip().startswith("|"):
                break
            cells = [clean(cell) for cell in row_line.strip().strip("|").split("|")]
            if len(cells) < len(header):
                continue
            rows.append(dict(zip(header, cells)))
        return rows
    return []


def load_features(gantt_dir: Path) -> list[str]:
    order = gantt_dir / "order.txt"
    if not order.exists():
        return []
    return [line.strip() for line in order.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def load_stories(project: Path, feature: str) -> list[Story]:
    path = project / "features" / feature / "planning/estimates.md"
    if not path.exists():
        return []
    required = {"Story ID", "Role", "Summary", "Agreed effort, дн", "Max parallelism", "Efficiency"}
    rows = markdown_table(path, required)
    stories: list[Story] = []
    for row in rows:
        role = row["Role"].upper()
        if role not in ROLE_ORDER:
            continue
        try:
            effort = float(row["Agreed effort, дн"])
            parallelism = int(row["Max parallelism"])
            efficiency = float(row["Efficiency"] or ROLE_DEFAULT_EFFICIENCY[role])
        except ValueError:
            continue
        if effort <= 0:
            continue
        stories.append(
            Story(
                story_id=row["Story ID"],
                role=role,
                summary=row["Summary"],
                effort=effort,
                max_parallelism=max(1, parallelism),
                efficiency=efficiency,
                depends_on=[clean(item) for item in row.get("Depends On", "").split(",") if clean(item) and "+" not in item],
                not_before=parse_date(row.get("Not before", "")),
            )
        )
    return sorted(stories, key=lambda item: ROLE_ORDER.index(item.role))


def load_closed_days(gantt_dir: Path) -> set[date]:
    result: set[date] = set()
    path = gantt_dir / "closed-days.txt"
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip().replace("/", "-")
        parsed = parse_date(value)
        if parsed:
            result.add(parsed)
    return result


def load_team(project: Path) -> tuple[dict[str, list[str]], dict[str, float], dict[str, set[date]]]:
    path = team_path(project)
    resources = {role: [] for role in ROLE_ORDER}
    coefficients: dict[str, float] = {}
    closed: dict[str, set[date]] = {}
    if not path.exists():
        return resources, coefficients, closed
    for row in markdown_table(path, {"Role", "Resources"}):
        role = row["Role"].upper()
        if role in resources:
            resources[role] = [clean(item) for item in row["Resources"].split(",") if clean(item)]
    for row in markdown_table(path, {"Resource", "Personal coefficient", "Closed intervals"}):
        resource = row["Resource"]
        if row["Personal coefficient"]:
            try:
                coefficients[resource] = float(row["Personal coefficient"])
            except ValueError:
                pass
        days: set[date] = set()
        for start_text, finish_text in INTERVAL_RE.findall(row["Closed intervals"]):
            current = date.fromisoformat(start_text)
            finish = date.fromisoformat(finish_text)
            while current <= finish:
                days.add(current)
                current += timedelta(days=1)
        closed[resource] = days
    return resources, coefficients, closed


def is_open(value: date, globally_closed: set[date]) -> bool:
    return value.weekday() < 5 and value not in globally_closed


def add_open_days(start: date, offset: int, globally_closed: set[date]) -> date:
    current = start
    remaining = offset
    while remaining > 0:
        current += timedelta(days=1)
        if is_open(current, globally_closed):
            remaining -= 1
    while not is_open(current, globally_closed):
        current += timedelta(days=1)
    return current


def open_span(start: date, duration: int, globally_closed: set[date]) -> list[date]:
    current = start
    result: list[date] = []
    while len(result) < duration:
        if is_open(current, globally_closed):
            result.append(current)
        current += timedelta(days=1)
    return result


def parse_buffer(plan_state: Path, feature: str) -> int:
    if not plan_state.exists():
        return 20
    for row in markdown_table(plan_state, {"Feature", "Approved %"}):
        if clean(row["Feature"]) == feature:
            try:
                return max(20, int(row["Approved %"].rstrip("%")))
            except ValueError:
                return 20
    return 20


def approved(plan_state: Path) -> bool:
    if not plan_state.exists():
        return False
    text = plan_state.read_text(encoding="utf-8", errors="ignore")
    return bool(re.search(r"^Status:\s*`?approved`?\s*$", text, re.MULTILINE | re.IGNORECASE))


def earliest_slot(
    story: Story,
    earliest: date,
    role_resources: list[str],
    coefficients: dict[str, float],
    personal_closed: dict[str, set[date]],
    occupied: dict[str, set[date]],
    globally_closed: set[date],
    buffer_percent: int,
) -> Scheduled:
    candidates = role_resources or [f"TBD_{story.role}"]
    max_count = min(story.max_parallelism, len(candidates))
    best: Scheduled | None = None
    for count in range(max_count, 0, -1):
        for group in itertools.combinations(candidates, count):
            capacity = story.efficiency * sum(coefficients.get(resource, 1.0) for resource in group)
            effort = story.effort * (1 + buffer_percent / 100)
            duration = max(1, math.ceil(effort / capacity))
            current = earliest
            while True:
                span = open_span(current, duration, globally_closed)
                if all(
                    all(day not in occupied.get(resource, set()) and day not in personal_closed.get(resource, set()) for day in span)
                    for resource in group
                ):
                    scheduled = Scheduled(story, span[0], span[-1], list(group), duration)
                    if best is None or (scheduled.finish, scheduled.start, -len(group)) < (best.finish, best.start, -len(best.resources)):
                        best = scheduled
                    break
                current = add_open_days(current, 1, globally_closed)
    if best is None:
        raise RuntimeError(f"Unable to schedule {story.story_id}")
    for resource in best.resources:
        occupied.setdefault(resource, set()).update(open_span(best.start, best.duration, globally_closed))
    return best


def schedule_view(
    project: Path,
    gantt_dir: Path,
    features: list[str],
    commander: bool,
) -> dict[str, list[Scheduled]]:
    quarter_start, _ = quarter_bounds(gantt_dir.parent.name)
    globally_closed = load_closed_days(gantt_dir)
    team, coefficients, personal_closed = load_team(project)
    occupied: dict[str, set[date]] = {}
    all_schedules: dict[str, list[Scheduled]] = {}
    story_schedule: dict[str, Scheduled] = {}
    for feature in features:
        feature_schedules: list[Scheduled] = []
        stories = load_stories(project, feature)
        role_map = {story.role: story for story in stories}
        for story in stories:
            earliest = story.not_before or quarter_start
            dependency_finishes = [story_schedule[item].finish for item in story.depends_on if item in story_schedule]
            if dependency_finishes:
                earliest = max(earliest, add_open_days(max(dependency_finishes), 1, globally_closed))
            if story.role == "BE" and "AN" in role_map:
                an = story_schedule.get(role_map["AN"].story_id)
                if an:
                    earliest = max(earliest, add_open_days(an.finish, 1, globally_closed))
            if story.role == "FE":
                an_story = role_map.get("AN")
                be_story = role_map.get("BE")
                if an_story and an_story.story_id in story_schedule:
                    earliest = max(earliest, add_open_days(story_schedule[an_story.story_id].finish, 1, globally_closed))
                if be_story and be_story.story_id in story_schedule:
                    earliest = max(earliest, add_open_days(story_schedule[be_story.story_id].start, 3, globally_closed))
            if story.role == "QA":
                finishes = [story_schedule[item.story_id].finish for item in (role_map.get("BE"), role_map.get("FE")) if item and item.story_id in story_schedule]
                if finishes:
                    earliest = max(earliest, add_open_days(max(finishes), 1, globally_closed))
            buffer_percent = parse_buffer(gantt_dir.parent / "plan-state.md", feature) if commander else 0
            scheduled = earliest_slot(
                story,
                earliest,
                team.get(story.role, []),
                coefficients,
                personal_closed,
                occupied,
                globally_closed,
                buffer_percent,
            )
            feature_schedules.append(scheduled)
            story_schedule[story.story_id] = scheduled
        if feature_schedules:
            all_schedules[feature] = feature_schedules
    return all_schedules


def alias(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value).upper()


def render_feature(feature: str, schedules: list[Scheduled], commander: bool) -> str:
    label = "CP" if commander else "QP"
    lines = [f"' FEATURE: {feature}", f"' Generated {label} from role planning stories"]
    for scheduled in schedules:
        task_alias = f"{alias(scheduled.story.story_id)}_{label}"
        resource_part = "".join(f" {{{resource}}}" for resource in scheduled.resources)
        lines.extend(
            [
                f"[{scheduled.story.summary}] as [{task_alias}] on{resource_part} starts {scheduled.start:%Y/%m/%d}",
                f"[{task_alias}] ends {scheduled.finish:%Y/%m/%d}",
                f"[{task_alias}] is colored in {ROLE_COLORS[scheduled.story.role]}",
                f"' effort={scheduled.story.effort}; duration={scheduled.duration}; role={scheduled.story.role}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_actualization(project: Path, quarter: str, feature: str, schedules: list[Scheduled]) -> None:
    path = project / "features" / feature / "planning/actualization.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        f"| {item.story.story_id} | {item.story.summary} | {item.start.isoformat()} | {item.duration} | virtual | explicit |  |  | {', '.join(item.story.depends_on)} |"
        for item in schedules
    ]
    path.write_text(
        "# Actualization map\n\n"
        f"Feature: `features/{feature}/feature.md`  \nQuarter: `{quarter}`  \nBaseline: `commander-plan`\n\n"
        "Approved baseline dates and duration remain unchanged. Mapping fields may later materialize role stories into task candidates and actual tasks.\n\n"
        "## Mapping\n\n"
        "| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |\n"
        "|---|---|---|---:|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate quarter and commander planning includes")
    parser.add_argument("project")
    parser.add_argument("quarter")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    gantt_dir = project / "planning" / args.quarter / "gantt"
    plan_state = gantt_dir.parent / "plan-state.md"
    if approved(plan_state):
        print(f"Approved plan is immutable: {plan_state}")
        return 1
    features = load_features(gantt_dir)
    if not features:
        print(f"No feature priority order in {gantt_dir / 'order.txt'}")
        return 1
    view_schedules: dict[str, dict[str, list[Scheduled]]] = {}
    for view, commander in (("quarter-plan", False), ("commander-plan", True)):
        schedules = schedule_view(project, gantt_dir, features, commander)
        view_schedules[view] = schedules
        target_dir = gantt_dir / "includes" / view
        target_dir.mkdir(parents=True, exist_ok=True)
        expected = {f"FEATURE-{feature}.puml" for feature in schedules}
        for stale in target_dir.glob("FEATURE-*.puml"):
            if stale.name not in expected:
                stale.unlink()
        for feature, feature_schedules in schedules.items():
            (target_dir / f"FEATURE-{feature}.puml").write_text(render_feature(feature, feature_schedules, commander), encoding="utf-8")
    for feature, schedules in view_schedules.get("commander-plan", {}).items():
        write_actualization(project, args.quarter, feature, schedules)
    sync = Path(__file__).with_name("sync-quarter-gantt.py")
    subprocess_result = subprocess.run([sys.executable, str(sync), str(gantt_dir)], check=False)
    return subprocess_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
