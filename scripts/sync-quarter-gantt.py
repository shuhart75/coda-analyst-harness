#!/usr/bin/env python3
from __future__ import annotations

from datetime import date
from pathlib import Path
import argparse
import importlib.util
import re
import sys

from actual_progress_scope import declared_aliases, expanded_with_paths, load_forecast_scope


VIEWS = [
    ("quarter-plan", "План на квартал"),
    ("commander-plan", "Командирский план"),
    ("actual-progress", "Реальный прогресс"),
]

QUARTER_START_MONTH = {
    1: 1,
    2: 4,
    3: 7,
    4: 10,
}

START_RE = re.compile(r"\b(?:starts|happens\s+at)\s+(\d{4})[-/](\d{2})[-/](\d{2})\b", re.IGNORECASE)
FEATURE_COMMENT_RE = re.compile(r"^'\s*FEATURE:\s*(.+?)\s*$", re.MULTILINE)
FEATURE_TITLE_RE = re.compile(r"^#\s+FEATURE-[A-Z0-9_-]+\s+[—-]\s+(.+?)\s*$", re.MULTILINE)
CLOSED_DAY_RE = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})$")
HAPPENS_AT_DATE_RE = re.compile(r"\bhappens\s+at\s+(\d{4})[-/](\d{2})[-/](\d{2})\b", re.IGNORECASE)
TODAY_HIGHLIGHT_COLOR = "LightSalmon"
MILESTONE_DAY_HIGHLIGHT_COLOR = "LightSteelBlue"
DEFAULT_STYLE_BLOCK = """<style>
ganttDiagram {
  task {
    BackGroundColor lightblue
  }
  milestone {
    BackGroundColor orange
    FontColor black
  }
}
</style>"""


def parse_quarter_start(gantt_dir: Path) -> date:
    quarter_id = gantt_dir.parent.name
    match = re.fullmatch(r"(\d{4})-Q([1-4])", quarter_id)
    if not match:
        raise ValueError(f"Cannot infer quarter from path: {gantt_dir}")
    year = int(match.group(1))
    quarter = int(match.group(2))
    return date(year, QUARTER_START_MONTH[quarter], 1)


def parse_task_starts(path: Path, contents: dict[Path, str]) -> list[date]:
    text = contents[path] if path in contents else path.read_text(encoding="utf-8")
    starts: list[date] = []
    for year, month, day in START_RE.findall(text):
        starts.append(date(int(year), int(month), int(day)))
    return starts


def view_start(quarter_start: date, include_files: list[Path], contents: dict[Path, str]) -> date:
    starts = [quarter_start]
    for include in include_files:
        starts.extend(parse_task_starts(include, contents))
    return min(starts)


def project_root(gantt_dir: Path) -> Path:
    # <project>/planning/<YYYY-QN>/gantt
    return gantt_dir.parents[2]


def feature_slug(path: Path) -> str:
    name = path.stem
    return name.removeprefix("FEATURE-")


def feature_title(gantt_dir: Path, path: Path, contents: dict[Path, str]) -> str:
    text = contents[path] if path in contents else path.read_text(encoding="utf-8")
    comment_match = FEATURE_COMMENT_RE.search(text)
    slug = feature_slug(path)

    root = project_root(gantt_dir)
    feature_md = root / "features" / slug / "feature.md"
    if feature_md.exists():
        feature_text = feature_md.read_text(encoding="utf-8")
        title_match = FEATURE_TITLE_RE.search(feature_text)
        if title_match:
            title = title_match.group(1).strip()
            if title and not title.startswith("<"):
                return title

    if comment_match:
        comment_title = comment_match.group(1).strip()
        if comment_title and not comment_title.startswith("<"):
            return comment_title

    return slug.replace("-", " ").title()


def format_project_start(start: date) -> str:
    return f"{start:%Y-%m-%d}"


def read_closed_days(gantt_dir: Path) -> list[str]:
    closed_days_path = gantt_dir / "closed-days.txt"
    if not closed_days_path.exists():
        return []

    result: list[str] = []
    for raw_line in closed_days_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = CLOSED_DAY_RE.fullmatch(line)
        if not match:
            raise ValueError(
                f"Invalid closed day '{line}' in {closed_days_path}; expected YYYY/MM/DD or YYYY-MM-DD"
            )
        year, month, day = match.groups()
        result.append(f"{year}/{month}/{day}")
    return result


