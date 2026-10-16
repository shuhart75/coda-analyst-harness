"""Explicit cancellation of a review candidate; never merge or delete remote refs."""
from pathlib import Path
import hashlib
import json
import shutil
import tempfile


def cancelled(record):
    value = record.get('publication_cancellation')
    if value is None:
        return False
    required = {'schema_version', 'reason', 'cancelled_at', 'request_commit',
                'request_branch', 'target_branch', 'repository_url', 'snapshot', 'files'}
    if (not isinstance(value, dict) or set(value) != required or value['schema_version'] != 1
            or record.get('publication_confirmed') or not value['reason'] or not value['files']):
        raise ValueError('Повреждена запись отмены публикации')
    strings = required - {'schema_version', 'files'}
    if any(not isinstance(value[k], str) or not value[k].strip() for k in strings):
        raise ValueError('Повреждена идентичность отмены')
    if (not isinstance(value['files'], dict)
            or any(not isinstance(k, str) or Path(k).is_absolute() or '..' in Path(k).parts
                   or not isinstance(v, str) or len(v) != 64
                   for k, v in value['files'].items())):
        raise ValueError('Повреждён инвентарь отмены')
    return True


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Снимок отсутствует или является симлинком')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Симлинк в снимке отменённой публикации')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify_snapshot(record):
    if not cancelled(record):
        raise ValueError('Нет записи отмены')
    c = record['publication_cancellation']
    root = Path(c['snapshot'])
    if not root.is_dir() or root.is_symlink() or inventory(root) != c['files']:
        raise ValueError('Снимок отменённой публикации изменён или недоступен')
    return root


