"""Explicit analyst decision: transport current bytes without the audit workflow."""
from pathlib import Path
import copy
import fcntl
import hashlib
import json
import re
from urllib.parse import urlsplit, quote
import shutil
import subprocess
import uuid

import sdd_bundle


def inventory(root):
    return sdd_bundle._files(root)


def checked_git(api, root, *args):
    result = api.git(root, *args)
    if result.returncode:
        raise ValueError('Git: ' + result.stderr.strip())
    return result.stdout.strip()


def input_snapshot(feature):
    requirements = feature / 'requirements.md'
    sdd_bundle._plain(requirements)
    data = requirements.read_bytes()
    descriptor = sdd_bundle.inspect_transport(feature / 'sdd') if (feature / 'sdd').exists() or (feature / 'sdd').is_symlink() else None
    materials = inventory(feature / 'materials') if (feature / 'materials').exists() or (feature / 'materials').is_symlink() else None
    return data, descriptor, materials


def stage_for(api, state, title):
    registry = state.get('delivery_stages', {})
    stages = registry.get('stages', [])
    active = registry.get('active_stage_id')
    for stage in stages:
        if stage.get('stage_id') == active:
            return {key: stage[key] for key in api.stages.STAGE_FIELDS}
    # A first transfer need not require a separate registration dialogue.
    if stages:
        raise ValueError('Нельзя определить адрес этапа в существующей истории')
    return dict(stage_id='stage-1', number=1, title=title,
                goal='Текущий объём требований по решению аналитика')


def save_record(api, state_path, operation, manifest_path, merged):
    # Re-read, preserving audit, cancellation records and concurrent unrelated fields.
    state = api.load_json(state_path) if state_path.exists() else {}
    registry = state.setdefault('delivery_stages', api.stages.empty_registry())
    entry = operation['entry']
    if not registry['stages']:
        registry['stages'].append(dict(entry['stage'], state='open', started_at=entry['created_at']))
        registry['active_stage_id'] = entry['stage_id']
    records = registry['revisions']
    for prior in operation.get('reserved_history', []):
        existing = next((r for r in records if r['entry']['revision'] == prior['entry']['revision']), None)
        if existing is None:
            records.append(copy.deepcopy(prior))
        elif api.stages.revision_identity(existing['entry']) != api.stages.revision_identity(prior['entry']):
            raise ValueError('История редакции изменилась параллельно; отправленная ветка сохранена')
    for record in records:
        if record['entry']['revision'] == entry['revision']:
            if record['entry'] != entry:
                raise ValueError('Номер редакции уже занят другим входом; отправленная ветка сохранена')
            record.update(manifest_path=str(manifest_path), publication_confirmed=merged)
            break
    else:
        records.append(dict(entry=entry, destination_role='code', manifest_path=str(manifest_path),
                            publication_confirmed=merged))
    for record in records:
        if record['entry']['revision'] < entry['revision'] and not record['publication_confirmed']:
            record.setdefault('replacement_by_decision', dict(revision=entry['revision'],
                decision=entry['delivery_decision'], request_commit=operation['request_commit']))
    if merged and entry['revision'] >= (state.get('last_published') or {}).get('revision', 0):
        state['last_published'] = dict(revision=entry['revision'], sdd_input=entry.get('sdd_input'),
            requirements_sha256=entry['sha256'], published_at=api.now(), manifest_path=str(manifest_path),
            destination_role='code', stage_id=entry['stage_id'], stage_revision=entry['stage_revision'],
            stage_sha256=entry['stage_sha256'], delivery_decision=entry['delivery_decision'])
        state['revision_offer'] = dict(state='not-needed', offered_at=None,
            reason='Передано по явному решению аналитика')
    state['updated_at'] = api.now()
    api.save_json(state_path, state)


def target_contains(api, clone, operation):
    """Check delivery completion only; never compare requirements with product code."""
    target = 'refs/remotes/origin/' + operation['target_branch']
    checked_git(api, clone, 'fetch', '--quiet', 'origin',
                'refs/heads/' + operation['target_branch'] + ':' + target)
    path = 'requirements-exchange/' + operation['feature']
    raw = api.git(clone, 'show', target + ':' + path + '/manifest.json')
    if raw.returncode:
        return False
    manifest = json.loads(raw.stdout)
    entry = next((e for e in manifest.get('revisions', []) if e.get('revision') == operation['entry']['revision']), None)
    if entry is None:
        return False
    if entry != operation['entry']:
        raise ValueError('В целевой ветке этот номер редакции занят другим входом')
    prefix = path + '/revisions/' + f"{entry['revision']:03d}/"
    for name, expected in operation['files'].items():
        blob = subprocess.run(['git', '-C', str(clone), 'show', target + ':' + prefix + name], capture_output=True)
        if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != expected:
            raise ValueError('В целевой ветке изменены байты переданной редакции')
    return True


