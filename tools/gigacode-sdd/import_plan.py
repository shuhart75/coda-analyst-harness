"""Import a corrected candidate for independent review, never accept a plan."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from bootstrap import app_root
from controller import (Blocked, Runner, digest, read, save, validate_plan,
                        plan_candidate_path)
from platform_support import FileLock
from workspace import snapshot, git


def import_plan(config, plan, expected_sha256):
    runner = Runner(config)
    # Atomic state pointer: interrupted imports leave the old candidate usable.
    with FileLock(runner.root / 'run.lock'):
        runner.state = read(runner.state_path)
        runner.guard()
        runner.check_preserved_processes()
        if runner.state.get('tasks') or runner.state.get('status') != 'initialized':
            raise Blocked('Only an initialized run without executor tasks can import a candidate')
        if git(runner.main, 'rev-parse', 'HEAD').strip() != runner.state.get('main_head', runner.cfg['baseline']):
            raise Blocked('Working copy HEAD changed')
        old = plan_candidate_path(runner.root, runner.state)
        if not old.is_file() or digest(old) != expected_sha256:
            raise Blocked('Original candidate SHA256 mismatch')
        scope = hashlib.sha256(json.dumps({'config': digest(runner.config_path),
            'workspace': snapshot(runner.main)}, sort_keys=True).encode()).hexdigest()
        metadata = runner.state.get('plan_candidate')
        if metadata and (metadata.get('scope') != scope or metadata.get('sha256') != expected_sha256):
            raise Blocked('Saved candidate metadata does not match current input')
        proposed = validate_plan(read(plan), runner.main, runner.cfg['requirements'])
        token = uuid.uuid4().hex
        archive = runner.root / 'plan-imports' / token
        save(archive / 'previous-state.json', runner.state)
        save(archive / 'previous-plan.json', read(old))
        target = runner.root / ('plan-candidate-' + token + '.json')
        save(target, proposed)
        save(archive / 'receipt.json', {'previous_sha256': expected_sha256,
            'candidate_sha256': digest(target), 'candidate_file': target.name,
            'scope': scope, 'accepted': False})
        runner.state['plan_candidate'] = {'file': target.name, 'scope': scope,
            'sha256': digest(target), 'read_errors': []}
        runner.state.pop('plan_review_pending', None)
        runner.state['plan_feedback'] = ''
        runner.state.pop('plan_failure_fingerprint', None)
        runner.state.pop('plan_failure_repeats', None)
        runner.persist()
        return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    try:
        config = args.config or Path(read(app_root() / 'last-run.json')['config'])
        target = import_plan(config, args.plan, args.expected_sha256)
        print('Кандидат сохранён для независимого ревью: ' + str(target))
        print('Preflight сохранён. План не принят. Продолжите запуск через bootstrap.py.')
    except (Blocked, BlockingIOError, OSError, ValueError, KeyError, TypeError) as error:
        print('Импорт остановлен: ' + str(error))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
