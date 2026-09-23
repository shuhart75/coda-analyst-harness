from __future__ import annotations

from contextlib import redirect_stdout
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
import re
from types import SimpleNamespace


def execution_mode(root: Path) -> bool:
    path = root / '.workspace-state/active-mode.md'
    return path.is_file() and re.search(r'^mode:\s*execution-update\s*$', path.read_text(), re.MULTILINE) is not None


def require_execution_work(root, analytics, work, api):
    if not execution_mode(root):
        raise ValueError('Switch to execution-update before registering or saving execution scope')
    if work.get('status') != 'active' or work.get('branch') in {'main', 'master'}:
        raise ValueError('Execution scope requires an active review branch, never main')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        result = api.git(analytics, 'rev-parse', '--git-path', name)
        if result.returncode != 0:
            raise ValueError(result.stderr)
        path = Path(result.stdout.strip())
        if (path if path.is_absolute() else analytics / path).exists():
            raise ValueError('Finish the pending Git operation before execution work')


def require_tracker_root(root: Path) -> None:
    from tracker_workflow import state_root
    if state_root().resolve() != (root / '.workspace-state').resolve():
        raise ValueError('Tracker state root differs from collaboration workspace; set ANALYST_HARNESS_STATE_ROOT explicitly')


def set_scope(args, api) -> int:
    root = api.root_path(args.root)
    analytics, _ = api.analytics_repository(root)
    state = api.load_state(root)
    work = api.require_active_work(root, analytics, state)
    require_execution_work(root, analytics, work, api)
    if not args.analyst_confirmed or not args.reason.strip():
        raise ValueError('Execution scope requires the analyst decision and its reason')
    features, quarters = sorted(set(args.feature)), sorted(set(args.quarter))
    if work['feature'] not in features:
        raise ValueError('Execution scope must include the active owning feature')
    for feature in features:
        if not api.SLUG_PATTERN.fullmatch(feature):
            raise ValueError('Invalid execution feature slug')
        path = analytics / 'features' / feature
        if path.resolve() != path or not path.is_dir():
            raise ValueError(f'Execution feature is missing or linked: {feature}')
    for quarter in quarters:
        if not re.fullmatch(r'\d{4}-Q[1-4]', quarter):
            raise ValueError('Invalid execution quarter')
        path = analytics / 'planning' / quarter / 'gantt'
        if path.resolve() != path or not path.is_dir():
            raise ValueError(f'Gantt directory is missing or linked: {quarter}')
    run_ids = sorted(set(args.run_id))
    if run_ids:
        from tracker_workflow import verified_result
        require_tracker_root(root)
        for run_id in run_ids:
            completion, _ = verified_result(run_id)
            if not completion['planning_application_allowed']:
                raise ValueError('Tracker run must allow application before scope registration')
    work['execution_scope'] = {'features': features, 'quarters': quarters, 'run_ids': run_ids,
                               'reason': args.reason, 'confirmed_at': api.utc_now()}
    api.write_state(root, state)
    print(json.dumps({'status': 'execution-scope-registered', 'branch': work['branch'],
                      'execution_scope': work['execution_scope'], 'analytics_writes_performed': False,
                      'content_approved': False}, ensure_ascii=False, indent=2))
    return 0


def path_kind(path: str, scope: dict) -> str | None:
    if path == 'planning/team.md':
        return 'shared-execution-source'
    parts = path.split('/')
    if len(parts) >= 3 and parts[0] == 'features' and parts[1] in scope['features']:
        relative = '/'.join(parts[2:])
        if relative in {'planning/actualization.md', 'execution-context.md'} or re.fullmatch(
            r'(?:slices/[a-z0-9-]+/)?execution/(?:[a-zA-Z0-9_./-]+)\.(?:md|json)', relative
        ):
            return 'execution-source'
    if len(parts) >= 4 and parts[0] == 'planning' and parts[1] in scope['quarters'] and parts[2] == 'gantt':
        relative = '/'.join(parts[3:])
        if relative in {'actual-progress.puml', 'actual-progress-confluence.puml'} or re.fullmatch(
            r'includes/actual-progress/FEATURE-[a-z0-9-]+\.puml', relative
        ):
            return 'generated-quarter-view'
        if relative in {'actual-progress-features.json', 'actual-progress-layout.json', 'preamble/actual-progress.puml'}:
            return 'execution-view-source'
        if re.fullmatch(r'[a-z0-9-]*decision[a-z0-9-]*\.md', relative):
            return 'execution-view-decision'
    return None


