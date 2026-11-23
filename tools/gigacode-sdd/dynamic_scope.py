"""Controller-owned, durable task footprints and cooperative scope handoff.

The pinned plan stays immutable. Only exact technical paths are added. A task
waiting for an active owner returns its executor slots and resource leases;
FIFO reacquisition of its entire footprint prevents circular hold-and-wait.
"""
from copy import deepcopy
from pathlib import Path, PurePosixPath
import json
import subprocess
import threading
import uuid

from workspace import (_allowed, _changed, _forbidden, _safe_path, clone_snapshot,
                       export_patch, git, snapshot, WorkspaceError)


class ScopeWait(RuntimeError):
    pass


def technical_path(root, name):
    if (not isinstance(name, str) or str(PurePosixPath(name)) != name or
            name in ('', '.', '/') or any(c in name for c in '*?[]:\x00\n\r') or _forbidden(name)):
        raise WorkspaceError('Invalid or protected scope path: ' + str(name))
    parts = PurePosixPath(name).parts
    if (PurePosixPath(name).name in ('requirements.md', 'requirements-state.json') or
            'specs' in parts and 'openspec' in parts or
            'openspec' in parts and 'context' in parts):
        raise WorkspaceError('Requirements/context cannot be added to technical scope: ' + name)
    target = _safe_path(root, name)
    cursor = Path(root).resolve()
    for part in parts:
        cursor /= part
        if cursor.is_symlink():
            raise WorkspaceError('Symlink cannot be added to technical scope: ' + name)
    if target.is_dir():
        raise WorkspaceError('Scope expansion needs exact files, not directories: ' + name)
    return name


