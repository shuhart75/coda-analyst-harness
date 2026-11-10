"""Developer assignment evidence for bugs and read-only quarterly aggregation."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from tracker_lifecycle import TaskHistory, timestamp


def assignment_facts(key: str, history: TaskHistory, participants: dict) -> dict:
    """Never date an assignment from creation, observation or a status transition."""
    assignments, undated, limitations = [], set(), []
    if not history.complete:
        limitations.append('assignment-history-incomplete')
    events = [event for event in history.events if event.assignee is not None]
    initial = events[0].assignee[0] if events else history.current_assignee
    if participants.get(initial) in {'BE', 'FE'}:
        undated.add(initial)
    previous = initial
    seen = {}
    for event in events:
        if event.event_id in seen:
            if seen[event.event_id] != event:
                raise ValueError('Conflicting defect assignment event')
            continue
        seen[event.event_id] = event
        old, new = event.assignee
        if old != previous:
            limitations.append('assignment-history-gap')
        if old != new and participants.get(new) in {'BE', 'FE'}:
            assignments.append({'event_id': event.event_id, 'developer': new,
                                'at': timestamp(event.at).isoformat()})
        previous = new
    if previous != history.current_assignee:
        limitations.append('assignment-snapshot-gap')
        if participants.get(history.current_assignee) in {'BE', 'FE'}:
            undated.add(history.current_assignee)
    return {'key': key, 'suit': 'bug', 'observed_at': timestamp(history.observed_at).isoformat(),
            'assignments': assignments, 'undated_developers': sorted(undated),
            'limitations': sorted(set(limitations))}


def quarterly_defects(facts: list[dict], quarter: str, timezone: str = 'Europe/Moscow') -> dict:
    if not re.fullmatch(r'\d{4}-Q[1-4]', quarter):
        raise ValueError('Quarter must be YYYY-QN')
    year, number = int(quarter[:4]), int(quarter[-1])
    zone = ZoneInfo(timezone)
    counts, unknown = defaultdict(set), defaultdict(set)
    events, limitations = {}, set()
    for fact in facts:
        if fact.get('suit') != 'bug' or not fact.get('key'):
            raise ValueError('Expected a bug with a stable tracker key')
        key = fact['key']
        limitations.update(f'{key}:{value}' for value in fact.get('limitations', []))
        for developer in fact.get('undated_developers', []):
            unknown[developer].add(key)
        for event in fact['assignments']:
            at = timestamp(datetime.fromisoformat(event['at'])).astimezone(zone)
            identity = (key, event['event_id'])
            value = (event['developer'], at)
            if identity in events and events[identity] != value:
                raise ValueError('Conflicting defect assignment across reviews')
            events[identity] = value
            if at.year == year and (at.month - 1) // 3 + 1 == number:
                counts[event['developer']].add(key)
    return {'quarter': quarter, 'timezone': timezone,
            'metric': 'unique-bugs-assigned-to-developer-in-quarter',
            'coverage': 'supplied-history-reviews-only',
            'developers': [{'developer': person, 'assigned_bug_count': len(counts[person]),
                            'keys': sorted(counts[person]),
                            'undated_keys': sorted(unknown[person] - counts[person])}
                           for person in sorted(counts.keys() | unknown.keys())],
            'limitations': sorted(limitations),
            'interpretation': 'Назначение дефекта разработчику не доказывает авторство ошибки.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quarter', required=True)
    parser.add_argument('--review', action='append', required=True, type=Path)
    parser.add_argument('--timezone', default='Europe/Moscow')
    args = parser.parse_args()
    facts, sources = [], []
    for path in args.review:
        raw = path.read_bytes()
        review = json.loads(raw)
        if (review.get('status') != 'history-review-ready'
                or 'defect_assignments' not in review or review.get('pending_history')):
            raise ValueError(f'{path}: требуется завершённый обзор истории с учётом дефектов')
        facts.extend(review['defect_assignments'])
        sources.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})
    report = quarterly_defects(facts, args.quarter, args.timezone)
    report['sources'] = sources
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
