"""Versioned, read-only data contract for a local operator interface.

This is a projection of controller state, never a second scheduler or a process
inventory. No models, services or control actions are started by this module.
"""
import argparse
import json
import os
from pathlib import Path
import time

from operator_attention import project_attention

SCHEMA = 'gigacode-operator-view-v1'


def project(value, *, observed_at=None):
    questions = [{key: q.get(key) for key in ('id', 'task', 'topic', 'question', 'options')}
                 for q in value.get('questions', [])]
    question_tasks = {q['task'] for q in questions}
    tasks = []
    for task in value.get('tasks', []):
        status = task.get('status')
        tasks.append({
            'id': task['id'], 'title': task.get('title', task['id']), 'status': status,
            'accepted': status == 'done', 'depends_on': list(task.get('depends_on', [])),
            'waiting_for': list(task.get('waiting_for', [])), 'error': task.get('error'),
            'attention': project_attention({
                'pending_input': task['id'] in question_tasks,
                'pending_guidance': status == 'blocked' and task['id'] not in question_tasks,
                'interrupted': status == 'interrupted',
            }),
            'candidates': [{key: c.get(key) for key in ('model', 'stage', 'status', 'updated_at', 'resource_wait')}
                           for c in task.get('candidates', [])],
        })
    status = value.get('status')
    ready = value.get('ready') is True and status == 'complete'
    stale = bool(value.get('telemetry_stale'))
    incomplete = bool(value.get('telemetry_incomplete'))
    # These are controller observations, not OS process-exit proofs. A stopped
    # controller or an empty list must never imply that all services have exited.
    operations = [{key: op.get(key) for key in (
        'task', 'task_id', 'model', 'candidate_model', 'stage', 'phase', 'role', 'title', 'kind', 'purpose',
        'elapsed_seconds', 'timeout_seconds', 'idle_seconds')}
        for op in value.get('operations', [])]
    for operation in operations:
        operation['current'] = status == 'running' and not stale and not incomplete
    attention = project_attention({
        'is_root': True, 'outcome': 'succeeded' if ready else None,
        'pending_input': bool(questions),
        'pending_guidance': status == 'blocked' or any(t['status'] == 'blocked' for t in tasks),
        'interrupted': status == 'interrupted',
    })
    if stale:
        attention['categories'].append('stale')
    if incomplete:
        attention['categories'].append('unverifiable')
        attention['requires_action'] = True
    return {
        'schema': SCHEMA, 'projected_at': time.time() if observed_at is None else observed_at,
        'run': {'status': status, 'accepted_tasks': sum(t['accepted'] for t in tasks),
                'total_tasks': len(tasks), 'ready': ready,
                'integration': value.get('integration'), 'attention': attention},
        'tasks': tasks, 'questions': questions,
        'execution': {'operations': operations, 'telemetry_stale': stale,
                      'telemetry_incomplete': incomplete, 'process_exit': 'not_observed'},
        'capabilities': {'read_status': True, 'answer_questions': False, 'stop_processes': False},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from run_status import snapshot
    config = args.config.expanduser().resolve(strict=True)
    if config.name != 'config.json':
        parser.error('Expected the saved run config.json')
    output = args.output.expanduser().absolute()
    # Never overwrite controller state, evidence or an existing user document.
    if output.exists() or output.is_symlink():
        parser.error('Output must be a new file')
    if output.resolve().is_relative_to(config.parent):
        parser.error('Output must be outside the saved run directory')
    value = project(snapshot(config.parent))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects against a concurrent exporter.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    print(output)


if __name__ == '__main__':
    main()
