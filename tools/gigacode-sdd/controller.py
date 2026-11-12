"""Persistent, isolated executor/reviewer orchestration. Python 3.10+, no dependencies."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from policy import prepare_permissions, nexus_environment, audit_dependency_sources, make_policy_prompt
from transport import run_session, _stop_group
from workspace import git, snapshot, clone_snapshot, export_patch, apply_patch, commit_changes

class Blocked(RuntimeError):
    pass


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with tmp.open('x', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise Blocked('Expected safe repository-relative path')
    if value != Path(value).as_posix():
        raise Blocked('Repository-relative paths must use canonical slash spelling')
    return value


def under(root, value):
    result = (Path(root) / relative(value)).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise Blocked('Path escapes its workspace')
    return result


def file_manifest(root):
    result = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_symlink():
            raise Blocked('Input packet may not contain symlinks')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = digest(p)
    return result


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise Blocked('Agent result must be a JSON object')
    return value


def validate_checks(checks, root):
    if not isinstance(checks, list) or not checks:
        raise Blocked('At least one executable acceptance check is required')
    for check in checks:
        if not isinstance(check, dict):
            raise Blocked('Invalid check')
        argv = check.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise Blocked('Check argv must be a nonempty string list')
        if not under(root, check.get('cwd', '.')).is_dir():
            raise Blocked('Check cwd does not exist')
        if not isinstance(check.get('timeout', 600), (int, float)) or check.get('timeout', 600) <= 0:
            raise Blocked('Invalid check timeout')


def validate_plan(plan, root, requirements):
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or not tasks:
        raise Blocked('Plan has no tasks')
    ids = [t.get('id') for t in tasks]
    if any(not isinstance(i, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', i) for i in ids) or len(set(ids)) != len(ids):
        raise Blocked('Task identifiers must be unique safe names')
    covered = set()
    for task in tasks:
        if task.get('cwd') not in ('.', 'backend', 'frontend') or not under(root, task['cwd']).is_dir():
            raise Blocked('Task cwd must be an existing root/backend/frontend')
        writes = task.get('writes')
        if not isinstance(writes, list) or not writes:
            raise Blocked('Task needs explicit ownership paths')
        for p in writes:
            relative(p)
            if p in ('.', './') or any(x in Path(p).parts for x in ('.git', '.gigacode', '.codex', 'AGENTS.md', 'GIGACODE.md')):
                raise Blocked('Task owns a protected path')
            under(root, p)
        deps = task.get('depends_on')
        if not isinstance(deps, list) or any(d not in ids or d == task['id'] for d in deps):
            raise Blocked('Invalid dependencies')
        if not isinstance(task.get('resources', []), list) or not all(isinstance(x, str) for x in task.get('resources', [])):
            raise Blocked('Invalid shared resources')
        if not isinstance(task.get('prompt'), str) or not task['prompt'].strip():
            raise Blocked('Task instruction is empty')
        if not isinstance(task.get('requirements'), list) or not set(task['requirements']) <= set(requirements):
            raise Blocked('Unknown requirement identifiers')
        covered.update(task['requirements'])
        validate_checks(task.get('checks'), root)
    if covered != set(requirements):
        raise Blocked('Plan does not cover every pinned requirement')
    done = set()
    while len(done) < len(ids):
        ready = {t['id'] for t in tasks if t['id'] not in done and set(t['depends_on']) <= done}
        if not ready:
            raise Blocked('Cyclic task dependencies')
        done.update(ready)
    validate_checks(plan.get('integration_checks'), root)
    return plan


def overlaps(a, b):
    if set(a.get('resources', [])) & set(b.get('resources', [])):
        return True
    return any(x == y or x.startswith(y.rstrip('/') + '/') or y.startswith(x.rstrip('/') + '/')
               for x in a['writes'] for y in b['writes'])


def stable_feedback(value):
    if isinstance(value, dict):
        return {k: stable_feedback(v) for k, v in value.items()
                if k not in {'log', 'events_path', 'session_id'}}
    if isinstance(value, list):
        return [stable_feedback(v) for v in value]
    return value


def git_controls(root):
    """Hash local hook/config state without logging configuration secrets."""
    gitdir = Path(git(root, 'rev-parse', '--absolute-git-dir'))
    paths = [gitdir / 'config']
    try:
        configured = git(root, 'config', '--path', '--get', 'core.hooksPath')
    except RuntimeError:
        configured = ''
    hooks = Path(configured) if configured else gitdir / 'hooks'
    if not hooks.is_absolute():
        hooks = Path(root) / hooks
    paths.extend(p for p in hooks.rglob('*') if p.is_file() or p.is_symlink())
    return {str(p): ('link:' + os.readlink(p) if p.is_symlink() else digest(p))
            for p in paths if p.exists() or p.is_symlink()}


def protected_context(state):
    return {p: value for p, value in state['files'].items()
            if any(x in Path(p).parts for x in ('.gigacode', 'AGENTS.md', 'GIGACODE.md'))}


class Runner:
    def __init__(self, config):
        self.config_path = Path(config).resolve()
        self.cfg = read(self.config_path)
        self.root = Path(self.cfg['state_dir'])
        self.main = self.root / 'work/main'
        self.state_path = self.root / 'state.json'
        self.state = read(self.state_path)
        self.stop = threading.Event()
        self.env = nexus_environment(self.cfg['nexus'])
        self._probed = set()
        self._preflight_reports = []
        self._report_lock = threading.Lock()
        self._config_digest = digest(self.config_path)

    def persist(self):
        save(self.state_path, self.state)

    @contextmanager
    def locked(self):
        with (self.root / 'run.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('Another controller owns this run') from None
            if digest(self.config_path) != self._config_digest:
                raise Blocked('Configuration changed while acquiring lock')
            self.state = read(self.state_path)
            old = {}
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    old[sig] = signal.signal(sig, lambda *_: self.stop.set())
            try:
                self.guard()
                yield
            finally:
                self.persist()
                for sig, handler in old.items():
                    signal.signal(sig, handler)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def guard(self):
        if digest(self.config_path) != self._config_digest:
            raise Blocked('Configuration changed during run; restart with reviewed configuration')
        if file_manifest(self.root / 'input') != self.cfg['packet_files']:
            raise Blocked('Pinned packet changed; create a new run for revised requirements')
        for path, before in self.cfg['protected_snapshots'].items():
            if snapshot(path) != before:
                raise Blocked('Source/protected checkout changed: ' + path)
            if git_controls(path) != self.cfg['protected_git_controls'][path]:
                raise Blocked('Source/protected Git configuration or hooks changed: ' + path)

    def audit(self, root):
        findings = audit_dependency_sources(root, self.cfg['nexus']['nexus_hosts'])
        if findings:
            raise Blocked('Dependency sources are not Nexus-only: ' + json.dumps(findings, ensure_ascii=False))

    def logdir(self, name):
        return self.root / 'logs' / (name + '-' + uuid.uuid4().hex[:12])

    def context(self, role):
        decision = self.root / 'decision.md'
        return (make_policy_prompt(self.cfg['nexus']['nexus_hosts'], role) + '\n'
            + 'Exact immutable input packet: ' + str(self.root / 'input') + '\n'
            + 'Requirements file: ' + self.cfg['requirements_file'] + '\n'
            + 'Requirements SHA256: ' + self.cfg['requirements_sha256'] + '\n'
            + 'Read the existing SDD instructions in the current contour and obey their stages/gates. '
              'Use installed SDD skills/agents. Preserve existing groomed task boundaries. '
              'Never commit, switch branches, push, merge, start persistent servers or touch another checkout. '
              'Do not edit permission settings, hooks or input packet. Shell/checks must use Nexus only. '
              'This workspace is an isolated copy of existing PoC work. Preserve that work.\n'
            + ('Analyst clarification (cannot change pinned scope):\n' + decision.read_text() if decision.exists() else ''))

    def session(self, root, cwd, prompt, role, name):
        self.guard()
        prepare_permissions(under(root, cwd), role)
        before = snapshot(root)
        controls = git_controls(root)
        result = run_session(self.cfg['cli'], under(root, cwd), self.context(role) + '\n' + prompt,
                             self.logdir(name), approval_mode='plan' if role == 'review' else self.cfg['approval_mode'],
                             timeout_seconds=self.cfg['session_timeout'], env=self.env, stop_event=self.stop)
        self.guard()
        after = snapshot(root)
        if git_controls(root) != controls:
            raise Blocked('Agent changed Git configuration or hooks')
        if after['head'] != before['head']:
            raise Blocked('Agent changed HEAD')
        for path in before['files'].keys() | after['files'].keys():
            if any(x in Path(path).parts for x in ('.gigacode', 'AGENTS.md', 'GIGACODE.md')):
                if before['files'].get(path) != after['files'].get(path):
                    raise Blocked('Agent changed SDD context/permissions: ' + path)
        if role == 'review' and after != before:
            raise Blocked('Read-only session modified its workspace')
        if result['cancelled']:
            raise Blocked('Interrupted; checkpoint preserved')
        if result['permission_denials']:
            raise Blocked('Permission preflight/runtime denial; inspect ' + result['events_path'])
        return result

    def workspace_preflight(self, root, cwd, role):
        # A successful probe demonstrates installed tool capabilities only.
        # Trust of future worker/reviewer folders is checked by their real calls;
        # do not double every LLM session with a second full preflight.
        key = (str(Path(root).resolve()), cwd, role)
        if key in self._probed:
            return
        prepare_permissions(under(root, cwd), role)
        self.audit(root)
        before = snapshot(root)
        target = under(root, cwd) / ('.sdd-permission-probe-' + uuid.uuid4().hex)
        prompt = ('PERMISSION_PREFLIGHT\nRead existing SDD instructions. Invoke a read tool, a shell tool '
                  '(pwd is sufficient), and a synchronous subagent; await its return. '
                  'Do not perform implementation or downloads. ')
        aliases = {'read': {'read', 'read_file'}, 'shell': {'bash', 'run_shell_command'},
                   'agent': {'agent', 'create_sub_session'}}
        if cwd != '.':
            prompt += 'Invoke an installed SDD skill in read-only/help mode. '
            aliases['skill'] = {'skill'}
        if role == 'execute':
            prompt += 'Use the write/edit tool to create ' + str(target) + ' containing exactly SDD_PROBE_OK. '
            aliases['write'] = {'write', 'edit', 'write_file', 'replace'}
        else:
            prompt += 'Read-only. Do not write files. '
        result = self.session(root, cwd, prompt, role, 'preflight')
        returned = {result['tool_calls'].get(x['tool_use_id'], '').lower() for x in result['tool_results']
                    if x['is_error'] is not True and x['has_content']}
        missing = [kind for kind, names in aliases.items() if not returned & names]
        if role == 'execute':
            if target.is_symlink() or not target.is_file() or target.read_text().strip() != 'SDD_PROBE_OK':
                missing.append('write-evidence')
            else:
                target.unlink()
        if snapshot(root) != before:
            raise Blocked('Preflight changed files beyond its disposable probe; inspect ' + str(root))
        report = {'root': str(root), 'cwd': cwd, 'role': role, 'result': result, 'missing': missing}
        save(self.logdir('preflight-report').with_suffix('.json'), report)
        with self._report_lock:
            self._preflight_reports.append(report)
            save(self.root / 'preflight.json', self._preflight_reports)
        if not result['ok'] or missing:
            raise Blocked('Preflight failed in probe folder; inspect ' + result['events_path'])
        self._probed.add(key)

    def preflight(self):
        def probe_one(cwd, role):
            probe = self.root / 'probes' / uuid.uuid4().hex
            clone_snapshot(self.main, probe)
            self.workspace_preflight(probe, cwd, role)
        with ThreadPoolExecutor(max_workers=self.cfg['jobs']) as pool:
            active = {pool.submit(probe_one, cwd, role)
                      for cwd in self.cfg['contours'] for role in ('execute', 'review')}
            try:
                while active:
                    completed, active = wait(active, return_when=FIRST_COMPLETED)
                    for future in completed:
                        future.result()
            except BaseException:
                self.stop.set()
                for future in active:
                    future.cancel()
                raise
        self.state['preflight'] = {'config_sha256': digest(self.config_path), 'passed': True}
        self.persist()

    def require_preflight(self):
        if self.state.get('preflight') != {'config_sha256': digest(self.config_path), 'passed': True}:
            raise Blocked('Run preflight successfully for this exact configuration first')

    def plan(self):
        self.require_preflight()
        if self.state.get('tasks'):
            raise Blocked('Existing plan is immutable; resume run instead')
        prompt = '''PLAN_SDD_WORK
Read ALL pinned requirements and existing backend/frontend SDD contexts and unfinished PoC.
Produce an executable DAG preserving groomed tasks if present. SDD design/tasks preparation must be
explicit prerequisite tasks using the existing framework; do not silently skip required approvals.
Maximize independent tasks. Declare exact repository-relative write prefixes. Shared ports, databases,
fixture stores and other mutable resources must have matching resources labels to serialize access.
Do not run tests or write files. Return ONLY JSON with this structure:
{"tasks":[{"id":"safe-id","cwd":"backend","writes":["backend/src","backend/openspec"],
"depends_on":[],"resources":[],"prompt":"specific work and SDD stages",
"requirements":["REQ-..."],"checks":[{"cwd":"backend","argv":["command","arg"],"timeout":600}]}],
"integration_checks":[{"cwd":".","argv":["command","arg"],"timeout":600}]}
Checks must exercise behavior, not just echo/compile. Use actual existing commands. No public downloads.
Do not invent blockers from unrelated legacy IT failures, but do not suppress relevant failing tests.
Every requirement must have an owner. Cross-contour work depends on contract/SDD prerequisites.
'''
        feedback = self.state.get('plan_feedback', '')
        previous = None
        repeats = 0
        while not self.stop.is_set():
            planner = self.root / 'planners' / uuid.uuid4().hex
            clone_snapshot(self.main, planner)
            result = self.session(planner, '.', prompt + '\nCorrect previous review findings:\n' + feedback,
                                  'review', 'plan')
            if not result['ok']:
                feedback = json.dumps({'errors': result['errors']})
            else:
                try:
                    plan = validate_plan(parse_json(result['text']), self.main, self.cfg['requirements'])
                except (Blocked, ValueError, TypeError, AttributeError) as error:
                    feedback = 'Correct invalid plan: ' + str(error)
                else:
                    verdict = self.review(planner, '.', 'Review proposed plan, coverage, SDD prerequisites '
                        'and executable checks against the complete input. A pass here validates the plan, '
                        'not completed implementation.\n' + json.dumps(plan, ensure_ascii=False),
                        self.cfg['requirements'], 'plan-review')
                    if verdict['verdict'] == 'pass':
                        save(self.root / 'plan.json', plan)
                        self.state['plan_sha256'] = digest(self.root / 'plan.json')
                        self.state['tasks'] = {t['id']: {'status': 'pending', 'attempts': 0} for t in plan['tasks']}
                        self.state['status'] = 'planned'
                        self.persist()
                        return
                    save(self.root / 'plan-rejected.json', {'plan': plan, 'review': verdict})
                    feedback = json.dumps(verdict, ensure_ascii=False)
                    if verdict['verdict'] == 'blocked':
                        self.state['plan_feedback'] = feedback
                        self.persist()
                        raise Blocked('Plan needs external input: ' + feedback)
            self.state['plan_feedback'] = feedback
            self.persist()
            repeats = repeats + 1 if feedback == previous else 0
            previous = feedback
            if repeats >= 2:
                raise Blocked('Planner repeatedly produced the same failure: ' + feedback)
        raise Blocked('Interrupted while planning; feedback preserved')

    def review(self, root, cwd, detail, requirements, name):
        prompt = ('INDEPENDENT_REVIEW\n' + detail + '\nInspect actual code/specs and evidence. '
                  'Do not rely on executor claims. Do not write files or execute mutating tests. '
                  'Return ONLY JSON {"verdict":"pass|changes|blocked","findings":["concrete issue"],'
                  '"coverage":{"REQ-id":"specific file/test/evidence or gap"}}. '
                  + ('pass requires a complete feasible plan for every assigned requirement with SDD stages/checks. '
                     if name == 'plan-review' else 'pass requires every assigned requirement satisfied and SDD followed. ')
                  +
                  'blocked is only for external dependency or analyst business decision. '
                  'Required identifiers: ' + json.dumps(requirements))
        result = self.session(root, cwd, prompt, 'review', name)
        if not result['ok']:
            raise Blocked('Reviewer failed: ' + result['events_path'])
        report = parse_json(result['text'])
        if report.get('verdict') not in ('pass', 'changes', 'blocked') or not isinstance(report.get('findings'), list):
            raise Blocked('Malformed reviewer verdict')
        coverage = report.get('coverage')
        if not isinstance(coverage, dict) or not set(requirements) <= coverage.keys() or any(not isinstance(coverage[r], str) or not coverage[r].strip() for r in requirements):
            raise Blocked('Reviewer omitted requirement evidence')
        if report['verdict'] == 'pass' and report['findings']:
            raise Blocked('Reviewer passed unresolved findings')
        if report['verdict'] != 'pass' and not report['findings']:
            raise Blocked('Reviewer rejected without actionable findings')
        return report

    def checks(self, root, checks, name):
        reports = []
        for check in checks:
            self.guard()
            before = snapshot(root)
            controls = git_controls(root)
            folder = self.logdir(name)
            folder.mkdir(parents=True, mode=0o700)
            with (folder / 'output.log').open('wb') as output:
                os.chmod(folder / 'output.log', 0o600)
                proc = subprocess.Popen(check['argv'], cwd=under(root, check.get('cwd', '.')),
                                        env=self.env, stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + check.get('timeout', 600)
                timed_out = False
                try:
                    while proc.poll() is None and not self.stop.is_set():
                        if time.monotonic() >= deadline:
                            timed_out = True
                            break
                        time.sleep(.05)
                finally:
                    _stop_group(proc)
                    proc.wait()
            reports.append({'check': check, 'returncode': proc.returncode, 'timed_out': timed_out,
                            'ok': proc.returncode == 0 and not timed_out and not self.stop.is_set(),
                            'log': str(folder / 'output.log')})
            self.guard()
            after = snapshot(root)
            if git_controls(root) != controls:
                raise Blocked('Check changed Git configuration or hooks')
            if after['head'] != before['head'] or protected_context(after) != protected_context(before):
                raise Blocked('Check changed HEAD or SDD context/permissions')
            if self.stop.is_set():
                raise Blocked('Interrupted during checks')
        return reports

    def worker(self, task, record):
        work = Path(record['workspace'])
        base = record['base']
        previous = record.get('failure_fingerprint')
        repeats = record.get('failure_repeats', 0)
        feedback = record.get('feedback', '')
        while not self.stop.is_set():
            record['attempts'] += 1
            save(work.parent / (work.name + '-checkpoint.json'), record)
            result = self.session(work, task['cwd'], 'EXECUTE_TASK\n' + json.dumps(task, ensure_ascii=False)
                                  + '\nPrevious review/check findings:\n' + feedback,
                                  'execute', task['id'] + '-execute')
            self.audit(work)
            patch = work.parent / (work.name + '.patch')
            exported = export_patch(work, base, task['writes'], patch)
            if not result['ok']:
                feedback = json.dumps({'session_errors': result['errors'], 'log': result['events_path']})
            else:
                checks = self.checks(work, task['checks'], task['id'] + '-check')
                # Checks may generate artifacts: enforce ownership again before review.
                self.audit(work)
                exported = export_patch(work, base, task['writes'], patch)
                if not all(c['ok'] for c in checks):
                    feedback = json.dumps(checks)
                else:
                    review_copy = self.root / 'reviews' / uuid.uuid4().hex
                    clone_snapshot(work, review_copy, include_dirty=True)
                    review = self.review(review_copy, task['cwd'], 'Task:\n' + json.dumps(task, ensure_ascii=False)
                                         + '\nActual controller checks:\n' + json.dumps(checks),
                                         task['requirements'], task['id'] + '-review')
                    save(work.parent / (work.name + '-review.json'), review)
                    if review['verdict'] == 'pass':
                        record.update(status='accepted', patch=exported, checks=checks, review=review)
                        save(work.parent / (work.name + '-checkpoint.json'), record)
                        return record
                    if review['verdict'] == 'blocked':
                        raise Blocked(json.dumps(review, ensure_ascii=False))
                    feedback = json.dumps(review, ensure_ascii=False)
            try:
                stable = json.dumps(stable_feedback(json.loads(feedback)), sort_keys=True)
            except ValueError:
                stable = feedback
            fingerprint = hashlib.sha256((exported['sha256'] + stable).encode()).hexdigest()
            repeats = repeats + 1 if fingerprint == previous else 0
            previous = fingerprint
            record.update(feedback=feedback, failure_fingerprint=fingerprint, failure_repeats=repeats)
            save(work.parent / (work.name + '-checkpoint.json'), record)
            if repeats >= 2:
                raise Blocked('Repeated identical failure without code progress; findings: ' + feedback)
        raise Blocked('Interrupted; worker changes preserved')

    def checkpoint(self, record):
        if 'workspace' not in record:
            return record
        work = Path(record['workspace'])
        path = work.parent / (work.name + '-checkpoint.json')
        if path.exists():
            saved = read(path)
            if saved.get('workspace') != record['workspace'] or saved.get('base') != record['base']:
                raise Blocked('Worker checkpoint belongs to another workspace')
            return {**record, **saved}
        return record

    def integrate(self, task, record):
        """Journal apply+commit so an interruption never requires discarding WIP."""
        self.guard()
        patch = Path(record['patch']['patch'])
        if not patch.is_file() or digest(patch) != record['patch']['sha256']:
            raise Blocked('Accepted patch changed before integration')
        before = record.get('integration_base')
        current = git(self.main, 'rev-parse', 'HEAD')
        if before is None:
            if snapshot(self.main)['status']:
                raise Blocked('Integration workspace contains unrelated changes')
            if current != self.state.get('main_head', self.cfg['baseline']):
                raise Blocked('Integration HEAD changed outside the controller')
            record.update(status='accepted', integration_base=current)
            self.persist()  # accepted result is already attached to state
            before = current
        if current == before:
            if snapshot(self.main)['status']:
                recovery = self.root / 'recovery' / (task['id'] + '.patch')
                actual = export_patch(self.main, before, task['writes'], recovery)
                if actual['sha256'] != record['patch']['sha256']:
                    raise Blocked('Interrupted integration differs from the accepted patch; preserved for inspection')
            else:
                apply_patch(self.main, patch)
            head = commit_changes(self.main, self.cfg['commit_prefix'] + ' SDD ' + task['id'], env=self.env)
        else:
            # A crash may happen after commit succeeds but before state is saved.
            head = current
        if snapshot(self.main)['status']:
            raise Blocked('Committed integration also has unrelated dirty changes')
        delta = subprocess.run(['git', '-C', str(self.main), 'diff', '--binary', '--full-index',
            '--no-ext-diff', '--no-renames', before, head, '--'], check=True, stdout=subprocess.PIPE).stdout
        if hashlib.sha256(delta).hexdigest() != record['patch']['sha256']:
            raise Blocked('Integration commit does not contain the accepted patch exactly; hook changes preserved')
        record.update(status='done', integrated_head=head)
        self.state['main_head'] = head
        self.persist()
        return record

    def start(self, retry_blocked=False):
        expected = {'config_sha256': digest(self.config_path), 'passed': True}
        if self.state.get('preflight') != expected:
            self.preflight()
        if not self.state.get('tasks'):
            self.plan()
        self.run(retry_blocked)

    def run(self, retry_blocked=False):
        self.require_preflight()
        plan_path = self.root / 'plan.json'
        if not plan_path.exists() or digest(plan_path) != self.state.get('plan_sha256'):
            raise Blocked('Missing or changed plan; pinned plan is required')
        plan = validate_plan(read(plan_path), self.main, self.cfg['requirements'])
        tasks = plan['tasks']
        for task in tasks:
            record = self.state['tasks'][task['id']]
            if record['status'] in ('running', 'accepted') or (retry_blocked and record['status'] == 'blocked'):
                # Integration journal takes precedence over the last worker checkpoint.
                if 'integration_base' not in record:
                    record = self.checkpoint(record)
                self.state['tasks'][task['id']] = record
                if record['status'] == 'accepted' or 'integration_base' in record:
                    record['status'] = 'accepted'
                    self.integrate(task, record)
                else:
                    record['status'] = 'pending'
        self.state['status'] = 'running'
        self.persist()
        active = {}
        with ThreadPoolExecutor(max_workers=self.cfg['jobs']) as pool:
            while not self.stop.is_set():
                for task in tasks:
                    record = self.state['tasks'][task['id']]
                    if record['status'] != 'pending' or len(active) >= self.cfg['jobs']:
                        continue
                    if any(self.state['tasks'][d]['status'] != 'done' for d in task['depends_on']):
                        continue
                    if any(overlaps(task, other) for other in active.values()):
                        continue
                    # A fresh copy includes all accepted predecessors and previous independent work.
                    # On resume preserve the original worker and baseline instead of overwriting WIP.
                    if 'workspace' not in record:
                        work = self.root / 'workers' / (task['id'] + '-' + uuid.uuid4().hex[:8])
                        clone_snapshot(self.main, work)
                        record.update(workspace=str(work), base=git(work, 'rev-parse', 'HEAD'))
                    record['status'] = 'running'
                    self.persist()
                    active[pool.submit(self.worker, task, record.copy())] = task
                if not active:
                    break
                completed, _ = wait(active, timeout=.2, return_when=FIRST_COMPLETED)
                for future in completed:
                    task = active.pop(future)
                    try:
                        accepted = future.result()
                        self.state['tasks'][task['id']] = accepted
                        self.persist()
                        self.integrate(task, accepted)
                    except Exception as exc:
                        failed = self.state['tasks'][task['id']]
                        if 'integration_base' not in failed:
                            failed = self.checkpoint(failed)
                        failed.update(status='blocked', error=str(exc))
                        self.state['tasks'][task['id']] = failed
                    self.persist()
        if self.stop.is_set():
            self.state['status'] = 'interrupted'
            return
        if any(r['status'] != 'done' for r in self.state['tasks'].values()):
            self.state['status'] = 'blocked'
            self.persist()
            raise Blocked('Some tasks are blocked; inspect status/logs, resolve cause, then run --retry-blocked')
        # Final full integration checks and a fresh review use the same repair loop.
        final = {'id': 'integration', 'cwd': '.', 'writes': sorted({p for t in tasks for p in t['writes']}),
                 'depends_on': [], 'resources': [], 'requirements': self.cfg['requirements'],
                 'checks': plan['integration_checks'],
                 'prompt': 'Verify the entire integrated PoC against every pinned requirement and SDD. '
                           'Fix cross-task integration defects. Preserve all accepted behavior. Do not weaken tests. '
                           'Previous task check/review evidence: ' + json.dumps(self.state['tasks'], ensure_ascii=False)}
        rec = self.state.get('integration')
        if rec and rec.get('status') == 'done':
            if git(self.main, 'rev-parse', 'HEAD') != rec['integrated_head'] or snapshot(self.main)['status']:
                raise Blocked('Completed integration changed after review')
            self.state['status'] = 'complete'
            return
        if rec and rec.get('status') == 'blocked' and not retry_blocked:
            raise Blocked('Integration is blocked; resolve cause and use --retry-blocked')
        if not rec:
            work = self.root / 'workers' / ('integration-' + uuid.uuid4().hex[:8])
            clone_snapshot(self.main, work)
            rec = {'workspace': str(work), 'base': git(work, 'rev-parse', 'HEAD'), 'status': 'running', 'attempts': 0}
        self.state['integration'] = rec
        self.persist()
        try:
            if 'integration_base' not in rec:
                rec = self.checkpoint(rec)
            accepted = rec if rec.get('status') == 'accepted' or 'integration_base' in rec else self.worker(final, rec.copy())
            self.state['integration'] = accepted
            self.persist()
            self.integrate(final, accepted)
            self.state['status'] = 'complete'
        except Exception as exc:
            failed = self.state['integration']
            if 'integration_base' not in failed:
                failed = self.checkpoint(failed)
            failed.update(status='blocked', error=str(exc))
            self.state['integration'] = failed
            self.state['status'] = 'blocked'
            raise
        finally:
            self.persist()

    def export(self, output):
        if self.state.get('status') != 'complete':
            raise Blocked('Only a completed and reviewed run may be exported')
        self.guard()
        if git(self.main, 'rev-parse', 'HEAD') != self.state['integration']['integrated_head'] or snapshot(self.main)['status']:
            raise Blocked('Integrated result changed after review')
        result = subprocess.run(['git', '-C', str(self.main), 'diff', '--binary', '--full-index',
                                 self.cfg['baseline'], 'HEAD', '--'], check=True, stdout=subprocess.PIPE)
        output = Path(output).resolve()
        if output.exists() or any(output.is_relative_to(Path(p)) for p in self.cfg['protected_snapshots']) or output.is_relative_to(self.root):
            raise Blocked('Export destination must be new and outside managed/protected roots')
        output.write_bytes(result.stdout)
        output.chmod(0o600)
        save(str(output) + '.json', {'baseline': self.cfg['baseline'], 'head': git(self.main, 'rev-parse', 'HEAD'),
                                   'sha256': digest(output), 'requirements_sha256': self.cfg['requirements_sha256'],
                                   'source_snapshot': 'Preserved separately; patch applies to captured WIP baseline',
                                   'state_dir': str(self.root)})
        return str(output)


def initialize(args):
    source, state, packet = Path(args.repo).resolve(), Path(args.state_dir).resolve(), Path(args.packet).resolve()
    protected = [source] + [Path(p).resolve() for p in args.protected_root]
    if any(state.is_relative_to(p) or p.is_relative_to(state) for p in protected):
        raise Blocked('State and source/protected roots must be disjoint')
    if state.is_relative_to(packet) or packet.is_relative_to(state):
        raise Blocked('State and input packet directories must be disjoint')
    if not packet.is_dir() or state.exists():
        raise Blocked('Packet must exist and state directory must be new')
    req = under(packet, args.requirements_file)
    if digest(req) != args.requirements_sha256.lower():
        raise Blocked('Requirements hash mismatch')
    requirements = sorted(set(re.findall(r'\bREQ-[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*', req.read_text())))
    if not requirements:
        raise Blocked('No stable REQ identifiers found in requirements')
    if args.jobs < 1 or args.session_timeout <= 0 or not args.commit_prefix.strip():
        raise Blocked('Invalid jobs/timeout/commit prefix')
    nexus = {'nexus_hosts': args.nexus_host}
    if args.npm_registry:
        nexus['npm_registry'] = args.npm_registry
    if args.pip_index_url:
        nexus['pip_index_url'] = args.pip_index_url
    environment = nexus_environment(nexus)
    packet_files = file_manifest(packet)
    protected_snapshots = {str(p): snapshot(p) for p in protected}
    protected_git_controls = {str(p): git_controls(p) for p in protected}
    state.mkdir(parents=True, mode=0o700)
    shutil.copytree(packet, state / 'input')
    if file_manifest(state / 'input') != packet_files:
        raise Blocked('Input changed during copying; preserved run is not initialized')
    clone_report = clone_snapshot(source, state / 'work/main', include_dirty=True)
    baseline = commit_changes(state / 'work/main', args.commit_prefix + ' preserve existing PoC work', allow_input=True, env=environment)
    cfg = {'schema': 1, 'source': str(source), 'state_dir': str(state), 'baseline': baseline,
           'source_head': protected_snapshots[str(source)]['head'], 'clone_report': clone_report,
           'packet_files': packet_files, 'requirements_file': args.requirements_file,
           'requirements_sha256': args.requirements_sha256.lower(), 'requirements': requirements,
           'protected_snapshots': protected_snapshots, 'protected_git_controls': protected_git_controls,
           'nexus': nexus, 'jobs': args.jobs,
           'cli': [args.cli], 'commit_prefix': args.commit_prefix, 'approval_mode': 'auto-edit',
           'session_timeout': args.session_timeout,
           'contours': ['.'] + [p for p in ('backend', 'frontend') if (source / p).is_dir()]}
    save(state / 'config.json', cfg)
    save(state / 'state.json', {'status': 'initialized', 'tasks': {}, 'main_head': baseline})
    return str(state / 'config.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    for flag in ('repo', 'packet', 'requirements-file', 'requirements-sha256', 'state-dir', 'commit-prefix'):
        init.add_argument('--' + flag, required=True)
    init.add_argument('--protected-root', action='append', default=[])
    init.add_argument('--nexus-host', action='append', required=True)
    init.add_argument('--npm-registry')
    init.add_argument('--pip-index-url')
    init.add_argument('--jobs', type=int, default=8)
    init.add_argument('--cli', default='gigacode')
    init.add_argument('--session-timeout', type=int, default=3600)
    for name in ('start', 'preflight', 'plan', 'run', 'status', 'export'):
        p = sub.add_parser(name)
        p.add_argument('--config', required=True)
        if name in ('run', 'start'):
            p.add_argument('--retry-blocked', action='store_true')
        if name == 'export':
            p.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            print(initialize(args))
            return 0
        runner = Runner(args.config)
        if args.command == 'status':
            print(json.dumps(runner.state, ensure_ascii=False, indent=2))
            return 0
        with runner.locked():
            if args.command in ('run', 'start'):
                getattr(runner, args.command)(args.retry_blocked)
            elif args.command == 'export':
                print(runner.export(args.output))
            else:
                getattr(runner, args.command)()
        print(json.dumps({'status': runner.state['status'], 'state_dir': str(runner.root)}, ensure_ascii=False))
        return 0 if not runner.stop.is_set() else 130
    except (Blocked, ValueError, RuntimeError, OSError) as exc:
        print('BLOCKED: ' + str(exc))
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
