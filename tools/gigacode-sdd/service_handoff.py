"""Explicit reuse of observed services; never authority to signal a process.

Readiness and experiment evidence remain the task's checks. A reuse decision
only lets a controller work alongside the exact listener the operator reviewed.
"""
from pathlib import Path

import operator_questions as questions

ACCEPT = 'Использовать работающий сервис'
DECLINE = 'Не использовать'


def identity(service):
    if (type(service.get('pid')) is not int or service['pid'] <= 1
            or not service.get('started') or not service.get('cwd')
            or not service.get('command_sha256')
            or service.get('kind') == 'gigacode' or service.get('agent_pids')):
        return None
    if service.get('uid') is None and not (service.get('owner_known') and service.get('same_user')):
        return None
    return {key: service.get(key) for key in
            ('pid', 'started', 'pgid', 'uid', 'kind', 'cwd', 'command_sha256', 'owner_known', 'same_user')} | {
                'ports': sorted(service.get('ports', []))}


def accepted(runner, service):
    from controller import read
    bound = identity(service)
    if not bound:
        return False
    path = runner.root / 'service-handoffs' / (questions.fingerprint(bound) + '.json')
    if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
        return False
    value = read(path)
    question = next((q for q in questions.questions(runner.root)
                     if q['id'] == value.get('question_id') and q['task'] == 'service-handoff'
                     and q['topic'] == 'reuse:' + questions.fingerprint(bound)), None)
    answer = question.get('answer') if question else None
    return value.get('identity') == bound and bool(answer and answer['text'] == ACCEPT)


def request(runner, service):
    from controller import Blocked, read, save
    bound = identity(service)
    if not bound:
        raise Blocked('Existing managed agent or service: недостаточно данных для передачи сервиса; нужны владелец, команда, время старта и рабочий каталог. PID: '
                      + str(service.get('pid')))
    cwd = Path(bound['cwd'])
    if not cwd.is_absolute() or not cwd.resolve().is_relative_to(runner.root.resolve()):
        raise Blocked('Сервис вне каталогов прогона требует отдельного решения о среде')
    token = questions.fingerprint(bound)
    if (runner.root / 'service-handoffs').is_symlink():
        raise Blocked('Service handoff registry must not be a symlink')
    record_path = runner.root / 'service-handoffs' / (token + '.json')
    generation = token
    if record_path.exists():
        if record_path.is_symlink():
            raise Blocked('Service handoff record must not be a symlink')
        previous = read(record_path)
        generation = previous.get('generation', token)
        question = next((q for q in questions.questions(runner.root) if q['id'] == previous.get('question_id')), None)
        if question and question.get('resolved'):
            generation += ':' + questions.fingerprint(question['resolved'])
    key = questions.request(runner.root, 'service-handoff', 'reuse:' + token,
        'Обнаружен работающий сервис ' + str(bound['kind']) + ' (PID ' + str(bound['pid'])
        + ', порты ' + ', '.join(map(str, bound['ports'])) + '). Использовать его в этом прогоне?'
        + '\nКаталог: ' + bound['cwd'] + '\nЗапущен: ' + str(bound['started'])
        + '\nОбвязка сохранит процесс и не будет его останавливать или перезапускать. '
          'Это не подтверждает готовность сервиса или результаты опыта: они проверяются отдельно.',
        options=[ACCEPT, DECLINE], details={'identity': bound, 'lifecycle': 'externally-owned'}, generation=generation)
    save(record_path, {'identity': bound, 'question_id': key, 'generation': generation})
    answer = questions.response(runner.root, key)
    if answer and answer['text'] == ACCEPT:
        return True
    return False


def refresh(runner, inventory):
    """Retire unanswered questions only after the bound process is gone."""
    from controller import read, save
    if not inventory.get('complete') or 'processes' not in inventory:
        return
    fields = ('pid', 'started', 'pgid', 'uid', 'command_sha256')
    live = {tuple(item.get(k) for k in fields) for item in inventory['processes']}
    pending = {q['id'] for q in questions.questions(runner.root, pending_only=True) if q['task'] == 'service-handoff'}
    for path in (runner.root/'service-handoffs').glob('*.json'):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        record = read(path)
        key = record.get('question_id')
        if key in pending and tuple(record['identity'].get(k) for k in fields) not in live:
            with questions.locked(runner.root) as folder:
                if not (folder/(key + '-answer.json')).exists():
                    save(folder/(key + '-resolved.json'), {'reason': 'service-process-no-longer-observed',
                                                        'inventory_at': inventory.get('captured_at')})


def reconcile(runner, services):
    """Record all questions in one pass; do not turn refusal into new prompts."""
    unresolved = [service for service in services if not accepted(runner, service) and not request(runner, service)]
    if unresolved:
        for service in unresolved:
            token = questions.fingerprint(identity(service))
            from controller import read, Blocked
            record = read(runner.root / 'service-handoffs' / (token + '.json'))
            answer = questions.response(runner.root, record['question_id'])
            if answer:
                runner.state['status'] = 'blocked'
                raise Blocked('Использование сервиса не подтверждено ответом оператора. '
                              'Процесс сохранён; продолжение возможно после изменения среды. PID: '
                              + str(service['pid']))
        runner.state['status'] = 'waiting-user'
        raise questions.WaitingForOperator(
            'Нужно решение по работающим сервисам. Откройте отдельный терминал и выполните:\n'
            + questions.command(runner.root)
            + '\nПосле ответа продолжите сохранённый запуск через мастер.')


def context(runner, inventory=None):
    from service_inventory import inspect_services
    if not (runner.root / 'service-handoffs').exists():
        return []
    inventory = inventory if inventory is not None else inspect_services()
    if not inventory.get('complete'):
        return []
    return [{'kind': item['kind'], 'cwd': item['cwd'], 'ports': item.get('ports', []),
             'lifecycle': 'externally-owned; do not stop/restart or assume readiness',
             'readiness': 'not-verified', 'artifacts': 'not-verified'}
            for item in inventory.get('services', []) if accepted(runner, item)]
