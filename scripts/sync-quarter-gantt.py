#!/usr/bin/env python3
from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import subprocess
import sys


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

START_RE = re.compile(r"\bstarts\s+(\d{4})[-/](\d{2})[-/](\d{2})\b", re.IGNORECASE)
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


def usage() -> None:
    print("Usage: sync-quarter-gantt.py <gantt-dir>")


def parse_quarter_start(gantt_dir: Path) -> date:
    quarter_id = gantt_dir.parent.name
    match = re.fullmatch(r"(\d{4})-Q([1-4])", quarter_id)
    if not match:
        raise ValueError(f"Cannot infer quarter from path: {gantt_dir}")
    year = int(match.group(1))
    quarter = int(match.group(2))
    return date(year, QUARTER_START_MONTH[quarter], 1)


def parse_task_starts(path: Path) -> list[date]:
    text = path.read_text(encoding="utf-8")
    starts: list[date] = []
    for year, month, day in START_RE.findall(text):
        starts.append(date(int(year), int(month), int(day)))
    return starts


def view_start(quarter_start: date, include_files: list[Path]) -> date:
    starts = [quarter_start]
    for include in include_files:
        starts.extend(parse_task_starts(include))
    return min(starts)


def project_root(gantt_dir: Path) -> Path:
    # <project>/planning/<YYYY-QN>/gantt
    return gantt_dir.parents[2]


def feature_slug(path: Path) -> str:
    name = path.stem
    return name.removeprefix("FEATURE-")


def feature_title(gantt_dir: Path, path: Path) -> str:
    text = path.read_text(encoding="utf-8")
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


def sync_actual_progress_overlays(gantt_dir: Path) -> None:
    script = Path(__file__).with_name("sync-actual-progress-overlay.py")
    if not script.exists():
        return
    feature_slugs = sorted(
        {
            feature_slug(path)
            for view in ("quarter-plan", "commander-plan")
            for path in (gantt_dir / "includes" / view).glob("FEATURE-*.puml")
        }
    )
    if feature_slugs:
        actual_dir = gantt_dir / "includes" / "actual-progress"
        actual_dir.mkdir(parents=True, exist_ok=True)
        allowed = {f"FEATURE-{slug}.puml" for slug in feature_slugs}
        for stale in actual_dir.glob("FEATURE-*.puml"):
            if stale.name not in allowed:
                stale.unlink()
    subprocess.run(
        [
            sys.executable,
            str(script),
            str(project_root(gantt_dir)),
            gantt_dir.parent.name,
            *feature_slugs,
        ],
        check=True,
    )


def sync_confluence_export(gantt_dir: Path) -> None:
    script = Path(__file__).with_name("expand-plantuml-includes.py")
    source = gantt_dir / "actual-progress.puml"
    target = gantt_dir / "actual-progress-confluence.puml"
    if not script.exists() or not source.exists():
        return
    subprocess.run([sys.executable, str(script), str(source), str(target)], check=True)


def main() -> int:
    if len(sys.argv) < 2:
        usage()
        return 1

    gantt_dir = Path(sys.argv[1])
    quarter_start = parse_quarter_start(gantt_dir)
    closed_days = read_closed_days(gantt_dir)
    order = feature_order(gantt_dir)
    sync_actual_progress_overlays(gantt_dir)

    for slug, title in VIEWS:
        include_dir = gantt_dir / "includes" / slug
        include_dir.mkdir(parents=True, exist_ok=True)
        include_files = sorted(
            include_dir.glob("FEATURE-*.puml"),
            key=lambda path: (
                order.get(feature_slug(path), len(order)),
                feature_slug(path),
            ),
        )
        preambles = preamble_files(gantt_dir, slug)
        start = view_start(quarter_start, preambles + include_files)
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
                lines.append(f"-- {feature_title(gantt_dir, path)} --")
                lines.append(f'!include {path.relative_to(gantt_dir).as_posix()}')
                lines.append("")
        else:
            lines.append("' No feature include files yet")

        lines.append("@endgantt")
        target = gantt_dir / f"{slug}.puml"
        target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        print(f"Wrote {target}", flush=True)

    sync_confluence_export(gantt_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
