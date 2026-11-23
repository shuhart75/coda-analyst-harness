"""Explicit, read-only-from-code migration of accepted exchange history to documents."""
from pathlib import Path
import shutil

import sdd_bundle
import delivery_stages
from tracker_safety import require_write_context


def validate(api, project, feature, state, *, root=None):
    records = [r for r in delivery_stages.registry(state)['revisions'] if r['destination_role'] == 'code']
    if not records:
        return
    root = root or project / api.EXCHANGE_DIR / feature
    proof = api.load_json(root / 'documents-migration.json')
    if (proof.get('schema_version') != 1 or proof.get('feature') != feature
            or not proof.get('analyst_confirmed') or not proof.get('reason')
            or proof.get('entries') != [delivery_stages.immutable_entry(r['entry']) for r in records]):
        raise ValueError('Explicit migration must preserve all historical revision identities')
    files = proof.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('Migration needs original file inventory')
    for name, digest in files.items():
        path = root / sdd_bundle._path(name)
        sdd_bundle._plain(path)
        if api.sha256(path) != digest:
            raise ValueError('Historical migration bytes changed: ' + name)


def command(api, args):
    project = Path(args.project).resolve()
    api.validate_feature(args.feature)
    if not args.analyst_confirmed or not args.reason.strip():
        raise ValueError('Migration requires an explicit analyst decision')
    require_write_context(project, modes=('requirements',), feature=args.feature)
    state = api.load_json(api.layout_feature_root(project, args.feature) / 'requirements-state.json')
    records = delivery_stages.registry(state)['revisions']
    if not records or any(not r['publication_confirmed'] for r in records):
        raise ValueError('Resolve pending publication before changing destination')
    latest = max(records, key=lambda r: r['entry']['revision'])
    if latest['destination_role'] != 'code':
        raise ValueError('This delivery is already in documents')
    source = api.publication_root(project, args.feature, latest)
    errors = api.validate_manifest(api.load_json(source / 'manifest.json'), source)
    if errors:
        raise ValueError('; '.join(errors))
    api.require_plain_exchange(source.parent)
    before = sdd_bundle._files(source)
    destination = project / api.EXCHANGE_DIR / args.feature
    sdd_bundle._plain(destination)
    if destination.exists():
        validate(api, project, args.feature, state)
        return {'status':'already-copied', 'path':str(destination), 'next_action':'save-and-accept-migration-before-prepare'}
    destination.parent.mkdir(parents=True, exist_ok=True)
    api.install_root_contract(destination.parent)
    try:
        shutil.copytree(source, destination)
        if sdd_bundle._files(source) != before or sdd_bundle._files(destination) != before:
            raise ValueError('Migration source changed during copy')
        # Manifest state can evolve in new revisions; immutable history stays pinned.
        historical = {k:v for k,v in before.items() if k.startswith('revisions/')}
        api.save_json(destination/'documents-migration.json', {
            'schema_version':1, 'feature':args.feature, 'analyst_confirmed':True,
            'reason':args.reason.strip(), 'entries':[delivery_stages.immutable_entry(r['entry']) for r in records if r['destination_role']=='code'],
            'files':historical, 'original_manifest_sha256':before['manifest.json']})
        validate(api, project, args.feature, state)
    except Exception:
        shutil.rmtree(destination)
        raise
    return {'status':'copied', 'path':str(destination), 'code_writes':False,
            'next_action':'save-and-accept-migration-before-prepare'}
