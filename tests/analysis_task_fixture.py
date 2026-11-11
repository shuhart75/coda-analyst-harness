"""Explicit fictitious commit-task ownership for temporary test projects."""
import json


def task_binding(key='RSCON-123', kind='delivery', owner_id=None):
    owner_type = 'delivery' if kind == 'delivery' else 'quarter'
    owner_id = owner_id or ('demo' if owner_type == 'delivery' else '2026-Q3')
    prefix = {'delivery': '[SA][PR]', 'planning': '[PLANNING_QUARTER]',
              'baseline': '[BASELINE]', 'context': '[CONTEXT]'}[kind]
    return dict(schema_version=2, provider='sbertrek', key=key, suit='task', kind=kind,
                summary=prefix + ' ' + owner_id, epic='RSCON-900',
                owner={'type': owner_type, 'id': owner_id},
                confirmation='Test analyst confirms owner epic; MCP fixture verifies task')


def write_binding(root, key='RSCON-123'):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'analysis-task.json').write_text(json.dumps(task_binding(key, owner_id=root.name)) + '\n')


def write_planning_binding(project, quarters=('2026-Q3',), key='RSCON-123', shared=None):
    path = project / 'planning/task-bindings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(schema_version=1,
        quarters={quarter: {**task_binding(key, 'planning', quarter), 'epic': 'RSCON-9' + quarter.replace('-Q', '')} for quarter in quarters},
        epics={quarter: dict(provider='sbertrek', key='RSCON-9' + quarter.replace('-Q', ''), confirmation='Confirmed quarter epic') for quarter in quarters},
        shared_paths=shared or {})) + '\n')