def check_save(root, analytics, work, paths, review_files, api) -> dict:
    require_execution_work(root, analytics, work, api)
    scope = work.get('execution_scope')
    if not scope:
        raise ValueError('Register the confirmed feature/quarter set with set-execution-scope first')
    classified = []
    for path in sorted(paths):
        api.exact_path(path)
        target = analytics / path
        kind = path_kind(path, scope)
        if not kind or target.resolve() != target:
            raise ValueError(f'Path outside confirmed execution scope or linked: {path}')
        classified.append({'path': path, 'kind': kind})
    reviewed_runs = set()
    if scope.get('run_ids') or review_files:
        require_tracker_root(root)
    for filename in review_files:
        from tracker_qa_application import check_qa_application
        review = json.loads(Path(filename).read_text())
        run_id = review['run_id']
        if run_id in reviewed_runs or run_id not in scope.get('run_ids', []):
            raise ValueError('History review must match exactly one registered tracker run')
        if review.get('pending_history') or review.get('status') != 'history-review-ready':
            raise ValueError('Complete history review before saving execution')
        owners = {feature['feature'] for feature in review['features']}
        if not owners <= set(scope['features']):
            raise ValueError('History review contains a feature outside execution scope')
        output = StringIO()
        with redirect_stdout(output):
            result = check_qa_application(SimpleNamespace(project_root=str(analytics), review_file=filename))
        if result != 0:
            raise ValueError('QA application check failed: ' + output.getvalue())
        reviewed_runs.add(run_id)
    if reviewed_runs != set(scope.get('run_ids', [])):
        raise ValueError('Pass --review-file for every registered tracker run; QA verification is mandatory')
    verified_quarters = []
    expander = import_module('expand-plantuml-includes')
    for quarter in scope['quarters']:
        gantt = analytics / 'planning' / quarter / 'gantt'
        source, export = gantt / 'actual-progress.puml', gantt / 'actual-progress-confluence.puml'
        dependencies = []
        expanded = '\n'.join(expander.expand_file(source, [], dependencies=dependencies)).rstrip() + '\n'
        if any(not path.is_relative_to(analytics) for path in dependencies) or export.resolve() != export:
            raise ValueError('Gantt dependency is outside analytics or export is linked')
        if export.read_text() != expanded:
            raise ValueError(f'Confluence export differs from actual-progress: {quarter}; regenerate --actual-only')
        verified_quarters.append(quarter)
    return {'paths': classified, 'confluence_verified': verified_quarters,
            'qa_verified_runs': sorted(reviewed_runs), 'generation_proven': False}


def save_preview(args, api) -> int:
    root = api.root_path(args.root)
    analytics, _ = api.analytics_repository(root)
    work = api.require_active_work(root, analytics, api.load_state(root))
    paths = api.changed_paths(analytics)
    checks = check_save(root, analytics, work, paths, args.review_file, api)
    command = ['python3', str(Path(api.__file__).resolve()), '--root', str(root), 'save',
               '--message', '<semantic-message>']
    for path in sorted(paths):
        command.extend(['--path', path])
    for filename in args.review_file:
        command.extend(['--review-file', filename])
    print(json.dumps({'status': 'execution-save-preview', 'branch': work['branch'],
                      'head': api.head(analytics), **checks, 'save_command': command,
                      'writes_performed': False, 'content_approved': False}, ensure_ascii=False, indent=2))
    return 0
