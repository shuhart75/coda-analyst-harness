"""Bind an agent-reviewed business release plan to the full expanded Gantt.

The marker proves freshness, not semantic correctness of business mappings.
This command prints a marker; it never edits or approves the plan.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import import_module
from pathlib import Path
import re

MARKER = re.compile(r'^<!-- release-plan-review:v1 gantt=([0-9a-f]{64}) plan=([0-9a-f]{64}) -->$', re.MULTILINE)


def review_marker(quarter: Path) -> str:
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
    body = MARKER.sub('', plan.read_text(encoding='utf-8')).strip()
    digest = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
    return f'<!-- release-plan-review:v1 gantt={digest(expanded)} plan={digest(body)} -->'


def check_review(quarter: Path) -> None:
    expected = review_marker(quarter)
    content = (quarter / 'release-plan.md').read_text(encoding='utf-8')
    matches = list(MARKER.finditer(content))
    if len(matches) != 1 or matches[0].group() != expected:
        raise ValueError(f'Release plan review is missing or stale: {quarter}/release-plan.md; '
                         'review every forecast against the full Gantt, then run '
                         f'python3 scripts/release_plan_review.py {quarter} and replace the review marker')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('quarter', type=Path)
    args = parser.parse_args()
    print(review_marker(args.quarter.resolve()))


if __name__ == '__main__':
    main()
