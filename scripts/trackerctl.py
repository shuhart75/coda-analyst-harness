#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "active-inventory-v1"
SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 4
STOP_EXIT = 3
PROVIDERS = ("sbertrek", "jira")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
TEAM_ID = re.compile(r"^(AN|A|BE|B|FE|F|QA|Q|OTHER|O)([1-9][0-9]*)$", re.I)
TEAM_PREFIXES = {
    "AN": ("AN", "analyst"), "A": ("AN", "analyst"),
    "BE": ("BE", "developer"), "B": ("BE", "developer"),
    "FE": ("FE", "developer"), "F": ("FE", "developer"),
    "QA": ("QA", "tester"), "Q": ("QA", "tester"),
    "OTHER": ("OTHER", "other"), "O": ("OTHER", "other"),
}
OBSERVATION_STATES = ("value", "absent", "not-returned")
RELEVANCE = ("relevant", "ambiguous", "irrelevant")
SELECTION_BASES = (
    "known-key", "linked-counterpart", "description-match", "same-epic", "ambiguous"
)
SP_UNITS = {
    "sp", "story point", "story points", "person day", "person days",
    "человеко день", "человеко дни", "человекодень", "человекодни",
    "чел день", "чел дни",
}
DEFAULT_CONFIG = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "primary_provider": "sbertrek",
    "setup_complete": False,
    "jira_enabled": None,
    "projects": {"sbertrek": [], "jira": []},
    "development_issue_types": [],
    "participants": {"sbertrek": {}, "jira": {}},
    "status_rules": {
        provider: {"completed": None, "excluded": None} for provider in PROVIDERS
    },
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def state_root() -> Path:
    override = os.environ.get("ANALYST_HARNESS_STATE_ROOT")
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parents[1] / ".workspace-state"


def config_path() -> Path:
    return state_root() / "tracker-config.json"


