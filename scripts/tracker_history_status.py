"""Read-only history reporting and effective status, separate from immutable card collection."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path


def collect_action(keys):
    return {'type': 'collect-history', 'keys': sorted(keys), 'dated_source_required': True,
            'capability_check_required': True,
            'guidance': 'Read current MCP capabilities. For history.list errors, check supported filtering for workflow_status and assigned_to; record actual arguments and pagination. A failed unfiltered request does not prove history unavailable.',
            'next_command': 'history-report --run-id RUN --manifest FILE',
            'role_confirmation_required_for_collection': False}


def status(run_id, reconciled_sha256):
    from tracker_workflow import run_root, load_json, digest_object, file_digest
    root = run_root(run_id)
    summaries = []
    for folder in ('history', 'history-reports'):
        for path in sorted((root / folder).glob('*.json')):
            value = load_json(path)
            if digest_object(value) != path.stem or value.get('run_id') != run_id:
                raise ValueError('Saved history checksum or run identity changed: ' + str(path))
            if value.get('reconciled_sha256') != reconciled_sha256:
                raise ValueError('Saved history belongs to another collection: ' + str(path))
            for evidence in value.get('evidence', []):
                for source in evidence.get('sources', []):
                    if file_digest(Path(source['path'])) != source['sha256']:
                        raise ValueError('Saved history source changed: ' + source['path'])
            summaries.append({'file': str(path), 'status': value['status'],
                              'pending_history': value.get('pending_history', []),
                              'keys': sorted({e['key'] for e in value.get('evidence', [])}),
                              'limitations': value.get('limitations', []),
                              'kind': folder})
    current = None
    pointer = root / 'history-current.json'
    if pointer.exists():
        filename = load_json(pointer)['file']
        current = next((s for s in summaries if s['file'] == filename), None)
        if current is None:
            raise ValueError('Current history reference is not a verified report')
    return {'state': 'reviewed' if summaries else 'not-collected', 'reports': summaries, 'current': current,
            'collection_artifact_is_not_history_status': True}


def history_report_command(args):
    from tracker_workflow import (verified_result, load_run, response_file, digest_bytes,
                                  digest_object, save_json, run_root)
    from tracker_history import read_history_source, decode_history, combine_history_parts, history_field_coverage
    completion, result = verified_result(args.run_id)
    if load_run(args.run_id).get('collection_mode') != 'adaptive':
        raise ValueError('Legacy history protocol is immutable')
    _, raw, manifest = response_file(args.manifest, args.run_id)
    if manifest.get('schema_version') != 1 or manifest.get('provider') != 'sbertrek':
        raise ValueError('Expected schema_version 1 and provider sbertrek')
    expected = {issue['sbertrek_key'] for issue in result['issues']}
    seen, histories, evidence, limits, incomplete = set(), [], [], [], set()
    for entry in manifest['responses']:
        key = entry['key']
        if key not in expected or key in seen or entry['provider'] != 'sbertrek':
            raise ValueError('History must cover unique keys from this run')
        seen.add(key)
        sources, parts = [], []
        for part in [entry, *entry.get('history_parts', [])]:
            if any(part.get(field) != entry.get(field) for field in ('key', 'provider', 'role')):
                raise ValueError('History parts must share task identity')
            payload, source = read_history_source(args.run_id, part, 'sbertrek', key, 'history')
            sources.append(source)
            observed = datetime.fromisoformat(part['call']['captured_at'])
            history, found = decode_history(payload, part, observed, check_fields=False, require_snapshot=False)
            parts.append((history, found, history_field_coverage(part)))
        latest_capture = max(history.observed_at for history, _, _ in parts)
        parts = [(replace(history, observed_at=latest_capture), found, fields)
                 for history, found, fields in parts]
        history, found = combine_history_parts(parts)
        if not history.complete:
            incomplete.add(key)
        histories.append({'key': key, 'complete': history.complete,
                          'events': [{**asdict(event), 'at': event.at.isoformat()} for event in history.events]})
        evidence.append({'key': key, 'sources': sources})
        limits.extend(f'{key}:{value}' for value in found)
    pending = sorted((expected - seen) | incomplete)
    output = {'schema_version': 1, 'run_id': args.run_id,
              'status': 'history-collection-incomplete' if pending else 'history-report-ready',
              'reconciled_sha256': completion['reconciled_sha256'], 'manifest_sha256': digest_bytes(raw),
              'histories': histories, 'evidence': evidence, 'limitations': sorted(set(limits)),
              'pending_history': pending, 'planning_application_allowed': False,
              'analytics_writes_performed': False}
    root = run_root(args.run_id)
    destination = root / 'history-reports' / (digest_object(output) + '.json')
    save_json(destination, output)
    save_json(root / 'history-current.json', {'file': str(destination)})
    print(json.dumps({**output, 'report_file': str(destination),
                      'next_action': collect_action(pending) if pending else None}, ensure_ascii=False, indent=2))
    return 0
