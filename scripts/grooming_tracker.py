"""Publication-bound Story payloads and a local single-attempt journal.

The agent uses current SberTrack capabilities; this tool performs no network calls.
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import grooming
import sdd_bundle


def read(path):
    return sdd_bundle._json(Path(path))


def prepare(project, feature, web_url):
    from project_layout import feature_root, exchange_binding
    root = feature_root(project, feature)
    binding = exchange_binding(project, feature)
    key = binding['delivery_key'] if binding else feature
    state = read(root / 'requirements-state.json')
    published = state.get('last_published') or {}
    if published.get('destination_role') != 'code':
        raise ValueError('Stories require confirmed publication in code, not local fallback')
    manifest = read(published['manifest_path'])
    proof = manifest.get('publication', {})
    if (proof.get('state') != 'merged' or not sdd_bundle.COMMIT.fullmatch(str(proof.get('target_commit', '')))
            or not proof.get('verified_at') or not proof.get('target_branch') or not proof.get('repository_url')):
        raise ValueError('Repeat prepare to verify publication before Story creation')
    revision = published['revision']
    entry = next(e for e in manifest['revisions'] if e['revision'] == revision)
    if (manifest.get('feature') != key or manifest.get('active_revision') != revision
            or entry.get('state') not in {'sent', 'in-progress'}
            or entry.get('delivery_binding') != binding):
        raise ValueError('Wrong or inactive published delivery')
    records = state.get('delivery_stages', {}).get('revisions', [])
    if not any(r.get('publication_confirmed') is True and r.get('destination_role') == 'code'
               and r['entry']['revision'] == revision and r['entry'].get('sdd_input') == entry.get('sdd_input') for r in records):
        raise ValueError('Publication is not confirmed in the delivery registry')
    text = (root / 'requirements.md').read_text()
    if grooming.digest(text.encode()) != entry['sha256']:
        raise ValueError('Published requirements differ from current source')
    sdd_bundle.verify_descriptor(root / 'sdd', text, entry['sdd_input'])
    if 'tasks' not in entry['sdd_input']:
        raise ValueError('Published revision has no grooming tasks')
    plan = read(root / 'tasks/grooming.json')
    if plan['delivery'] != key or plan['stage_id'] != entry['stage_id']:
        raise ValueError('Grooming delivery/stage mismatch')
    parsed = urlsplit(web_url)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError('Repository browser URL must be HTTPS without credentials/query/fragment')
    payloads = []
    for task in plan['tasks']:
        identity = {'repository': proof['repository_url'], 'delivery': key,
                    'stage': plan['stage_id'], 'task': task['id']}
        request_id = grooming.fingerprint(identity)
        path = f"requirements-exchange/{key}/revisions/{revision:03d}/tasks/{task['id']}/requirements.md"
        url = web_url.rstrip('/') + '?path=' + quote(path, safe='')
        payloads.append({'local_id': task['id'], 'request_id': request_id, 'type': 'Story',
                         'summary': task['summary'], 'epic_key': plan['epic_key'],
                         'estimate': task['estimate'], 'estimate_unit': 'person-days', 'labels': ['RSCON_dev'],
                         'requirements_url': url,
                         'description': task['description'] + '\n\nКритерий приёмки: ' + task['acceptance']
                         + '\n\nТребования: ' + url + '\n\nГруминг: grooming/' + request_id})
    data = {'schema_version': 1, 'provider': 'sbertrek', 'delivery': key, 'revision': revision,
            'input_sha256': entry['sdd_input']['sha256'], 'repository_url': proof['repository_url'],
            'target_branch': proof['target_branch'], 'web_url': web_url, 'tasks': payloads}
    return {**data, 'confirmation_sha256': grooming.fingerprint(data)}


def pointer(data, path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Evidence mapping needs JSON pointers into the full response')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def verify_evidence(raw, mapping, payload):
    data = json.loads(raw)
    fields = {name: pointer(data, mapping[name]) for name in
              ('key', 'type', 'summary', 'epic_key', 'estimate', 'labels', 'description')}
    if 'requirements_url' in mapping:
        fields['requirements_url'] = pointer(data, mapping['requirements_url'])
        if fields['requirements_url'] != payload['requirements_url']:
            raise ValueError('Created Story differs from confirmed payload: requirements_url')
    if not grooming.KEY.fullmatch(str(fields['key'])):
        raise ValueError('Evidence has no native SberTrack key')
    for name in ('type', 'summary', 'epic_key', 'estimate'):
        if fields[name] != payload[name] or (name == 'estimate' and type(fields[name]) not in (int, float)):
            raise ValueError('Created Story differs from confirmed payload: ' + name)
    if not isinstance(fields['labels'], list) or 'RSCON_dev' not in fields['labels']:
        raise ValueError('Created Story is missing RSCON_dev')
    if (not isinstance(fields['description'], str) or 'grooming/' + payload['request_id'] not in fields['description']
            or payload['description'] not in fields['description']):
        raise ValueError('Evidence has no request identity and requirements link')
    return fields['key']


def operate(area, batch, action, task_id, *, confirmation=None, evidence=None, mapping=None):
    task = next((t for t in batch['tasks'] if t['local_id'] == task_id), None)
    if task is None:
        raise ValueError('Unknown grooming task')
    # CLI always reconstructs this batch from the current verified publication.
    request = task['request_id']
    if not re.fullmatch(r'[0-9a-f]{64}', request):
        raise ValueError('Invalid request identity')
    area = Path(area)
    sdd_bundle._plain(area)
    area.mkdir(parents=True, exist_ok=True)
    ledger = area / (request + '.json')
    lock_path = area / '.lock'
    sdd_bundle._plain(lock_path)
    sdd_bundle._plain(ledger)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior = read(ledger) if ledger.exists() else {}
        payload_hash = grooming.fingerprint(task)
        if action == 'status':
            return {'task': task, 'record': prior, 'next_action':
                    'reconcile-required' if prior.get('state') == 'attempted' else
                    'complete' if prior.get('payload_sha256') == payload_hash else
                    'review-update' if prior.get('key') else 'search-request-id-before-create'}
        if action == 'claim':
            if prior.get('state') == 'attempted':
                return {**prior, 'create_allowed': False, 'update_allowed': False, 'status': 'reconcile-required'}
            if prior.get('payload_sha256') == payload_hash:
                return {**prior, 'create_allowed': False, 'update_allowed': False, 'status': 'already-bound'}
            if confirmation != batch['confirmation_sha256']:
                raise ValueError('Confirm the complete current Story batch before external writes')
            record = {'state': 'attempted', 'request_id': request, 'payload_sha256': payload_hash,
                      'payload': task, 'batch_sha256': confirmation, 'key': prior.get('key'),
                      'operation': 'update' if prior.get('key') else 'create'}
            save(ledger, record)
            return {**record, 'create_allowed': record['operation'] == 'create',
                    'update_allowed': record['operation'] == 'update',
                    'next_action': 'perform-one-external-operation'}
        if prior.get('state') != 'attempted':
            raise ValueError('Claim the exact payload before binding; never retry blindly')
        # Reconcile a previous revision first; the next claim can then update it.
        task = prior['payload']
        raw = Path(evidence).read_bytes()
        key = verify_evidence(raw, read(mapping), task)
        if prior.get('key') and prior['key'] != key:
            raise ValueError('Update cannot replace the existing Story key')
        # Do not let two local tasks bind to the same native story.
        for path in area.glob('*.json'):
            if path != ledger and read(path).get('key') == key:
                raise ValueError('Story is already bound to another task')
        record = {**prior, 'state': 'bound', 'key': key, 'evidence_sha256': grooming.digest(raw)}
        save(ledger, record)
        return {**record, 'next_action': 'continue-remaining-stories'}


def save(path, value):
    temporary = path.with_suffix('.tmp')
    sdd_bundle._plain(temporary)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def main():
    p = argparse.ArgumentParser(description='SberTrack grooming Story journal, no network calls')
    p.add_argument('action', choices=['preview', 'status', 'claim', 'bind'])
    p.add_argument('project', type=Path)
    p.add_argument('feature')
    p.add_argument('--web-url', required=True)
    p.add_argument('--task')
    p.add_argument('--confirmed-batch')
    p.add_argument('--evidence', type=Path)
    p.add_argument('--mapping', type=Path)
    a = p.parse_args()
    try:
        from workspace_paths import state_root
        batch = prepare(a.project.resolve(), a.feature, a.web_url)
        result = batch if a.action == 'preview' else operate(state_root() / 'grooming-tracker', batch,
                 a.action, a.task, confirmation=a.confirmed_batch, evidence=a.evidence, mapping=a.mapping)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as exc:
        p.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