def run_root(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Некорректный run_id чтения трекеров")
    return state_root() / "tracker-runs" / run_id


def snapshot_path(run_id: str, provider: str) -> Path:
    return run_root(run_id) / "input" / f"{provider}.json"


def status_path(run_id: str) -> Path:
    return run_root(run_id) / "run-status.json"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать JSON {path}: {exc}") from exc


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def key(value: str, label: str = "Ключ задачи") -> str:
    if not ISSUE_KEY.fullmatch(value):
        raise ValueError(f"{label} должен иметь вид PROJECT-123: {value}")
    return value


def evidence(value: str, provider: str) -> str:
    if not value.startswith(f"mcp:{provider}:") or value.casefold().count("mcp:") != 1:
        raise ValueError(f"Evidence должен описывать один вызов и начинаться с mcp:{provider}:")
    if any(mark in value for mark in ("\n", "\r", ";")) or value.endswith(":none"):
        raise ValueError("Evidence не может объединять вызовы или быть placeholder")
    return value


def normalized_team_id(value: str) -> str:
    match = TEAM_ID.fullmatch(value.strip())
    if not match:
        raise ValueError("team_id должен иметь вид AN1/A1, BE2/B2, FE1/F1, QA1/Q1 или OTHER1/O1")
    prefix, number = match.groups()
    return f"{TEAM_PREFIXES[prefix.upper()][0]}{number}"


def team_role(team_id: str) -> str:
    normalized = normalized_team_id(team_id)
    match = TEAM_ID.fullmatch(normalized)
    assert match
    return TEAM_PREFIXES[match.group(1).upper()][1]


def validate_config(config: Any) -> dict:
    if not isinstance(config, dict) or config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая схема tracker-config.json")
    if config.get("primary_provider") != "sbertrek":
        raise ValueError("Основным трекером должен оставаться sbertrek")
    if config.get("jira_enabled") not in {True, False, None}:
        raise ValueError("jira_enabled должен быть true, false или null")
    if not isinstance(config.get("setup_complete"), bool):
        raise ValueError("setup_complete должен быть логическим значением")
    for provider in PROVIDERS:
        projects = config.get("projects", {}).get(provider)
        if not isinstance(projects, list) or not all(isinstance(item, str) and item for item in projects):
            raise ValueError(f"projects.{provider} должен быть списком строк")
        rules = config.get("status_rules", {}).get(provider)
        if not isinstance(rules, dict):
            raise ValueError(f"status_rules.{provider} должен быть объектом")
        for kind in ("completed", "excluded"):
            values = rules.get(kind)
            if values is not None and (not isinstance(values, list) or not all(isinstance(item, str) and item for item in values)):
                raise ValueError(f"status_rules.{provider}.{kind} должен быть списком или null")
        mapping = config.get("participants", {}).get(provider)
        if not isinstance(mapping, dict):
            raise ValueError(f"participants.{provider} должен быть объектом")
        used: dict[str, str] = {}
        for account, participant in mapping.items():
            if not isinstance(account, str) or not isinstance(participant, dict):
                raise ValueError(f"Некорректный participants.{provider}")
            team = participant.get("team_id")
            if not isinstance(team, str) or normalized_team_id(team) != team:
                raise ValueError(f"Некорректный team_id participants.{provider}.{account}")
            previous = used.setdefault(team, account)
            if previous != account:
                raise ValueError(f"team_id {team} назначен нескольким аккаунтам {provider}")
    types = config.get("development_issue_types")
    if not isinstance(types, list) or not all(isinstance(item, str) for item in types):
        raise ValueError("development_issue_types должен быть списком строк")
    return config


def migrate_config(config: Any) -> tuple[dict, bool]:
    if not isinstance(config, dict):
        return config, False
    version = config.get("schema_version")
    if version == CONFIG_SCHEMA_VERSION:
        migrated = dict(config)
        changed = "issue_pairs" in migrated
        migrated.pop("issue_pairs", None)
        return migrated, changed
    if version not in {1, 2, 3}:
        return config, False
    projects = config.get("projects") if isinstance(config.get("projects"), dict) else {"sbertrek": [], "jira": []}
    migrated = {
        **DEFAULT_CONFIG,
        "jira_enabled": config.get("jira_enabled") if config.get("jira_enabled") in {True, False} else bool(projects.get("jira")) or None,
        "projects": {provider: list(projects.get(provider, [])) for provider in PROVIDERS},
        "development_issue_types": list(config.get("development_issue_types", [])),
        "participants": config.get("participants", {"sbertrek": {}, "jira": {}}) if version == 3 else {"sbertrek": {}, "jira": {}},
        "status_rules": config.get("status_rules", DEFAULT_CONFIG["status_rules"]) if version == 3 else DEFAULT_CONFIG["status_rules"],
        "setup_complete": bool(config.get("setup_complete")) if version == 3 else False,
    }
    migrated.pop("issue_pairs", None)
    return migrated, True


def load_config() -> dict:
    config, changed = migrate_config(load_json(config_path()))
    config = validate_config(config)
    if changed:
        save_json(config_path(), config)
    return config


def config_gaps(config: dict, include_confirmation: bool = True) -> list[str]:
    gaps = []
    if not config["projects"]["sbertrek"]:
        gaps.append("projects.sbertrek")
    if config["jira_enabled"] is None:
        gaps.append("jira_enabled")
    elif config["jira_enabled"] and not config["projects"]["jira"]:
        gaps.append("projects.jira")
    if not config["development_issue_types"]:
        gaps.append("development_issue_types")
    for provider in PROVIDERS:
        if provider == "jira" and not config["jira_enabled"]:
            continue
        for kind in ("completed", "excluded"):
            if config["status_rules"][provider][kind] is None:
                gaps.append(f"status_rules.{provider}.{kind}")
    if include_confirmation and not config["setup_complete"]:
        gaps.append("setup_complete")
    return gaps


def stop_payload(question: str, **extra: Any) -> dict:
    return {
        **extra,
        "must_stop": True,
        "workflow_complete": False,
        "final_response_allowed": False,
        "allowed_next_action": "ask-user",
        "next_question": question,
        "response_contract": {
            "type": "exact-single-question",
            "text": question,
            "additional_text_forbidden": True,
            "examples_forbidden": True,
        },
    }


def config_status(config: dict) -> dict:
    questions = {
        "projects.sbertrek": "Какие проекты SberTrek входят в область чтения?",
        "jira_enabled": "Jira доступна для дополнительного чтения на этой рабочей области?",
        "projects.jira": "Какие проекты Jira соответствуют выбранным проектам SberTrek?",
        "development_issue_types": "Какие типы объектов трекера являются единицами разработки?",
        "status_rules.sbertrek.completed": "Какие статусы SberTrek однозначно означают завершение разработки?",
        "status_rules.sbertrek.excluded": "Какие статусы SberTrek исключают задачу из выполнения?",
        "status_rules.jira.completed": "Какие статусы Jira однозначно означают завершение разработки?",
        "status_rules.jira.excluded": "Какие статусы Jira исключают задачу из выполнения?",
        "setup_complete": "Подтверждаете сохранённую базовую настройку трекеров?",
    }
    gaps = config_gaps(config)
    if gaps:
        return {"status": "tracker-config-incomplete", "path": str(config_path()), "gaps": gaps, **stop_payload(questions[gaps[0]])}
    return {
        "status": "tracker-config-ready", "path": str(config_path()), "gaps": [],
        "must_stop": False, "allowed_next_action": "begin",
    }


def snapshot_template(provider: str, feature: str, known: list[dict], config: dict) -> dict:
    return {
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "captured_at": None,
        "scope": {
            "feature": feature,
            "projects": config["projects"][provider],
            "known_keys": [item["key"] for item in known],
            "known_key_evidence": known,
        },
        "inventory": {
            "state": "pending", "query": None, "pages": [], "keys": [], "jira_links": {},
            "unavailable_reason": None, "unavailable_evidence": None,
        },
        "selection": {"state": "pending", "issues": []},
    }


def validate_snapshot(snapshot: Any, provider: str, finalized: bool = False) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("protocol") != PROTOCOL or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Снимок создан старым протоколом; начни новый tracker-run")
    if snapshot.get("provider") != provider:
        raise ValueError(f"Ожидался снимок {provider}")
    if finalized and not snapshot.get("captured_at"):
        raise ValueError(f"Снимок {provider} не финализирован")
    return snapshot


def load_snapshot(run_id: str, provider: str) -> tuple[Path, dict]:
    path = snapshot_path(run_id, provider)
    if not path.is_file():
        raise ValueError(f"Снимок {provider} не создан для run_id={run_id}")
    return path, validate_snapshot(load_json(path), provider)


def ensure_mutable(snapshot: dict) -> None:
    if snapshot.get("captured_at"):
        raise ValueError("Финализированный снимок неизменяем")


def write_status(run_id: str, status: str, *, gaps: list[str] | None = None, allowed: str | None = None, complete: bool = False) -> dict:
    payload = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "status": status, "workflow_complete": complete,
        "final_response_allowed": complete, "gaps": gaps or [],
        "allowed_next_action": allowed,
    }
    save_json(status_path(run_id), payload)
    return payload


def parse_known(values: list[str]) -> list[dict]:
    result = []
    for value in values:
        issue_key, sep, source = value.partition("=")
        if not sep or not source.strip():
            raise ValueError("--known-key задаётся как KEY=SOURCE")
        result.append({"key": key(issue_key), "source": source.strip()})
    unique = {item["key"]: item for item in result}
    return [unique[item] for item in sorted(unique)]


def issue_by_key(snapshot: dict, issue_key: str) -> dict | None:
    return next((item for item in snapshot["selection"]["issues"] if item["key"] == issue_key), None)


def page_evidence_for_key(snapshot: dict, issue_key: str) -> set[str]:
    return {page["evidence"] for page in snapshot["inventory"]["pages"] if issue_key in page["keys"]}


