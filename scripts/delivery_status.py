"""Read-only evidence for publication, registration and current authored input."""
from pathlib import Path
import json
import subprocess

import delivery_stages as stages
import sdd_bundle


def _json_at(api, project, commit, path):
    result = api.git(project, 'show', f'{commit}:{path}')
    return json.loads(result.stdout) if result.returncode == 0 else None


def inspect(api, project, feature, *, refresh=False):
    project = Path(project).resolve()
    api.validate_feature(feature)
    root = api.layout_feature_root(project, feature)
    state_path = root / 'requirements-state.json'
    state = api.load_json(state_path)
    records = stages.registry(state)['revisions']
    result = {'feature': feature, 'publication': 'unknown', 'registration': 'unknown',
              'tracker_updates': 'not-checked', 'implementation': 'not-checked',
              'evidence_source': 'fetched-origin/main' if refresh else 'cached-origin/main',
              'errors': [], 'cleanup_warnings': []}
    # This command never uses a local feature branch as proof of acceptance.
    if refresh:
        fetched = api.git(project, 'fetch', '--no-tags', 'origin', 'main')
        if fetched.returncode:
            result.update(status='verification-unavailable', next_action='retry-status-with-refresh')
            result['errors'].append('Не удалось получить origin/main: ' + fetched.stderr.strip())
            return result
    resolved = api.git(project, 'rev-parse', '--verify', 'refs/remotes/origin/main')
    commit = resolved.stdout.strip() if resolved.returncode == 0 else None
    if not commit:
        result.update(status='verification-unavailable', next_action='run-status-with-refresh')
        result['errors'].append('Нет проверяемой ссылки origin/main')
        return result
    result['target_commit'] = commit
    packet_path = f'{api.EXCHANGE_DIR}/{feature}'
    manifest = _json_at(api, project, commit, packet_path + '/manifest.json')
    if manifest is None:
        migration = any(r['destination_role'] == 'code' for r in records)
        result.update(status='migration-required' if migration else 'not-published', publication='absent',
                      registration='legacy-code' if migration else 'absent',
                      next_action='migrate-code-exchange' if migration else 'prepare-requirements')
        return result
    with api.DisposablePublicationClone(prefix='delivery-status-', cleanup_warnings=result['cleanup_warnings']) as tmp:
        exchange = Path(tmp) / api.EXCHANGE_DIR
        packet = exchange / feature
        packet.mkdir(parents=True)
        tree = api.git(project, 'ls-tree', '-r', '-z', commit, '--', packet_path, api.EXCHANGE_DIR + '/AGENTS.md')
        if tree.returncode:
            raise ValueError('Не удалось прочитать состав принятого пакета')
        for row in filter(None, tree.stdout.split('\0')):
            meta, name = row.split('\t', 1)
            mode, kind, oid = meta.split()
            if mode not in {'100644', '100755'} or kind != 'blob' or name.lower().endswith('.zip'):
                raise ValueError('Недопустимый файл пакета: ' + name)
            destination = Path(tmp) / sdd_bundle._path(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = subprocess.run(['git', '-C', str(project), 'cat-file', 'blob', oid], capture_output=True, check=True).stdout
            destination.write_bytes(data)
        errors = api.validate_manifest(manifest, packet)
        try:
            api.require_manifest_binding(project, feature, manifest, historical=api.registered_bindings(records))
            stages.require_manifest_history(state, manifest)
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            result.update(status='invalid-published-packet', next_action='review-published-packet')
            result['errors'] = errors
            return result
        entry = next(x for x in manifest['revisions'] if x['revision'] == manifest['active_revision'])
        result.update(revision=entry['revision'], requirements_repository_path=packet_path + '/' + entry['requirements_path'])
        if entry.get('state') == 'cancelled':
            result.update(status='cancelled', publication='cancelled', next_action='review-cancelled-publication')
            return result
        result['publication'] = 'accepted'
        target_index = _json_at(api, project, commit, 'delivery-index.json')
        target_root = (target_index.get('deliveries', {}).get(feature, {}).get('path')
                       if isinstance(target_index, dict) else None)
        target_state_path = (sdd_bundle._path(target_root) + '/requirements-state.json' if target_root else
                             state_path.relative_to(project).as_posix())
        target_state = _json_at(api, project, commit, target_state_path)
        def registered(value):
            return any(r.get('publication_confirmed') is True and r.get('destination_role') == 'analytics'
                       and stages.immutable_entry(r['entry']) == stages.immutable_entry(entry)
                       for r in stages.registry(value)['revisions']) if value else False
        result['registration'] = 'accepted' if registered(target_state) else 'local-only' if registered(state) else 'absent'
        result['status'] = 'published' if result['registration'] == 'accepted' else 'publication-registration-pending'
        result['next_action'] = ('handoff-complete' if result['registration'] == 'accepted' else
                                 'save-and-accept-publication-registration' if result['registration'] == 'local-only' else
                                 'verify-prepare-then-requirementsctl-mark-published')
        # New authoring does not undo an already accepted publication.
        current = root / 'requirements.md'
        result['current_requirements_match'] = current.is_file() and api.sha256(current) == entry['sha256']
        try:
            expected = entry.get('sdd_input')
            result['current_sdd_match'] = (sdd_bundle.inspect(root / 'sdd', current.read_text()) == expected
                                           if expected else not (root / 'sdd').exists())
        except (OSError, ValueError):
            result['current_sdd_match'] = False
    return result
