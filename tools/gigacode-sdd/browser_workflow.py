"""Delegate reproducible UI checks to execution, retaining independent acceptance."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time

PROTOCOL = 1
INSTRUCTIONS = '''
AUTONOMOUS_BROWSER_CHECKS:
For UI work, discover the installed Playwright MCP tools and inspect the actual running test
application yourself. Use the declared test stand and isolated test records. Browser tool approval
is not permission to alter production data. Do not send the operator your routine test checklist.
If a stand is unavailable, request its normal managed service/prerequisite first. Do not rebuild a
frozen artifact. Respect actual tool denials; never bypass them with shell or unsafe browser code.
Consult tool schemas before calls. Save screenshots inside this candidate's declared evidence paths,
not Downloads or another workspace. For browser_take_screenshot include scale="css", type="png"
and an explicit filename. Close your own browser when finished; do not stop application services.
Record a readable browser-checks.md alongside evidence: actual URL, build/manifest identity and how
it was verified, requirement, preconditions, each action, expected and observed result, screenshot
paths, failures and remaining limitations. A page load, screenshot or CLI exit code alone is not
proof of a scenario. Exercise existing required scenarios with permitted browser actions; preserve
application behavior, API contracts, business wording, and the agreed feature scope.
For UI changes, capture before/after at the normal desktop size and a narrower viewport. Inspect
readability, clipped identifiers/text, table column sizing and horizontal scrolling, primary action
visibility, hierarchy/spacing/contrast, loading/empty/error states and keyboard focus. Fix observed
layout defects within the task scope, then repeat checks. Do not redesign solely on preference or
invent product requirements. Include concrete visual findings and their disposition in the report.
Delegated browser checks are mandatory work, not human answers or waivers. Prior manual fallback
caused by missing tooling does not by itself require another human test now that tools are available.
Human business decisions remain human decisions. If a current attempt fails for a real access or
environment limitation, explain the precise failed operation and evidence in an operator question.
'''
REVIEW = '''
BROWSER_REVIEW: inspect the pinned browser report, scenario results and screenshot artifacts.
Check build identity and requirement coverage, not just tool success. Review visual findings and
before/after evidence for changed UI; if your tools cannot inspect images, state that limitation.
Missing feasible browser proof or fixable UI defects are changes for the executor, not a request
that the operator repeat the tests. Genuine external decisions/access limitations remain blocked.
Do not launch browsers or touch the live stand from the immutable review session.
'''


def enabled(root):
    path = Path(root)/'browser-automation.json'
    if not path.is_file() or path.is_symlink():
        return False
    return json.loads(path.read_text()).get('enabled') is True


def activate(runner):
    from controller import save
    import operator_questions as questions
    path = runner.root/'browser-automation.json'
    desired = runner.cfg.get('browser_automation', True)
    if type(desired) is not bool:
        raise ValueError('browser_automation must be a boolean')
    if not path.exists() or enabled(runner.root) != desired:
        save(path, {'protocol': PROTOCOL, 'enabled': desired})
    if desired:
        for question in questions.questions(runner.root, pending_only=True):
            delegate(runner.root, question)


def verification_context(root, question):
    """Use explicit task/topic lineage, never classify business prose by keywords."""
    if question.get('manual_check'):
        return {'check': question['manual_check'], 'source_question': question['id']}
    import operator_questions as questions
    related = [q for q in questions.questions(root) if q.get('manual_check')
               and all(q.get(k) == question.get(k) for k in ('binding', 'task', 'topic'))
               and q.get('created_at', 0) <= question.get('created_at', 0)]
    if not related:
        return None
    source = max(related, key=lambda q: (q.get('created_at', 0), q['id']))
    return {'check': source['manual_check'], 'source_question': source['id'],
            'followup': question['question'], 'requests': question.get('requests', []),
            'rule': 'Earlier checklist is context, not a current verdict. Read the follow-up and current '
                    'scenario before testing; verify current build/preconditions. Preserve previous '
                    'human observations. Do not claim the old expectations or answers prove this check.'}


def delegate(root, question):
    from controller import save
    import operator_questions as questions
    if not enabled(root) or question.get('answer') or question.get('resolved') or question.get('action'):
        return False
    context = verification_context(root, question)
    if context is None:
        return False
    with questions.locked(root) as folder:
        if any((folder/(question['id']+suffix)).exists() for suffix in ('-answer.json', '-resolved.json')):
            return False
        path = folder/(question['id']+'-automation.json')
        if not path.exists():
            save(path, {'status': 'assigned', 'assigned_at': time.time(),
                        'binding': question['binding'], 'verification': context,
                        'rule': 'Assigned to executor, not accepted or answered'})
        return json.loads(path.read_text()).get('status') == 'assigned'


def assigned(root, key):
    import operator_questions as questions
    if not enabled(root):
        return None
    return next((q for q in questions.questions(root) if q['id'] == key
                 and q.get('automation', {}).get('status') == 'assigned'
                 and not q.get('answer') and not q.get('resolved')), None)


def obligations(root, task=None):
    import operator_questions as questions
    if not enabled(root):
        return []
    return [{'id': q['id'], 'task': q['task'],
             'check': q.get('manual_check') or q['automation']['verification']['check'],
             **({'clarification': q['automation']['verification']}
                if q.get('automation', {}).get('verification', {}).get('followup') else {})}
            for q in questions.questions(root) if q.get('automation', {}).get('status') == 'assigned'
            and not q.get('answer') and not q.get('resolved')
            and (task in (None, 'integration') or q['task'] == task)]


def failed_browser_call(result):
    """Only a recorded failed browser call permits returning a delegated check to a human."""
    calls = result.get('resolved_tool_calls', {})
    if not calls and result.get('events_path'):
        from transport import parse_events
        calls = parse_events(Path(result['events_path'])).get('resolved_tool_calls', {})
    return any(item.get('is_error') is True and calls.get(item.get('tool_use_id'), '').startswith('mcp__playwright__browser_')
               for item in result.get('tool_results', []))


def release(root, key, result):
    from controller import save
    import operator_questions as questions
    if not failed_browser_call(result):
        return False
    with questions.locked(root) as folder:
        path = folder/(key+'-automation.json')
        if not path.is_file():
            return False
        record = json.loads(path.read_text())
        record.update(status='needs-human', events_path=result['events_path'], updated_at=time.time())
        save(path, record)
    from desktop_notifications import notify_question
    notify_question(root, key)
    return True


def resume_candidate(root, task, value):
    checks = obligations(root, task['id'])
    keys = {item['id'] for item in checks}
    wait_keys = set(value.get('acceptance_wait', {}).get('question_ids', []))
    if not keys or not (value.get('question_id') in keys or wait_keys & keys):
        return False
    marker = hashlib.sha256(json.dumps(checks, sort_keys=True).encode()).hexdigest()
    if value.get('browser_assignment') == marker:
        return False
    value['browser_assignment'] = marker
    value.update(status='pending', stage='execute', feedback={'browser_checks': checks,
        'instruction': 'Perform these delegated checks yourself. Preserve accepted work. '+INSTRUCTIONS})
    if value.get('acceptance_wait'):
        value.setdefault('acceptance_wait_history', []).append(value.pop('acceptance_wait'))
    value.pop('recovered_review', None)
    return True


def task_context(root, task):
    checks = obligations(root, task['id'])
    task = {**task, 'browser_checks': checks} if checks else dict(task)
    # Add a runtime reservation, never rewrite the pinned plan. This also serializes
    # competing candidates through the existing ResourceLane.
    paths = [Path(p) for p in task.get('writes', [])]
    ui = any(p.suffix.lower() in ('.tsx', '.jsx', '.html', '.css', '.vue', '.svelte')
             or any(part.lower() in ('frontend', 'ui', 'web') for part in p.parts) for p in paths)
    if enabled(root) and (checks or ui or task.get('id') == 'integration'):
        task['resources'] = sorted(set(task.get('resources', [])) | {'sdd-browser'})
    return task


def captures(work, value):
    """Bind successful navigation/snapshot/screenshot calls to extant candidate PNGs."""
    work = Path(work).resolve()
    found = []
    for name, expected in value.get('evidence_files', {}).items():
        path = Path(name)
        if path.name != 'stdout.jsonl' or not path.is_file() or path.is_symlink():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            continue
        calls, cwd, url, snapshot = {}, None, None, False
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get('parent_tool_use_id') is not None:
                    continue
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    directory = event.get('cwd')
                    cwd = Path(directory).resolve() if isinstance(directory, str) and directory else None
                message = event.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                for item in content if isinstance(content, list) else []:
                    if not isinstance(item, dict):
                        continue
                    if item.get('type') == 'tool_use':
                        tool, args = item.get('name'), item.get('input', {})
                        if tool == 'tool_call' and isinstance(args, dict):
                            tool, args = args.get('name'), args.get('arguments', {})
                        calls[item.get('id')] = (tool, args)
                    if item.get('type') != 'tool_result' or item.get('is_error') is not False:
                        continue
                    tool, args = calls.get(item.get('tool_use_id'), (None, {}))
                    if not isinstance(args, dict) or cwd is None or not cwd.is_relative_to(work):
                        continue
                    if tool == 'mcp__playwright__browser_navigate':
                        url, snapshot = args.get('url'), False
                    if tool == 'mcp__playwright__browser_snapshot':
                        snapshot = True
                    if tool != 'mcp__playwright__browser_take_screenshot' or not url or not snapshot:
                        continue
                    filename = args.get('filename')
                    if not isinstance(filename, str) or not filename:
                        continue
                    image = cwd/filename
                    if image.is_symlink() or not image.resolve().is_relative_to(work) or not image.is_file():
                        continue
                    with image.open('rb') as source:
                        if source.read(8) != b'\x89PNG\r\n\x1a\n':
                            continue
                    found.append({'url': url, 'screenshot': image.resolve().relative_to(work).as_posix(),
                                  'sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'session_sha256': expected})
    return found


def missing_evidence(root, task, work, value):
    checks = obligations(root, task['id'])
    if not checks:
        return []
    captured = {item['url'] for item in captures(work, value)}
    # A follow-up may change route or preconditions. Its historical checklist is
    # context; independent review must check the current scenario and build.
    return [item for item in checks if not captured
            or (not item.get('clarification') and item['check']['url'] not in captured)]


def complete(root, record):
    """Only the controller's accepted final integration can close delegated obligations."""
    from controller import save
    import operator_questions as questions
    pending = obligations(root)
    if not pending:
        return
    if record.get('manual_review_state') != questions.manual_state(root, 'integration'):
        raise ValueError('Browser obligations changed after final review')
    if record.get('review', {}).get('verdict') != 'pass':
        raise ValueError('Independent final acceptance required')
    for item in pending:
        with questions.locked(root) as folder:
            save(folder/(item['id']+'-resolved.json'), {'reason': 'Delegated check accepted by final independent review',
                 'resolved_at': time.time(), 'review': record['review'], 'integrated_head': record.get('integrated_head')})


@contextmanager
def session_lease(runner, task, stop):
    if 'sdd-browser' not in task.get('resources', []) or not enabled(runner.root):
        yield
        return
    from bootstrap import app_root
    from platform_support import FileLock
    from controller import Interrupted
    folder = app_root()/'browser-runtime'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = FileLock(folder/'session.lock')
    acquired = False
    try:
        with runner.activity('Ожидание браузера для ' + task['id'],
                             metadata={'kind': 'resource', 'task_id': task['id'], 'phase': 'ожидание браузера'}):
            while True:
                if stop.is_set():
                    raise Interrupted('Interrupted while waiting for browser')
                try:
                    lock.__enter__()
                    acquired = True
                    break
                except BlockingIOError:
                    stop.wait(.2)
        yield
    finally:
        if acquired:
            lock.__exit__(None, None, None)
