"""Narrow creation grants for combined delivery intake and execution saves."""
from __future__ import annotations

import json
import re
from project_layout import layout, delivery_selection, safe_path


def creation_paths(analytics, scope, api, *, registering=False):
    current = layout(analytics)
    prior = api.git(analytics, 'show', 'HEAD:delivery-index.json')
    if current is None or prior.returncode:
        raise ValueError('Creation scope requires an existing accepted delivery-index.json')
    previous = json.loads(prior.stdout)
    if {k:v for k,v in previous.items() if k != 'deliveries'} != {k:v for k,v in current.items() if k != 'deliveries'}:
        raise ValueError('Creation scope cannot change delivery registry metadata')
    old, new = previous['deliveries'], current['deliveries']
    if any(new.get(key) != value for key,value in old.items()):
        raise ValueError('Creation scope cannot change or delete existing delivery identities')
    additions = {key:value for key,value in new.items() if key not in old}
    if not set(additions) <= set(scope['features']):
        raise ValueError('New delivery is outside confirmed execution scope')
    if registering and not additions:
        raise ValueError('No new deliveries to register for creation')
    if not registering and additions and additions != scope.get('creation_deliveries'):
        raise ValueError('New delivery identities changed; repeat set-execution-scope with --include-creation')
    if not additions:
        return {}, additions
    bindings = delivery_selection(analytics, list(additions))
    old_paths = {entry['path'] for entry in old.values()}
    for key, entry in additions.items():
        if bindings[key]['quarter'] not in scope['quarters'] or entry['path'] in old_paths:
            raise ValueError('New delivery must have its own path in a confirmed quarter')
        expected = f"quarters/{entry['quarter']}/features/{entry['feature_id']}/deliveries/{entry['delivery_id']}"
        if entry['path'] != expected or api.git(analytics, 'cat-file', '-e', 'HEAD:' + expected).returncode == 0:
            raise ValueError('Creation scope requires a new canonical delivery directory')
    if len({entry['path'] for entry in additions.values()}) != len(additions):
        raise ValueError('New deliveries must have distinct paths')
    existing_owners = {entry['feature_id'] for entry in old.values()}
    owners = {entry['feature_id'] for entry in additions.values()} - existing_owners
    allowed = {'delivery-index.json': 'delivery-creation-index'}
    for entry in additions.values():
        allowed[entry['path'] + '/README.md'] = 'delivery-creation-source'
    for owner in owners:
        for name in ('README.md', 'deliveries.md', 'requirements.md', 'backlog.md'):
            path = f'features/{owner}/{name}'
            if api.git(analytics, 'cat-file', '-e', 'HEAD:' + path).returncode == 0:
                raise ValueError('Creation scope cannot change an existing feature document: ' + path)
            allowed[path] = 'feature-creation-source'
    index = safe_path(analytics, 'features/README.md')
    if owners and index.is_file():
        prior = api.git(analytics, 'show', 'HEAD:features/README.md')
        before = prior.stdout.splitlines() if prior.returncode == 0 else []
        after = index.read_text().splitlines()
        # Keep all accepted content in order; only scoped feature links may be added.
        import difflib
        for tag, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            if any(line.strip() for line in before[a:b]):
                raise ValueError('Creation scope cannot replace existing feature index content')
            for line in after[c:d]:
                if not line.strip():
                    continue
                links = re.findall(r'\]\(([^)]+)\)', line)
                if (not before and (line.startswith('#') or re.fullmatch(r'[| :\-]+', line))):
                    continue
                if not links or any(not any(link in {f'{owner}/README.md', f'./{owner}/README.md',
                                                     f'{owner}/', f'./{owner}/'} for owner in owners) for link in links):
                    raise ValueError('Feature index additions must link only newly confirmed features')
        allowed['features/README.md'] = 'feature-creation-index'
    for relative in allowed:
        safe_path(analytics, relative)
    return allowed, additions
