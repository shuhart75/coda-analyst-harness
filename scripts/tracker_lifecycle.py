from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping


ROLES = {"AN", "BE", "FE", "QA"}


@dataclass(frozen=True)
class HistoryEvent:
    event_id: str
    at: datetime
    assignee: tuple[str | None, str | None] | None = None
    status: tuple[str, str] | None = None


@dataclass(frozen=True)
class TaskHistory:
    task_key: str
    development_role: str
    observed_at: datetime
    current_assignee: str | None
    current_status: str
    events: tuple[HistoryEvent, ...] = ()
    complete: bool = False


@dataclass(frozen=True)
class StatusRules:
    not_started: frozenset[str] = field(default_factory=frozenset)
    development_started: frozenset[str] = field(default_factory=frozenset)
    development_completed: frozenset[str] = field(default_factory=frozenset)
    qa_started: frozenset[str] = field(default_factory=frozenset)
    qa_completed: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AnalystCompletion:
    source: str
    confirmed_at: datetime
    analyst_confirmed: bool
    finished_on: date | None = None


def timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("History timestamps require a time and an explicit UTC offset")
    return value


def status_code(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty explicit status code is required")
    return value.strip().casefold()


def status_sets(rules: StatusRules) -> dict[str, set[str]]:
    values = {name: {status_code(code) for code in getattr(rules, name)} for name in rules.__dataclass_fields__}
    if values["not_started"] & set().union(*(codes for name, codes in values.items() if name != "not_started")):
        raise ValueError("Not-started statuses conflict with lifecycle transitions")
    if values["development_started"] & (values["development_completed"] | values["qa_started"] | values["qa_completed"]):
        raise ValueError("Development-start statuses conflict with completion or QA")
    if values["qa_started"] & values["qa_completed"]:
        raise ValueError("QA-start and QA-completion statuses overlap")
    return values


def calculate_task(history: TaskHistory, participants: Mapping[str, str], rules: StatusRules) -> dict:
    if not history.task_key or history.development_role not in {"BE", "FE"}:
        raise ValueError("A task key and a confirmed BE/FE development role are required")
    if type(history.complete) is not bool:
        raise ValueError("History completeness must be explicit")
    if any(not identity or role not in ROLES for identity, role in participants.items()):
        raise ValueError("Participant identities require confirmed AN/BE/FE/QA roles")
    observed = timestamp(history.observed_at)
    codes = status_sets(rules)
    current_status = status_code(history.current_status)
    limitations = set() if history.complete else {"history-incomplete"}
    evidence = []
    development_start = development_finish = development_bound = None
    qa_start = qa_start_bound = qa_finish = None
    development_closed = qa_completed = qa_active = returned = False
    last_assignee = last_status = None
    assignee_seen = status_seen = False
    previous_at = None
    seen = {}

    def role(identity: str | None) -> str | None:
        if identity is not None and identity not in participants:
            limitations.add(f"participant-role-unknown:{identity}")
        return participants.get(identity)

    role(history.current_assignee)
    for event in history.events:
        moment = timestamp(event.at)
        if not event.event_id or (event.assignee is None and event.status is None):
            raise ValueError("An event needs an identity and a field change")
        if event.event_id in seen:
            if event != seen[event.event_id]:
                raise ValueError("Conflicting duplicate history event")
            continue
        seen[event.event_id] = event
        if moment > observed or (previous_at is not None and moment <= previous_at):
            raise ValueError("Events must be ordered, unambiguous and no later than the snapshot")
        previous_at = moment
        old_role = new_role = None
        if event.assignee is not None:
            if len(event.assignee) != 2:
                raise ValueError("An assignment change needs old and new identities")
            old_assignee, new_assignee = event.assignee
            if assignee_seen and old_assignee != last_assignee:
                raise ValueError("Discontinuous assignment history")
            last_assignee, assignee_seen = new_assignee, True
            old_role, new_role = role(old_assignee), role(new_assignee)
            if any(value in {"BE", "FE"} and value != history.development_role for value in (old_role, new_role)):
                raise ValueError("Assignment role conflicts with the confirmed task role")
        old_status = new_status = None
        if event.status is not None:
            if len(event.status) != 2:
                raise ValueError("A status change needs old and new codes")
            old_status, new_status = map(status_code, event.status)
            if old_status == new_status or (status_seen and old_status != last_status):
                raise ValueError("Discontinuous or unchanged status transition")
            last_status, status_seen = new_status, True

        handoff = old_role == history.development_role and new_role == "QA"
        qa_return = old_role == "QA" and new_role == history.development_role
        start_development = (old_role == "AN" and new_role == history.development_role) or new_status in codes["development_started"]
        finish_development = handoff or (
            new_status in codes["development_completed"] | codes["qa_started"]
            and new_status not in codes["qa_completed"]
        )
        start_qa = handoff or new_status in codes["qa_started"]
        finish_qa = new_status in codes["qa_completed"]
        if qa_return and finish_qa:
            raise ValueError("The same event cannot return work to development and complete QA")
        signals = []
        if start_development and not development_closed and development_start is None:
            development_start = moment
            signals.append("development-start")
        if finish_development and not development_closed:
            development_finish = development_bound = moment
            development_closed = True
            signals.append("development-completed")
        if start_qa:
            if qa_start is None and qa_start_bound is None:
                qa_start = qa_start_bound = moment
            qa_active = True
            signals.append("qa-start")
        if qa_return:
            if not development_closed:
                development_closed, development_bound = True, moment
            if qa_start_bound is None:
                qa_start_bound = moment
            qa_active, qa_completed, returned = True, False, True
            qa_finish = None
            signals.append("qa-rework")
        if finish_qa:
            if not qa_completed:
                qa_finish = moment
            qa_completed, qa_active, returned = True, True, False
            if not development_closed:
                development_closed, development_bound = True, moment
            signals.append("qa-completed")
        elif new_status is not None and old_status in codes["qa_completed"]:
            qa_completed, qa_active = False, True
            qa_finish = None
            signals.append("qa-completion-withdrawn")
        if signals:
            evidence.append({"event_id": event.event_id, "at": moment.isoformat(), "signals": signals})

    if assignee_seen and last_assignee != history.current_assignee:
        raise ValueError("Assignment history does not reach the supplied snapshot")
    if status_seen and last_status != current_status:
        raise ValueError("Status history does not reach the supplied snapshot")
    if current_status in codes["qa_completed"] and not returned:
        qa_completed = True
        development_closed = True
        development_bound = development_bound or observed
    elif current_status in codes["development_completed"] | codes["qa_started"]:
        development_closed = True
        development_bound = development_bound or observed
    if current_status in codes["qa_started"]:
        qa_active = True
        qa_start_bound = qa_start_bound or observed
    if current_status not in set().union(*codes.values()):
        limitations.add(f"current-status-unmapped:{history.current_status}")
    exact = history.complete and not any(item.startswith("participant-role-unknown:") for item in limitations)

    def formatted(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    not_started = current_status in codes["not_started"] and role(history.current_assignee) == "AN"
    development_state = "completed" if development_closed else "in-progress" if development_start or current_status in codes["development_started"] else "not-started" if not_started else "unknown"
    before_qa = codes["not_started"] | codes["development_started"] | codes["development_completed"]
    qa_not_started = not_started or (exact and current_status in before_qa and role(history.current_assignee) in {"AN", "BE", "FE"})
    qa_state = "completed" if qa_completed else "in-progress" if qa_active else "not-started" if qa_not_started else "unknown"
    return {
        "task_key": history.task_key, "development_role": history.development_role,
        "development": {
            "state": development_state,
            "started_at": formatted(development_start) if exact else None,
            "finished_at": formatted(development_finish) if exact else None,
            "started_by": formatted(development_start or (observed if development_state == "in-progress" else None)),
            "completed_by": formatted(development_bound),
        },
        "qa": {
            "state": qa_state,
            "started_at": formatted(qa_start) if exact else None,
            "finished_at": formatted(qa_finish) if exact and qa_completed else None,
            "started_by": formatted(qa_start_bound),
            "completed_by": formatted(qa_finish or observed) if qa_completed else None,
        },
        "limitations": sorted(limitations), "evidence": evidence,
    }


def calculate_feature(
    feature: str, histories: tuple[TaskHistory, ...], participants: Mapping[str, str],
    rules: StatusRules, qa_task_keys: tuple[str, ...], scope_confirmed: bool,
    analyst_completion: AnalystCompletion | None = None,
) -> dict:
    if not feature or type(scope_confirmed) is not bool:
        raise ValueError("A feature and explicit scope confirmation are required")
    if len(set(qa_task_keys)) != len(qa_task_keys) or any(not key for key in qa_task_keys):
        raise ValueError("QA scope must contain unique nonempty task keys")
    tasks = {}
    for history in histories:
        if history.task_key in tasks:
            raise ValueError("Duplicate task history")
        tasks[history.task_key] = calculate_task(history, participants, rules)
    missing = sorted(set(qa_task_keys) - set(tasks))
    limitations = {f"{key}:{item}" for key, task in tasks.items() for item in task["limitations"]}
    if not scope_confirmed:
        limitations.add("qa-scope-not-confirmed")
    if not qa_task_keys:
        limitations.add("qa-scope-empty")
    limitations.update(f"qa-task-history-missing:{key}" for key in missing)
    members = [tasks[key]["qa"] for key in qa_task_keys if key in tasks]
    complete_scope = scope_confirmed and bool(qa_task_keys) and not missing
    completed = complete_scope and all(member["state"] == "completed" for member in members)
    started = [member["started_at"] for member in members if member["started_at"]]
    known_starts = all(member["started_at"] or member["state"] == "not-started" for member in members)
    finished = [member["finished_at"] for member in members if member["finished_at"]]

    def extreme(values: list[str], operation) -> str | None:
        return operation(values, key=datetime.fromisoformat) if values else None

    qa = {
        "state": "completed" if completed else "in-progress" if any(member["state"] in {"in-progress", "completed"} for member in members) else "not-started" if complete_scope and all(member["state"] == "not-started" for member in members) else "unknown",
        "started_at": extreme(started, min) if complete_scope and known_starts else None,
        "finished_at": extreme(finished, max) if completed and len(finished) == len(members) else None,
        "finished_on": None,
        "completed_by": extreme([member["completed_by"] for member in members if member["completed_by"]], max) if completed else None,
        "completion_source": "all-qa-members" if completed else None,
    }
    if analyst_completion is not None:
        confirmation = analyst_completion
        confirmed_at = timestamp(confirmation.confirmed_at)
        if confirmation.analyst_confirmed is not True or not confirmation.source.strip():
            raise ValueError("Analyst completion requires an explicit decision and its source")
        if confirmation.finished_on is not None and (type(confirmation.finished_on) is not date or confirmation.finished_on > confirmed_at.date()):
            raise ValueError("An analyst completion date cannot be later than the confirmation")
        if qa["started_at"] and confirmation.finished_on and confirmation.finished_on < datetime.fromisoformat(qa["started_at"]).date():
            raise ValueError("Analyst completion precedes the known QA start")
        if any(history.observed_at > confirmed_at for history in histories):
            raise ValueError("The analyst decision predates the supplied snapshots; review newer evidence")
        qa.update(state="completed", finished_at=qa["finished_at"] if confirmation.finished_on is None else None,
                  finished_on=confirmation.finished_on.isoformat() if confirmation.finished_on else None,
                  completed_by=confirmed_at.isoformat(), completion_source=confirmation.source)
    return {
        "feature": feature, "tasks": tasks, "qa_task_keys": list(qa_task_keys), "qa": qa,
        "limitations": sorted(limitations), "writes_performed": False,
        "planning_application_allowed": False, "history_adapter_verified": False,
    }
