"""Explicit fictitious AN ownership for temporary test deliveries."""
import json


def write_binding(root, key='RSCON-123'):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'analysis-task.json').write_text(json.dumps(dict(schema_version=1,
        provider='sbertrek', key=key, role='AN', confirmation='Test analyst confirms this delivery')) + '\n')


def write_planning_binding(project, quarters=('2026-Q3',), key='RSCON-123', shared=None):
    path = project / 'planning/task-bindings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(schema_version=1, quarters={quarter: dict(
        schema_version=1, provider='sbertrek', key=key, role='AN',
        confirmation='Test analyst confirms quarterly planning') for quarter in quarters},
        shared_paths=shared or {})) + '\n')
