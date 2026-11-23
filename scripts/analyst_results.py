"""Analyst-owned immutable implementation evidence. Never writes to product code."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

import grooming
import sdd_bundle
from readable_requirements import authored_ids

CONTRACT = 3
KEY = re.compile(r'RSCON-[0-9]+')
SHA = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def api():
    spec = importlib.util.spec_from_file_location('requirements_exchange_results', Path(__file__).with_name('requirements-exchange.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    sdd_bundle._plain(Path(path))
    return sdd_bundle._json(Path(path))


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    if result.returncode:
        raise ValueError('Git evidence unavailable: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Expected repository-relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {'', '..', '.', '.git'} for p in value.split('/')):
        raise ValueError('Unsafe evidence path')
    return value


def snapshot(root):
    return (git(root, 'rev-parse', 'HEAD'), git(root, 'rev-parse', '--abbrev-ref', 'HEAD'),
            git(root, 'status', '--porcelain=v1', '--untracked-files=all'), git(root, 'config', '--local', '--list'))


def published_root(module, project, feature, record):
    """Require accepted bytes in the analytics checkout; cached proof is not source."""
    cached = read(record['manifest_path'])
    proof = cached.get('publication', {})
    root = project / module.EXCHANGE_DIR / feature
    if (proof.get('state') != 'merged' or not SHA.fullmatch(str(proof.get('target_commit', '')))
            or proof.get('repository_url') != module.git_value(project, 'remote', 'get-url', 'origin')):
        raise ValueError('Repeat prepare to verify documents publication')
    git(project, 'merge-base', '--is-ancestor', proof['target_commit'], 'HEAD')
    manifest = read(root / 'manifest.json')
    entry = record['entry']
    actual = next((e for e in manifest['revisions'] if e['revision'] == entry['revision']), None)
    if module.stages.immutable_entry(actual or {}) != module.stages.immutable_entry(entry):
        raise ValueError('Update documents: accepted revision differs or is missing')
    prefix = f"{module.EXCHANGE_DIR}/{feature}/revisions/{entry['revision']:03d}"
    names = git(project, 'ls-tree', '-r', '--name-only', proof['target_commit'], '--', prefix).decode().splitlines()
    for name in names:
        local = project / safe_relative(name)
        sdd_bundle._plain(local)
        if not local.is_file() or local.read_bytes() != git(project, 'show', proof['target_commit'] + ':' + name):
            raise ValueError('Published input bytes changed: ' + name)
    return root


def packet(module, project, feature, revision=None):
    state = read(module.layout_feature_root(project, feature) / 'requirements-state.json')
    records = module.stages.registry(state)['revisions']
    if not records:
        raise ValueError('No published revision')
    record = next((r for r in records if r['entry']['revision'] == revision), None) if revision else max(records, key=lambda r: r['entry']['revision'])
    if not record or record['entry'].get('returns_contract_version') != CONTRACT:
        raise ValueError('Use the original return contract for legacy revisions')
    root = module.publication_root(project, feature, record)
    errors = module.validate_manifest(read(root / 'manifest.json'), root)
    if errors:
        raise ValueError('; '.join(errors))
    return root, record['entry'], record


def bound_tasks(root, entry, bindings):
    task_path = root / f"revisions/{entry['revision']:03d}/tasks/grooming.json"
    plan = read(task_path) if task_path.exists() else {'tasks': []}
    tasks = plan['tasks']
    if (not isinstance(bindings, dict) or set(bindings) != {t['id'] for t in tasks}):
        raise ValueError('Bindings must cover exactly the published grooming task IDs')
    keys = []
    for value in bindings.values():
        if (not isinstance(value, dict) or not KEY.fullmatch(str(value.get('key', '')))
                or not isinstance(value.get('evidence'), str) or not value['evidence'].strip()):
            raise ValueError('Each task binding needs a native key and confirmed evidence')
        keys.append(value['key'])
    if len(keys) != len(set(keys)):
        raise ValueError('Different groomed tasks cannot share one tracker key')
    return tasks


def collect(code, commit, roots, bindings, target_commit=None):
    """Candidates only. Follow one pinned reachable history, never --all or fuzzy keys."""
    if not SHA.fullmatch(commit):
        raise ValueError('Evidence requires a full inspected commit SHA')
    roots = [safe_relative(r) for r in roots]
    if not roots or len(roots) != len(set(roots)):
        raise ValueError('Select distinct OpenSpec roots from the published package')
    before = snapshot(code)
    if before[2].strip():
        raise ValueError('Code inspection requires a clean worktree')
    try:
        if git(code, 'rev-parse', '--is-shallow-repository').strip() == b'true':
            raise ValueError('Complete Git history is required for attribution')
        git(code, 'merge-base', '--is-ancestor', commit, 'HEAD')
        if git(code, 'rev-parse', commit + '^{commit}').decode().strip() != commit:
            raise ValueError('Expected exact commit')
        if target_commit is not None:
            if not SHA.fullmatch(target_commit):
                raise ValueError('Target snapshot needs a full SHA')
            git(code, 'cat-file', '-e', target_commit + '^{commit}')
        included = (subprocess.run(['git', '-C', str(code), 'merge-base', '--is-ancestor', commit, target_commit], capture_output=True).returncode == 0) if target_commit else None
        repository = git(code, 'remote', 'get-url', 'origin').decode().strip()
        # The source is immutable Git objects, not mutable working files.
        paths = git(code, 'ls-tree', '-r', '--name-only', commit, '--', *roots).decode().splitlines()
        archives = {}
        for path in paths:
            for root in roots:
                prefix = root + '/changes/'
                if path.startswith(prefix):
                    tail = path[len(prefix):].split('/')
                    size = 2 if tail[0] == 'archive' else 1
                    if len(tail) > size:
                        directory = prefix + '/'.join(tail[:size])
                        archives.setdefault(directory, []).append(path)
        commits = []
        keys = {b['key'] for b in bindings.values()}
        for line in git(code, 'log', '--format=%H%x00%s', commit).decode().splitlines():
            sha, subject = line.split('\0', 1)
            match = re.match(r'^(RSCON-\d+)(?=\s|:|$)', subject)
            if match and match[1] in keys:
                files = git(code, 'diff-tree', '--root', '-m', '--no-commit-id', '--name-only', '-r', sha).decode().splitlines()
                commits.append({'commit': sha, 'key': match[1], 'subject': subject, 'paths': sorted(set(files))})
        if keys:
            touched = {p for c in commits for p in c['paths']}
            def related(directory, files):
                if touched.intersection(files):
                    return True
                # Follow the exact active change name into its dated archive.
                name = directory.rsplit('/', 1)[-1]
                slug = name[11:] if re.match(r'^\d{4}-\d{2}-\d{2}-', name) else name
                base = directory.split('/changes/')[0]
                return any(p.startswith(base + '/changes/' + slug + '/') for p in touched)
            archives = {d:f for d,f in archives.items() if related(d,f)}
        changes = []
        for directory, files in sorted(archives.items()):
            # First-parent transitions find when this tree entered the inspected branch.
            # A later added file inside the directory is not mistaken for its creation.
            history = git(code, 'log', '--first-parent', '--format=%H', commit, '--', directory).decode().splitlines()
            introduced = None
            for candidate in reversed(history):
                if git_object_exists(code, candidate + ':' + directory):
                    introduced = candidate
                    break
            if not introduced:
                raise ValueError('Incomplete Git history for ' + directory)
            subject = git(code, 'show', '-s', '--format=%s', introduced).decode().strip()
            match = re.match(r'^(RSCON-\d+)(?=\s|:|$)', subject)
            changes.append({'path': directory, 'state': 'archived' if '/changes/archive/' in directory else 'active',
                            'branch_entry_commit': introduced,
                            'subject': subject, 'task_key': match[1] if match else None,
                            'attribution': 'candidate' if match else 'unresolved',
                            'files': {p: hashlib.sha256(git(code, 'show', commit + ':' + p)).hexdigest() for p in files}})
        return {'schema_version': 1, 'repository': repository, 'commit': commit,
                'roots': roots, 'bindings': bindings, 'commits': commits, 'changes': changes,
                'target_commit': target_commit, 'included_in_target': included,
                'limitations': ['Task keys are attribution candidates, not proof of requirement coverage.',
                                'Archive dates and checked tasks do not prove testing or deployment.']}
    finally:
        if snapshot(code) != before:
            raise ValueError('Code worktree changed during inspection; owner review required')


def git_object_exists(root, value):
    return subprocess.run(['git', '-C', str(root), 'cat-file', '-e', value], capture_output=True).returncode == 0


def expected_scenarios(root, entry, tasks):
    if tasks:
        return {(t['id'], r['req_id'], scenario) for t in tasks for r in t['requirements'] for scenario in r['scenarios']}
    text = (root / entry['requirements_path']).read_text()
    blocks = grooming.requirement_blocks(text)
    return {('', req, scenario) for req, block in blocks.items() for scenario in block['scenarios']}


def validate_report(root, entry, report):
    if (report.get('schema_version') != 1 or report.get('kind') != 'analyst-implementation-review'
            or report.get('revision') != entry['revision'] or report.get('feature') != root.name
            or report.get('requirements_sha256') != entry['sha256']
            or report.get('sdd_sha256') != (entry.get('sdd_input') or {}).get('sha256')):
        raise ValueError('Analysis belongs to another input revision')
    evidence = report.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('Analysis needs pinned evidence snapshots')
    tasks = bound_tasks(root, entry, report.get('bindings'))
    allowed = {}
    for source in evidence:
        if (not isinstance(source, dict) or not SHA.fullmatch(str(source.get('commit', '')))
                or not source.get('repository') or source.get('bindings') != report['bindings']):
            raise ValueError('Invalid evidence snapshot or task bindings')
        allowed[grooming.fingerprint(source)] = source
    artifacts = {}
    for artifact in report.get('external_evidence', []):
        if not isinstance(artifact, dict):
            raise ValueError('Invalid external evidence')
        safe_relative(artifact.get('path'))
        for field in ('content', 'environment', 'procedure', 'result'):
            if not isinstance(artifact.get(field), str) or not artifact[field].strip():
                raise ValueError('External evidence needs ' + field)
        if (artifact.get('kind') not in {'test-result', 'deployment-record'}
                or artifact.get('source_commit') not in {s['commit'] for s in evidence}
                or hashlib.sha256(artifact['content'].encode()).hexdigest() != artifact.get('sha256')
                or artifact['sha256'] in artifacts):
            raise ValueError('External evidence identity/hash mismatch')
        artifacts[artifact['sha256']] = artifact
    expected = expected_scenarios(root, entry, tasks)
    scenarios = report.get('scenarios')
    if not isinstance(scenarios, list) or not all(isinstance(s, dict) for s in scenarios):
        raise ValueError('Expected scenario assessments')
    found = [(s.get('task_id'), s.get('requirement'), s.get('scenario')) for s in scenarios]
    if len(found) != len(set(found)) or set(found) != expected:
        raise ValueError('Analysis must cover every groomed task/requirement/scenario exactly once')
    for item in scenarios:
        for name, values in {'implementation': {'full','partial','none','unknown'},
                             'conformity': {'matches','differs','unknown','not-applicable'},
                             'verification': {'passed','failed','not-run','unknown'},
                             'merge': {'merged','not-merged','unknown'},
                             'deployment': {'deployed','not-deployed','unknown'}}.items():
            if item.get(name) not in values:
                raise ValueError('Invalid scenario assessment: ' + name)
        for name in ('expected', 'actual', 'limitations'):
            if not isinstance(item.get(name), str) or not item[name].strip():
                raise ValueError('Scenario needs ' + name)
        refs = item.get('evidence')
        if not isinstance(refs, list):
            raise ValueError('Scenario evidence must be a list')
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get('reason'):
                raise ValueError('Evidence must explain the observation')
            if 'artifact' in ref:
                if ref['artifact'] not in artifacts:
                    raise ValueError('Unknown external artifact')
            else:
                if ref.get('snapshot') not in allowed or ref.get('kind', 'static') != 'static':
                    raise ValueError('Git paths are static evidence, not runtime results')
                safe_relative(ref.get('path'))
        if (item['implementation'] != 'unknown' or item['verification'] in {'passed','failed'}
                or item['merge'] == 'merged' or item['deployment'] == 'deployed') and not refs:
            raise ValueError('Observed result requires explicit evidence')
        for state, kind in [('verification', 'test-result'), ('deployment', 'deployment-record')]:
            if item[state] in {'passed', 'failed', 'deployed'} and not any(artifacts.get(r.get('artifact'), {}).get('kind') == kind for r in refs):
                raise ValueError(state + ' requires recorded runtime evidence, not static inspection')
        if item['merge'] == 'merged' and not any(allowed.get(r.get('snapshot'), {}).get('included_in_target') is True for r in refs):
            raise ValueError('Merged assessment requires a pinned target ancestry check')
    return set(authored_ids((root / entry['requirements_path']).read_text()))


def is_result_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-z0-9-]+:\d+:revisions/\d+/analysis/[0-9a-f]{64}\.json:[0-9a-f]{64}', value))


def processing_status(root, entry):
    directory = root / f"revisions/{entry['revision']:03d}/analysis"
    reports = sorted(directory.glob('*.json'))
    errors = []
    current = directory / 'current'
    if reports:
        sdd_bundle._plain(current)
        if not current.is_file() or current.read_text().strip() not in {p.stem for p in reports}:
            errors.append('Current analysis pointer is absent or invalid')
    for path in reports:
        try:
            report = read(path)
            if path.stem != grooming.fingerprint(report):
                raise ValueError('Immutable analysis changed')
            validate_report(root, entry, report)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    return {'state': 'invalid' if errors else 'analysis-present' if reports else 'awaiting-analysis',
            'returns_contract_version': CONTRACT, 'receipt_present': False, 'tasks_present': False,
            'summary_present': False, 'task_result_count': 0, 'analysis_count': len(reports), 'errors': errors}


def validate_result(module, project, feature, result_id, review, publication=None):
    parts = result_id.split(':')
    if not is_result_id(result_id) or parts[0] != feature:
        raise ValueError('Analysis identity belongs to another delivery')
    root, entry, record = packet(module, project, feature, int(parts[1]))
    if publication and module.stages.immutable_entry(publication['entry']) != module.stages.immutable_entry(entry):
        raise ValueError('Review publication differs')
    path = root / safe_relative(parts[2])
    if path.parent != root / f"revisions/{entry['revision']:03d}/analysis":
        raise ValueError('Wrong analysis revision')
    report = read(path)
    current = path.parent / 'current'
    sdd_bundle._plain(current)
    if not current.is_file() or current.read_text().strip() != path.stem:
        raise ValueError('A newer analysis exists or the current analysis is not registered')
    if path.stem != grooming.fingerprint(report) or module.sha256(path) != parts[3]:
        raise ValueError('Analysis changed; review the new snapshot')
    ids = validate_report(root, entry, report)
    if (review.get('schema_version') != 1 or review.get('return_id') != result_id
            or review.get('requirements_sha256') != entry['sha256']):
        raise ValueError('Decision must bind exact analysis and requirements')
    for item in review.get('items', []):
        scenarios = [s for s in report['scenarios'] if s['requirement'] == item.get('requirement')]
        if any(s['implementation'] == 'unknown' or s['conformity'] == 'unknown' or s['verification'] in {'unknown','not-run'} for s in scenarios) and item.get('follow_up', {}).get('action') != 'investigate':
            raise ValueError('Unresolved scenarios require investigation')
        if item.get('baseline', {}).get('action') == 'record-deployed' and any(s['deployment'] != 'deployed' for s in scenarios):
            raise ValueError('Deployment is not established for every scenario')
        if item.get('implementation') == 'full' and any(s['implementation'] != 'full' for s in scenarios):
            raise ValueError('Full implementation contradicts scenario evidence')
        if item.get('verification') == 'passed' and any(s['verification'] != 'passed' for s in scenarios):
            raise ValueError('Passed verification contradicts scenario evidence')
        if item.get('conformity') == 'matches' and any(s['conformity'] != 'matches' for s in scenarios):
            raise ValueError('Conformity contradicts scenario evidence')
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'record-analysis'])
    parser.add_argument('project', type=Path)
    parser.add_argument('feature')
    parser.add_argument('--revision', type=int)
    parser.add_argument('--commit')
    parser.add_argument('--target-commit')
    parser.add_argument('--openspec-root', action='append')
    parser.add_argument('--contour', choices=['backend','frontend'])
    parser.add_argument('--bindings', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    module = api()
    try:
        project = args.project.resolve()
        root, entry, record = packet(module, project, args.feature, args.revision)
        if args.action == 'collect':
            bindings = read(args.bindings) if args.bindings else {}
            bound_tasks(root, entry, bindings)
            code = module.resolve_code_root(project, None)
            if code is None:
                raise ValueError('Registered code repository is unavailable')
            package_path = root / f"revisions/{entry['revision']:03d}/sdd/package.json"
            if package_path.exists():
                changes = read(package_path)['changes']
                if len({c['contour'] for c in changes}) > 1 and not args.contour:
                    raise ValueError('Select one contour per inspection')
                roots = sorted({c['target_root'] for c in changes if not args.contour or c['contour'] == args.contour})
            else:
                roots = args.openspec_root or []
            result = collect(code, args.commit, roots, bindings, args.target_commit)
        else:
            if root != project / module.EXCHANGE_DIR / args.feature:
                raise ValueError('Archived delivery is immutable; create a new delivery for further work')
            from tracker_safety import require_write_context
            require_write_context(project, modes=('requirements', 'release-finalization'), feature=args.feature)
            report = read(args.report)
            validate_report(root, entry, report)
            code = module.resolve_code_root(project, None)
            if code is None:
                raise ValueError('Registered code repository is unavailable')
            for source in report['evidence']:
                if collect(code, source['commit'], source['roots'], report['bindings'], source.get('target_commit')) != source:
                    raise ValueError('Evidence snapshot differs from inspected Git history')
                for assessment in report['scenarios']:
                    for ref in assessment['evidence']:
                        if ref.get('snapshot') == grooming.fingerprint(source):
                            tree_entry = git(code, 'ls-tree', source['commit'], '--', ref['path'])
                            if not tree_entry.startswith((b'100644 blob ', b'100755 blob ')):
                                raise ValueError('Evidence must identify a regular tracked file')
                            git(code, 'show', source['commit'] + ':' + ref['path'])
            for artifact in report.get('external_evidence', []):
                path = project / safe_relative(artifact['path'])
                sdd_bundle._plain(path)
                if path.read_bytes() != artifact['content'].encode():
                    raise ValueError('External evidence must preserve exact source bytes')
            name = grooming.fingerprint(report)
            path = root / f"revisions/{entry['revision']:03d}/analysis/{name}.json"
            sdd_bundle._plain(path)
            current = path.parent / 'current'
            sdd_bundle._plain(current)
            current_hash = current.read_text().strip() if current.exists() else None
            if current_hash not in {None, name} and report.get('supersedes') != current_hash:
                raise ValueError('New analysis must explicitly supersede the current snapshot')
            if not path.exists():
                module.save_json(path, report)
            elif read(path) != report:
                raise ValueError('Immutable analysis collision')
            temporary = current.with_suffix('.tmp')
            sdd_bundle._plain(temporary)
            temporary.write_text(name + '\n')
            temporary.replace(current)
            result = {'status': 'recorded', 'path': str(path),
                      'return_id': f"{args.feature}:{entry['revision']:03d}:{path.relative_to(root).as_posix()}:{module.sha256(path)}",
                      'next_action': 'review-scenarios-and-record-analyst-decisions'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
