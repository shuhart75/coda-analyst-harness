"""Transport requests enrich the standard commit-task resolver; no network or writes."""
from __future__ import annotations
import json
import re
import uuid

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
