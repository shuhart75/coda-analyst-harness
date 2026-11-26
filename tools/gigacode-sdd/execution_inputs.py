"""Small execution inputs and observable check prerequisites, without installations."""
import os
from pathlib import Path
import shutil


def accepted_tasks(records):
    """Exclude recursive session history, snapshots and transcripts from prompts."""
    return [{'id': key, 'status': value.get('status'),
             'integrated_head': value.get('integrated_head'),
             'review_verdict': (value.get('review') or {}).get('verdict'),
             'deferred_evidence': (value.get('review') or {}).get('deferred_evidence', []),
             'checks': [{'ok': check.get('ok'), 'log': check.get('log')}
                        for check in value.get('checks', [])]}
            for key, value in records.items()]


def missing_prerequisites(root, checks, env):
    """Only inspect explicit runtime paths; never infer a test failure from prose.

    A missing product script is still an ordinary failed check. Dependency-owned
    entry points and unavailable executables are preparation failures. Unknown
    command forms run normally and retain their actual results.
    """
    missing = []
    root = Path(root).resolve()
    for check in checks:
        argv = check['argv']
        cwd = (root / check.get('cwd', '.')).resolve()
        if not cwd.is_relative_to(root):
            raise ValueError('Check directory escapes workspace')
        executable = argv[0]
        if '/' in executable or '\\' in executable:
            available = (cwd / executable).is_file()
        else:
            available = shutil.which(executable, path=env.get('PATH', os.defpath)) is not None
        reason = None
        if not available:
            reason = {'kind': 'missing-executable', 'path': executable}
        elif len(argv) > 1 and Path(executable).stem.lower() in ('node', 'nodejs', 'python', 'python3', 'bash', 'sh'):
            entry = Path(argv[1])
            if not argv[1].startswith('-') and any(p in ('node_modules', '.venv', 'venv') for p in entry.parts):
                target = (cwd / entry).resolve()
                if target.is_relative_to(root) and not target.is_file():
                    reason = {'kind': 'missing-dependency-entry', 'path': str(cwd / entry)}
        if reason:
            missing.append({'check': check, **reason})
    return missing


PREPARE = ('Prepare the current isolated workspace using its package manifests and lockfiles, '
           'existing project setup instructions and approved Nexus configuration. '
           'Dependencies and generated files are not carried by Git clones. '
           'Do not share mutable dependency directories with another candidate, change lockfiles '
           'to evade setup, bypass registry policy, weaken checks or rebuild frozen experiment artifacts. '
           'If credentials or an external prerequisite are unavailable, report the concrete blocker '
           'through the operator protocol. Recheck the exact missing paths before proceeding.')


def prompt_value(root, value, limit=24000):
    """Keep oversized evidence losslessly on disk rather than truncate obligations."""
    import hashlib
    import json
    from controller import save
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text.encode('utf-8')) <= limit:
        return value
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    path = Path(root) / 'execution-inputs' / (sha + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Execution input must not be a symlink')
    if path.exists():
        existing = json.loads(path.read_text(encoding='utf-8'))
        if existing != value:
            raise ValueError('Saved execution input changed')
    else:
        save(path, value)
    return {'full_input_file': str(path),
            'instruction': 'Read this complete JSON input in targeted sections before acting. '
                           'All requirements, task boundaries and decisions in it still apply. '
                           'Do not dump the entire file into the conversation or treat this reference as omitted scope.'}
