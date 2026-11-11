"""Confirmed grooming input and deterministic task documents.

analyst-sdd-receiver:v1

No tracker calls. This module is shipped with the exchange receiver.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile

ROLE = {'FE': 'frontend', 'BE': 'backend', 'QA': None, 'AN': None}
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
KEY = re.compile(r'[A-Z][A-Z0-9_]*-[1-9][0-9]*\Z')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def approval_hash(value):
    return fingerprint({k: v for k, v in value.items() if k != 'confirmation'})


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Grooming: missing ' + name)
    return value


def confirm(value, name):
    c = value.get('confirmation', {})
    nonempty(c.get('text'), name + ' confirmation')
    if c.get('sha256') != approval_hash(value):
        raise ValueError('Grooming: stale confirmation: ' + name)


def requirement_blocks(text):
    """Keep authored wording, splitting only at actual Markdown headings."""
    matches = list(re.finditer(r'^### (REQ-[A-Z0-9-]+)\.\s+(.+)$', text, re.M))
    result = {}
    for match in matches:
        if match[1] in result:
            raise ValueError('Grooming: duplicate requirement')
        tail = text[match.end():]
        end = re.search(r'^#{1,3} ', tail, re.M)
        block = text[match.start():match.end()] + tail[:end.start() if end else len(tail)]
        scenarios = list(re.finditer(r'^#### (?:Сценарий|Scenario):\s*(.+)$', block, re.M))
        scenario_blocks = {}
        for i, s in enumerate(scenarios):
            if s[1] in scenario_blocks:
                raise ValueError('Grooming: duplicate scenario')
            scenario_blocks[s[1]] = block[s.start():scenarios[i+1].start() if i+1 < len(scenarios) else len(block)].rstrip()
        result[match[1]] = {'title': match[2], 'rule': block[:scenarios[0].start() if scenarios else len(block)].rstrip(),
                            'scenarios': scenario_blocks}
    if not result:
        raise ValueError('Grooming: no authored requirements')
    return result


def task_source_hash(task, blocks):
    selected = {}
    for item in task['requirements']:
        block = blocks[item['req_id']]
        selected[item['req_id']] = {'rule': block['rule'],
            'scenarios': {name: block['scenarios'][name] for name in item['scenarios']}}
    return fingerprint(selected)


def validate(plan, text, package, *, confirmed=True, complete=True):
    if plan.get('schema_version') != 1 or plan.get('estimate_unit') != 'person-days':
        raise ValueError('Grooming: schema 1 and person-days required')
    if plan.get('requirements_sha256') != digest(text.encode()):
        raise ValueError('Grooming: requirements changed')
    for field in ('delivery', 'stage_id'):
        if not SLUG.fullmatch(str(plan.get(field, ''))):
            raise ValueError('Grooming: invalid ' + field)
    markers = re.findall(r'^Этап поставки: (.+)$', text, re.M)
    if markers != [plan['stage_id']]:
        raise ValueError('Grooming: stage mismatch')
    if complete:
        if not KEY.fullmatch(str(plan.get('epic_key', ''))):
            raise ValueError('Grooming: confirmed epic key required')
        nonempty(plan.get('coverage_review'), 'coverage review')
        nonempty(plan.get('qa_review'), 'QA scope and estimate decision')
    blocks = requirement_blocks(text)
    changes = {c['contour'] + '/' + c['change_id']: c for c in package['changes']} if package else {}
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Grooming: tasks required')
    ids, covered, scenarios, change_coverage = set(), set(), set(), set()
    for task in tasks:
        task_id = task.get('id', '')
        if not SLUG.fullmatch(task_id) or task_id in ids:
            raise ValueError('Grooming: invalid or duplicate task ID')
        ids.add(task_id)
        summary = nonempty(task.get('summary'), 'summary')
        prefix = re.match(r'^(AN|BE|FE|QA)(?=\s|:)', summary)
        if not prefix or task.get('role') != prefix[1]:
            raise ValueError('Grooming: role must match summary prefix')
        estimate = task.get('estimate')
        if type(estimate) not in (int, float) or not math.isfinite(estimate) or estimate <= 0:
            raise ValueError('Grooming: positive finite person-days required')
        nonempty(task.get('description'), 'short description')
        nonempty(task.get('acceptance'), 'observable acceptance result')
        deps = task.get('depends_on')
        if not isinstance(deps, list) or len(set(deps)) != len(deps):
            raise ValueError('Grooming: dependency list required')
        selected = task.get('sdd_changes')
        if not isinstance(selected, list) or len(set(selected)) != len(selected):
            raise ValueError('Grooming: SDD change list required')
        contour = ROLE[task['role']]
        if complete and contour and not selected:
            raise ValueError('Grooming: FE/BE needs SDD changes')
        for change in selected:
            if package and (change not in changes or (contour and changes[change]['contour'] != contour)):
                raise ValueError('Grooming: SDD contour mismatch')
        mapping = task.get('requirements')
        if not isinstance(mapping, list) or not mapping:
            raise ValueError('Grooming: requirement mapping required')
        seen = set()
        for item in mapping:
            req = item.get('req_id')
            if req not in blocks or req in seen:
                raise ValueError('Grooming: unknown or duplicate requirement')
            seen.add(req)
            nonempty(item.get('scope'), 'task responsibility')
            names = item.get('scenarios')
            if not isinstance(names, list) or len(set(names)) != len(names) or not set(names) <= set(blocks[req]['scenarios']):
                raise ValueError('Grooming: unknown or duplicate scenario')
            if blocks[req]['scenarios'] and not names:
                raise ValueError('Grooming: select scenarios')
            if contour:
                matching = [key for key in selected if key in changes and any(x['req_id'] == req for x in changes[key]['coverage'])]
                if package and not matching:
                    raise ValueError('Grooming: requirement absent from selected contour SDD')
                covered.add(req)
                scenarios.update((req, name) for name in names)
                change_coverage.update((key, req) for key in matching)
        if complete and task.get('source_sha256') != task_source_hash(task, blocks):
            raise ValueError('Grooming: task source changed: ' + task_id)
        if confirmed:
            confirm(task, task_id)
    expected = {(key, item['req_id']) for key, c in changes.items() for item in c['coverage']}
    expected_scenarios = {(req, name) for req, block in blocks.items() for name in block['scenarios']}
    if complete and (covered != set(blocks) or scenarios != expected_scenarios or change_coverage != expected):
        raise ValueError('Grooming: incomplete requirement/scenario/contour coverage')
    graph = {t['id']: t['depends_on'] for t in tasks}
    def visit(node, stack, done):
        if node not in graph or node in stack:
            raise ValueError('Grooming: unknown dependency or dependency cycle')
        if node in done:
            return
        for parent in graph[node]:
            visit(parent, stack | {node}, done)
        done.add(node)
    done = set()
    for node in graph:
        visit(node, set(), done)
    if confirmed:
        confirm(plan, 'complete plan')
    return blocks


def require_identity(root, delivery, stage_id):
    import sdd_bundle as bundle
    plan = bundle._json(Path(root) / 'grooming.json')
    if plan.get('delivery') != delivery or plan.get('stage_id') != stage_id:
        raise ValueError('Grooming: input belongs to another delivery or stage')


def rebase_links(text):
    # Derived documents are two directories below the authored root.
    def replace(match):
        url = match[1]
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', url) or url.startswith('/'):
            return match[0]
        target = '../../requirements.md' + url if url.startswith('#') else '../../' + url
        return '](' + target + ')'
    parts = re.split(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)', text)
    return ''.join(part if i % 2 else re.sub(r'\]\(([^\s)]+)\)', replace, part) for i, part in enumerate(parts))


def task_document(task, plan, blocks):
    lines = ['# ' + task['summary'], '', f"Оценка: {task['estimate']:g} чел.-дн.",
             f"Этап поставки: {plan['stage_id']}", '', task['description'], '',
             '## Приёмка', '', task['acceptance'], '',
             '## Зависимости', '']
    lines += [f"- [{dep}](../{dep}/requirements.md)" for dep in task['depends_on']] or ['Нет.']
    lines += ['', '## Общий контекст', '', '[Полный договор поставки](../../requirements.md). '
              'Общие ограничения и определения этого договора действуют для задачи.', '',
              '## SDD', '']
    lines += [f'- [Proposal: {key}](../../sdd/{key}/proposal.md)' for key in task['sdd_changes']] or ['Отдельное изменение SDD не требуется.']
    lines += ['', '## Требования задачи', '']
    for item in task['requirements']:
        block = blocks[item['req_id']]
        lines += ['Объём этой задачи: ' + item['scope'], '', rebase_links(block['rule']), '']
        for name in item['scenarios']:
            lines += [rebase_links(block['scenarios'][name]), '']
    return '\n'.join(lines).rstrip() + '\n'


def files_for(plan, text, package):
    blocks = validate(plan, text, package)
    files = {'grooming.json': json.dumps(plan, ensure_ascii=False, indent=2) + '\n',
             **{task['id'] + '/requirements.md': task_document(task, plan, blocks) for task in plan['tasks']}}
    files['SHA256SUMS'] = ''.join(digest(value.encode()) + '  ' + name + '\n' for name, value in sorted(files.items()))
    return files


def inspect(root, text, package):
    import sdd_bundle as bundle
    root = Path(root)
    plan = bundle._json(root / 'grooming.json')
    expected = files_for(plan, text, package)
    actual = bundle._files(root)
    wanted = {name: digest(content.encode()) for name, content in expected.items()}
    if actual != wanted:
        raise ValueError('Grooming: task documents differ from confirmed source')
    allowed = {t['id'] for t in plan['tasks']}
    if any(p.relative_to(root).as_posix() not in allowed for p in root.rglob('*') if p.is_dir()):
        raise ValueError('Grooming: unexpected task directory')
    return {'files': actual, 'sha256': fingerprint(actual)}


def preview(plan, text, package):
    blocks = validate(plan, text, package, confirmed=False, complete=False)
    pending = []
    for original in plan['tasks']:
        task = {**original, 'source_sha256': task_source_hash(original, blocks)}
        try:
            confirm(task, task['id'])
        except ValueError:
            pending.append(task)
    task = pending[0] if pending else None
    return {'next_action': 'confirm-one-task' if task else 'review-complete-plan',
            'task': ({'summary': task['summary'], 'estimate_person_days': task['estimate'],
                      'requirements': [{'title': blocks[x['req_id']]['title'], 'scope': x['scope'],
                                        'scenarios': x['scenarios']} for x in task['requirements']],
                      'acceptance': task['acceptance'],
                      'depends_on': [next(t['summary'] for t in plan['tasks'] if t['id'] == dep) for dep in task['depends_on']]}
                     if task else None),
            'confirmation_sha256': approval_hash(task or plan),
            'source_sha256': task['source_sha256'] if task else None, 'remaining_tasks': len(pending),
            'total_person_days': sum(t['estimate'] for t in plan['tasks'])}


def build(project, feature, plan):
    from project_layout import feature_root
    from tracker_safety import require_write_context
    import sdd_bundle as bundle
    require_write_context(project, modes=('requirements',), feature=feature)
    root = feature_root(project, feature)
    # Canonical key, even when a long-lived feature alias resolves one delivery.
    from project_layout import exchange_binding
    binding = exchange_binding(project, feature)
    if plan['delivery'] != (binding['delivery_key'] if binding else feature):
        raise ValueError('Grooming: canonical delivery key required')
    bundle._plain(root / 'requirements.md')
    bundle._plain(root / 'requirements-state.json')
    source_files = bundle._files(root / 'sdd')
    text = (root / 'requirements.md').read_text()
    package = bundle._json(root / 'sdd/package.json')
    state = bundle._json(root / 'requirements-state.json')
    if state.get('delivery_stages', {}).get('active_stage_id') != plan['stage_id']:
        raise ValueError('Grooming: inactive stage')
    content = files_for(plan, text, package)
    material_files = bundle._files(root / 'materials') if (root / 'materials').exists() else None
    target = root / 'tasks'
    bundle._plain(target)
    if target.exists():
        # Never replace unrelated content. Old generated files must still match their source.
        old_plan = bundle._json(target / 'grooming.json')
        bundle.materials_descriptor(target)  # Refuse edited, added or missing generated files.
        if old_plan['delivery'] != plan['delivery']:
            raise ValueError('Grooming: existing tasks belong to another delivery')
        old_ids = {t['id'] for t in old_plan['tasks']}
        new_ids = {t['id'] for t in plan['tasks']}
        removed = {t['id']: t for t in plan.get('removed_tasks', [])}
        if not (old_ids - new_ids) <= set(removed):
            raise ValueError('Grooming: explicitly decide the fate of removed tasks')
        for task_id in old_ids - new_ids:
            nonempty(removed[task_id].get('decision'), 'removed task decision')
    previous_files = bundle._files(target) if target.exists() else None
    package = {**package, 'schema_version': 3}
    with tempfile.TemporaryDirectory(prefix='grooming-', dir=root.parent) as temp:
        staged = Path(temp)
        shutil.copytree(root / 'sdd', staged / 'sdd')
        if (root / 'materials').exists():
            shutil.copytree(root / 'materials', staged / 'materials')
        (staged / 'sdd/package.json').write_text(json.dumps(package, ensure_ascii=False, indent=2) + '\n')
        for name, value in content.items():
            path = staged / 'tasks' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        bundle.inspect(staged / 'sdd', text)
        if ((root / 'requirements.md').read_text() != text or bundle._files(root / 'sdd') != source_files
                or bundle._json(root / 'requirements-state.json') != state
                or (bundle._files(target) if target.exists() else None) != previous_files
                or (bundle._files(root / 'materials') if (root / 'materials').exists() else None) != material_files):
            raise ValueError('Grooming: source changed during generation')
        require_write_context(project, modes=('requirements',), feature=feature)
        # Validate the complete replacement before the first mutation. Preserve an old tree until replacement succeeds.
        backup = staged / 'previous-tasks'
        if target.exists():
            target.rename(backup)
        try:
            (staged / 'tasks').rename(target)
            (staged / 'sdd/package.json').replace(root / 'sdd/package.json')
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if backup.exists():
                backup.rename(target)
            raise
    return {'status': 'grooming-built', 'task_count': len(plan['tasks']), 'path': str(target),
            'next_action': 'review-save-merge-then-delivery-audit', 'tracker_writes': False}


def main():
    p = argparse.ArgumentParser(description='Confirmed grooming in person-days')
    p.add_argument('action', choices=['preview', 'build', 'check'])
    p.add_argument('project', type=Path)
    p.add_argument('feature')
    p.add_argument('--plan', type=Path)
    a = p.parse_args()
    try:
        from project_layout import feature_root
        root = feature_root(a.project.resolve(), a.feature)
        import sdd_bundle as bundle
        if a.action == 'build':
            result = build(a.project.resolve(), a.feature, bundle._json(a.plan))
        elif a.action == 'check':
            result = bundle.inspect(root / 'sdd', (root / 'requirements.md').read_text())
        else:
            plan = bundle._json(a.plan)
            result = preview(plan, (root / 'requirements.md').read_text(),
                             bundle._json(root / 'sdd/package.json') if (root / 'sdd/package.json').is_file() else None)
            previous = root / 'tasks/grooming.json'
            if previous.exists():
                old = {t['id']: t for t in bundle._json(previous)['tasks']}
                new = {t['id']: t for t in plan['tasks']}
                result['changes'] = {
                    'added': [t['summary'] for key, t in new.items() if key not in old],
                    'removed': [t['summary'] for key, t in old.items() if key not in new],
                    'changed': [t['summary'] for key, t in new.items() if key in old and approval_hash(t) != approval_hash(old[key])]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        p.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
