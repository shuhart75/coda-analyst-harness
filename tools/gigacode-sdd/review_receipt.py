"""Revalidate completed task reviews against their exact candidate and controller receipt."""
import json
from pathlib import Path

from transport import parse_events
from workspace import snapshot, review_copy_binding
from session_outcome import classify as session_outcome


def load(runner, task, value, events_path, model, *, validate=True):
    from controller import Blocked, digest, file_manifest, read, git_controls, save
    from execution_cycle import _receipt_valid, fingerprint
    stage = value.get('stage')
    name = task['id'] + ('-review' if stage == 'review' else '-adjudicate')
    events = Path(events_path)
    if (stage not in ('review', 'adjudicate') or events.name != 'stdout.jsonl'
            or events.is_symlink() or not events.resolve().is_relative_to((runner.root/'logs').resolve())
            or not events.parent.name.startswith(name + '-')):
        raise Blocked('Saved review does not belong to the current task stage')
    receipt_path = events.parent/'session-receipt.json'
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise Blocked('Saved review has no audited controller receipt')
    receipt = read(receipt_path)
    root = Path(receipt.get('workspace', ''))
    result = receipt.get('result') or {}
    decision = runner.root/'decision.md'
    prompt = events.parent/'prompt.txt'
    if (not root.resolve().is_relative_to((runner.root/'reviews').resolve())
            or receipt.get('model') != model or result.get('events_path') != str(events)
            or receipt.get('events_sha256') != digest(events)
            or receipt.get('workspace_snapshot') != snapshot(root, normalize_settings=True)
            or ('git_controls' in receipt and receipt['git_controls'] != git_controls(root))
            or receipt.get('decision_sha256') != (digest(decision) if decision.is_file() else None)
            or prompt.is_symlink() or not prompt.is_file() or digest(prompt) != receipt.get('prompt_sha256')):
        raise Blocked('Saved review receipt changed or belongs to different inputs')
    _receipt_valid(value)
    current = snapshot(value['workspace'], normalize_settings=True)
    captured = receipt['workspace_snapshot']
    binding = review_copy_binding(value['workspace'], root, current, captured)
    binding['task_matches'] = read(root/'.review-input/task.json') == task
    binding['packet_matches'] = file_manifest(root/'.review-input/packet') == file_manifest(runner.root/'input')
    binding_path = events.parent/'recovery-binding.json'
    save(binding_path, binding)
    if (not binding['head_matches'] or binding['file_difference_count']
            or not binding['task_matches'] or not binding['packet_matches']):
        raise Blocked('Saved review candidate differs from the current task or pinned packet; inspect ' + str(binding_path))
    text = prompt.read_text(encoding='utf-8')
    checks = json.dumps(value['checks'])
    if ('INDEPENDENT_REVIEW\n' not in text or 'Task:\n' + json.dumps(task, ensure_ascii=False) not in text
            or checks not in text):
        raise Blocked('Saved review prompt does not bind this task and its checks')
    if stage == 'adjudicate':
        issues = {fingerprint(item)[:16]: item for item in value['original_review']['findings']}
        if 'ISSUE_ADJUDICATION' not in text or json.dumps(issues, ensure_ascii=False) not in text:
            raise Blocked('Saved adjudication belongs to different findings')
    parsed = parse_events(events)
    if parsed['text'] != result.get('text') or parsed['permission_denials'] != result.get('permission_denials'):
        raise Blocked('Saved review result differs from its original stream')
    if (result.get('cleanup') or {}).get('preserved'):
        raise Blocked('Saved review has unresolved cleanup failure')
    # Integrity is separate from usability. A bound but incomplete review may
    # enter protocol repair, never acceptance. Reparse older receipts to detect
    # transport faults that their older parser did not recognize.
    result = dict(result)
    result['errors'] = list(dict.fromkeys(result.get('errors', []) + parsed['errors']))
    result['ok'] = not result['errors']
    binding['session'] = {key: result.get(key) for key in ('returncode', 'timed_out', 'cancelled', 'terminal_result')}
    binding['disposition'] = session_outcome(result, 'review')
    save(binding_path, binding)
    report = runner.validate_review_result(root, result, task['requirements'], name, model) if validate else None
    return root, report