def command(api, args):
    if not args.analyst_confirmed or not args.reason.strip():
        raise ValueError('Отмена требует явного решения аналитика и причины')
    project = Path(args.project).resolve()
    api.validate_feature(args.feature)
    api.require_exchange_delivery_binding(project, args.feature)
    state_path = api.layout_feature_root(project, args.feature) / 'requirements-state.json'
    original = state_path.read_bytes()
    state = api.load_json(state_path)
    records = api.stages.registry(state)['revisions']
    record = next((r for r in records if r['entry']['revision'] == args.revision), None)
    if not record or record['destination_role'] != 'code' or record['publication_confirmed']:
        raise ValueError('Можно отменить только неподтверждённого кандидата в code')
    if cancelled(record):
        if record['publication_cancellation']['request_commit'] != args.expected_commit:
            raise ValueError('Коммит отмены не совпал')
        verify_snapshot(record)
        print(json.dumps({'status': 'already-cancelled', 'revision': args.revision}))
        return 0
    manifest_path = Path(record['manifest_path'])
    manifest = api.load_json(manifest_path)
    api.require_manifest_binding(project, args.feature, manifest)
    api.stages.require_manifest_history(state, manifest)
    pub = manifest.get('publication', {})
    if pub.get('state') != 'awaiting-merge' or pub.get('request_commit') != args.expected_commit:
        raise ValueError('Коммит или состояние кандидата не совпали')
    branch, target, remote = (pub.get(k) for k in ('request_branch', 'target_branch', 'repository_url'))
    if not branch or not target or not remote or branch == target:
        raise ValueError('Некорректная идентичность кандидата')
    refs = api.remote_heads(remote, f'refs/heads/{branch}')
    if refs.get(f'refs/heads/{branch}') != args.expected_commit:
        raise ValueError('Ветка кандидата отсутствует или изменилась; отмена остановлена')
    with tempfile.TemporaryDirectory(prefix='cancel-publication-') as tmp:
        clone = Path(tmp) / 'code'
        import subprocess
        r = subprocess.run(['git', 'clone', '--quiet', '--single-branch', '--branch', target, remote, str(clone)], capture_output=True, text=True)
        if r.returncode:
            raise ValueError('Не удалось проверить целевую ветку')
        if api.git(clone, 'fetch', '--quiet', 'origin', f'refs/heads/{branch}').returncode:
            raise ValueError('Не удалось прочитать кандидата')
        if api.git_value(clone, 'rev-parse', 'FETCH_HEAD') != args.expected_commit:
            raise ValueError('Ветка кандидата изменилась')
        if api.git(clone, 'merge-base', '--is-ancestor', args.expected_commit, 'HEAD').returncode == 0:
            raise ValueError('Кандидат уже слит; отмена запрещена')
        target_manifest = clone / api.EXCHANGE_DIR / args.feature / 'manifest.json'
        if target_manifest.exists() and any(e.get('revision') == args.revision for e in api.load_json(target_manifest).get('revisions', [])):
            raise ValueError('Редакция уже существует в целевой ветке; требуется разбор')
        if api.git(clone, 'switch', '--quiet', '--detach', args.expected_commit).returncode:
            raise ValueError('Не удалось открыть кандидата')
        feature_dir = clone / api.EXCHANGE_DIR / args.feature
        api.require_plain_exchange(clone / api.EXCHANGE_DIR)
        candidate = api.load_json(feature_dir / 'manifest.json')
        errors = api.validate_manifest(candidate, feature_dir)
        request = api.validate_publication_request(candidate)
        if not request or request.get('request_branch') != branch or request.get('target_branch') != target:
            raise ValueError('Идентичность кандидата не совпала с запросом')
        api.require_manifest_binding(project, args.feature, candidate)
        api.stages.require_manifest_history(state, candidate)
        entry = next((e for e in candidate['revisions'] if e['revision'] == args.revision), None)
        if errors or entry != record['entry']:
            raise ValueError('Снимок кандидата не совпадает с реестром')
        archive = api.state_root() / 'cancelled-publications' / hashlib.sha256(remote.encode()).hexdigest() / args.feature / args.expected_commit
        revision = feature_dir / 'revisions' / f'{args.revision:03d}'
        files = inventory(revision)
        if archive.exists():
            if inventory(archive) != files:
                raise ValueError('Существующий архив отмены отличается')
        else:
            archive.parent.mkdir(parents=True, exist_ok=True)
            staging = archive.with_name(archive.name + '.pending')
            if staging.exists():
                raise ValueError('Есть незавершённое сохранение архива; требуется проверка')
            shutil.copytree(revision, staging)
            if inventory(staging) != files:
                raise ValueError('Не удалось сохранить точный снимок')
            staging.rename(archive)
        # Recheck the remote refs and local state immediately before recording the decision.
        if api.remote_heads(remote, f'refs/heads/{branch}').get(f'refs/heads/{branch}') != args.expected_commit:
            raise ValueError('Ветка изменилась во время отмены')
        target_sha = api.git_value(clone, 'rev-parse', f'origin/{target}')
        if api.remote_heads(remote, f'refs/heads/{target}').get(f'refs/heads/{target}') != target_sha:
            raise ValueError('Целевая ветка изменилась во время отмены')
        if state_path.read_bytes() != original:
            raise ValueError('Состояние изменилось во время отмены')
        record['publication_cancellation'] = dict(schema_version=1, reason=args.reason.strip(),
            cancelled_at=api.now(), request_commit=args.expected_commit, request_branch=branch,
            target_branch=target, repository_url=remote, snapshot=str(archive), files=files)
        state['updated_at'] = api.now()
        api.save_json(state_path, state)
    print(json.dumps({'status': 'publication-cancelled', 'revision': args.revision,
        'snapshot': str(archive), 'remote_changed': False, 'publication_confirmed': False,
        'next_action': 'Закрыть PR/MR кандидата без слияния; затем prepare нового входа'}, ensure_ascii=False))
    return 0


def restore_history(api, exchange, feature, state, remote, target):
    """Include cancelled immutable snapshots in the next review branch, never overwrite target history."""
    records = [r for r in api.stages.registry(state)['revisions'] if cancelled(r)]
    if not records:
        return
    root = exchange / feature
    manifest_path = root / 'manifest.json'
    # Use the cached manifest only as a skeleton; entries come from verified state.
    manifest = api.load_json(manifest_path) if manifest_path.exists() else api.load_json(Path(records[-1]['manifest_path']))
    manifest.pop('publication', None)
    manifest.pop('publication_request', None)
    if not manifest_path.exists():
        manifest['revisions'] = []
    for record in records:
        c = record['publication_cancellation']
        if (c['repository_url'], c['target_branch']) != (remote, target):
            raise ValueError('Отменённая история относится к другому месту передачи')
        snapshot = verify_snapshot(record)
        entry = dict(record['entry'])
        entry['state'] = 'cancelled'
        existing = next((e for e in manifest['revisions'] if e['revision'] == entry['revision']), None)
        dest = root / 'revisions' / f"{entry['revision']:03d}"
        if existing:
            if existing != entry or inventory(dest) != c['files']:
                raise ValueError('Отменённая редакция появилась или изменилась в target; требуется разбор')
        else:
            if dest.exists():
                raise ValueError('Путь отменённой редакции уже занят')
            shutil.copytree(snapshot, dest)
            manifest['revisions'].append(entry)
    api.save_json(manifest_path, manifest)
