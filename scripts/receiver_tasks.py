"""Transport requests enrich the standard commit-task resolver; no network or writes."""
from __future__ import annotations
import json
import hashlib
import re
import uuid
from pathlib import PurePosixPath

REQUEST_FILE = 'analysis-task-request.json'
TOKEN = 'receiver-task/'
KEY = re.compile(r'[A-Z][A-Z0-9_]*-[1-9][0-9]*')

def validate_request(value, delivery=None):
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('target') != 'receiver' or value.get('role') != 'AN' or value.get('provider') != 'sbertrek':
        raise ValueError('Invalid receiver task request')
    if str(uuid.UUID(value.get('request_id', ''))) != value['request_id']:
        raise ValueError('Invalid task request identity')
    for field in ('delivery', 'summary', 'confirmation'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError('Missing task request field: ' + field)
    if delivery is not None and value['delivery'] != delivery:
        raise ValueError('Task request belongs to another delivery')
    if 'key' in value:
        raise ValueError('A request is not a tracker binding')
    return value

def request_token(value):
    return TOKEN + validate_request(value)['request_id']

def requests_for_paths(paths, read):
    """Describe requests from the same prospective snapshot as import task checks."""
    raw = read('delivery-index.json')
    entries = json.loads(raw)['deliveries'] if raw else {}
    result = []
    for delivery, entry in entries.items():
        root = entry['path']
        if not any(path == 'delivery-index.json' or path.startswith(root + '/') for path in paths):
            continue
        path = root + '/' + REQUEST_FILE
        raw = read(path)
        if raw is None:
            continue
        request = validate_request(json.loads(raw), delivery)
        result.append({**request, 'path': path, 'kind': 'delivery',
                       'owner': {'type': 'delivery', 'id': delivery}})
    ids = [item['request_id'] for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate task request identity')
    return result


def binding_targets_for_paths(paths, read):
    """Expose affected delivery bindings, without inventing receiver requests."""
    paths = list(paths)
    raw = read('delivery-index.json')
    entries = json.loads(raw)['deliveries'] if raw else {
        path.split('/')[1]: {'path': '/'.join(path.split('/')[:2])}
        for path in paths if path.startswith('features/') and len(path.split('/')) >= 3}
    result, roots = [], set()
    for delivery, entry in entries.items():
        root = entry['path']
        parts = PurePosixPath(root).parts
        if (not parts or PurePosixPath(root).is_absolute() or '..' in parts
                or '\\' in root or str(PurePosixPath(root)) != root
                or parts[0] not in ('features', 'quarters') or root in roots):
            raise ValueError('Некорректный или неоднозначный путь поставки в импорте')
        roots.add(root)
        if not any(path == 'delivery-index.json' or path.startswith(root + '/') for path in paths):
            continue
        relative = root + '/analysis-task.json'
        raw = read(relative)
        value = json.loads(raw) if raw is not None else None
        status = 'missing' if raw is None else 'existing'
        if (isinstance(value, dict) and value.get('schema_version') == 1
                and value.get('provider') == 'sbertrek' and value.get('role') == 'AN'
                and isinstance(value.get('key'), str) and KEY.fullmatch(value['key'])
                and isinstance(value.get('confirmation'), str) and value['confirmation'].strip()):
            status = 'legacy'
        result.append(dict(path=relative, owner={'type': 'delivery', 'id': delivery},
                           kind='delivery', status=status, previous_binding=value,
                           binding_sha256=hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None))
    return result


def work_targets_for_paths(paths, read):
    """Shared work needs explicit path ownership; never infer it from a delivery."""
    from commit_tasks import work_keys
    targets = []
    for path in sorted(set(paths)):
        kind = ('baseline' if path.startswith(('baseline/', 'releases/'))
                else 'context' if path.startswith('context/') else None)
        if not kind or path.endswith('/analysis-task.json'):
            continue
        if work_keys(path, read, kind) is not None or read(kind + '/analysis-task.json') is not None:
            continue
        targets.append(dict(path=path, kind=kind))
    return targets