def highlighted_milestone_days(paths: list[Path]) -> list[str]:
    dates: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for year, month, day in HAPPENS_AT_DATE_RE.findall(text):
            dates.add(f"{year}/{month}/{day}")
    return sorted(dates)


def preamble_files(gantt_dir: Path, view_slug: str) -> list[Path]:
    preamble_dir = gantt_dir / "preamble"
    candidates = [
        preamble_dir / "common.puml",
        preamble_dir / f"{view_slug}.puml",
    ]
    return [path for path in candidates if path.exists()]


def feature_order(gantt_dir: Path) -> dict[str, int]:
    order_file = gantt_dir / "order.txt"
    if not order_file.exists():
        return {}
    order: dict[str, int] = {}
    for raw_line in order_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        order.setdefault(line, len(order))
    return order


def header_lines(
    gantt_dir: Path,
    title: str,
    start: date,
    closed_days: list[str],
    highlighted_days: list[str],
) -> list[str]:
    lines = [
        "@startgantt",
        f"title {title} - {gantt_dir.parent.name}",
        f"Project starts {format_project_start(start)}",
        "saturday are closed",
        "sunday are closed",
        "",
    ]
    for closed_day in closed_days:
        lines.append(f"{closed_day} is closed")
    if closed_days:
        lines.append("")
    lines.extend(
        [
            "printscale daily zoom 1.5",
            "projectscale daily",
            f"today is colored in {TODAY_HIGHLIGHT_COLOR}",
            "!$now = %now()",
            '[Мы сейчас здесь] as [TODAY_MARK] happens %date("YYYY-MM-dd", $now)',
        ]
    )
    for highlighted_day in highlighted_days:
        lines.append(f"{highlighted_day} is colored in {MILESTONE_DAY_HIGHLIGHT_COLOR}")
    lines.extend(
        [
            "",
            DEFAULT_STYLE_BLOCK,
            f"' {title}",
            "",
        ]
    )
    return lines


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sync_actual_progress_overlays(gantt_dir: Path, overlay) -> dict[Path, str]:
    feature_slugs = sorted(
        {
            feature_slug(path)
            for view in ("quarter-plan", "commander-plan", "actual-progress")
            for path in (gantt_dir / "includes" / view).glob("FEATURE-*.puml")
        }
    )
    return overlay.prepare_outputs(project_root(gantt_dir), gantt_dir.parent.name, feature_slugs or None)


