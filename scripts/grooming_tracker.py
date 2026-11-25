"""Publication-bound Story payloads and a local single-attempt journal.

The agent uses current SberTrack capabilities; this tool performs no network calls.
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import grooming
import sdd_bundle
import story_delivery


def read(path):
    return sdd_bundle._json(Path(path))


def prepare(project, feature, web_url, task_keys=None):
    from project_layout import feature_root, exchange_binding
    root = feature_root(project, feature)
    binding = exchange_binding(project, feature)
    key = binding['delivery_key'] if binding else feature
    state = read(root / 'requirements-state.json')
    published = state.get('last_published') or {}
    if published.get('destination_role') not in {'code', 'analytics'}:
        raise ValueError('Stories require a confirmed repository publication, not a local fallback')
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
    if not any(r.get('publication_confirmed') is True and r.get('destination_role') == published['destination_role']
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
    if manifest.get('schema_version') == 6:
        repository_name = urlsplit(proof['repository_url']).path.rstrip('/').split('/')[-1].removesuffix('.git')
        if parsed.path.rstrip('/').split('/')[-1] != repository_name:
            raise ValueError('Story links must point to the published documents repository')
    keys = story_delivery.task_keys(plan, task_keys)
    payloads = []
    for task in plan['tasks']:
        identity = {'repository': proof['repository_url'], 'delivery': key,
                    'stage': plan['stage_id'], 'task': task['id']}
        request_id = grooming.fingerprint(identity)
        payloads.append({'local_id': task['id'], 'request_id': request_id, 'type': 'Story',
                         **({'tracker_key': keys[task['id']]} if task['id'] in keys else {}),
                         'summary': task['summary'], 'epic_key': plan['epic_key'],
                         **({'estimate': task['estimate']} if task.get('estimate') is not None else {}),
                         'estimate_unit': 'person-days', 'labels': ['RSCON_dev'],
                         **story_delivery.REQUIRED_FIELDS,
                         **story_delivery.delivery_content(task, key, revision, proof['target_commit'], web_url, request_id),
                         'publication_commit': proof['target_commit']})
    data = {'schema_version': 2, 'provider': 'sbertrek', 'delivery': key, 'revision': revision,
            'input_sha256': entry['sdd_input']['sha256'], 'repository_url': proof['repository_url'],
            'target_branch': proof['target_branch'], 'web_url': web_url, 'tasks': payloads}
    return {**data, 'confirmation_sha256': grooming.fingerprint(data)}


def pointer(data, path):
    if isinstance(path, dict):
        # A raw card may expose an enum/reference ID, with its meaning in a
        # separate, unchanged metadata response. Verify the join, not a guessed ID.
        lookup = path['lookup']
        actual_id = pointer(data, path['path'])
        metadata = read(lookup['evidence'])
        if actual_id != pointer(metadata, lookup['id']):
            raise ValueError('Tracker field ID does not match the supplied metadata evidence')
        return pointer(metadata, lookup['value'])
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Evidence mapping needs JSON pointers into the full response')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def verify_evidence(raw, mapping, payload):
    data = json.loads(raw)
    fields = {name: pointer(data, mapping[name]) for name in
              ('key', 'type', 'summary', 'epic_key', 'labels', 'description')}
    if payload.get('estimate') is not None:
        fields['estimate'] = pointer(data, mapping['estimate'])
    if 'requirements_url' in mapping:
        fields['requirements_url'] = pointer(data, mapping['requirements_url'])
        if fields['requirements_url'] != payload['requirements_url']:
            raise ValueError('Created Story differs from confirmed payload: requirements_url')
    if not grooming.KEY.fullmatch(str(fields['key'])):
        raise ValueError('Evidence has no native SberTrack key')
    if payload.get('tracker_key') and fields['key'] != payload['tracker_key']:
        raise ValueError('Evidence does not belong to the supplied SberTrack task')
    for name in ('type', 'summary', 'epic_key') + (('estimate',) if payload.get('estimate') is not None else ()):
        if fields[name] != payload[name] or (name == 'estimate' and type(fields[name]) not in (int, float)):
            raise ValueError('Created Story differs from confirmed payload: ' + name)
    if not isinstance(fields['labels'], list) or 'RSCON_dev' not in fields['labels']:
        raise ValueError('Created Story is missing RSCON_dev')
    if not set(payload.get('labels', [])) <= set(fields['labels']):
        raise ValueError('Existing labels were lost during update')
    for name in story_delivery.REQUIRED_FIELDS:
        if name in payload:
            if name not in mapping:
                raise ValueError('Read back the mandatory field: ' + name)
            if not story_delivery.field_matches(name, pointer(data, mapping[name]), payload[name]):
                raise ValueError('Story has an incorrect mandatory field: ' + name)
    if (not isinstance(fields['description'], str) or 'grooming/' + payload['request_id'] not in fields['description']
            or payload['description'].replace('\r\n', '\n') not in fields['description'].replace('\r\n', '\n')):
        raise ValueError('Evidence has no request identity and requirements link')
    return fields['key']


def observe(raw, mapping):
    data = json.loads(raw)
    fields = {name: pointer(data, mapping[name]) for name in ('key', 'type', 'summary', 'labels', 'description')}
    if not grooming.KEY.fullmatch(str(fields['key'])):
        raise ValueError('Evidence has no native SberTrack key')
    # Explicit nulls mean empty fields; absent JSON pointers remain an error.
    if fields['description'] is None:
        fields['description'] = ''
    if fields['labels'] is None:
        fields['labels'] = []
    if (not isinstance(fields['type'], str) or not fields['type'].strip()
            or not isinstance(fields['description'], str) or not isinstance(fields['summary'], str)
            or not isinstance(fields['labels'], list) or not all(isinstance(x, str) for x in fields['labels'])):
        raise ValueError('Read the complete current task description, summary and labels')
    return fields


def unique_key(area, ledger, key):
    for path in area.glob('*.json'):
        if path != ledger and read(path).get('key') == key:
            raise ValueError('Story is already bound to another task')


def update_payload(task, fields, previous=None):
    # Existing titles and estimates are not part of this task's metadata update.
    result = {**task, 'tracker_key': fields['key'], 'type': fields['type'], 'summary': fields['summary'],
              'labels': list(dict.fromkeys(fields['labels'] + ['RSCON_dev'])),
              'description': story_delivery.merge_description(fields['description'], task, previous)}
    result.pop('estimate', None)
    return result


def write_fields(payload, operation):
    names = ['epic_key', 'labels', 'description', *story_delivery.REQUIRED_FIELDS]
    if operation == 'create':
        if payload.get('type') != 'Story':
            raise ValueError('New delivery tasks must be Story')
        names += ['type', 'summary', 'estimate']
    return {name: payload[name] for name in names if name in payload}


def evidence_hashes(mapping):
    result = {}
    def collect(value):
        if isinstance(value, dict):
            if 'lookup' in value:
                path = Path(value['lookup']['evidence']).resolve()
                result[str(path)] = grooming.digest(path.read_bytes())
            for child in value.values():
                collect(child)
    collect(mapping)
    return result


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
        if task.get('tracker_key') and prior.get('key') and task['tracker_key'] != prior['key']:
            raise ValueError('Supplied key conflicts with the previously resolved task')
        if action == 'status':
            return {'task': task, 'record': prior, 'next_action':
                    'reconcile-required' if prior.get('state') == 'attempted' else
                    'complete' if prior.get('state') == 'bound' and prior.get('payload_sha256') == payload_hash else
                    'claim-update' if prior.get('state') == 'resolved' and prior.get('source_sha256') == payload_hash else
                    'read-existing-task' if prior.get('key') or task.get('tracker_key') else 'search-request-id-before-create'}
        if action == 'record-created':
            if prior.get('state') != 'attempted' or prior.get('operation') != 'create':
                raise ValueError('A creation response requires an outstanding create attempt')
            raw = Path(evidence).read_bytes()
            key = pointer(json.loads(raw), read(mapping)['key'])
            if not grooming.KEY.fullmatch(str(key)) or (prior.get('key') and prior['key'] != key):
                raise ValueError('Invalid or conflicting created task key')
            unique_key(area, ledger, key)
            record = {**prior, 'key': key, 'creation_evidence_sha256': grooming.digest(raw)}
            save(ledger, record)
            return {**record, 'next_action': 'read-created-task-and-resolve',
                    'create_allowed': False, 'complete': False}
        if action == 'resolve':
            raw = Path(evidence).read_bytes()
            paths = read(mapping)
            fields = observe(raw, paths)
            if prior.get('state') == 'attempted' and prior.get('operation') == 'create' and fields['type'] != 'Story':
                raise ValueError('Created task must be Story; creation is not complete')
            expected = prior.get('key') or task.get('tracker_key')
            if expected and fields['key'] != expected:
                raise ValueError('Read the exact supplied or previously resolved SberTrack task')
            if not expected and 'grooming/' + request not in fields['description']:
                raise ValueError('Found task has no stable request marker; provide the analyst-supplied key mapping')
            unique_key(area, ledger, fields['key'])
            desired = update_payload(task, fields, prior.get('payload'))
            record = {'state': 'resolved', 'request_id': request, 'key': fields['key'],
                      'source_sha256': payload_hash, 'payload': desired,
                      'evidence_sha256': grooming.digest(raw), 'metadata_evidence': evidence_hashes(paths)}
            try:
                verify_evidence(raw, paths, desired)
            except (ValueError, KeyError, TypeError, IndexError):
                pass
            else:
                record.update(state='bound', payload_sha256=payload_hash)
            save(ledger, record)
            return {**record, 'next_action': 'complete' if record['state'] == 'bound' else 'claim-update',
                    'write_fields': write_fields(desired, 'update') if record['state'] != 'bound' else {}}
        if action == 'claim':
            if prior.get('state') == 'attempted':
                return {**prior, 'create_allowed': False, 'update_allowed': False, 'status': 'reconcile-required'}
            if prior.get('state') == 'bound' and prior.get('payload_sha256') == payload_hash:
                return {**prior, 'create_allowed': False, 'update_allowed': False, 'status': 'already-bound'}
            if prior.get('key') or task.get('tracker_key'):
                if prior.get('state') != 'resolved' or prior.get('source_sha256') != payload_hash:
                    return {'create_allowed': False, 'update_allowed': False, 'status': 'read-existing-task',
                            'key': prior.get('key') or task.get('tracker_key'), 'next_action': 'resolve-from-current-card'}
            if confirmation != batch['confirmation_sha256']:
                raise ValueError('Use the current batch hash under the analyst-authorized delivery request')
            record = {'state': 'attempted', 'request_id': request, 'payload_sha256': payload_hash,
                      'payload': prior['payload'] if prior.get('state') == 'resolved' else task,
                      'batch_sha256': confirmation, 'key': prior.get('key'),
                      'operation': 'update' if prior.get('key') else 'create'}
            fields = write_fields(record['payload'], record['operation'])
            save(ledger, record)
            return {**record, 'create_allowed': record['operation'] == 'create',
                    'update_allowed': record['operation'] == 'update',
                    'next_action': 'perform-one-external-operation',
                    'write_fields': fields}
        if prior.get('state') != 'attempted':
            raise ValueError('Claim the exact payload before binding; never retry blindly')
        # Reconcile a previous revision first; the next claim can then update it.
        task = prior['payload']
        raw = Path(evidence).read_bytes()
        paths = read(mapping)
        key = verify_evidence(raw, paths, task)
        if prior.get('key') and prior['key'] != key:
            raise ValueError('Update cannot replace the existing Story key')
        # Do not let two local tasks bind to the same native story.
        unique_key(area, ledger, key)
        record = {**prior, 'state': 'bound', 'key': key, 'evidence_sha256': grooming.digest(raw),
                  'metadata_evidence': evidence_hashes(paths)}
        save(ledger, record)
        return {**record, 'next_action': 'continue-remaining-stories'}


def save(path, value):
    temporary = path.with_suffix('.tmp')
    sdd_bundle._plain(temporary)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def batch_status(area, batch):
    tasks = [operate(area, batch, 'status', task['local_id']) for task in batch['tasks']]
    completed = sum(item['next_action'] == 'complete' for item in tasks)
    return {'status': 'complete' if completed == len(tasks) else 'tasks-pending',
            'completed': completed, 'total': len(tasks), 'tasks': tasks,
            'next_task': next((item['task']['local_id'] for item in tasks if item['next_action'] != 'complete'), None),
            'evidence': 'verified-card-responses-in-local-journal'}


def main():
    p = argparse.ArgumentParser(description='SberTrack grooming Story journal, no network calls')
    p.add_argument('action', choices=['preview', 'status', 'resolve', 'claim', 'record-created', 'bind'])
    p.add_argument('project', type=Path)
    p.add_argument('feature')
    p.add_argument('--web-url', required=True)
    p.add_argument('--task')
    p.add_argument('--confirmed-batch')
    p.add_argument('--evidence', type=Path)
    p.add_argument('--mapping', type=Path)
    p.add_argument('--task-keys', type=Path, help='JSON local task ID -> supplied native SberTrack key')
    a = p.parse_args()
    try:
        from workspace_paths import state_root
        batch = prepare(a.project.resolve(), a.feature, a.web_url, read(a.task_keys) if a.task_keys else None)
        area = state_root() / 'grooming-tracker'
        result = (batch if a.action == 'preview' else batch_status(area, batch) if a.action == 'status' and not a.task
                  else operate(area, batch, a.action, a.task, confirmation=a.confirmed_batch,
                               evidence=a.evidence, mapping=a.mapping))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, IndexError, StopIteration) as exc:
        p.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
