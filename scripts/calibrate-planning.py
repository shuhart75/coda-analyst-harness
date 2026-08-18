#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


DEFAULTS = {"AN": 0.80, "BE": 0.70, "FE": 0.65, "QA": 0.80}


def clean(value: str) -> str:
    return value.strip().strip("`")


def tables(path: Path) -> list[tuple[list[str], list[list[str]]]]:
    result: list[tuple[list[str], list[list[str]]]] = []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        header = [clean(cell) for cell in lines[index].strip().strip("|").split("|")]
        rows: list[list[str]] = []
        index += 2
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            rows.append([clean(cell) for cell in lines[index].strip().strip("|").split("|")])
            index += 1
        result.append((header, rows))
    return result


def feature_order(project: Path, quarter: str) -> list[str]:
    path = project / "planning" / quarter / "gantt/order.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def planned(project: Path, feature: str) -> dict[str, float]:
    path = project / "features" / feature / "planning/estimates.md"
    result: dict[str, float] = {}
    if not path.exists():
        return result
    for header, rows in tables(path):
        if not {"Role", "Agreed effort, дн"}.issubset(set(header)):
            continue
        role_idx, effort_idx = header.index("Role"), header.index("Agreed effort, дн")
        for row in rows:
            try:
                result[row[role_idx].upper()] = float(row[effort_idx])
            except (IndexError, ValueError):
                pass
    return result


def open_days(start: date, finish: date) -> int:
    current = start
    total = 0
    while current <= finish:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def actual(project: Path, feature: str) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for path in (project / "features" / feature).glob("slices/*/execution/tasks.md"):
        for header, rows in tables(path):
            if not {"Role", "Estimate (дн)", "Status", "Actual Start", "Actual Finish"}.issubset(set(header)):
                continue
            role_idx = header.index("Role")
            estimate_idx = header.index("Estimate (дн)")
            status_idx = header.index("Status")
            start_idx = header.index("Actual Start")
            finish_idx = header.index("Actual Finish")
            for row in rows:
                try:
                    if row[status_idx].lower() in {"superseded", "cancelled"}:
                        continue
                    start_text, finish_text = row[start_idx], row[finish_idx]
                    if start_text and finish_text:
                        duration = open_days(date.fromisoformat(start_text), date.fromisoformat(finish_text))
                    elif row[status_idx].lower() in {"done", "completed", "closed"}:
                        duration = float(row[estimate_idx])
                    else:
                        continue
                    result[row[role_idx].upper()] += duration
                except (IndexError, ValueError):
                    pass
    return dict(result)


def suggested_efficiency(role: str, plan: float, fact: float) -> float:
    if plan <= 0 or fact <= 0:
        return DEFAULTS[role]
    return max(0.30, min(1.00, round(DEFAULTS[role] * plan / fact, 2)))


def suggested_buffer(plan_total: float, actual_total: float) -> int:
    ratio = actual_total / plan_total if plan_total else 1
    if ratio > 1.5:
        return 50
    if ratio > 1.25:
        return 40
    if ratio > 1.10:
        return 30
    return 20


def main() -> int:
    parser = argparse.ArgumentParser(description="Build plan-vs-actual calibration proposals")
    parser.add_argument("project")
    parser.add_argument("quarter")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    rows: list[str] = []
    role_plan: dict[str, float] = defaultdict(float)
    role_actual: dict[str, float] = defaultdict(float)
    for feature in feature_order(project, args.quarter):
        plan = planned(project, feature)
        fact = actual(project, feature)
        for role in DEFAULTS:
            if role not in plan and role not in fact:
                continue
            plan_value, fact_value = plan.get(role, 0), fact.get(role, 0)
            role_plan[role] += plan_value
            role_actual[role] += fact_value
            variance = fact_value - plan_value
            rows.append(f"| {feature} | {role} | {plan_value:g} | {fact_value:g} | {variance:+g} |  |")
    plan_total, actual_total = sum(role_plan.values()), sum(role_actual.values())
    output = project / "planning" / args.quarter / "retrospective.md"
    output.write_text(
        f"# Planning Retrospective — {args.quarter}\n\n"
        "Approved quarter and commander plans remain unchanged. Actual values use task actual-date duration; completed tasks without dates fall back to their estimate.\n\n"
        "## Plan Versus Actual\n\n"
        "| Feature | Role | Planned effort | Actual task effort | Variance | Explanation |\n"
        "|---|---|---:|---:|---:|---|\n"
        + ("\n".join(rows) if rows else "| - | - | 0 | 0 | 0 | no comparable role-story data |")
        + "\n\n## Calibration Proposals\n\n"
        "| Parameter | Current | Suggested | Evidence | Decision |\n"
        "|---|---:|---:|---|---|\n"
        + "\n".join(
            f"| {role} efficiency | {DEFAULTS[role]:.2f} | {suggested_efficiency(role, role_plan[role], role_actual[role]):.2f} | planned={role_plan[role]:g}, actual={role_actual[role]:g} | pending |"
            for role in DEFAULTS
        )
        + f"\n| Minimum risk buffer | 20% | {suggested_buffer(plan_total, actual_total)}% | total planned={plan_total:g}, actual={actual_total:g} | pending |\n\n"
        "## Rules\n\n- Apply accepted calibration only to future draft plans.\n- Do not rewrite the approved historical plan.\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
