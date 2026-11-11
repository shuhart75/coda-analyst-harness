"""Explicit source-side task requests and resumable receiver binding. No network calls."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path, PurePosixPath

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

def git(repo, *args):
    r = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True)
    if r.returncode:
        raise ValueError(r.stderr.strip() or 'Git failed')
    return r.stdout

def read_blob(repo, revision, path):
    r = subprocess.run(['git', '-C', str(repo), 'show', revision + ':' + path], text=True, capture_output=True)
    return r.stdout if r.returncode == 0 else None

def pending_requests(repo, revision='HEAD'):
    raw = read_blob(repo, revision, 'delivery-index.json')
    index = json.loads(raw) if raw else {'deliveries': {}}
    entries = index['deliveries']
    paths = git(repo, 'ls-tree', '-r', '--name-only', revision).splitlines()
    result = []
    for path in paths:
        if not path.endswith('/' + REQUEST_FILE):
            continue
        parent = path.rsplit('/', 1)[0]
        owners = [key for key, entry in entries.items() if entry['path'] == parent]
        if len(owners) != 1:
            raise ValueError('Task request outside a registered delivery: ' + path)
        request = validate_request(json.loads(read_blob(repo, revision, path)), owners[0])
        binding = read_blob(repo, revision, parent + '/analysis-task.json')
        if binding is not None:
            b = json.loads(binding)
            if (b.get('provider') != 'sbertrek' or b.get('role') != 'AN'
                    or not KEY.fullmatch(str(b.get('key', ''))) or not b.get('confirmation')):
                raise ValueError('Invalid receiver task binding: ' + parent)
            continue
        result.append({**request, 'path': path})
    ids = [r['request_id'] for r in result]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate task request identity')
    return result

def import_context(root):
    state = json.loads((root / '.workspace-state/source-import.json').read_text())
    incoming = state['source_commit']
    if not re.fullmatch(r'[0-9a-f]{40,64}', incoming):
        raise ValueError('Invalid source import identity')
    repo = root / '.workspace-state/source-imports' / incoming / 'repository'
    if repo.resolve() != Path(state['checkout']).resolve():
        raise ValueError('Unexpected import checkout')
    if git(repo, 'branch', '--show-current').strip() != state['request_branch']:
        raise ValueError('Unexpected import branch')
    if git(repo, 'remote', 'get-url', 'origin').strip() != state['repository_url']:
        raise ValueError('Unexpected import remote')
    return repo

def operate(root, action, request_id, key=None, evidence=None):
    # Same workspace lock as sync; a second agent cannot claim the request twice.
    import fcntl
    folder = root / '.workspace-state'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'repository-exchange.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        repo = import_context(root)
        matches = [r for r in pending_requests(repo) if r['request_id'] == request_id]
        ledger = folder / 'receiver-task-attempts' / (str(uuid.UUID(request_id)) + '.json')
        if not matches:
            return {'status': 'already-bound-or-not-pending', 'request_id': request_id}
        request = matches[0]
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        receipt = json.loads(ledger.read_text()) if ledger.exists() else None
        if receipt and receipt['request_sha256'] != fingerprint:
            raise ValueError('Task request changed after creation attempt')
        if action == 'claim':
            if receipt:
                return {**receipt, 'status': 'reconcile-required', 'create_allowed': False}
            if git(repo, 'status', '--porcelain').strip():
                raise ValueError('Import checkout is not clean')
            receipt = {'request_id': request_id, 'request_sha256': fingerprint,
                       'status': 'creation-attempt-recorded', 'create_allowed': True}
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open('x') as out:
                json.dump(receipt, out, ensure_ascii=False, indent=2)
            return receipt
        if not receipt:
            raise ValueError('Claim request before binding a creation result')
        if not KEY.fullmatch(key or '') or evidence is None:
            raise ValueError('Actual tracker key and evidence file required')
        raw = evidence.read_bytes()
        if not re.search(r'(?<![A-Za-z0-9_])' + re.escape(key) + r'(?![A-Za-z0-9_])', raw.decode()):
            raise ValueError('Tracker evidence does not contain the key')
        relative = request['path'].rsplit('/', 1)[0] + '/analysis-task.json'
        path = repo / relative
        if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
            raise ValueError('Unsafe binding path')
        binding = {'schema_version': 1, 'provider': 'sbertrek', 'role': 'AN', 'key': key,
                   'confirmation': request['confirmation'], 'request_id': request_id,
                   'evidence_sha256': hashlib.sha256(raw).hexdigest()}
        payload = json.dumps(binding, ensure_ascii=False, indent=2) + '\n'
        dirty = git(repo, 'status', '--porcelain', '--untracked-files=all').splitlines()
        if dirty and (any(line[3:] != relative for line in dirty) or not path.is_file() or path.read_text() != payload):
            raise ValueError('Unrelated or changed files in import checkout')
        path.write_text(payload)
        git(repo, 'add', '--', relative)
        git(repo, 'diff', '--cached', '--check')
        git(repo, '-c', 'user.name=Analyst Harness', '-c', 'user.email=analyst-harness@local.invalid',
            'commit', '-m', key + ': Связать аналитику поставки с задачей')
        receipt.update(status='bound', create_allowed=False, key=key, commit=git(repo, 'rev-parse', 'HEAD').strip())
        ledger.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
        return receipt

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('action', choices=['claim', 'bind'])
    p.add_argument('--request-id', required=True)
    p.add_argument('--key')
    p.add_argument('--evidence', type=Path)
    a = p.parse_args()
    try:
        print(json.dumps(operate(a.root.resolve(), a.action, a.request_id, a.key, a.evidence), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        p.exit(1, str(exc) + '\n')

if __name__ == '__main__':
    main()