def sync_confluence_export(gantt_dir: Path, contents: dict[Path, str]) -> None:
    source = gantt_dir / "actual-progress.puml"
    target = gantt_dir / "actual-progress-confluence.puml"
    expander = load_tool("expand-plantuml-includes")
    contents[target] = "\n".join(expander.expand_file(source, [], contents)).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Генерация Ганта с проверкой источников до записи")
    parser.add_argument("gantt_dir")
    parser.add_argument("--actual-only", action="store_true", help="Не изменять quarter-plan и commander-plan")
    args = parser.parse_args()
    gantt_dir = Path(args.gantt_dir).resolve()
    quarter_start = parse_quarter_start(gantt_dir)
    closed_days = read_closed_days(gantt_dir)
    order = feature_order(gantt_dir)
    overlay = load_tool("sync-actual-progress-overlay")
    outputs = sync_actual_progress_overlays(gantt_dir, overlay)
    scope = load_forecast_scope(project_root(gantt_dir), gantt_dir.parent.name)
    if scope.role_baselines and not args.actual_only:
        raise ValueError("Ролевой PLAN требует --actual-only; исходные планы нельзя перегенерировать")

    for slug, title in VIEWS:
        if args.actual_only and slug != "actual-progress":
            continue
        include_dir = gantt_dir / "includes" / slug
        include_files = sorted(
            set(include_dir.glob("FEATURE-*.puml"))
            | {path for path in outputs if path.parent == include_dir}
            | (set(scope.baselines.values()) if slug == "actual-progress" else set()),
            key=lambda path: (
                order.get(feature_slug(path), len(order)),
                feature_slug(path),
            ),
        )
        preambles = preamble_files(gantt_dir, slug)
        preserved_paths = sorted(set(scope.includes.values())) if slug == "actual-progress" else []
        start = view_start(quarter_start, preambles + include_files + preserved_paths, outputs)
        lines = header_lines(
            gantt_dir,
            title,
            start,
            closed_days,
            highlighted_milestone_days(preambles),
        )

        for preamble in preambles:
            lines.append(f'!include {preamble.relative_to(gantt_dir).as_posix()}')
            lines.append("")

        if include_files:
            for path in include_files:
                title = feature_title(gantt_dir, path, outputs)
                excluded_slug = feature_slug(path)
                if slug == "actual-progress" and excluded_slug in scope.exclusions:
                    decision = scope.exclusions[excluded_slug]
                    title += f" (PLAN; вне прогноза {gantt_dir.parent.name})"
                    lines.append(f"' Forecast exclusion: {excluded_slug}; {decision['reason']}")
                    lines.append(f"' Decision source: {decision['source']}")
                elif slug == "actual-progress" and excluded_slug in scope.preserved:
                    title += " (PLAN)"
                lines.append(f"-- {title} --")
                lines.append(f'!include {path.relative_to(gantt_dir).as_posix()}')
                lines.append("")
        else:
            lines.append("' No feature include files yet")

        if preserved_paths:
            dependencies: list[Path] = []
            for path in preambles + include_files:
                _, included = expanded_with_paths(path, outputs)
                dependencies.extend(included)
            for feature, decision in scope.preserved.items():
                lines.append(f"' Preserved forecast: {feature}; {decision['reason']}")
                lines.append(f"' Decision source: {decision['source']}; aliases: {', '.join(decision['aliases'])}")
            for path in preserved_paths:
                if dependencies.count(path) > 1:
                    raise ValueError(f"{path}: повторное подключение сохранённого прогноза")
                if path not in dependencies:
                    lines.append(f'!include {path.relative_to(gantt_dir).as_posix()}')
                    lines.append("")

        lines.append("@endgantt")
        target = gantt_dir / f"{slug}.puml"
        outputs[target] = "\n".join(lines).rstrip() + "\n"

    sync_confluence_export(gantt_dir, outputs)
    if scope.preserved:
        expanded, dependencies = expanded_with_paths(gantt_dir / "actual-progress.puml", outputs)
        _, previous_dependencies = expanded_with_paths(gantt_dir / "actual-progress.puml")
        lost_forecasts = {
            path for path in previous_dependencies
            if path.name.startswith("FORECAST-") and path not in dependencies
        }
        if lost_forecasts:
            raise ValueError("Будут потеряны прежние FORECAST-подключения; требуется явное решение: "
                             + ", ".join(str(path) for path in sorted(lost_forecasts)))
        if any(dependencies.count(path) != 1 for path in set(scope.includes.values())):
            raise ValueError("Сохранённый прогноз должен быть подключён ровно один раз")
        aliases = declared_aliases(expanded)
        if len(set(aliases)) != len(aliases):
            raise ValueError("Повторяющиеся идентификаторы PlantUML в сохранённом прогнозе и Ганте")
        if load_forecast_scope(project_root(gantt_dir), gantt_dir.parent.name) != scope:
            raise ValueError("Решение о сохранении прогноза изменилось во время генерации")
    if scope.role_baselines:
        expanded, _ = expanded_with_paths(gantt_dir / "actual-progress.puml", outputs)
        aliases = declared_aliases(expanded)
        if len(set(aliases)) != len(aliases):
            raise ValueError("Повторяющиеся идентификаторы PlantUML в ролевом PLAN и Ганте")
        if load_forecast_scope(project_root(gantt_dir), gantt_dir.parent.name) != scope:
            raise ValueError("Ролевой PLAN изменился во время генерации")
    overlay.publish_outputs(outputs)
    for path in outputs:
        print(f"Wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
