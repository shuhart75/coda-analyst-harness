from __future__ import annotations

import json


def completed_executions(state: dict) -> dict:
    completed = dict(state.get('completed_execution_runs', {}))
    for work in state.get('completed_work', []):
        if work.get('status') != 'merged':
            continue
        for run_id in (work.get('execution_scope') or {}).get('run_ids', []):
            completed.setdefault(run_id, {
                'state': 'applied', 'branch': work['branch'],
                'merged_at': work.get('merged_at'), 'origin_main': work.get('origin_main'),
                'saved_commit': work.get('last_saved_commit'),
                'evidence': 'collaboration-finish',
            })
    return completed


def applied_execution(run_id: str) -> dict | None:
    from tracker_workflow import load_json, state_root

    path = state_root() / 'collaboration.json'
    if not path.is_file():
        return None
    return completed_executions(load_json(path)).get(run_id)


def resume_command(args) -> int:
    from tracker_workflow import (RUN_ID, STOP_EXIT, config_status_payload, load_config,
                                  load_run, run_path, state_root, status_payload, verified_result)

    gate = config_status_payload(load_config())
    if gate.get('must_stop'):
        print(json.dumps(gate, ensure_ascii=False, indent=2))
        return STOP_EXIT
    candidates, archived = [], []
    for directory in sorted((state_root() / 'tracker-runs').glob('*')):
        if not directory.is_dir() or not RUN_ID.fullmatch(directory.name):
            continue
        if args.run_id and directory.name != args.run_id:
            continue
        run = load_run(directory.name)
        scope = run['scope']
        matches = (scope['kind'] == args.scope_kind and scope['provider'] == args.scope_provider
                   and set(scope['ids']) == set(args.scope_id) and scope['intent'] == args.intent
                   and set(scope.get('epic_ids', [])) == set(args.epic_id))
        if not matches:
            if args.run_id:
                raise ValueError('Requested run does not match the current request scope and intent')
            continue
        applied = applied_execution(run['run_id'])
        if applied or run['status'] in {'tracker-read-abandoned', 'tracker-read-failed'}:
            archived.append({'run_id': run['run_id'], 'scope': scope,
                             'state': 'applied' if applied else run['status']})
        else:
            candidates.append(run)
    if len(candidates) != 1:
        print(json.dumps({'status': 'tracker-resume-ambiguous' if candidates else 'tracker-resume-not-found',
                          'candidates': [{'run_id': run['run_id'], 'scope': run['scope']} for run in candidates],
                          'archived': archived, 'writes_performed': False,
                          'next_action': {'type': 'confirm-exact-run' if candidates else 'inspect-request-scope'},
                          'message': 'Do not substitute another release, abandon a run or begin a replacement automatically'},
                         ensure_ascii=False, indent=2))
        return 2
    run = candidates[0]
    payload = status_payload(run)
    payload.update(scope=run['scope'], run_json=str(run_path(run['run_id'])),
                   writes_performed=False, resume=True)
    if run['status'] == 'tracker-read-reconciled':
        completion, _ = verified_result(run['run_id'])
        payload['planning_application_allowed'] = completion['planning_application_allowed']
        payload['application_state'] = completion.get('application_state', {'state': 'pending' if
            run['scope']['intent'] == 'update-planning' else 'not-requested'})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return STOP_EXIT if payload.get('must_stop') else 0
