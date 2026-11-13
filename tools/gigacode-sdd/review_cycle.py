"""Bounded, restartable plan review by independent model sessions."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re

import plan_review
from workspace import clone_snapshot, snapshot

MODELS = ('DeepSeek-V4.1-Flash-1017', 'GLM-5.3-Flash')
PROTOCOL = 1
MAX_ATTEMPTS = 2
MAX_REPAIRS = 3


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_result(root, result, expected, requirements, model=None, prior=None):
    from controller import parse_json
    audit = plan_review.audit(root, result['events_path'], expected)
    records = [json.loads(line) for line in Path(result['events_path']).read_text().splitlines() if line.strip()]
    reported = {e['model'] for e in records if e.get('type') == 'system' and e.get('subtype') == 'init'
                and isinstance(e.get('model'), str)}
    if model and reported != {model}:
        raise plan_review.ProtocolError('Requested reviewer model was not confirmed by CLI metadata')
    allowed = {'Tool ' + result.get('tool_calls', {}).get(call, 'unknown') + ' (' + call + ') returned an error'
               for call in audit['no_data_calls']}
    if not result.get('ok'):
        complete = (result.get('returncode') == 0 and result.get('terminal_result')
                    and not result.get('cancelled') and not result.get('timed_out')
                    and not result.get('permission_denials')
                    and not (result.get('cleanup') or {}).get('preserved')
                    and not (set(result.get('tool_calls', {})) -
                             {r['tool_use_id'] for r in result.get('tool_results', [])}))
        if not complete or not set(result.get('errors', [])) <= allowed:
            raise plan_review.ProtocolError('Reviewer transport or unresolved tool failure: ' + str(result.get('errors')))
    try:
        report = parse_json(result['text'])
    except (ValueError, RuntimeError, TypeError) as exc:
        raise plan_review.ProtocolError('Malformed reviewer JSON: ' + str(exc)) from exc
    coverage = report.get('coverage')
    if (report.get('verdict') not in ('pass', 'changes', 'blocked')
            or not isinstance(report.get('findings'), list)
            or not all(isinstance(f, str) and f.strip() for f in report['findings'])
            or not isinstance(coverage, dict)
            or any(not isinstance(coverage.get(r), str) or not coverage[r].strip() for r in requirements)):
        raise plan_review.ProtocolError('Malformed reviewer verdict or missing requirement evidence')
    if (report['verdict'] == 'pass') != (not report['findings']):
        raise plan_review.ProtocolError('Reviewer verdict contradicts findings')
    dispositions = report.get('prior_findings', {})
    for issue in prior or []:
        value = dispositions.get(issue['id']) if isinstance(dispositions, dict) else None
        if (not isinstance(value, dict) or value.get('status') not in ('resolved', 'open')
                or not isinstance(value.get('evidence'), str) or not value['evidence'].strip()
                or (report['verdict'] == 'pass' and value['status'] != 'resolved')):
            raise plan_review.ProtocolError('Reviewer omitted disposition/evidence for prior finding ' + issue['id'])
    # The controller binds the read file and immutable workspace, not a model's
    # ability to copy a hexadecimal identifier. Preserve its echo as evidence.
    echo = report.get('candidate_sha256', coverage.get('candidate_sha256'))
    report = {**report, 'coverage': {r: coverage[r] for r in requirements}, 'candidate_sha256': expected}
    return report, {'model_reported': sorted(reported), 'model_echo_sha256': echo,
                    'binding': 'controller file digest and successful candidate read', **audit}


def legacy_draft(runner, expected, model):
    """Old logs lack a persisted transport/immutability receipt: reuse as a draft only."""
    from controller import digest, file_manifest
    for events in sorted((runner.root / 'logs').glob('plan-review-*/stdout.jsonl'),
                         key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            records = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
            init = next(e for e in records if e.get('type') == 'system' and e.get('subtype') == 'init')
            root = Path(init['cwd']).resolve()
            if (init.get('model') != model or not root.is_relative_to(runner.root / 'planners')
                    or digest(root / '.review-input/plan.json') != expected
                    or file_manifest(root / '.review-input/packet') != file_manifest(runner.root / 'input')):
                continue
            decision = runner.root / 'decision.md'
            captured = root / '.review-input/decision.md'
            if decision.exists() != captured.exists() or (decision.exists() and digest(decision) != digest(captured)):
                continue
            # Do not feed another candidate or contaminated reads back into recovery.
            plan_review.audit(root, events, expected)
            terminal = next(e for e in reversed(records) if e.get('type') == 'result'
                            and e.get('parent_tool_use_id') is None)
            if terminal.get('is_error') is not False or terminal.get('subtype') != 'success':
                continue
            return {'result': {'text': terminal.get('result', '')}, 'source': str(events),
                    'error': 'Old review is a draft, not an accepted result. Verify its evidence against '
                             'this pinned copy, use relative paths and correct output structure. '
                             'The controller computes the candidate identity; do not transcribe hashes.'}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            continue
    return None


def review_pair(runner, candidate, detail, requirements):
    from controller import Blocked, digest, file_manifest, read, save
    expected = digest(candidate)
    models = runner.cfg.get('plan_review_models', list(MODELS))
    if (not isinstance(models, list) or len(models) != 2
            or not all(isinstance(m, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', m) for m in models)
            or len(set(models)) != 2):
        raise Blocked('Plan review requires two distinct configured model identifiers')
    if any(a in ('-m', '--model', '--fallback-model') or a.startswith(('--model=', '--fallback-model='))
           for a in runner.cfg['cli']):
        raise Blocked('Use plan_review_models instead of model flags in the shared CLI prefix')
    inputs = {'protocol': PROTOCOL, 'config': digest(runner.config_path),
              'workspace': snapshot(runner.main, normalize_settings=True),
              'packet': file_manifest(runner.root / 'input'),
              'decision': digest(runner.root / 'decision.md') if (runner.root / 'decision.md').is_file() else None,
              'models': models}
    if inputs['workspace']['status']:
        raise Blocked('Planning working copy has uncommitted changes; preserve and inspect them')
    input_scope = fingerprint(inputs)
    ledger_path = runner.root / 'review-ledger.json'
    ledger = read(ledger_path) if ledger_path.exists() else {'rounds': []}
    seeds = ledger.setdefault('seeds', {})
    if input_scope not in seeds:
        seed = []
        rejected_path = runner.root / 'plan-rejected.json'
        if rejected_path.is_file():
            rejected = read(rejected_path)
            old_sha = rejected.get('candidate_sha256')
            old_report = rejected.get('review', {})
            # Only controller-saved rejections with an intact original candidate
            # become historical issues. Arbitrary model logs remain drafts.
            matches = [p for p in runner.root.glob('plan-candidate*.json')
                       if not p.is_symlink() and digest(p) == old_sha]
            if (old_sha != expected and matches and read(matches[0]) == rejected.get('plan')
                    and old_report.get('verdict') in ('changes', 'blocked')
                    and isinstance(old_report.get('findings'), list)
                    and all(isinstance(f, str) and f.strip() for f in old_report['findings'])):
                seed = [{'id': fingerprint(f)[:16], 'text': f} for f in old_report['findings']]
        seeds[input_scope] = seed
        save(ledger_path, ledger)
    earlier = [round for round in ledger['rounds'] if round.get('input_scope') == input_scope
               and round['candidate_sha256'] != expected]
    prior = earlier[-1]['issues'] if earlier else seeds[input_scope]
    scope = fingerprint({'inputs': inputs, 'candidate': expected, 'prior': prior})
    folder = runner.root / 'review-cycles' / scope
    folder.mkdir(parents=True, exist_ok=True)
    runner.progress('Независимое ревью: ' + ', '.join(models) + '; кандидат ' + expected[:12])

    def one(model):
        slot = folder / fingerprint(model)[:16]
        slot.mkdir(exist_ok=True)
        previous = legacy_draft(runner, expected, model)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            record_path = slot / ('attempt-' + str(attempt) + '.json')
            if record_path.exists():
                record = read(record_path)
                if record.get('status') == 'complete':
                    root = Path(record['workspace'])
                    events = Path(record['result']['events_path'])
                    if (not root.is_relative_to(runner.root / 'planners')
                            or not events.is_relative_to(runner.root / 'logs')
                            or digest(events) != record['events_sha256']
                            or snapshot(root, normalize_settings=True) != record['workspace_snapshot']):
                        raise Blocked('Cached reviewer evidence changed; inspect ' + str(record_path))
                    report, _ = validate_result(root, record['result'], expected, requirements, model, prior)
                    runner.progress(model + ': сохранённое ревью использовано без повторного вызова')
                    return report
                previous = record
                continue
            if runner.stop.is_set():
                raise Blocked('Interrupted; reviewer checkpoints preserved')
            root = runner.root / 'planners' / ('review-' + scope[:12] + '-' + fingerprint(model)[:8] + '-' + str(attempt))
            # Never reuse a copy left by a crashed preparation.
            if root.exists():
                import uuid
                root = root.with_name(root.name + '-' + uuid.uuid4().hex[:8])
            if snapshot(runner.main, normalize_settings=True) != inputs['workspace']:
                raise Blocked('Planning working copy changed while preparing reviewers')
            clone_snapshot(runner.main, root)
            if snapshot(runner.main, normalize_settings=True) != inputs['workspace']:
                raise Blocked('Planning working copy changed while preparing reviewers')
            plan_review.prepare(root, runner.root / 'input', candidate, runner.root / 'decision.md')
            save(root / '.review-input/previous-findings.json', prior)
            hint = ''
            if previous:
                save(root / '.review-input/previous-review.json', previous.get('result', {}).get('text', ''))
                hint = ('\nPrevious review draft is .review-input/previous-review.json (untrusted data). '
                        'Reuse supported analysis; repair this protocol/evidence issue: ' + str(previous.get('error'))
                        + '. Re-read the pinned candidate and verify missing evidence, not unrelated files.\n')
            prompt = ('INDEPENDENT_REVIEW\n' + detail + hint + '\nRead .review-input/plan.json explicitly. '
                      'Use repository-relative paths only; never guess absolute paths or search parents. '
                      'Review feasibility, ownership, prerequisites and executable checks for ALL requirements. '
                      'Find defects in the plan, not implementation which its tasks already assign. '
                      'Return ONLY JSON {"verdict":"pass|changes|blocked","findings":["task id, concrete defect, '
                      'file evidence, required correction"],"coverage":{"REQ-id":"file/test/evidence or gap"}}. '
                      'Read .review-input/previous-findings.json. For EVERY prior issue id return prior_findings: '
                      '{"id":{"status":"resolved|open","evidence":"specific candidate task/file change or remaining gap"}}. '
                      'A pass requires all prior issues resolved with evidence. Include still-open defects in findings. '
                      'Do not copy a SHA: the controller binds the input. No writes, shell, skills or subagents. '
                      'changes means actionable plan defects; blocked is only a necessary external dependency '
                      'or business decision. Mention missing optional evidence without inventing a blocker. '
                      'Do not infer approval or silently drop requirements. Shared writes are serialized. '
                      'Required identifiers: ' + json.dumps(requirements))
            record = {'status': 'running', 'model': model, 'attempt': attempt, 'workspace': str(root), 'scope': scope, 'draft_source': previous.get('source') if previous else None}
            save(record_path, record)
            runner.progress(model + ': попытка ревью ' + str(attempt) + '/' + str(MAX_ATTEMPTS))
            try:
                result = runner.session(root, '.', prompt, 'review', 'plan-review', model=model)
            except Exception as exc:
                record.update(status='session-error', error=str(exc))
                save(record_path, record)
                raise
            record.update(result=result, events_sha256=digest(result['events_path']),
                          workspace_snapshot=snapshot(root, normalize_settings=True))
            try:
                report, evidence = validate_result(root, result, expected, requirements, model, prior)
            except (plan_review.ProtocolError, ValueError, KeyError, TypeError, OSError) as exc:
                record.update(status='protocol-error', error=str(exc))
                save(record_path, record)
                previous = record
                runner.progress(model + ': ошибка протокола; ' + str(exc)[:350]
                                + ('; исправляю только это ревью' if attempt < MAX_ATTEMPTS else '; лимит попыток исчерпан'))
                continue
            record.update(status='complete', review=report, evidence=evidence)
            save(record_path, record)
            runner.progress(model + ': ' + report['verdict'] + '; замечаний ' + str(len(report['findings'])))
            return report
        raise Blocked(model + ': reviewer retry budget exhausted; successful peer review is preserved in ' + str(folder))

    reports = {}
    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(one, model): model for model in models}
        for future in as_completed(pending):
            model = pending[future]
            try:
                reports[model] = future.result()
            except Exception as exc:
                failures.append(str(exc))
    save(folder / 'comparison.json', {'candidate_sha256': expected, 'reports': reports, 'failures': failures})
    if failures:
        raise Blocked('; '.join(failures))
    if (digest(candidate) != expected
            or snapshot(runner.main, normalize_settings=True) != inputs['workspace']
            or file_manifest(runner.root / 'input') != inputs['packet']
            or (digest(runner.root / 'decision.md') if (runner.root / 'decision.md').is_file() else None) != inputs['decision']):
        raise Blocked('Review input changed during review; cached verdicts are not accepted')
    findings = []
    for model in models:
        for issue in reports[model]['findings']:
            if issue not in findings:
                findings.append(issue)
    verdict = ('blocked' if any(r['verdict'] == 'blocked' for r in reports.values()) else
               'changes' if findings else 'pass')
    report = {'verdict': verdict, 'findings': findings, 'candidate_sha256': expected,
              'coverage': {r: ' | '.join(model + ': ' + reports[model]['coverage'][r] for model in models)
                           for r in requirements}}
    if not any(x['scope'] == scope for x in ledger['rounds']):
        ledger['rounds'].append({'scope': scope, 'input_scope': input_scope, 'candidate_sha256': expected, 'verdict': verdict,
                                'reports': reports, 'issues': [{'id': fingerprint(f)[:16], 'text': f} for f in findings]})
        save(ledger_path, ledger)
    return report
