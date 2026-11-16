"""Carry audited same-model research into a fresh review, never a verdict."""
import hashlib
import json
from pathlib import Path

import plan_review
from workspace import snapshot

MAX_BYTES = 512 * 1024
MAX_ITEM_BYTES = 24 * 1024


def normalize(value, root):
    if isinstance(value, str):
        return value.replace(str(root), '.').replace(root.as_posix(), '.')
    if isinstance(value, list):
        return [normalize(item, root) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item, root) for key, item in value.items()}
    return value


def _bounded(value):
    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(raw) <= MAX_ITEM_BYTES:
        return value
    return {'truncated': True, 'original_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'head': raw[:MAX_ITEM_BYTES // 2].decode('utf-8', 'ignore'),
            'tail': raw[-MAX_ITEM_BYTES // 2:].decode('utf-8', 'ignore')}


def collect(runner, records, expected, model):
    from controller import Blocked, digest, file_manifest
    result = {'kind': 'untrusted-research-not-verdict', 'candidate_sha256': expected,
              'model': model, 'sources': [], 'items': [], 'omitted_items': 0}
    seen = set()
    used = 0
    # Newest evidence wins the bounded space; keep older distinct observations.
    for record in reversed(records[-4:]):
        saved = record.get('result') or {}
        if (record.get('model') != model or not record.get('events_sha256')
                or not record.get('workspace_snapshot') or not saved.get('events_path')
                or saved.get('permission_denials') or (saved.get('cleanup') or {}).get('preserved')):
            continue
        root, events = Path(record['workspace']), Path(saved['events_path'])
        if (not root.resolve().is_relative_to((runner.root/'planners').resolve())
                or not events.resolve().is_relative_to((runner.root/'logs').resolve())
                or digest(events) != record['events_sha256']
                or snapshot(root, normalize_settings=True) != record['workspace_snapshot']):
            raise Blocked('Saved review research changed; inspect ' + str(events))
        if events.stat().st_size > 16 * 1024 * 1024:
            result['omitted_items'] += 1
            continue
        try:
            plan_review.audit(root, events, expected)
        except (plan_review.ProtocolError, ValueError, KeyError, TypeError):
            # Incomplete or out-of-scope tool evidence is not silently trusted.
            continue
        rows = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines() if line.strip()]
        reported = {r.get('model') for r in rows if r.get('type') == 'system' and r.get('subtype') == 'init'}
        if reported != {model} or file_manifest(root/'.review-input/packet') != file_manifest(runner.root/'input'):
            continue
        decision, captured = runner.root/'decision.md', root/'.review-input/decision.md'
        if decision.exists() != captured.exists() or (decision.exists() and digest(decision) != digest(captured)):
            continue
        source = {'attempt': record['attempt'], 'events_path': str(events), 'events_sha256': record['events_sha256']}
        result['sources'].append(source)
        calls = {}
        items = []
        for row in rows:
            for item in row.get('message', {}).get('content', []):
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'tool_use':
                    calls[item['id']] = item
                elif item.get('type') == 'tool_result' and item.get('is_error') is False:
                    call = calls.get(item.get('tool_use_id'), {})
                    args = call.get('input', {})
                    # Do not recursively copy prior notebooks, candidates or
                    # other controller handoff files; the new copy has these.
                    if '.review-input' in json.dumps(args):
                        continue
                    items.append({'kind': 'tool_observation', 'tool': call.get('name'),
                                  'input': args, 'output': item.get('content'), 'call': item.get('tool_use_id')})
                elif item.get('type') == 'text' and row.get('type') == 'assistant':
                    items.append({'kind': 'working_note', 'text': item.get('text', '')})
        for item in items:
            # Paths from an older copy must never direct the next agent there.
            text = json.dumps(normalize(item, root), ensure_ascii=False)
            identity = hashlib.sha256(text.encode('utf-8')).hexdigest()
            if identity in seen:
                continue
            seen.add(identity)
            bounded = _bounded(json.loads(text))
            size = len(json.dumps(bounded, ensure_ascii=False).encode('utf-8'))
            if used + size > MAX_BYTES - 64 * 1024 or len(result['items']) >= 256:
                result['omitted_items'] += 1
                continue
            result['items'].append({'source_attempt': record['attempt'], 'value': bounded})
            used += size
    return result
