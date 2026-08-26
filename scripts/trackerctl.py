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


SCHEMA_VERSION = 1
PROVIDERS = ("sbertrek", "jira")
ROLES = {"developer", "tester", "analyst", "other"}
COLLECTION_CAPABILITIES = (
    "history",
    "epic_links",
    "release_links",
    "counterpart_lookup",
    "epic_neighbors",
)
COLLECTION_STATES = {"complete", "unavailable", "not-applicable"}
DISCOVERY_VALUES = {"seed", "counterpart", "epic-neighbor", "feature-search-candidate"}
CANDIDATE_DISCOVERY_VALUES = {"epic-neighbor", "feature-search-candidate"}
RELEVANCE_VALUES = {
    "proposed",
    "ambiguous",
    "confirmed-relevant",
    "confirmed-not-relevant",
    "reviewed-not-relevant",
}
RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "primary_provider": "sbertrek",
    "setup_complete": False,
    "jira_enabled": None,
    "projects": {"sbertrek": [], "jira": []},
    "issue_pairs": {},
    "development_issue_types": [],
    "participants": {"sbertrek": {}, "jira": {}},
    "status_rules": {
        "completed": [],
        "excluded": [],
    },
}
MERGED_FIELDS = (
    "summary",
    "description",
    "issue_type",
    "status",
    "assignee",
    "estimate",
    "epic",
    "releases",
    "discovery",
    "feature_relevance",
    "relevance_basis",
    "updated_at",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def timestamp_value(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def state_root() -> Path:
    configured = os.environ.get("ANALYST_HARNESS_STATE_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[1] / ".workspace-state"


def config_path() -> Path:
    return state_root() / "tracker-config.json"


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def run_root(run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Некорректный run_id чтения трекеров")
    return state_root() / "tracker-runs" / run_id


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать JSON {path}: {exc}") from exc


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def canonical_field(field: str, value: Any) -> Any:
    if field != "releases" or not isinstance(value, list):
        return value
    unique = {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in value}
    return [unique[key] for key in sorted(unique)]


def canonical_participant(value: Any, provider: str, config: dict) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return value
    participant = config.get("participants", {}).get(provider, {}).get(value["id"], {})
    return participant.get("canonical_id") or value["id"]


def comparable_field(field: str, value: Any, provider: str, config: dict) -> Any:
    if field == "assignee":
        return canonical_participant(value, provider, config)
    if field == "issue_type" and isinstance(value, str):
        return value.strip().casefold()
    return value


def normalized_statuses(config: dict, name: str) -> set[str]:
    values = config.get("status_rules", {}).get(name, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"status_rules.{name} должен быть списком строк")
    return {value.strip().casefold() for value in values if value.strip()}


def validate_config(payload: Any) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая схема tracker-config.json")
    if payload.get("primary_provider") != "sbertrek":
        raise ValueError("Основным трекером должен оставаться sbertrek")
    if not isinstance(payload.get("setup_complete", False), bool):
        raise ValueError("setup_complete должен быть логическим значением")
    if payload.get("jira_enabled") not in {True, False, None}:
        raise ValueError("jira_enabled должен быть true, false или null")
    projects = payload.get("projects", {})
    for provider in PROVIDERS:
        values = projects.get(provider) if isinstance(projects, dict) else None
        if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"projects.{provider} должен быть списком непустых строк")
    pairs = payload.get("issue_pairs", {})
    if not isinstance(pairs, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in pairs.items()):
        raise ValueError("issue_pairs должен сопоставлять ключи SberTrek ключам Jira")
    issue_types = payload.get("development_issue_types", [])
    if not isinstance(issue_types, list) or not all(isinstance(value, str) for value in issue_types):
        raise ValueError("development_issue_types должен быть списком строк")
    participants = payload.get("participants", {})
    canonical_roles: dict[str, str] = {}
    for provider in PROVIDERS:
        mapping = participants.get(provider, {}) if isinstance(participants, dict) else None
        if not isinstance(mapping, dict):
            raise ValueError(f"participants.{provider} должен быть объектом")
        for account, participant in mapping.items():
            if not isinstance(account, str) or not isinstance(participant, dict):
                raise ValueError(f"Некорректный участник participants.{provider}")
            canonical_id = participant.get("canonical_id")
            if not isinstance(canonical_id, str) or not canonical_id.strip():
                raise ValueError(f"Не задан canonical_id participants.{provider}.{account}")
            if participant.get("role") not in ROLES:
                raise ValueError(f"Неизвестная роль participants.{provider}.{account}")
            previous = canonical_roles.setdefault(canonical_id, participant["role"])
            if previous != participant["role"]:
                raise ValueError(f"Противоречащие роли участника {canonical_id}")
    normalized_statuses(payload, "completed")
    normalized_statuses(payload, "excluded")
    return payload


def base_config_gaps(config: dict, require_confirmation: bool = True) -> list[str]:
    gaps: list[str] = []
    if not config.get("projects", {}).get("sbertrek"):
        gaps.append("projects.sbertrek")
    if config.get("jira_enabled") is None:
        gaps.append("jira_enabled")
    elif config.get("jira_enabled") and not config.get("projects", {}).get("jira"):
        gaps.append("projects.jira")
    if not config.get("development_issue_types"):
        gaps.append("development_issue_types")
    if not config.get("status_rules", {}).get("completed"):
        gaps.append("status_rules.completed")
    if not config.get("status_rules", {}).get("excluded"):
        gaps.append("status_rules.excluded")
    if require_confirmation and not config.get("setup_complete", False):
        gaps.append("setup_complete")
    return gaps


def require_base_config(config: dict) -> None:
    gaps = base_config_gaps(config)
    if gaps:
        raise ValueError(
            "Первичная настройка трекеров не завершена: "
            + ", ".join(gaps)
            + ". Задай аналитику один недостающий вопрос и используй команды настройки trackerctl."
        )


def collection_template(provider: str) -> dict:
    return {
        capability: {
            "state": "not-applicable" if capability == "counterpart_lookup" and provider == "sbertrek" else "pending",
            "reason": None,
        }
        for capability in COLLECTION_CAPABILITIES
    } | {"not_found_keys": [], "expanded_epic_keys": []}


def snapshot_template(provider: str, config: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "captured_at": None,
        "scope": {
            "projects": config.get("projects", {}).get(provider, []),
            "query": None,
            "seed_keys": [],
            "seed_evidence": [],
            "expected_epic_keys": [],
            "expected_release_keys": [],
        },
        "collection": collection_template(provider),
        "issues": [],
    }


def validate_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{label} должен быть списком непустых строк")
    return value


def validate_collection(payload: Any, provider: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"Снимок {provider} должен содержать collection")
    for capability in COLLECTION_CAPABILITIES:
        item = payload.get(capability)
        if not isinstance(item, dict) or item.get("state") not in COLLECTION_STATES:
            raise ValueError(
                f"collection.{capability} снимка {provider} должен иметь state "
                "complete, unavailable или not-applicable"
            )
        if item["state"] == "unavailable" and not str(item.get("reason") or "").strip():
            raise ValueError(f"collection.{capability}={item['state']} требует reason")
        if capability in {"history", "epic_links", "release_links", "epic_neighbors"} and item["state"] == "not-applicable":
            raise ValueError(f"collection.{capability} не может быть not-applicable для {provider}")
    counterpart_state = payload["counterpart_lookup"]["state"]
    if provider == "sbertrek" and counterpart_state != "not-applicable":
        raise ValueError("collection.counterpart_lookup для sbertrek должен быть not-applicable")
    if provider == "jira" and counterpart_state == "not-applicable":
        raise ValueError("collection.counterpart_lookup для jira не может быть not-applicable")
    validate_string_list(payload.get("not_found_keys", []), f"collection.not_found_keys снимка {provider}")
    validate_string_list(payload.get("expanded_epic_keys", []), f"collection.expanded_epic_keys снимка {provider}")
    return payload


def validate_snapshot(payload: Any, provider: str) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Неподдерживаемая схема снимка {provider}")
    if payload.get("provider") != provider:
        raise ValueError(f"Снимок должен иметь provider={provider}")
    if timestamp_value(payload.get("captured_at")) is None:
        raise ValueError(f"Снимок {provider} должен иметь корректный captured_at")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise ValueError(f"Снимок {provider} должен содержать scope")
    for field in ("projects", "seed_keys", "expected_epic_keys", "expected_release_keys"):
        validate_string_list(scope.get(field, []), f"scope.{field} снимка {provider}")
    evidence = scope.get("seed_evidence")
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and item["key"].strip()
        and isinstance(item.get("source"), str)
        and item["source"].strip()
        for item in evidence
    ):
        raise ValueError(f"scope.seed_evidence снимка {provider} должен содержать key и source")
    evidence_keys = {item["key"] for item in evidence}
    missing_evidence = sorted(set(scope["seed_keys"]) - evidence_keys)
    if missing_evidence:
        raise ValueError(
            f"Seed-ключи снимка {provider} не имеют аналитического источника: "
            + ", ".join(missing_evidence)
        )
    validate_collection(payload.get("collection"), provider)
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise ValueError(f"Снимок {provider} должен содержать список issues")
    seen: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str) or not issue["key"].strip():
            raise ValueError(f"Каждая задача {provider} должна иметь непустой key")
        if issue["key"] in seen:
            raise ValueError(f"Повторяющийся ключ {provider}: {issue['key']}")
        seen.add(issue["key"])
        discovery = issue.get("discovery")
        if discovery not in DISCOVERY_VALUES:
            raise ValueError(f"Некорректный discovery задачи {issue['key']}: {discovery}")
        if discovery == "seed" and issue["key"] not in scope["seed_keys"]:
            raise ValueError(f"Задача {issue['key']} помечена seed, но отсутствует в scope.seed_keys")
        if discovery in CANDIDATE_DISCOVERY_VALUES:
            if issue.get("feature_relevance") not in RELEVANCE_VALUES:
                raise ValueError(
                    f"Задача-кандидат {issue['key']} должна иметь feature_relevance"
                )
            if not str(issue.get("relevance_basis") or "").strip():
                raise ValueError(
                    f"Задача-кандидат {issue['key']} должна иметь relevance_basis"
                )
        history = issue.get("history", [])
        if not isinstance(history, list) or not all(isinstance(event, dict) for event in history):
            raise ValueError(f"history задачи {issue['key']} должен быть списком объектов")
        for event in history:
            if not isinstance(event.get("field"), str) or not event["field"].strip():
                raise ValueError(f"Событие history задачи {issue['key']} должно иметь field")
            if timestamp_value(event.get("at")) is None:
                raise ValueError(f"Событие history задачи {issue['key']} должно иметь корректный at")
    if payload["collection"]["epic_neighbors"]["state"] == "complete":
        discovered_epics = issue_group_keys(issues, "epic")
        expanded_epics = set(payload["collection"].get("expanded_epic_keys", []))
        missing = sorted(discovered_epics - expanded_epics)
        if missing:
            raise ValueError(
                f"Снимок {provider} объявил epic_neighbors=complete, но не расширил эпики: "
                + ", ".join(missing)
            )
    return payload


def history_fingerprint(event: dict, provider: str, config: dict) -> str:
    stable = {key: event.get(key) for key in ("at", "field", "from", "to")}
    if event.get("field") == "assignee":
        stable["from"] = canonical_participant(event.get("from"), provider, config)
        stable["to"] = canonical_participant(event.get("to"), provider, config)
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_history(sber_issue: dict, jira_issue: dict | None, config: dict) -> list[dict]:
    events: dict[str, dict] = {}
    for provider, issue in (("sbertrek", sber_issue), ("jira", jira_issue)):
        if not issue:
            continue
        for event in issue.get("history", []):
            fingerprint = history_fingerprint(event, provider, config)
            if fingerprint in events:
                if provider not in events[fingerprint]["sources"]:
                    events[fingerprint]["sources"].append(provider)
                continue
            merged = {key: value for key, value in event.items() if key != "provider"}
            merged["sources"] = [provider]
            events[fingerprint] = merged
    return sorted(events.values(), key=lambda event: timestamp_value(event["at"]))


def role_of(value: Any, providers: list[str], config: dict) -> str:
    if not isinstance(value, dict):
        return "unknown"
    account = value.get("id")
    if not isinstance(account, str) or not account:
        return "unknown"
    roles = {
        config.get("participants", {}).get(provider, {}).get(account, {}).get("role")
        for provider in providers
    }
    roles.discard(None)
    if len(roles) != 1:
        return "unknown"
    role = roles.pop()
    return "developer" if role == "developer" else "non-developer"


def development_state(issue: dict, config: dict) -> dict:
    status = str(issue.get("status") or "").strip()
    normalized = status.casefold()
    if normalized in normalized_statuses(config, "excluded"):
        return {"state": "excluded", "reason": f"явный статус {status}"}
    if normalized in normalized_statuses(config, "completed"):
        return {"state": "completed-by-status", "reason": f"явный статус {status}"}
    development_types = {
        str(value).strip().casefold()
        for value in config.get("development_issue_types", [])
        if str(value).strip()
    }
    if str(issue.get("issue_type") or "").strip().casefold() not in development_types:
        return {
            "state": "not-inferred",
            "reason": "тип объекта не настроен как единица разработки",
        }

    current_source = issue.get("field_sources", {}).get("assignee")
    current_role = role_of(
        issue.get("assignee"),
        [current_source] if current_source in PROVIDERS else [],
        config,
    )
    if current_role == "developer":
        return {"state": "in-development", "reason": "текущий исполнитель является разработчиком"}

    last_to_developer = -1
    last_handoff = -1
    last_known_role = "unknown"
    for index, event in enumerate(issue.get("history", [])):
        if event.get("field") != "assignee":
            continue
        sources = [provider for provider in event.get("sources", []) if provider in PROVIDERS]
        from_role = role_of(event.get("from"), sources, config)
        to_role = role_of(event.get("to"), sources, config)
        if to_role != "unknown":
            last_known_role = to_role
        if to_role == "developer":
            last_to_developer = index
        if from_role == "developer" and to_role == "non-developer":
            last_handoff = index

    effective_role = current_role if current_role != "unknown" else last_known_role
    if effective_role == "non-developer" and last_handoff > last_to_developer:
        return {
            "state": "development-completed-handoff",
            "reason": "история подтверждает последнее переназначение с разработчика на другого исполнителя",
        }
    return {
        "state": "unknown",
        "reason": "нет достаточной истории и классификации исполнителей",
    }


def paired_jira_key(sber_issue: dict, jira_by_key: dict[str, dict], config: dict) -> str | None:
    explicit = config.get("issue_pairs", {}).get(sber_issue["key"])
    if explicit:
        return explicit
    counterpart = sber_issue.get("counterpart_key")
    if isinstance(counterpart, str) and counterpart:
        return counterpart
    for key, issue in jira_by_key.items():
        if issue.get("counterpart_key") == sber_issue["key"]:
            return key
    return sber_issue["key"] if sber_issue["key"] in jira_by_key else None


def development_types(config: dict) -> set[str]:
    return {
        str(value).strip().casefold()
        for value in config.get("development_issue_types", [])
        if str(value).strip()
    }


def participant_values(issue: dict) -> list[dict]:
    values: list[dict] = []
    if isinstance(issue.get("assignee"), dict):
        values.append(issue["assignee"])
    for event in issue.get("history", []):
        if event.get("field") != "assignee":
            continue
        for field in ("from", "to"):
            if isinstance(event.get(field), dict):
                values.append(event[field])
    return values


def first_unknown_participant(snapshots: list[tuple[str, dict]], config: dict) -> dict | None:
    configured_types = development_types(config)
    terminal_statuses = normalized_statuses(config, "completed") | normalized_statuses(config, "excluded")
    unknown: dict[tuple[str, str], dict] = {}
    for provider, snapshot in snapshots:
        mapping = config.get("participants", {}).get(provider, {})
        for issue in snapshot.get("issues", []):
            if str(issue.get("issue_type") or "").strip().casefold() not in configured_types:
                continue
            if str(issue.get("status") or "").strip().casefold() in terminal_statuses:
                continue
            for value in participant_values(issue):
                account = value.get("id")
                if isinstance(account, str) and account and account not in mapping:
                    unknown[(provider, account)] = {
                        "provider": provider,
                        "account_id": account,
                        "name": value.get("name"),
                    }
    return unknown[sorted(unknown)[0]] if unknown else None


def issue_group_keys(issues: list[dict], field: str) -> set[str]:
    values: set[str] = set()
    for issue in issues:
        raw = issue.get(field)
        items = raw if field == "releases" and isinstance(raw, list) else [raw]
        for item in items:
            label = group_label(item)
            if label:
                values.add(label)
    return values


def release_proposals(issues: list[dict], known_keys: set[str]) -> list[dict]:
    proposals: dict[str, dict] = {}
    for issue in issues:
        for release in issue.get("releases") or []:
            key = group_label(release)
            if key and key not in known_keys:
                proposals.setdefault(key, release if isinstance(release, dict) else {"key": key})
    return [proposals[key] for key in sorted(proposals)]


def collection_limitations(provider: str, snapshot: dict) -> list[str]:
    collection = snapshot["collection"]
    limitations = [
        f"{provider}-{capability}-unavailable"
        for capability in COLLECTION_CAPABILITIES
        if collection[capability]["state"] == "unavailable"
    ]
    if collection["history"]["state"] == "complete" and snapshot["issues"]:
        if not any(issue.get("history") for issue in snapshot["issues"]):
            limitations.append(f"{provider}-history-returned-no-events")
    epics = issue_group_keys(snapshot["issues"], "epic")
    releases = issue_group_keys(snapshot["issues"], "releases")
    for key in snapshot["scope"].get("expected_epic_keys", []):
        if key not in epics:
            limitations.append(f"{provider}-expected-epic-not-resolved:{key}")
    for key in snapshot["scope"].get("expected_release_keys", []):
        if key not in releases:
            limitations.append(f"{provider}-expected-release-not-resolved:{key}")
    return limitations


def validate_counterpart_lookup(sber_snapshot: dict, jira_snapshot: dict | None, config: dict) -> None:
    if jira_snapshot is None:
        return
    collection = jira_snapshot["collection"]
    if collection["counterpart_lookup"]["state"] != "complete":
        return
    jira_keys = {issue["key"] for issue in jira_snapshot["issues"]}
    not_found = set(collection.get("not_found_keys", []))
    for issue in sber_snapshot["issues"]:
        expected = paired_jira_key(issue, {}, config)
        if expected and expected not in jira_keys and expected not in not_found:
            raise ValueError(
                f"Прямая Jira-пара {expected} для {issue['key']} не прочитана и не указана в "
                "collection.not_found_keys"
            )


def validate_snapshot_scope(snapshot: dict, provider: str, config: dict) -> None:
    configured = set(config.get("projects", {}).get(provider, []))
    actual = set(snapshot.get("scope", {}).get("projects", []))
    if not actual:
        raise ValueError(f"scope.projects снимка {provider} не должен быть пустым")
    unexpected = sorted(actual - configured)
    if unexpected:
        raise ValueError(
            f"Снимок {provider} содержит ненастроенные проекты: {', '.join(unexpected)}"
        )


def normalize_jira_only_issue(issue: dict, config: dict) -> dict:
    normalized = {field: canonical_field(field, issue.get(field)) for field in MERGED_FIELDS}
    normalized["key"] = issue["key"]
    normalized["field_sources"] = {
        field: "jira" if present(normalized.get(field)) else None
        for field in MERGED_FIELDS
    }
    normalized["history"] = merge_history({}, issue, config)
    normalized["development_state"] = development_state(normalized, config)
    return normalized


def reconcile(sber_snapshot: dict, jira_snapshot: dict | None, config: dict) -> dict:
    jira_by_key = {issue["key"]: issue for issue in (jira_snapshot or {}).get("issues", [])}
    used_jira: set[str] = set()
    merged_issues: list[dict] = []
    discrepancies: list[dict] = []

    for sber_issue in sber_snapshot["issues"]:
        jira_key = paired_jira_key(sber_issue, jira_by_key, config)
        jira_issue = jira_by_key.get(jira_key) if jira_key else None
        if jira_issue:
            used_jira.add(jira_issue["key"])
        merged: dict[str, Any] = {
            "key": sber_issue["key"],
            "jira_key": jira_issue["key"] if jira_issue else None,
            "field_sources": {},
            "enriched_from_jira": [],
            "conflicting_fields": [],
        }
        for field in MERGED_FIELDS:
            primary = canonical_field(field, sber_issue.get(field))
            fallback = canonical_field(field, jira_issue.get(field)) if jira_issue else None
            if present(primary):
                merged[field] = primary
                merged["field_sources"][field] = "sbertrek"
                conflict = (
                    comparable_field(field, fallback, "jira", config)
                    != comparable_field(field, primary, "sbertrek", config)
                )
                if present(fallback) and conflict and field not in {"updated_at"}:
                    merged["conflicting_fields"].append(field)
                    discrepancies.append({
                        "kind": "field-conflict",
                        "key": sber_issue["key"],
                        "jira_key": jira_issue["key"],
                        "field": field,
                        "effective_source": "sbertrek",
                        "sbertrek_value": primary,
                        "jira_value": fallback,
                    })
            elif present(fallback):
                merged[field] = fallback
                merged["field_sources"][field] = "jira"
                merged["enriched_from_jira"].append(field)
            else:
                merged[field] = None
                merged["field_sources"][field] = None
        merged["history"] = merge_history(sber_issue, jira_issue, config)
        if jira_issue and jira_issue.get("history"):
            merged["enriched_from_jira"].append("history")
        if jira_snapshot is not None and not jira_issue:
            counterpart_state = jira_snapshot["collection"]["counterpart_lookup"]["state"]
            not_found = set(jira_snapshot["collection"].get("not_found_keys", []))
            if jira_key and counterpart_state == "complete" and jira_key in not_found:
                discrepancies.append({
                    "kind": "jira-pair-not-found",
                    "key": sber_issue["key"],
                    "expected_jira_key": jira_key,
                })
            elif jira_key:
                discrepancies.append({
                    "kind": "jira-pair-not-read",
                    "key": sber_issue["key"],
                    "expected_jira_key": jira_key,
                })
            else:
                discrepancies.append({"kind": "jira-pair-unmapped", "key": sber_issue["key"]})
        if jira_issue:
            sber_updated = timestamp_value(sber_issue.get("updated_at"))
            jira_updated = timestamp_value(jira_issue.get("updated_at"))
            if sber_updated and jira_updated and jira_updated > sber_updated:
                discrepancies.append({
                    "kind": "jira-newer",
                    "key": sber_issue["key"],
                    "jira_key": jira_issue["key"],
                    "effective_source": "sbertrek",
                    "sbertrek_updated_at": sber_issue["updated_at"],
                    "jira_updated_at": jira_issue["updated_at"],
                })
        merged["development_state"] = development_state(merged, config)
        merged_issues.append(merged)

    jira_only_issues = []
    for key in sorted(set(jira_by_key) - used_jira):
        issue = normalize_jira_only_issue(jira_by_key[key], config)
        jira_only_issues.append(issue)
        discrepancies.append({"kind": "jira-only", "jira_key": key})

    limitations = collection_limitations("sbertrek", sber_snapshot)
    if jira_snapshot is None:
        limitations.append("jira-unavailable" if config.get("jira_enabled") else "jira-disabled")
    else:
        limitations.extend(collection_limitations("jira", jira_snapshot))
    discrepancy_counts: dict[str, int] = {}
    for item in discrepancies:
        discrepancy_counts[item["kind"]] = discrepancy_counts.get(item["kind"], 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "primary_provider": "sbertrek",
        "jira_used": jira_snapshot is not None,
        "limitations": sorted(set(limitations)),
        "counts": {
            "sbertrek": len(sber_snapshot["issues"]),
            "jira": len((jira_snapshot or {}).get("issues", [])),
            "matched": len(used_jira),
            "discrepancies": len(discrepancies),
            "discrepancies_by_kind": dict(sorted(discrepancy_counts.items())),
        },
        "issues": merged_issues,
        "jira_only_issues": jira_only_issues,
        "release_proposals": release_proposals(
            merged_issues + jira_only_issues,
            set(sber_snapshot["scope"].get("expected_release_keys", [])),
        ),
        "discrepancies": discrepancies,
    }


def report_text(payload: dict) -> str:
    counts = payload["counts"]
    lines = [
        "# Сверка SberTrek и Jira",
        "",
        f"Сформировано: `{payload['generated_at']}`",
        "Основной источник: `SberTrek`.",
        f"Задач SberTrek: **{counts['sbertrek']}**.",
        f"Задач Jira: **{counts['jira']}**.",
        f"Сопоставлено пар: **{counts['matched']}**.",
        f"Расхождений: **{counts['discrepancies']}**.",
        "Ограничения полноты:",
    ]
    if payload["limitations"]:
        lines.extend(f"- `{item}`" for item in payload["limitations"])
    else:
        lines.append("- нет")
    lines.extend([
        "",
        "| SberTrek | Jira | Название | Найдена через | Отношение к фиче | Эпик | Релизы | Состояние разработки | Дообогащение | Конфликты |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ])
    for issue in payload["issues"]:
        enriched = ", ".join(issue["enriched_from_jira"]) or "—"
        conflicts = ", ".join(issue["conflicting_fields"]) or "—"
        epic = markdown_cell(group_label(issue.get("epic")) or "Без эпика")
        releases = markdown_cell(", ".join(group_label(item) for item in (issue.get("releases") or [])) or "Без релиза")
        lines.append(
            f"| {markdown_cell(issue['key'])} | {markdown_cell(issue.get('jira_key') or '—')} | "
            f"{markdown_cell(issue.get('summary') or '—')} | "
            f"{issue.get('discovery') or 'seed'} | {issue.get('feature_relevance') or 'known'} | {epic} | {releases} | "
            f"{issue['development_state']['state']} | {enriched} | {conflicts} |"
        )
    append_grouping(lines, payload["issues"], "Эпики", lambda issue: [group_label(issue.get("epic")) or "Без эпика"])
    append_grouping(
        lines,
        payload["issues"],
        "Релизы",
        lambda issue: [group_label(item) for item in (issue.get("releases") or [])] or ["Без релиза"],
    )
    if payload["jira_only_issues"]:
        lines.extend(["", "## Только в Jira", ""])
        for issue in payload["jira_only_issues"]:
            lines.append(
                f"- `{issue['key']}` — {issue.get('summary') or 'Без названия'} "
                f"(`{issue.get('discovery') or 'не указано'}`, "
                f"`{issue.get('feature_relevance') or 'не классифицировано'}`, "
                f"состояние `{issue['development_state']['state']}`)"
            )
    if payload["release_proposals"]:
        lines.extend(["", "## Релизы, отсутствующие в actual-progress", ""])
        for release in payload["release_proposals"]:
            lines.append(f"- **{group_label(release)}**: предложение для отдельного подтверждения")
    if payload["discrepancies"]:
        lines.extend(["", "## Расхождения", ""])
        for kind, count in payload["counts"]["discrepancies_by_kind"].items():
            lines.append(f"- `{kind}`: **{count}**")
        lines.extend(["", "### Детали", ""])
        for item in payload["discrepancies"]:
            lines.append(f"- `{json.dumps(item, ensure_ascii=False, sort_keys=True)}`")
    lines.extend([
        "",
        "Этот отчёт не изменяет требования, планы, задачи или данные трекеров.",
        "",
    ])
    return "\n".join(lines)


def group_label(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("key") or value.get("name") or "").strip()
    return ""


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def append_grouping(lines: list[str], issues: list[dict], title: str, labels) -> None:
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        for label in labels(issue):
            grouped.setdefault(label or f"Без {title.lower()[:-1]}", []).append(issue["key"])
    lines.extend(["", f"## {title}", ""])
    for label in sorted(grouped):
        lines.append(f"- **{label}**: {', '.join(sorted(grouped[label]))}")


def init_config(force: bool) -> int:
    path = config_path()
    if path.exists() and not force:
        raise ValueError(f"Настройка уже существует: {path}")
    save_json(path, DEFAULT_CONFIG)
    print(json.dumps({
        "status": "tracker-config-created",
        "path": str(path),
        "setup_complete": False,
        "next_action": "Задай аналитику один вопрос из config-status; begin пока заблокирован",
    }, ensure_ascii=False, indent=2))
    return 0


def migrate_legacy_config(config: Any) -> tuple[Any, bool]:
    if not isinstance(config, dict) or "setup_complete" in config:
        return config, False
    migrated = dict(config)
    migrated["setup_complete"] = False
    projects = migrated.get("projects", {})
    migrated["jira_enabled"] = True if isinstance(projects, dict) and projects.get("jira") else None
    looks_like_empty_v1 = (
        projects == {"sbertrek": [], "jira": []}
        and migrated.get("issue_pairs") == {}
        and migrated.get("development_issue_types") == ["development-task"]
        and migrated.get("participants") == {"sbertrek": {}, "jira": {}}
        and migrated.get("status_rules") == {
            "completed": [],
            "excluded": ["Отменена", "Удалена"],
        }
    )
    if looks_like_empty_v1:
        migrated["development_issue_types"] = []
        migrated["status_rules"] = {"completed": [], "excluded": []}
    return migrated, True


def load_config() -> dict:
    config, changed = migrate_legacy_config(load_json(config_path()))
    validated = validate_config(config)
    if changed:
        save_json(config_path(), validated)
    return validated


def config_status_command(_: argparse.Namespace) -> int:
    config = load_config()
    gaps = base_config_gaps(config)
    questions = {
        "projects.sbertrek": "Какие проекты SberTrek входят в область чтения?",
        "jira_enabled": "Jira доступна для дополнительного чтения на этой рабочей области?",
        "projects.jira": "Какие проекты Jira соответствуют выбранным проектам SberTrek?",
        "development_issue_types": "Какие типы объектов трекера являются единицами разработки?",
        "status_rules.completed": "Какие статусы однозначно означают завершение?",
        "status_rules.excluded": "Какие статусы исключают задачу из выполнения?",
        "setup_complete": "Подтверждаете сохранённую базовую настройку трекеров?",
    }
    print(json.dumps({
        "status": "tracker-config-ready" if not gaps else "tracker-config-incomplete",
        "path": str(config_path()),
        "gaps": gaps,
        "next_question": questions.get(gaps[0]) if gaps else None,
    }, ensure_ascii=False, indent=2))
    return 0


def set_projects_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["projects"][args.provider] = list(dict.fromkeys(args.projects))
    config["setup_complete"] = False
    save_json(config_path(), config)
    return config_status_command(args)


def set_jira_mode_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["jira_enabled"] = args.mode == "enabled"
    config["setup_complete"] = False
    save_json(config_path(), config)
    return config_status_command(args)


def set_issue_types_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["development_issue_types"] = list(dict.fromkeys(args.issue_types))
    config["setup_complete"] = False
    save_json(config_path(), config)
    return config_status_command(args)


def set_statuses_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["status_rules"][args.kind] = list(dict.fromkeys(args.statuses))
    config["setup_complete"] = False
    save_json(config_path(), config)
    return config_status_command(args)


def set_participant_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["participants"][args.provider][args.account_id] = {
        "canonical_id": args.canonical_id,
        "role": args.role,
    }
    validate_config(config)
    save_json(config_path(), config)
    print(json.dumps({
        "status": "tracker-participant-saved",
        "provider": args.provider,
        "account_id": args.account_id,
        "canonical_id": args.canonical_id,
        "role": args.role,
    }, ensure_ascii=False, indent=2))
    return 0


def complete_config_command(_: argparse.Namespace) -> int:
    config = load_config()
    gaps = base_config_gaps(config, require_confirmation=False)
    if gaps:
        raise ValueError("Нельзя завершить настройку; не заполнены: " + ", ".join(gaps))
    config["setup_complete"] = True
    save_json(config_path(), config)
    print(json.dumps({"status": "tracker-config-ready", "path": str(config_path())}, ensure_ascii=False, indent=2))
    return 0


def begin_command(_: argparse.Namespace) -> int:
    config = load_config()
    require_base_config(config)
    run_id = new_run_id()
    inputs = run_root(run_id) / "input"
    inputs.mkdir(parents=True, exist_ok=False)
    sber_path = inputs / "sbertrek.json"
    save_json(sber_path, snapshot_template("sbertrek", config))
    jira_path = inputs / "jira.json" if config.get("jira_enabled") else None
    if jira_path:
        save_json(jira_path, snapshot_template("jira", config))
    print(json.dumps({
        "status": "tracker-read-started",
        "run_id": run_id,
        "sbertrek_input": str(sber_path),
        "jira_input": str(jira_path) if jira_path else None,
        "input_state": "templates-created; replace pending collection states with complete or unavailable",
        "project_changed": False,
        "tracker_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    config = (
        validate_config(load_json(Path(args.config).resolve()))
        if args.config
        else load_config()
    )
    require_base_config(config)
    sber = validate_snapshot(load_json(Path(args.sbertrek).resolve()), "sbertrek")
    jira = validate_snapshot(load_json(Path(args.jira).resolve()), "jira") if args.jira else None
    if jira is not None and not config.get("jira_enabled"):
        raise ValueError("Jira отключена в tracker-config.json, но передан Jira-снимок")
    validate_snapshot_scope(sber, "sbertrek", config)
    if jira:
        validate_snapshot_scope(jira, "jira", config)
    validate_counterpart_lookup(sber, jira, config)
    unknown = first_unknown_participant(
        [("sbertrek", sber)] + ([("jira", jira)] if jira else []),
        config,
    )
    if unknown:
        raise ValueError(
            "Не настроена роль участника: "
            f"provider={unknown['provider']}, account_id={unknown['account_id']}, "
            f"name={unknown.get('name') or 'не указано'}. "
            "Задай аналитику один вопрос и сохрани ответ командой set-participant."
        )
    result = reconcile(sber, jira, config)
    run_id = args.run_id or new_run_id()
    output_root = run_root(run_id)
    result_path = output_root / "reconciled.json"
    report_path = output_root / "report.md"
    save_json(result_path, result)
    report_path.write_text(report_text(result), encoding="utf-8")
    print(json.dumps({
        "status": "tracker-read-reconciled",
        "run_id": run_id,
        "issue_count": len(result["issues"]),
        "discrepancy_count": len(result["discrepancies"]),
        "counts": result["counts"],
        "limitations": result["limitations"],
        "result": str(result_path),
        "report": str(report_path),
        "project_changed": False,
        "tracker_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Безопасная сверка прочитанных данных SberTrek и Jira")
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init-config")
    initialize.add_argument("--force", action="store_true")
    initialize.set_defaults(handler=lambda args: init_config(args.force))
    config_status = subparsers.add_parser("config-status")
    config_status.set_defaults(handler=config_status_command)
    set_projects = subparsers.add_parser("set-projects")
    set_projects.add_argument("--provider", choices=PROVIDERS, required=True)
    set_projects.add_argument("projects", nargs="+")
    set_projects.set_defaults(handler=set_projects_command)
    set_jira_mode = subparsers.add_parser("set-jira-mode")
    set_jira_mode.add_argument("mode", choices=("enabled", "disabled"))
    set_jira_mode.set_defaults(handler=set_jira_mode_command)
    set_issue_types = subparsers.add_parser("set-issue-types")
    set_issue_types.add_argument("issue_types", nargs="+")
    set_issue_types.set_defaults(handler=set_issue_types_command)
    set_statuses = subparsers.add_parser("set-statuses")
    set_statuses.add_argument("--kind", choices=("completed", "excluded"), required=True)
    set_statuses.add_argument("statuses", nargs="+")
    set_statuses.set_defaults(handler=set_statuses_command)
    set_participant = subparsers.add_parser("set-participant")
    set_participant.add_argument("--provider", choices=PROVIDERS, required=True)
    set_participant.add_argument("--account-id", required=True)
    set_participant.add_argument("--canonical-id", required=True)
    set_participant.add_argument("--role", choices=sorted(ROLES), required=True)
    set_participant.set_defaults(handler=set_participant_command)
    complete_config = subparsers.add_parser("complete-config")
    complete_config.set_defaults(handler=complete_config_command)
    begin = subparsers.add_parser("begin")
    begin.set_defaults(handler=begin_command)
    merge = subparsers.add_parser("reconcile")
    merge.add_argument("--sbertrek", required=True)
    merge.add_argument("--jira")
    merge.add_argument("--config")
    merge.add_argument("--run-id")
    merge.set_defaults(handler=reconcile_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
