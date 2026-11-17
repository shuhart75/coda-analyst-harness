"""Bind an agent-reviewed business release plan to the full expanded Gantt.

Validate every forecast using registered release work; bind its reviewed business mapping.
This command prints a marker; it never edits or approves the plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from importlib import import_module
from pathlib import Path
import re

MARKER = re.compile(r'^<!-- release-plan-review:v2 gantt=([0-9a-f]{64}) plan=([0-9a-f]{64}) sources=([0-9a-f]{64}) -->$', re.MULTILINE)
OLD_MARKER = re.compile(r'^<!-- release-plan-review:v1 .*? -->$', re.MULTILINE)


def review_inputs(quarter: Path) -> tuple[str, str, dict, list[dict]]:
    plan = quarter / 'release-plan.md'
    source = quarter / 'gantt/actual-progress.puml'
    export = quarter / 'gantt/actual-progress-confluence.puml'
    if any(path.resolve() != path.absolute() or not path.is_file() for path in (plan, source, export)):
        raise ValueError('Release plan review requires regular plan, full Gantt and Confluence files')
    dependencies = []
    expanded = '\n'.join(import_module('expand-plantuml-includes').expand_file(
        source, [], dependencies=dependencies)).rstrip() + '\n'
    if export.read_text(encoding='utf-8') != expanded:
        raise ValueError('Refresh the full Gantt Confluence export before release plan review')
    project = quarter.parent.parent.resolve()
    if any(not dependency.resolve().is_relative_to(project) for dependency in dependencies):
        raise ValueError('Release-plan Gantt includes must stay inside project')
    body = OLD_MARKER.sub('', MARKER.sub('', plan.read_text(encoding='utf-8'))).strip()
    from release_plan_forecast import forecast_rows
    bindings, rows = forecast_rows(quarter, body, expanded)
    return body, expanded, bindings, rows


def review_marker(quarter: Path) -> str:
    body, expanded, bindings, rows = review_inputs(quarter)
    mismatches = [f"{row['release']} / {row['feature']}: {row['forecast']} -> {row['expected_forecast']}"
                  for row in rows if row['forecast'] != row['expected_forecast']]
    if mismatches:
        raise ValueError('Release-plan forecasts differ from release work in the full Gantt: '
                         + '; '.join(mismatches) + '. Update forecasts, not planned release dates.')
    report = quarter / 'gantt/actual-progress-resources.json'
    if report.exists():
        from datetime import date
        as_of = date.fromisoformat(json.loads(report.read_text())['as_of']).strftime('%d.%m.%Y')
        if not re.search(r'^Обновлено: ' + re.escape(as_of) + r'\.', body, re.M):
            raise ValueError('Update release-plan document date to the reviewed Gantt as_of: ' + as_of)
    digest = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
    sources = digest(json.dumps({'bindings': bindings, 'rows': rows}, ensure_ascii=False, sort_keys=True))
    return f'<!-- release-plan-review:v2 gantt={digest(expanded)} plan={digest(body)} sources={sources} -->'


def check_review(quarter: Path) -> None:
    content = (quarter / 'release-plan.md').read_text(encoding='utf-8')
    matches = list(MARKER.finditer(content))
    if len(matches) != 1 or OLD_MARKER.search(content):
        raise ValueError(f'Release plan review is missing or stale: {quarter}/release-plan.md; '
                         'review every forecast against the full Gantt, then run '
                         f'python3 scripts/release_plan_review.py {quarter} --report and correct the forecasts')
    if matches[0].group() != review_marker(quarter):
        raise ValueError('Release plan review is missing or stale: ' + str(quarter))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('quarter', type=Path)
    parser.add_argument('--report', action='store_true', help='Show expected forecasts and unresolved scope without writing')
    args = parser.parse_args()
    if args.report:
        print(json.dumps(review_inputs(args.quarter.resolve())[3], ensure_ascii=False, indent=2))
    else:
        print(review_marker(args.quarter.resolve()))


if __name__ == '__main__':
    main()