def canonical_estimate(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    unit = result.get("unit")
    if isinstance(unit, str) and " ".join(unit.casefold().replace("-", " ").replace("_", " ").split()) in SP_UNITS:
        result["unit"] = "story-points"
    return result


def parse_release(value: str) -> dict:
    release_key, sep, name = value.partition("=")
    if sep:
        return {"key": release_key.strip(), "name": name.strip()}
    return {"key": value.strip(), "name": value.strip()}


def participant(account_id: str | None, name: str | None) -> dict | None:
    return {"id": account_id, "name": name} if account_id else None


def normalized_status_set(config: dict, provider: str, kind: str) -> set[str]:
    return {item.strip().casefold() for item in config["status_rules"][provider][kind] or []}


def participant_role(config: dict, provider: str, value: Any) -> str | None:
    if not isinstance(value, dict) or not value.get("id"):
        return None
    mapping = config["participants"][provider].get(value["id"])
    return team_role(mapping["team_id"]) if mapping else None


def selected_relevant(snapshot: dict) -> list[dict]:
    return [item for item in snapshot["selection"]["issues"] if item["relevance"] == "relevant"]


def all_enabled_snapshots(run_id: str, config: dict, finalized: bool = False) -> dict[str, dict]:
    providers = PROVIDERS if config["jira_enabled"] else ("sbertrek",)
    return {provider: validate_snapshot(load_json(snapshot_path(run_id, provider)), provider, finalized) for provider in providers}


def inventory_gaps(snapshot: dict) -> list[str]:
    inventory = snapshot["inventory"]
    if inventory["state"] == "pending":
        return [f"{snapshot['provider']}.inventory.pending"]
    if inventory["state"] == "complete":
        pages = inventory["pages"]
        if not pages or not pages[-1]["last_page"]:
            return [f"{snapshot['provider']}.inventory.pagination-incomplete"]
    return []


def selection_gaps(snapshot: dict) -> list[str]:
    gaps = []
    if snapshot["selection"]["state"] != "complete":
        gaps.append(f"{snapshot['provider']}.selection.pending")
    for issue in selected_relevant(snapshot):
        history = issue["history"]
        if history["state"] == "pending":
            gaps.append(f"{snapshot['provider']}.{issue['key']}.history.pending")
    return gaps


def required_known_key_gaps(snapshot: dict) -> list[str]:
    active_known = set(snapshot["scope"]["known_keys"]) & set(snapshot["inventory"]["keys"])
    selected = {item["key"] for item in selected_relevant(snapshot)}
    return [f"{snapshot['provider']}.{item}.active-known-key-not-selected" for item in sorted(active_known - selected)]


def active_link_map(snapshots: dict[str, dict]) -> dict[str, str]:
    sber = snapshots["sbertrek"]
    return {
        source: target for source, target in sber["inventory"]["jira_links"].items()
        if source in sber["inventory"]["keys"]
        and "jira" in snapshots and target in snapshots["jira"]["inventory"]["keys"]
    }


def link_closure_gaps(snapshots: dict[str, dict]) -> list[str]:
    if "jira" not in snapshots:
        return []
    links = active_link_map(snapshots)
    sber_selected = {item["key"] for item in selected_relevant(snapshots["sbertrek"])}
    jira_selected = {item["key"] for item in selected_relevant(snapshots["jira"])}
    gaps = []
    for sber_key, jira_key in links.items():
        if sber_key in sber_selected and jira_key not in jira_selected:
            gaps.append(f"jira.{jira_key}.linked-counterpart-not-selected")
        if jira_key in jira_selected and sber_key not in sber_selected:
            gaps.append(f"sbertrek.{sber_key}.linked-counterpart-not-selected")
    return gaps


def first_ambiguous(snapshots: dict[str, dict]) -> tuple[str, dict] | None:
    for provider in PROVIDERS:
        if provider not in snapshots:
            continue
        for issue in snapshots[provider]["selection"]["issues"]:
            if issue["relevance"] == "ambiguous":
                return provider, issue
    return None


def relevance_question(provider: str, issue: dict) -> str:
    summary = issue.get("summary") or "без названия"
    return f"Относится ли задача {provider} {issue['key']} «{summary}» к этой фиче?"


def first_unknown_participant(snapshots: dict[str, dict], config: dict) -> dict | None:
    development_types = {item.casefold() for item in config["development_issue_types"]}
    for provider in PROVIDERS:
        if provider not in snapshots:
            continue
        for issue in selected_relevant(snapshots[provider]):
            if str(issue.get("issue_type") or "").casefold() not in development_types:
                continue
            values = [issue.get("assignee")]
            for event in issue["history"]["events"]:
                if event["field"] == "assignee":
                    values.extend((event.get("from"), event.get("to")))
            for value in values:
                if isinstance(value, dict) and value.get("id") and value["id"] not in config["participants"][provider]:
                    return {"provider": provider, "account_id": value["id"], "name": value.get("name") or value["id"]}
    return None


def pending_participant_path(run_id: str) -> Path:
    return run_root(run_id) / "pending-participant.json"


def pending_relevance_path(run_id: str) -> Path:
    return run_root(run_id) / "pending-relevance.json"


def merged_value(field: str, sber: dict | None, jira: dict | None) -> tuple[Any, str | None, dict | None]:
    svalue = sber.get(field) if sber else None
    jvalue = jira.get(field) if jira else None
    if field == "estimate":
        svalue, jvalue = canonical_estimate(svalue), canonical_estimate(jvalue)
    chosen, source = (svalue, "sbertrek") if svalue not in (None, "", [], {}) else (jvalue, "jira")
    conflict = None
    if svalue not in (None, "", [], {}) and jvalue not in (None, "", [], {}) and svalue != jvalue:
        conflict = {"field": field, "sbertrek": svalue, "jira": jvalue, "resolution": "sbertrek-preserved"}
    return chosen, source if chosen not in (None, "", [], {}) else None, conflict


def merged_history(sber: dict | None, jira: dict | None) -> list[dict]:
    seen: set[str] = set()
    result = []
    for provider, issue in (("sbertrek", sber), ("jira", jira)):
        if not issue:
            continue
        for event in issue["history"]["events"]:
            fingerprint = json.dumps({k: event.get(k) for k in ("at", "field", "from", "to")}, ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            result.append({**event, "source": provider})
    return sorted(result, key=lambda item: item["at"])


def development_state(sber: dict | None, jira: dict | None, config: dict) -> dict:
    issue = sber or jira or {}
    provider = "sbertrek" if sber else "jira"
    issue_type = str(issue.get("issue_type") or "").casefold()
    if issue_type not in {item.casefold() for item in config["development_issue_types"]}:
        return {"state": "not-development-unit", "basis": "issue-type"}
    status = str(issue.get("status") or "")
    normalized = status.casefold()
    if normalized in normalized_status_set(config, provider, "excluded"):
        return {"state": "excluded", "basis": f"{provider}-status", "status": status}
    if normalized in normalized_status_set(config, provider, "completed"):
        return {"state": "completed", "basis": f"{provider}-status", "status": status}
    history = merged_history(sber, jira)
    assignee_events = [event for event in history if event["field"] == "assignee"]
    if assignee_events:
        latest = assignee_events[-1]
        source = latest["source"]
        before = participant_role(config, source, latest.get("from"))
        after = participant_role(config, source, latest.get("to"))
        if before == "developer" and after and after != "developer":
            return {"state": "completed", "basis": "developer-handoff", "at": latest["at"]}
    return {"state": "in-progress", "basis": f"{provider}-active-inventory"}


def reconcile_data(snapshots: dict[str, dict], config: dict) -> dict:
    sber = snapshots["sbertrek"]
    jira = snapshots.get("jira")
    sber_issues = {item["key"]: item for item in selected_relevant(sber)}
    jira_issues = {item["key"]: item for item in selected_relevant(jira)} if jira else {}
    links = sber["inventory"]["jira_links"]
    paired_jira = set()
    merged = []
    discrepancies = []
    for sber_key, sissue in sorted(sber_issues.items()):
        jira_key = links.get(sber_key)
        jissue = jira_issues.get(jira_key) if jira_key else None
        if jissue:
            paired_jira.add(jira_key)
        record = {"sbertrek_key": sber_key, "jira_key": jira_key}
        sources = {}
        conflicts = []
        for field in ("summary", "description", "issue_type", "status", "assignee", "estimate", "epic", "releases"):
            value, source, conflict = merged_value(field, sissue, jissue)
            record[field] = value
            sources[field] = source
            if conflict:
                conflicts.append(conflict)
                discrepancies.append({"kind": "field-conflict", "sbertrek_key": sber_key, "jira_key": jira_key, **conflict})
        record["field_sources"] = sources
        record["conflicts"] = conflicts
        record["history"] = merged_history(sissue, jissue)
        record["development"] = development_state(sissue, jissue, config)
        merged.append(record)
    for jira_key, jissue in sorted(jira_issues.items()):
        if jira_key in paired_jira:
            continue
        record = {"sbertrek_key": None, "jira_key": jira_key, "history": merged_history(None, jissue), "conflicts": []}
        for field in ("summary", "description", "issue_type", "status", "assignee", "estimate", "epic", "releases"):
            record[field] = canonical_estimate(jissue.get(field)) if field == "estimate" else jissue.get(field)
        record["field_sources"] = {field: "jira" if record.get(field) not in (None, "", [], {}) else None for field in ("summary", "description", "issue_type", "status", "assignee", "estimate", "epic", "releases")}
        record["development"] = development_state(None, jissue, config)
        merged.append(record)
        discrepancies.append({"kind": "jira-only", "jira_key": jira_key})
    known = set(sber["scope"]["known_keys"])
    inventory_keys = set(sber["inventory"]["keys"]) | (set(jira["inventory"]["keys"]) if jira else set())
    limitations = [f"known-key-not-in-active-inventory:{item}" for item in sorted(known - inventory_keys)]
    if jira:
        for source, target in sorted(sber["inventory"]["jira_links"].items()):
            if source in sber_issues and target not in jira["inventory"]["keys"]:
                limitations.append(f"linked-jira-not-in-active-inventory:{source}={target}")
    if jira and jira["inventory"]["state"] == "unavailable":
        limitations.append("jira-active-inventory-unavailable")
    for provider, snapshot in snapshots.items():
        for issue in selected_relevant(snapshot):
            if issue["history"]["state"] == "unavailable":
                limitations.append(f"{provider}-history-unavailable:{issue['key']}")
    counts = {
        "sbertrek_inventory": len(sber["inventory"]["keys"]),
        "jira_inventory": len(jira["inventory"]["keys"]) if jira else 0,
        "sbertrek_selected": len(sber_issues),
        "jira_selected": len(jira_issues),
        "matched": len(paired_jira),
        "merged": len(merged),
        "discrepancies": len(discrepancies),
    }
    groupings = {"epics": {}, "releases": {}}
    for issue in merged:
        identity = issue.get("sbertrek_key") or issue.get("jira_key")
        epic = issue.get("epic")
        epic_key = epic.get("key") if isinstance(epic, dict) else None
        groupings["epics"].setdefault(epic_key or "unassigned", []).append(identity)
        releases = issue.get("releases") or []
        if not releases:
            groupings["releases"].setdefault("unassigned", []).append(identity)
        for release in releases:
            release_key = release.get("key") if isinstance(release, dict) else str(release)
            groupings["releases"].setdefault(release_key or "unassigned", []).append(identity)
    return {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION,
        "status": "tracker-read-reconciled", "feature": sber["scope"]["feature"],
        "counts": counts, "issues": merged, "groupings": groupings, "discrepancies": discrepancies,
        "limitations": limitations,
    }


def render_report(result: dict) -> str:
    lines = [f"# Сверка трекеров: {result['feature']}", "", "## Сводка", ""]
    labels = {
        "sbertrek_inventory": "Активных в SberTrek", "jira_inventory": "Активных в Jira",
        "sbertrek_selected": "Выбрано в SberTrek", "jira_selected": "Выбрано в Jira",
        "matched": "Склеено пар", "merged": "Итоговых задач", "discrepancies": "Расхождений",
    }
    lines += [f"- {labels[name]}: {value}" for name, value in result["counts"].items()]
    lines += ["", "## Задачи", "", "| SberTrek | Jira | Название | Статус | Исполнитель | Оценка | Состояние |", "|---|---|---|---|---|---|---|"]
    for issue in result["issues"]:
        assignee = issue.get("assignee") or {}
        estimate = issue.get("estimate") or {}
        cells = [
            issue.get("sbertrek_key") or "—", issue.get("jira_key") or "—",
            issue.get("summary") or "—", issue.get("status") or "—",
            assignee.get("name") or assignee.get("id") or "—" if isinstance(assignee, dict) else str(assignee),
            f"{estimate.get('value')} {estimate.get('unit')}" if isinstance(estimate, dict) and estimate.get("value") is not None else "—",
            issue["development"]["state"],
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in result["limitations"]] or ["- Нет"]
    for grouping, title in (("epics", "Группировка по эпикам"), ("releases", "Группировка по релизам")):
        lines += ["", f"## {title}", ""]
        lines += [f"- **{group_key}**: {', '.join(keys)}" for group_key, keys in sorted(result["groupings"][grouping].items())] or ["- Нет"]
    lines += ["", "## Расхождения", ""]
    lines += [f"- `{item['kind']}`: {item.get('sbertrek_key') or '—'} / {item.get('jira_key') or '—'}{(' / ' + item['field']) if item.get('field') else ''}" for item in result["discrepancies"]] or ["- Нет"]
    return "\n".join(lines) + "\n"


def init_config_command(args: argparse.Namespace) -> int:
    path = config_path()
    if path.exists() and not args.force:
        raise ValueError(f"Настройка уже существует: {path}")
    save_json(path, DEFAULT_CONFIG)
    print(json.dumps({"status": "tracker-config-created", "path": str(path)}, ensure_ascii=False, indent=2))
    return 0


def config_status_command(_: argparse.Namespace) -> int:
    payload = config_status(load_config())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return STOP_EXIT if payload.get("must_stop") else 0


def update_config(args: argparse.Namespace) -> int:
    config = load_config()
    if args.command == "set-projects":
        config["projects"][args.provider] = list(dict.fromkeys(args.projects))
    elif args.command == "set-jira-mode":
        config["jira_enabled"] = args.mode == "enabled"
    elif args.command == "set-issue-types":
        config["development_issue_types"] = list(dict.fromkeys(args.issue_types))
    elif args.command == "set-statuses":
        if args.none == bool(args.statuses):
            raise ValueError("Укажи статусы либо только --none")
        config["status_rules"][args.provider][args.kind] = [] if args.none else list(dict.fromkeys(args.statuses))
    config["setup_complete"] = False
    save_json(config_path(), validate_config(config))
    print(json.dumps(config_status(config), ensure_ascii=False, indent=2))
    return 0


def complete_config_command(_: argparse.Namespace) -> int:
    config = load_config()
    gaps = config_gaps(config, include_confirmation=False)
    if gaps:
        raise ValueError("Базовая настройка не заполнена: " + ", ".join(gaps))
    config["setup_complete"] = True
    save_json(config_path(), config)
    print(json.dumps(config_status(config), ensure_ascii=False, indent=2))
    return 0


def begin_command(args: argparse.Namespace) -> int:
    config = load_config()
    gaps = config_gaps(config)
    if gaps:
        payload = config_status(config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    known = parse_known(args.known_key)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    root = run_root(run_id)
    for provider in PROVIDERS if config["jira_enabled"] else ("sbertrek",):
        save_json(snapshot_path(run_id, provider), snapshot_template(provider, args.feature, known, config))
    status = write_status(run_id, "tracker-read-collecting", gaps=["inventories.pending"], allowed="inventory-page")
    print(json.dumps({**status, "paths": {"run_status": str(status_path(run_id)), "input": str(root / "input")}}, ensure_ascii=False, indent=2))
    return 0


def inventory_page_command(args: argparse.Namespace) -> int:
    config = load_config()
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    inventory = snapshot["inventory"]
    if inventory["state"] == "unavailable" or (inventory["pages"] and inventory["pages"][-1]["last_page"]):
        raise ValueError("Инвентаризация уже завершена")
    if set(args.scope_project) != set(config["projects"][args.provider]):
        raise ValueError("--scope-project должен точно совпадать с настроенной областью провайдера")
    if not args.unfinished_confirmed:
        raise ValueError("Активная инвентаризация требует --unfinished-confirmed")
    expected_page = len(inventory["pages"]) + 1
    if args.page_number != expected_page:
        raise ValueError(f"Ожидалась страница {expected_page}")
    if args.last_page and args.next_cursor:
        raise ValueError("Последняя страница не может иметь --next-cursor")
    if not args.last_page and not args.next_cursor:
        raise ValueError("Непоследняя страница требует --next-cursor")
    if inventory["pages"] and args.cursor != inventory["pages"][-1]["next_cursor"]:
        raise ValueError("--cursor должен совпадать с next_cursor предыдущей страницы")
    if not inventory["pages"] and args.cursor:
        raise ValueError("Первая страница не может иметь --cursor")
    page_keys = sorted(set(key(item) for item in args.key))
    links = {}
    for item in args.jira_link:
        source, sep, target = item.partition("=")
        if not sep:
            raise ValueError("--jira-link задаётся как SBER=JIRA")
        source, target = key(source), key(target)
        if source not in page_keys:
            raise ValueError(f"Связь {source} отсутствует на этой странице SberTrek")
        links[source] = target
    if args.provider != "sbertrek" and links:
        raise ValueError("Поле Объект Jira записывается только из SberTrek")
    call = evidence(args.evidence, args.provider)
    if any(page["evidence"] == call for page in inventory["pages"]):
        raise ValueError("Один MCP-вызов нельзя записать как две страницы")
    inventory["query"] = args.query if inventory["query"] is None else inventory["query"]
    if inventory["query"] != args.query:
        raise ValueError("Запрос нельзя менять между страницами")
    inventory["pages"].append({
        "number": args.page_number, "cursor": args.cursor, "next_cursor": args.next_cursor,
        "last_page": args.last_page, "evidence": call, "keys": page_keys,
    })
    inventory["keys"] = sorted(set(inventory["keys"]) | set(page_keys))
    for source, target in links.items():
        previous = inventory["jira_links"].get(source)
        if previous and previous != target:
            raise ValueError(f"SberTrek вернул противоречащие Объект Jira для {source}")
        inventory["jira_links"][source] = target
    inventory["state"] = "complete" if args.last_page else "collecting"
    save_json(path, snapshot)
    print(json.dumps({"status": "inventory-page-recorded", "provider": args.provider, "page": args.page_number, "inventory_state": inventory["state"], "key_count": len(inventory["keys"])}, ensure_ascii=False, indent=2))
    return 0


def inventory_unavailable_command(args: argparse.Namespace) -> int:
    if args.provider == "sbertrek":
        raise ValueError("SberTrek является обязательным основным источником")
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    if snapshot["inventory"]["pages"]:
        raise ValueError("Нельзя объявить недоступной уже начатую инвентаризацию")
    snapshot["inventory"].update({"state": "unavailable", "unavailable_reason": args.reason, "unavailable_evidence": evidence(args.evidence, args.provider)})
    save_json(path, snapshot)
    print(json.dumps({"status": "inventory-unavailable-recorded", "provider": args.provider}, ensure_ascii=False, indent=2))
    return 0


def record_issue_command(args: argparse.Namespace) -> int:
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    if snapshot["selection"]["state"] == "complete":
        raise ValueError("Завершённую выборку нельзя дополнять")
    issue_key = key(args.key)
    if issue_key not in snapshot["inventory"]["keys"]:
        raise ValueError("Подробная карточка разрешена только для задачи из активной инвентаризации")
    if issue_by_key(snapshot, issue_key):
        raise ValueError(f"Задача {issue_key} уже записана")
    call = evidence(args.evidence, args.provider)
    page_calls = page_evidence_for_key(snapshot, issue_key)
    exact_call = re.search(rf"(?<![A-Z0-9_]){re.escape(issue_key)}(?![0-9])", call, re.I)
    if call not in page_calls and not exact_call:
        raise ValueError("Evidence карточки должен быть страницей инвентаря с этим ключом или отдельным точным чтением")
    observations = {
        "assignee": args.assignee_state, "estimate": args.estimate_state,
        "epic": args.epic_state, "releases": args.releases_state,
    }
    values = {
        "assignee": participant(args.assignee_id, args.assignee_name),
        "estimate": {"value": args.estimate, "unit": args.estimate_unit} if args.estimate is not None else None,
        "epic": {"key": args.epic_key, "name": args.epic_name} if args.epic_key else None,
        "releases": [parse_release(item) for item in args.release],
    }
    for field, state in observations.items():
        present = values[field] not in (None, [], {})
        if (state == "value") != present:
            raise ValueError(f"{field}: состояние value должно точно соответствовать переданному значению")
    if args.relevance == "ambiguous" and args.selected_by != "ambiguous":
        raise ValueError("Сомнительная задача должна иметь selected_by=ambiguous")
    if args.selected_by == "known-key" and issue_key not in snapshot["scope"]["known_keys"]:
        raise ValueError("selected_by=known-key требует известный ключ из begin")
    if args.selected_by == "linked-counterpart":
        links = (
            snapshot["inventory"]["jira_links"]
            if args.provider == "sbertrek"
            else load_snapshot(args.run_id, "sbertrek")[1]["inventory"]["jira_links"]
        )
        linked = issue_key in links if args.provider == "sbertrek" else issue_key in links.values()
        if not linked:
            raise ValueError("selected_by=linked-counterpart требует явный SberTrek Объект Jira")
    if issue_key in snapshot["scope"]["known_keys"] and args.relevance != "relevant":
        raise ValueError("Активный известный ключ должен быть выбран как relevant")
    issue = {
        "key": issue_key, "evidence": call, "summary": args.summary,
        "description": args.description, "issue_type": args.issue_type,
        "status": args.status, **values, "field_observations": observations,
        "relevance": args.relevance, "relevance_basis": args.relevance_basis,
        "selected_by": args.selected_by, "updated_at": args.updated_at,
        "history": {"state": "pending" if args.relevance != "irrelevant" else "not-applicable", "evidence": [], "events": [], "reason": None},
    }
    snapshot["selection"]["issues"].append(issue)
    save_json(path, snapshot)
    print(json.dumps({"status": "tracker-issue-recorded", "provider": args.provider, "key": issue_key, "relevance": args.relevance}, ensure_ascii=False, indent=2))
    return 0


def decide_relevance_command(args: argparse.Namespace) -> int:
    pending_path = pending_relevance_path(args.run_id)
    if not pending_path.is_file():
        raise ValueError("Нет ожидающего вопроса о релевантности")
    pending = load_json(pending_path)
    if (args.provider, args.key) != (pending["provider"], pending["key"]):
        raise ValueError("Разрешено ответить только на текущий вопрос о релевантности")
    path, snapshot = load_snapshot(args.run_id, args.provider)
    issue = issue_by_key(snapshot, args.key)
    if not issue or issue["relevance"] != "ambiguous":
        raise ValueError("Задача больше не ожидает решения")
    issue["relevance"] = args.relevance
    issue["selected_by"] = "description-match" if args.relevance == "relevant" else "ambiguous"
    issue["relevance_basis"] = args.basis
    issue["history"]["state"] = "pending" if args.relevance == "relevant" else "not-applicable"
    save_json(path, snapshot)
    pending_path.unlink()
    print(json.dumps({"status": "relevance-decided", "provider": args.provider, "key": args.key, "relevance": args.relevance}, ensure_ascii=False, indent=2))
    return 0


def selection_complete_command(args: argparse.Namespace) -> int:
    config = load_config()
    snapshots = all_enabled_snapshots(args.run_id, config)
    gaps = sum((inventory_gaps(snapshot) + required_known_key_gaps(snapshot) for snapshot in snapshots.values()), [])
    if gaps:
        raise ValueError("Инвентаризация не завершена: " + ", ".join(gaps))
    ambiguous = first_ambiguous(snapshots)
    if ambiguous:
        provider, issue = ambiguous
        pending = {"provider": provider, "key": issue["key"], "question": relevance_question(provider, issue)}
        save_json(pending_relevance_path(args.run_id), pending)
        payload = stop_payload(pending["question"], status="tracker-selection-blocked", run_id=args.run_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    closure = link_closure_gaps(snapshots)
    if closure:
        raise ValueError("Не замкнуты явные связи SberTrek Объект Jira: " + ", ".join(closure))
    for provider, snapshot in snapshots.items():
        path = snapshot_path(args.run_id, provider)
        snapshot["selection"]["state"] = "complete"
        save_json(path, snapshot)
    print(json.dumps({"status": "tracker-selection-complete", "run_id": args.run_id, "selected": {provider: len(selected_relevant(snapshot)) for provider, snapshot in snapshots.items()}}, ensure_ascii=False, indent=2))
    return 0


def history_event_command(args: argparse.Namespace) -> int:
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    issue = issue_by_key(snapshot, key(args.key))
    if not issue or issue["relevance"] != "relevant":
        raise ValueError("История разрешена только для выбранной релевантной задачи")
    event = {
        "at": args.at, "field": args.field,
        "from": participant(args.from_id, args.from_name) if args.field == "assignee" else args.from_value,
        "to": participant(args.to_id, args.to_name) if args.field == "assignee" else args.to_value,
    }
    issue["history"]["events"].append(event)
    save_json(path, snapshot)
    print(json.dumps({"status": "history-event-recorded", "provider": args.provider, "key": args.key}, ensure_ascii=False, indent=2))
    return 0


def history_complete_command(args: argparse.Namespace) -> int:
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    issue = issue_by_key(snapshot, key(args.key))
    if not issue or issue["relevance"] != "relevant":
        raise ValueError("История требуется только для выбранной релевантной задачи")
    if args.state == "unavailable" and not args.reason:
        raise ValueError("Недоступная история требует --reason")
    call = evidence(args.evidence, args.provider)
    if not re.search(rf"(?<![A-Z0-9_]){re.escape(issue['key'])}(?![0-9])", call, re.I):
        raise ValueError("Evidence истории должен содержать точный ключ выбранной задачи")
    issue["history"].update({"state": args.state, "evidence": [call], "reason": args.reason})
    save_json(path, snapshot)
    print(json.dumps({"status": "history-complete", "provider": args.provider, "key": args.key, "history_state": args.state}, ensure_ascii=False, indent=2))
    return 0


def finalize_command(args: argparse.Namespace) -> int:
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    gaps = inventory_gaps(snapshot) + selection_gaps(snapshot)
    if gaps:
        raise ValueError("Снимок не готов: " + ", ".join(gaps))
    snapshot["captured_at"] = now()
    save_json(path, snapshot)
    print(json.dumps({"status": "tracker-snapshot-finalized", "provider": args.provider, "captured_at": snapshot["captured_at"]}, ensure_ascii=False, indent=2))
    return 0


def run_status_command(args: argparse.Namespace) -> int:
    completion = run_root(args.run_id) / "completion-status.json"
    if completion.is_file():
        payload = load_json(completion)
        if payload.get("protocol") != PROTOCOL:
            raise ValueError("Completion-envelope создан старым протоколом")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    config = load_config()
    snapshots = all_enabled_snapshots(args.run_id, config)
    gaps = []
    for snapshot in snapshots.values():
        gaps += inventory_gaps(snapshot) + required_known_key_gaps(snapshot) + selection_gaps(snapshot)
        if not snapshot.get("captured_at"):
            gaps.append(f"{snapshot['provider']}.snapshot.not-finalized")
    gaps += link_closure_gaps(snapshots)
    payload = write_status(args.run_id, "tracker-read-ready-to-reconcile" if not gaps else "tracker-read-collecting", gaps=sorted(set(gaps)), allowed="reconcile" if not gaps else "complete-reported-gaps")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not gaps else 2


def set_participant_command(args: argparse.Namespace) -> int:
    pending_path = pending_participant_path(args.run_id)
    if not pending_path.is_file():
        raise ValueError("Нет ожидающего вопроса об участнике; сначала выполни reconcile")
    pending = load_json(pending_path)
    if (args.provider, args.account_id) != (pending["provider"], pending["account_id"]):
        raise ValueError("Разрешено сохранить только участника из текущего вопроса")
    config = load_config()
    config["participants"][args.provider][args.account_id] = {"team_id": normalized_team_id(args.team_id)}
    save_json(config_path(), validate_config(config))
    pending_path.unlink()
    print(json.dumps({"status": "tracker-participant-saved", "run_id": args.run_id, "provider": args.provider, "account_id": args.account_id, "team_id": normalized_team_id(args.team_id), "allowed_next_action": "reconcile"}, ensure_ascii=False, indent=2))
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    config = load_config()
    snapshots = all_enabled_snapshots(args.run_id, config, finalized=True)
    gaps = sum((inventory_gaps(snapshot) + required_known_key_gaps(snapshot) + selection_gaps(snapshot) for snapshot in snapshots.values()), []) + link_closure_gaps(snapshots)
    if gaps:
        raise ValueError("Tracker-run неполон: " + ", ".join(gaps))
    unknown = first_unknown_participant(snapshots, config)
    if unknown:
        question = f"Какой командный идентификатор имеет участник {unknown['name']} ({unknown['provider']} account {unknown['account_id']})?"
        save_json(pending_participant_path(args.run_id), {**unknown, "question": question})
        payload = stop_payload(question, status="tracker-read-blocked", run_id=args.run_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    result = reconcile_data(snapshots, config)
    root = run_root(args.run_id)
    save_json(root / "reconciled.json", result)
    (root / "report.md").write_text(render_report(result), encoding="utf-8")
    completion = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id, "status": "tracker-read-reconciled",
        "workflow_complete": True, "final_response_allowed": True,
        "counts": result["counts"], "limitations": result["limitations"],
        "paths": {"reconciled": str(root / "reconciled.json"), "report": str(root / "report.md")},
    }
    save_json(root / "completion-status.json", completion)
    save_json(status_path(args.run_id), completion)
    print(json.dumps(completion, ensure_ascii=False, indent=2))
    return 0


def result_status_command(args: argparse.Namespace) -> int:
    path = run_root(args.run_id) / "completion-status.json"
    if not path.is_file():
        raise ValueError("У tracker-run ещё нет официального completion-status")
    payload = load_json(path)
    if payload.get("protocol") != PROTOCOL or payload.get("status") != "tracker-read-reconciled":
        raise ValueError("Completion-status создан старым или незавершённым протоколом")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Guarded active-inventory tracker reconciliation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-config"); init.add_argument("--force", action="store_true"); init.set_defaults(handler=init_config_command)
    status = commands.add_parser("config-status"); status.set_defaults(handler=config_status_command)
    projects = commands.add_parser("set-projects"); projects.add_argument("--provider", choices=PROVIDERS, required=True); projects.add_argument("projects", nargs="+"); projects.set_defaults(handler=update_config)
    jira = commands.add_parser("set-jira-mode"); jira.add_argument("mode", choices=("enabled", "disabled")); jira.set_defaults(handler=update_config)
    types = commands.add_parser("set-issue-types"); types.add_argument("issue_types", nargs="+"); types.set_defaults(handler=update_config)
    statuses = commands.add_parser("set-statuses"); statuses.add_argument("--provider", choices=PROVIDERS, required=True); statuses.add_argument("--kind", choices=("completed", "excluded"), required=True); statuses.add_argument("--none", action="store_true"); statuses.add_argument("statuses", nargs="*"); statuses.set_defaults(handler=update_config)
    complete = commands.add_parser("complete-config"); complete.set_defaults(handler=complete_config_command)
    begin = commands.add_parser("begin"); begin.add_argument("--feature", required=True); begin.add_argument("--known-key", action="append", default=[]); begin.set_defaults(handler=begin_command)
    page = commands.add_parser("inventory-page"); page.add_argument("--run-id", required=True); page.add_argument("--provider", choices=PROVIDERS, required=True); page.add_argument("--query", required=True); page.add_argument("--scope-project", action="append", default=[]); page.add_argument("--unfinished-confirmed", action="store_true"); page.add_argument("--page-number", type=int, required=True); page.add_argument("--cursor"); page.add_argument("--next-cursor"); page.add_argument("--last-page", action="store_true"); page.add_argument("--evidence", required=True); page.add_argument("--key", action="append", default=[]); page.add_argument("--jira-link", action="append", default=[]); page.set_defaults(handler=inventory_page_command)
    unavailable = commands.add_parser("inventory-unavailable"); unavailable.add_argument("--run-id", required=True); unavailable.add_argument("--provider", choices=PROVIDERS, required=True); unavailable.add_argument("--reason", required=True); unavailable.add_argument("--evidence", required=True); unavailable.set_defaults(handler=inventory_unavailable_command)
    issue = commands.add_parser("record-issue"); issue.add_argument("--run-id", required=True); issue.add_argument("--provider", choices=PROVIDERS, required=True); issue.add_argument("--key", required=True); issue.add_argument("--evidence", required=True); issue.add_argument("--summary", required=True); issue.add_argument("--description", default=""); issue.add_argument("--issue-type", required=True); issue.add_argument("--status", required=True); issue.add_argument("--assignee-id"); issue.add_argument("--assignee-name"); issue.add_argument("--assignee-state", choices=OBSERVATION_STATES, required=True); issue.add_argument("--estimate", type=float); issue.add_argument("--estimate-unit", default="story-points"); issue.add_argument("--estimate-state", choices=OBSERVATION_STATES, required=True); issue.add_argument("--epic-key"); issue.add_argument("--epic-name"); issue.add_argument("--epic-state", choices=OBSERVATION_STATES, required=True); issue.add_argument("--release", action="append", default=[]); issue.add_argument("--releases-state", choices=OBSERVATION_STATES, required=True); issue.add_argument("--relevance", choices=RELEVANCE, required=True); issue.add_argument("--relevance-basis", required=True); issue.add_argument("--selected-by", choices=SELECTION_BASES, required=True); issue.add_argument("--updated-at"); issue.set_defaults(handler=record_issue_command)
    decide = commands.add_parser("decide-relevance"); decide.add_argument("--run-id", required=True); decide.add_argument("--provider", choices=PROVIDERS, required=True); decide.add_argument("--key", required=True); decide.add_argument("--relevance", choices=("relevant", "irrelevant"), required=True); decide.add_argument("--basis", required=True); decide.set_defaults(handler=decide_relevance_command)
    select = commands.add_parser("selection-complete"); select.add_argument("--run-id", required=True); select.set_defaults(handler=selection_complete_command)
    event = commands.add_parser("history-event"); event.add_argument("--run-id", required=True); event.add_argument("--provider", choices=PROVIDERS, required=True); event.add_argument("--key", required=True); event.add_argument("--at", required=True); event.add_argument("--field", required=True); event.add_argument("--from-id"); event.add_argument("--from-name"); event.add_argument("--from-value"); event.add_argument("--to-id"); event.add_argument("--to-name"); event.add_argument("--to-value"); event.set_defaults(handler=history_event_command)
    history = commands.add_parser("history-complete"); history.add_argument("--run-id", required=True); history.add_argument("--provider", choices=PROVIDERS, required=True); history.add_argument("--key", required=True); history.add_argument("--state", choices=("complete", "unavailable"), required=True); history.add_argument("--reason"); history.add_argument("--evidence", required=True); history.set_defaults(handler=history_complete_command)
    finalize = commands.add_parser("snapshot-finalize"); finalize.add_argument("--run-id", required=True); finalize.add_argument("--provider", choices=PROVIDERS, required=True); finalize.set_defaults(handler=finalize_command)
    run_status = commands.add_parser("run-status"); run_status.add_argument("--run-id", required=True); run_status.set_defaults(handler=run_status_command)
    participant_parser = commands.add_parser("set-participant"); participant_parser.add_argument("--run-id", required=True); participant_parser.add_argument("--provider", choices=PROVIDERS, required=True); participant_parser.add_argument("--account-id", required=True); participant_parser.add_argument("--team-id", required=True); participant_parser.set_defaults(handler=set_participant_command)
    reconcile = commands.add_parser("reconcile"); reconcile.add_argument("--run-id", required=True); reconcile.set_defaults(handler=reconcile_command)
    result = commands.add_parser("result-status"); result.add_argument("--run-id", required=True); result.set_defaults(handler=result_status_command)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except ValueError as exc:
        payload = {
            "status": "tracker-read-blocked", "run_id": getattr(args, "run_id", None),
            "error": str(exc), "must_stop": True, "workflow_complete": False,
            "final_response_allowed": False, "allowed_next_action": "fix-reported-gap",
            "required_success_status": "tracker-read-reconciled",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