class Manager:
    def __init__(self, runner):
        self.runner = runner
        self.lock = threading.RLock()
        self.active = {}
        self.data = None

    def load(self):
        from controller import read, digest
        if self.data is not None:
            return
        self.path = self.runner.root/'dynamic-scopes.json'
        plan_path = self.runner.root/'plan.json'
        self.plan = read(plan_path)
        self.binding = digest(plan_path)
        self.tasks = {t['id']: t for t in self.plan['tasks']}
        if self.path.is_symlink():
            raise WorkspaceError('Scope ledger must not be a symlink')
        self.data = read(self.path) if self.path.exists() else {'plan': self.binding, 'next_ticket': 0, 'tasks': {}}
        if self.data.get('plan') != self.binding:
            raise WorkspaceError('Dynamic scopes belong to a different plan')
        for key, entry in self.data['tasks'].items():
            if key not in self.tasks and key != 'integration':
                raise WorkspaceError('Unknown dynamic scope task')
            for name in entry['paths']:
                technical_path(self.runner.main, name)

    def save(self):
        from controller import save
        save(self.path, self.data)

    def effective(self, task):
        with self.lock:
            self.load()
            result = deepcopy(task)
            entry = self.data['tasks'].get(task['id'], {})
            result['writes'] = list(dict.fromkeys(result['writes'] + entry.get('paths', [])))
            # Re-run the immutable checks of accepted owners whose behavior is
            # changed. Resource union prevents concurrent use of their fixtures.
            for owner in entry.get('owners', []):
                other = self.tasks[owner]
                result['resources'] = sorted(set(result.get('resources', [])) | set(other.get('resources', [])))
                if self.runner.state['tasks'].get(owner, {}).get('status') == 'done':
                    for check in other['checks']:
                        if check not in result['checks']:
                            result['checks'].append(check)
            if entry.get('paths'):
                instruction = ('\nController technical scope extensions: ' + json.dumps(entry['paths']) +
                    '. Preserve accepted behavior and resolve any rebased conflicts. Never weaken assertions, '
                    'checks, requirements or permissions. All task and affected accepted-owner checks must pass; '
                    'independent review is required on this exact new code.')
                if instruction not in result['prompt']:
                    result['prompt'] += instruction
            return result

    def try_start(self, task):
        from controller import overlaps
        with self.lock:
            effective = self.effective(task)
            if any(overlaps(effective, self.effective(t)) for t in self.active.values()):
                return False
            mine = self.data['tasks'].get(task['id'], {}).get('ticket', float('inf'))
            for key, entry in self.data['tasks'].items():
                if (key == task['id'] or not entry.get('queued') or entry['ticket'] >= mine
                        or self.runner.state['tasks'].get(key, {}).get('status') in ('done', 'blocked')):
                    continue
                other = self.tasks.get(key)
                if (other and all(self.runner.state['tasks'][d]['status'] == 'done' for d in other['depends_on'])
                        and overlaps(effective, self.effective(other))):
                    return False
            self.active[task['id']] = task
            if task['id'] in self.data['tasks']:
                self.data['tasks'][task['id']]['queued'] = False
                self.save()
            return True

    def finish(self, task):
        with self.lock:
            self.active.pop(task['id'], None)

    def expand(self, task, value, paths, *, refreshing=False, questions=None):
        from controller import overlaps
        with self.lock:
            self.load()
            work = Path(value['workspace'])
            paths = sorted({technical_path(work, p) for p in paths})
            if not paths:
                return self.effective(task)
            import operator_questions as oq
            from scope_repair import DENY
            questions = oq.questions(self.runner.root) if questions is None else questions
            for question in questions:
                action = question.get('action') or {}
                if (question.get('task') == task['id'] and action.get('kind') == 'scope-repair-v1'
                        and (question.get('answer') or {}).get('text') == DENY
                        and any(item['path'] in paths for item in action['files'])):
                    raise WorkspaceError('Technical repair explicitly denied by operator')
            previous_resources = set(self.effective(task).get('resources', []))
            entry = self.data['tasks'].setdefault(task['id'], {'paths': [], 'owners': [], 'queued': False})
            previous = set(entry['paths'])
            entry['paths'] = sorted(previous | set(paths))
            owners = {key for key, other in self.tasks.items() if key != task['id']
                      and any(_allowed(p, self.effective(other)['writes']) for p in paths)}
            entry['owners'] = sorted(set(entry['owners']) | owners)
            effective = self.effective(task)
            conflicts = sorted(key for key, other in self.active.items()
                               if key != task['id'] and overlaps(effective, self.effective(other)))
            # Even a completed owner may have committed since this worker was
            # copied. Re-enter through the scheduler to refresh both variants.
            stale = not refreshing and bool(git(self.runner.main, 'diff', '--name-only', value['base'], 'HEAD', '--', *paths))
            if conflicts or stale or set(effective.get('resources', [])) != previous_resources:
                if 'ticket' not in entry or not entry.get('queued'):
                    entry['ticket'] = self.data['next_ticket']
                    self.data['next_ticket'] += 1
                entry.update(queued=True, waiting_for=conflicts)
                self.save()
                raise ScopeWait('Пересечение файлов; сохранены изменения, ожидание: ' +
                                (', '.join(conflicts) or 'обновление рабочей копии'))
            self.save()
            if set(paths) - previous:
                self.runner.progress(task['id'] + ': расширена техническая область: ' + ', '.join(paths))
            return effective

    def requested(self, task, value, request, *, questions=None, allow_reserved=False):
        paths = request.get('paths')
        if paths is None and isinstance(request.get('patch'), dict):
            paths = [request['patch'].get('file')]
        if not isinstance(paths, list) or not paths:
            raise ValueError('Scope request needs exact file paths')
        work = Path(value['workspace'])
        resolved = []
        for raw in paths:
            technical_path(work, raw)
            options = {raw, (PurePosixPath(task['cwd'])/raw).as_posix()}
            found = {p for p in options if (work/p).is_file() or git(work, 'ls-tree', value['base'], '--', p)}
            if len(found) > 1:
                raise ValueError('Ambiguous repair path: ' + raw)
            resolved.append(next(iter(found)) if found else raw)
        if all(_allowed(p, self.effective(task)['writes']) for p in resolved):
            if allow_reserved:
                return self.effective(task)
            raise ValueError('Requested paths are already reserved; continue implementation')
        return self.expand(task, value, resolved, questions=questions)

    def discover(self, task, value, *, refreshing=False):
        with self.lock:
            effective = self.effective(task)
            paths = [p for p in _changed(Path(value['workspace']), value['base'])
                     if not _allowed(p, effective['writes'])]
            return self.expand(task, value, paths, refreshing=refreshing)

    def refresh(self, task, value, slot):
        """Reapply WIP onto a fresh accepted tree; never overwrite the old copy."""
        with self.lock:
            effective = self.effective(task)
            entry = self.data['tasks'].get(task['id'], {})
            if git(self.runner.main, 'rev-parse', 'HEAD') == value['base']:
                return False
            if not entry.get('paths') and not git(self.runner.main, 'diff', '--name-only', value['base'], 'HEAD', '--', *effective['writes']):
                return False
            old = Path(value['workspace'])
            self.runner.audit(old)
            effective = self.discover(task, value, refreshing=True)
            patch = export_patch(old, value['base'], effective['writes'], slot/('scope-wip-'+uuid.uuid4().hex+'.patch'))
            new = self.runner.root/'workers'/(task['id']+'-scope-'+uuid.uuid4().hex[:12])
            clone_snapshot(self.runner.main, new)
            conflicts = []
            if Path(patch['patch']).stat().st_size:
                proc = subprocess.run(['git', '-C', str(new), 'apply', '--3way', '--binary', patch['patch']],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                conflicts = git(new, 'diff', '--name-only', '--diff-filter=U').splitlines()
                if proc.returncode and not conflicts:
                    raise WorkspaceError('Scope WIP preserved; cannot apply to new base: ' + proc.stderr.decode(errors='replace'))
                if conflicts:
                    git(new, 'add', '--', *conflicts)
            value.setdefault('scope_rebases', []).append({'workspace': str(old), 'base': value['base'],
                'patch': patch, 'checks': value.get('checks'), 'review': value.get('review')})
            status = 'waiting-user' if value.get('status') == 'waiting-user' else 'pending'
            value.update(workspace=str(new), base=git(new, 'rev-parse', 'HEAD'), status=status, stage='execute',
                         feedback={'scope_rebase': 'Accepted base changed; preserve accepted work, resolve conflicts, rerun all checks.',
                                   'conflicts': conflicts}, scope_conflicts=conflicts)
            for key in ('patch', 'workspace_snapshot', 'checks', 'review', 'review_draft', 'recovered_review',
                        'pending_session', 'session_result', 'review_resolution', 'recovery_wait'):
                value.pop(key, None)
            value['workspace_snapshot'] = snapshot(new, normalize_settings=True)
            return True

    def verify_conflicts(self, value):
        for name in value.get('scope_conflicts', []):
            path = Path(value['workspace'])/name
            if path.exists() and any(line.startswith((b'<<<<<<< ', b'=======', b'>>>>>>> ')) for line in path.read_bytes().splitlines()):
                raise ValueError('Resolve preserved merge conflict: ' + name)

    @staticmethod
    def question_request(question):
        if question.get('answer') or question.get('resolved'):
            return None
        action = question.get('action') or {}
        if action.get('kind') == 'scope-repair-v1':
            return {'paths': [f['path'] for f in action['files']]}
        if question.get('topic', '').startswith('scope-unavailable-'):
            for variant in question.get('requests', []):
                details = variant.get('details') or {}
                if isinstance(details, dict) and isinstance(details.get('model_request'), dict):
                    return details['model_request']
        return None

    def needs_question_refresh(self, ids):
        import operator_questions as oq
        return any(q['id'] in ids and self.question_request(q) for q in oq.questions(self.runner.root))

    def resume_question(self, task, value):
        import operator_questions as oq
        from controller import save
        import time
        with self.lock:
            questions = oq.questions(self.runner.root)
            question = next((q for q in questions if q['id'] == value.get('question_id')), None)
            request = self.question_request(question or {})
            if not request:
                return False
            with oq.locked(self.runner.root) as folder:
                if (folder/(question['id']+'-answer.json')).exists():
                    return False
                try:
                    paths = request.get('paths') or [request.get('patch', {}).get('file')]
                    if not all(isinstance(p, str) and _allowed(p, self.effective(task)['writes']) for p in paths):
                        self.requested(task, value, request, questions=questions)
                except (ValueError, WorkspaceError):
                    return False  # Protected or ambiguous requests remain human questions.
                save(folder/(question['id']+'-resolved.json'), {'reason': 'Technical scope managed by controller',
                                                              'resolved_at': time.time()})
                value.update(status='pending', stage='execute')
                value.pop('question_id', None)
                result = value.get('session_result', {})
                if result:
                    value['handled_question_result'] = oq.fingerprint([result.get('events_path'), result.get('text')])
                return True

    def recover_fatal(self, folder):
        """Migrate only the known legacy scope error, preserving its receipt."""
        from controller import read
        path = folder/'fatal.json'
        if not path.is_file() or path.is_symlink():
            return
        value = read(path)
        if value.get('scope') != folder.name:
            return
        errors = [value, *value.get('related_failures', [])]
        prefix = 'Change outside task ownership: '
        if all(v.get('type') == 'WorkspaceError' and str(v.get('error', '')).startswith(prefix) for v in errors):
            for item in errors:
                technical_path(self.runner.main, item['error'][len(prefix):])
            path.rename(folder/('legacy-scope-fatal-'+uuid.uuid4().hex+'.json'))
