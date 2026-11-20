"""Revalidate completed task reviews against their exact candidate and controller receipt."""
import json
from pathlib import Path

from transport import revalidate_saved_result
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
    resolution = value.get('review_resolution') if stage == 'adjudicate' else None
    marker = 'FOCUSED_REVIEW_DISPUTE\n' if resolution else 'INDEPENDENT_REVIEW\n'
    prompt_task = {key: task[key] for key in ('id', 'cwd', 'requirements')} if resolution else task
    if (marker not in text or 'Task:\n' + json.dumps(prompt_task, ensure_ascii=False) not in text
            or checks not in text):
        raise Blocked('Saved review prompt does not bind this task and its checks')
    if resolution:
        import review_resolution
        item = next((item for item in resolution['requests'] if item['id'] == resolution.get('active_id')), None)
        if not item or review_resolution.prompt(task, resolution, item) not in text:
            raise Blocked('Saved dispute decision belongs to different findings or objections')
    elif stage == 'adjudicate':
        issues = {fingerprint(item)[:16]: item for item in value['original_review']['findings']}
        if 'ISSUE_ADJUDICATION' not in text or json.dumps(issues, ensure_ascii=False) not in text:
            raise Blocked('Saved adjudication belongs to different findings')
    try:
        result = revalidate_saved_result(result, events)
    except ValueError as exc:
        raise Blocked(str(exc)) from exc
    if (result.get('cleanup') or {}).get('preserved'):
        raise Blocked('Saved review has unresolved cleanup failure')
    binding['session'] = {key: result.get(key) for key in ('returncode', 'timed_out', 'cancelled', 'terminal_result')}
    binding['disposition'] = session_outcome(result, 'review')
    save(binding_path, binding)
    report = None
    if validate:
        report = (review_resolution.validate(runner, root, task, result, model) if resolution else
                  runner.validate_review_result(root, result, task['requirements'], name, model))
    return root, report