def publish(api, project, feature, state_path, operation_path, operation, before, code_root):
    clone = Path(operation['checkout'])
    entry = operation['entry']
    packet = clone / api.EXCHANGE_DIR / feature
    revision = packet / 'revisions' / f"{entry['revision']:03d}"
    if inventory(revision) != operation['files'] or api.git_value(clone, 'rev-parse', 'HEAD') != operation['request_commit']:
        raise ValueError('Подготовленный снимок отправки изменён; сохранён для разбора')
    if api.git(clone, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
        raise ValueError('Подготовленный checkout изменён; повторная отправка остановлена')
    merged = target_contains(api, clone, operation)
    ref = 'refs/heads/' + operation['request_branch']
    heads = api.remote_heads(operation['repository_url'], ref)
    if not merged:
        if heads and heads.get(ref) != operation['request_commit']:
            raise ValueError('Ветка отправки занята другим коммитом; история не перезаписана')
        if not heads:
            pushed = api.git(clone, 'push', '--porcelain',
                             'origin', operation['request_commit'] + ':' + ref)
            urls = re.findall(r'https?://[^\s]+', pushed.stdout + pushed.stderr)
            for url in urls:
                if any(part in url for part in ('/pull/', '/compare/', '/pull-requests/', '/merge_requests/')):
                    operation['merge_request_create_url'] = url
                    break
            observed = api.remote_heads(operation['repository_url'], ref)
            if observed.get(ref) != operation['request_commit']:
                raise ValueError('Отправка не подтверждена; повтори ту же команду. Снимок сохранён. ' + pushed.stderr.strip())
    remote_url = urlsplit(operation['repository_url'])
    if remote_url.hostname == 'github.com':
        repository_path = remote_url.path.removesuffix('.git').strip('/')
        operation['merge_request_create_url'] = ('https://github.com/' + repository_path + '/compare/' +
            quote(operation['target_branch'], safe='') + '...' + quote(operation['request_branch'], safe='') + '?expand=1')
    publication = dict(state='merged' if merged else 'awaiting-merge',
        repository_url=operation['repository_url'], target_branch=operation['target_branch'],
        request_branch=operation['request_branch'], request_commit=operation['request_commit'],
        target_commit=api.git_value(clone, 'rev-parse', 'refs/remotes/origin/' + operation['target_branch']),
        verified_at=api.now())
    # A new unique revision cache is used; old manifests are never rewritten.
    cache_scope = hashlib.sha256((publication['repository_url'] + '\n' + publication['target_branch']).encode()).hexdigest()
    cache_path = api.state_root() / 'exchange-publications' / cache_scope / feature / f"{entry['revision']:03d}" / 'manifest.json'
    if cache_path.exists():
        old = api.load_json(cache_path)
        old_entry = next((e for e in old.get('revisions', []) if e.get('revision') == entry['revision']), None)
        if old_entry != entry:
            raise ValueError('Кэш номера редакции занят другим входом; прежний манифест сохранён')
    cached = api.cache_manifest(feature, entry['revision'], packet / 'manifest.json', publication)
    save_record(api, state_path, operation, cached, merged)
    operation['publication_confirmed'] = merged
    api.save_json(operation_path, operation)
    if api.code_snapshot(code_root) != before:
        raise ValueError('Обычный клон code изменился параллельно; ветка отправки сохранена')
    return dict(status='published' if merged else 'awaiting-merge', feature=feature,
        revision=entry['revision'], manifest=str(cached), requirements_sha256=entry['sha256'],
        sdd_sha256=(entry.get('sdd_input') or {}).get('sha256'), delivery_decision=entry['delivery_decision'],
        request_branch=operation['request_branch'], request_commit=operation['request_commit'],
        target_branch=operation['target_branch'], repository_url=operation['repository_url'],
        publication_confirmed=merged, local_code_worktree_unchanged=True,
        operation=str(operation_path), replaced_candidates=operation['replaced_candidates'],
        merge_request_created=False, merge_request_create_url=operation.get('merge_request_create_url'),
        next_action='receiver-import' if merged else 'human-merge-then-repeat-same-command')


def command(api, args):
    if not args.analyst_confirmed or not args.reason.strip():
        raise ValueError('Нужно явное решение аналитика и его формулировка --reason')
    project = Path(args.project).expanduser().resolve()
    feature = args.feature
    api.validate_feature(feature)
    feature_root = api.layout_feature_root(project, feature)
    state_path = feature_root / 'requirements-state.json'
    state = api.load_json(state_path) if state_path.exists() else {}
    data, descriptor, materials = input_snapshot(feature_root)
    analyst = api.current_analyst(args.analyst, project) or 'analyst'
    code_root = api.resolve_code_root(project, args.code_root)
    if code_root is None:
        raise ValueError('Для отправки укажи доступный репозиторий разработки через --code-root')
    before = api.code_snapshot(code_root)
    if before is None:
        raise ValueError('Недоступен адрес репозитория разработки')
    remote = before['remote']
    target = api.delivery_target(remote, args.target_branch or api.configured_delivery_target())
    title = api.title_from_requirements(data.decode('utf-8'), feature)
    stage = stage_for(api, state, title)
    identity = dict(project=str(project), feature=feature, repository=remote, target=target,
                    requirements=hashlib.sha256(data).hexdigest(), sdd=descriptor, materials=materials, stage=stage)
    scope = api.stages.checksum(dict(project=str(project), feature=feature, repository=remote, target=target))
    area = (api.state_root() / 'decision-transfers' / scope).resolve()
    area.mkdir(parents=True, exist_ok=True)
    operation_path = area / (api.stages.checksum(identity) + '.json')
    with (area / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if operation_path.exists():
            operation = api.load_json(operation_path)
        else:
            operation = prepare(api, args, project, feature_root, state, data, descriptor, materials,
                                analyst, stage, before, target, area)
            api.save_json(operation_path, operation)  # durable before any push
        result = publish(api, project, feature, state_path, operation_path, operation, before, code_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def prepare(api, args, project, feature_root, state, data, descriptor, materials, analyst, stage, before, target, area):
    feature = args.feature
    clone = area / ('checkout-' + uuid.uuid4().hex[:12])
    result = subprocess.run(['git', 'clone', '--quiet', '--single-branch', '--branch', target,
                             before['remote'], str(clone)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError('Не удалось прочитать целевую ветку: ' + result.stderr.strip())
    exchange = clone / api.EXCHANGE_DIR
    if not exchange.is_dir():
        raise ValueError('В репозитории разработки отсутствует каталог requirements-exchange')
    api.require_plain_exchange(exchange)
    binding = api.exchange_binding(project, feature)
    packet = exchange / feature
    manifest_path = packet / 'manifest.json'
    manifest = api.load_json(manifest_path) if manifest_path.exists() else {}
    if manifest and (manifest.get('feature') != feature or manifest.get('delivery_binding') != binding):
        raise ValueError('Путь обмена занят другой поставкой')
    known = copy.deepcopy(state.get('delivery_stages', {}).get('revisions', []))
    for journal in area.glob('*.json'):
        previous = api.load_json(journal)
        known.append(dict(entry=previous['entry'], publication_confirmed=previous.get('publication_confirmed', False),
                          manifest_path=str(Path(previous['checkout']) / api.EXCHANGE_DIR / feature / 'manifest.json')))
    entries = manifest.setdefault('revisions', [])
    all_entries = [*entries, *(r['entry'] for r in known)]
    occupied = [int(p.name) for p in (packet / 'revisions').glob('*') if p.name.isdecimal()]
    number = max([0, *occupied, *(e['revision'] for e in all_entries)]) + 1
    stage_revision = max([0, *(e.get('stage_revision', 0) for e in all_entries if e.get('stage_id') == stage['stage_id'])]) + 1
    replaced = []
    seen = set()
    for record in known:
        n = record['entry']['revision']
        if not record.get('publication_confirmed') and n not in seen:
            seen.add(n)
            cached = Path(record.get('manifest_path', ''))
            publication = api.load_json(cached).get('publication') if cached.is_file() else None
            replaced.append(dict(entry=record['entry'], previous_publication=publication,
                                 disposition='replaced-by-analyst-decision'))
    decision = dict(schema_version=1, mode=sdd_bundle.DECISION_MODE, analyst_confirmed=True,
        analyst=analyst, reason=args.reason.strip(), decided_at=api.now(),
        requirements_sha256=hashlib.sha256(data).hexdigest(), sdd_sha256=descriptor['sha256'] if descriptor else None,
        checks={key:'not-performed' for key in sdd_bundle.DECISION_SKIPPED})
    entry = dict(revision=number, stage_id=stage['stage_id'], stage_revision=stage_revision,
        stage=stage, stage_sha256=api.stages.checksum(stage), state='sent', created_at=api.now(),
        requirements_path=f'revisions/{number:03d}/requirements.md', returns_path=f'revisions/{number:03d}/returns',
        returns_contract_version=2 if descriptor else 1, sha256=decision['requirements_sha256'],
        source=dict(repository_role='analytics', feature_path=(feature_root / 'requirements.md').relative_to(project).as_posix(),
                    commit=api.git_value(project, 'rev-parse', 'HEAD'), snapshot='working-tree'), delivery_decision=decision)
    if descriptor:
        entry['sdd_input'] = descriptor
    if binding is not None:
        entry['delivery_binding'] = binding
    destination = packet / 'revisions' / f'{number:03d}'
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'requirements.md').write_bytes(data)
    (destination / 'returns').mkdir()
    if descriptor:
        shutil.copytree(feature_root / 'sdd', destination / 'sdd')
    if materials is not None:
        shutil.copytree(feature_root / 'materials', destination / 'materials')
    if input_snapshot(feature_root) != (data, descriptor, materials) or input_snapshot(destination) != (data, descriptor, materials):
        raise ValueError('Файлы изменились во время снимка; повтори команду')
    for previous in entries:
        if previous.get('state') in {'sent','in-progress','paused'}:
            previous['state'] = 'superseded'
    entries.append(entry)
    schema = 5 if descriptor else (4 if binding else 3)
    schema = max(schema, manifest.get('schema_version', 0))
    manifest.update(schema_version=schema, exchange_kind='feature-requirements', feature=feature,
        title=title_from(api, data, feature), owner={'analyst_id':analyst}, active_revision=number,
        requested_returns=api.requested_returns_contract(schema), traceability=api.traceability_contract(schema),
        developer_sdd=api.developer_sdd_contract(schema), sdd_contract='../AGENTS.md')
    if binding is not None:
        manifest['delivery_binding'] = binding
    if descriptor:
        manifest.setdefault('sdd_revision_floor', number)
    manifest.pop('publication_request', None)
    manifest.pop('publication', None)
    manifest.setdefault('replaced_candidates', []).extend(replaced)
    api.install_root_contract(exchange)
    api.save_json(manifest_path, manifest)
    branch = 'requirements/decision/' + uuid.uuid4().hex[:20]
    checked_git(api, clone, 'switch', '--quiet', '-c', branch)
    checked_git(api, clone, 'config', 'user.name', analyst)
    checked_git(api, clone, 'config', 'user.email', analyst + '@analyst.invalid')
    api.install_commit_message_hook(clone, api.harness_root() / 'scripts' / 'commit_message_policy.py')
    # Only exact exchange paths may be staged; whitespace in authored files is not a gate.
    paths = api.git_paths(clone, 'diff', '--name-only', '-z') + api.git_paths(clone, 'ls-files', '--others', '--exclude-standard', '-z')
    if any(not p.startswith(api.EXCHANGE_DIR + '/') for p in paths):
        raise ValueError('Изменения вышли за requirements-exchange')
    # Exact file inventory includes data ignored by a material's .gitignore.
    files_to_stage = [api.EXCHANGE_DIR + '/' + name for name in inventory(exchange)]
    checked_git(api, clone, 'add', '--force', '--', *files_to_stage)
    staged = api.git_paths(clone, 'diff', '--cached', '--name-only', '-z')
    if any(not name.startswith(api.EXCHANGE_DIR + '/') for name in staged):
        raise ValueError('Индекс вышел за requirements-exchange')
    message = 'Передать требования по решению аналитика'
    api.require_valid_commit_message(message)
    checked_git(api, clone, 'commit', '--quiet', '-m', message)
    return dict(schema_version=1, feature=feature, checkout=str(clone), entry=entry,
        request_branch=branch, request_commit=api.git_value(clone, 'rev-parse', 'HEAD'),
        repository_url=before['remote'], target_branch=target, replaced_candidates=replaced,
        files=inventory(destination), reserved_history=known, publication_confirmed=False)


def title_from(api, data, feature):
    return api.title_from_requirements(data.decode('utf-8'), feature)
