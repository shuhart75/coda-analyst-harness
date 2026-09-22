from __future__ import annotations

from datetime import date


DECISIONS = ("accept-dates", "accept-dates-and-statuses", "keep-current", "custom")


def qa_start_from_current(rows: list[dict], missing_keys: list[str]) -> dict:
    finishes, unresolved = [], list(missing_keys)
    for row in rows:
        if row['role'] not in {'BE', 'FE'}:
            continue
        facts = row['current']
        status = facts.get('Status', '').strip().casefold()
        if status in {'cancelled', 'canceled', 'superseded'}:
            continue
        finish = facts.get('Actual Finish', '').strip()
        if finish and finish not in {'-', '—'}:
            try:
                finishes.append((date.fromisoformat(finish), row['task_id']))
            except ValueError:
                unresolved.append(row['task_id'])
        elif status in {'done', 'completed'}:
            unresolved.append(row['task_id'])
    first = min(finishes) if finishes else None
    return {'started_on': first[0].isoformat() if first and not unresolved else None,
            'earliest_known_finish': first[0].isoformat() if first else None,
            'source_task': first[1] if first else None, 'unresolved': sorted(set(unresolved)),
            'basis': 'current-execution-actual-finish', 'requires_review': True}


def build_comparison(preview: dict, reviews: list[dict], provider: str) -> dict:
    rows = []
    for review in reviews:
        feature = review["feature"]
        for item in preview["items"]:
            for target in item["targets"]:
                if target["feature"] != feature or target["role"] not in {"BE", "FE"}:
                    continue
                identity = f"{item.get(provider + '_key')}/{target['role']}"
                calculation = review["tasks"].get(identity)
                rows.append({"feature": feature, "task_id": target["task_id"], "role": target["role"],
                             "registry": target["registry"], "current": target["saved_facts"],
                             "registration_required": target.get("registration_required", False),
                             "role_estimates": item.get("role_estimates", {}),
                             "history": calculation["development"] if calculation else None,
                             "limitations": calculation["limitations"] if calculation else ["history-not-collected"]})
        proposal = next(item for item in preview["feature_qa_proposals"] if item["feature"] == feature)
        qa_targets = proposal["existing_targets"] or [{"task_id": "QA (new)", "registry": None, "saved_facts": {}}]
        if "qa_groups" in review:
            existing = {target["task_id"]: target for target in proposal["existing_targets"]}
            qa_targets = [{**existing.get(group["task_id"], {"saved_facts": {}}),
                           "task_id": group["task_id"], "registry": group["registry"], "group": group}
                          for group in review["qa_groups"]]
        for target in qa_targets:
            group = target.get("group")
            rows.append({"feature": feature, "task_id": target["task_id"], "role": "QA",
                         "registry": target["registry"], "current": target["saved_facts"],
                         "history": group["qa"] if group else review["qa"],
                         "members": group["members"] if group else None,
                         "role_estimates": {"QA": {"value": group["estimate"] if group else proposal.get("estimate", {}).get("value")}},
                         "limitations": group["limitations"] if group else review["limitations"]})

    def cell(value):
        return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")

    lines = ["| Фича / задача | Текущие начало / конец / статус | По истории: начало / конец / состояние | Границы и ограничения | FE | BE | QA |",
             "|---|---|---|---|---:|---:|---:|"]
    for row in rows:
        current, history = row["current"], row["history"] or {}
        previous = " / ".join(cell(current.get(name)) for name in ("Actual Start", "Actual Finish", "Status"))
        previous += f"; прогресс {cell(current.get('Progress %'))}; завершено к {cell(current.get('Completed By'))}"
        proposed = " / ".join(cell(history.get(name)) for name in ("started_at", "finished_at", "state"))
        bounds = f"начато не позднее {cell(history.get('started_by'))}; завершено не позднее {cell(history.get('completed_by'))}"
        notes = cell("; ".join(row["limitations"]))
        notes += "; прежнее основание: " + cell(current.get("Details") or current.get("Notes"))
        estimates = " | ".join(cell(row.get("role_estimates", {}).get(role, {}).get("value")) for role in ("FE", "BE", "QA"))
        lines.append(f"| {cell(row['feature'])} / {cell(row['task_id'])} | {previous} | {proposed} | {bounds}; {notes} | {estimates} |")
    name = "Jira" if provider == "jira" else "SberTrek"
    choices = [f"Принять сроки {name} для всех задач", f"Принять сроки и статусы {name} для всех задач",
               "Оставить текущие сроки и статусы для всех задач", "Свой вариант"]
    fields = [["Actual Start", "Actual Finish"], ["Actual Start", "Actual Finish", "Status", "Progress %"], [], None]
    current_qa_starts = {
        proposal['feature']: qa_start_from_current(
            [row for row in rows if row['feature'] == proposal['feature']], proposal.get('missing_card_keys', []))
        for proposal in preview['feature_qa_proposals']
    }
    qa_checks = []
    for feature, start in current_qa_starts.items():
        qa_rows = [row for row in rows if row['feature'] == feature and row['role'] == 'QA']
        qa_checks.append({'feature': feature, 'required': True,
                          'fields': ['Actual Start', 'Actual Finish', 'Completed By', 'Status', 'Progress %', 'Estimate'],
                          'start_from_current_execution': start,
                          'start_change_required': bool(start['started_on']) and any(
                              row['current'].get('Actual Start') != start['started_on'] for row in qa_rows),
                          'targets': [{'task_id': row['task_id'], 'registry': row['registry'],
                                       'current': row['current'], 'history': row['history']} for row in qa_rows],
                          'decision_required': True, 'application_verified': False})
        for row, target in zip(qa_rows, qa_checks[-1]['targets']):
            if row.get('members') is not None:
                target['start_from_current_execution'] = qa_start_from_current(
                    [candidate for candidate in rows if candidate['feature'] == feature
                     and candidate['role'] in {'BE', 'FE'} and candidate['task_id'] in row['members']], [])
                target['members'] = row['members']
        if any(row.get('members') is not None for row in qa_rows):
            qa_checks[-1]['start_from_current_execution'] = None
            qa_checks[-1]['start_change_required'] = any(
                target.get('start_from_current_execution', {}).get('started_on')
                and target['current'].get('Actual Start') != target['start_from_current_execution']['started_on']
                for target in qa_checks[-1]['targets'])
            current_qa_starts[feature] = {'basis': 'per-qa-group', 'targets': qa_checks[-1]['targets']}
            lines.append(f"\nQA {cell(feature)}: начало, окончание и прогресс проверяются отдельно для каждой группы; общий интервал не переносить в части.")
            continue
        lines.append(f"\nQA {cell(feature)}: начало по сохранённым фактическим окончаниям разработки — "
                     f"{cell(start['started_on'])}; задача-источник {cell(start['source_task'])}. "
                     f"Неизвестные окончания/неполученные задачи: {cell(', '.join(start['unresolved']))}. "
                     "После выбора новых сроков разработки пересчитать; отдельное подтверждение QA не перезаписывать.")
        if qa_checks[-1]['start_change_required']:
            lines.append("**QA: начало в реестре отличается; совпадение сроков BE/FE не означает отсутствие изменений.**")
    return {"rows": rows, "table": "\n".join(lines),
            "qa_start_from_current_execution": current_qa_starts,
            "required_qa_application_checks": qa_checks,
            "decision_required": True,
            "choices": [{"id": identity, "label": label, "fields": selected}
                        for identity, label, selected in zip(DECISIONS, choices, fields)],
            "scope": "shown-rows-only", "unknown_dates_preserve_current": True,
            "bounds_are_not_exact_dates": True, "analyst_confirmation_requires_explicit_override": True,
            "application_allowed": False}
