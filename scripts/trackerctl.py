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


SCHEMA_VERSION = 4
CONFIG_SCHEMA_VERSION = 4
CONFIG_INCOMPLETE_EXIT = 3
PROVIDERS = ("sbertrek", "jira")
TEAM_ID_PATTERN = re.compile(r"^(AN|A|BE|B|FE|F|QA|Q|OTHER|O)([1-9][0-9]*)$", re.IGNORECASE)
TEAM_PREFIXES = {
    "AN": ("AN", "analyst"),
    "A": ("AN", "analyst"),
    "BE": ("BE", "developer"),
    "B": ("BE", "developer"),
    "FE": ("FE", "developer"),
    "F": ("FE", "developer"),
    "QA": ("QA", "tester"),
    "Q": ("QA", "tester"),
    "OTHER": ("OTHER", "other"),
    "O": ("OTHER", "other"),
}
COLLECTION_CAPABILITIES = (
    "history",
    "epic_links",
    "release_links",
    "cross_provider_lookup",
    "epic_neighbors",
)
COLLECTION_STATES = {"complete", "unavailable", "not-applicable"}
COLLECTION_FAILURE_KINDS = {"capability-absent", "call-failed", "permission-denied"}
OBSERVED_FIELDS = ("assignee", "estimate", "epic", "releases")
FIELD_OBSERVATION_STATES = {"value", "absent", "not-returned"}
SP_EQUIVALENT_UNITS = {
    "sp",
    "story point",
    "story points",
    "person day",
    "person days",
    "человеко день",
    "человеко дни",
    "человекодень",
    "человекодни",
    "чел день",
    "чел дни",
}
SKIPPED_COLLECTION_REASON = re.compile(
    r"(?:не\s+(?:вызван|вызывал|выполн|провер|прочитан|запрош)|not\s+(?:called|attempted|read)|skipped)",
    re.IGNORECASE,
)
DISCOVERY_VALUES = {"seed", "cross-provider-key", "epic-neighbor", "feature-search-candidate"}
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
    "schema_version": CONFIG_SCHEMA_VERSION,
    "primary_provider": "sbertrek",
    "setup_complete": False,
    "jira_enabled": None,
    "projects": {"sbertrek": [], "jira": []},
    "development_issue_types": [],
    "participants": {"sbertrek": {}, "jira": {}},
    "status_rules": {
        provider: {"completed": None, "excluded": None}
        for provider in PROVIDERS
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


def completion_status_path(run_id: str) -> Path:
    return run_root(run_id) / "completion-status.json"


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


def canonical_estimate(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    unit = normalized.get("unit")
    if isinstance(unit, str):
        comparable_unit = " ".join(
            unit.strip().casefold().replace("_", " ").replace("-", " ").split()
        )
        if comparable_unit in SP_EQUIVALENT_UNITS:
            normalized["unit"] = "story-points"
    return normalized


def canonical_field(field: str, value: Any) -> Any:
    if field == "estimate":
        return canonical_estimate(value)
    if field == "releases" and isinstance(value, list):
        unique = {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in value}
        return [unique[key] for key in sorted(unique)]
    return value


def canonical_participant(value: Any, provider: str, config: dict) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return value
    participant = config.get("participants", {}).get(provider, {}).get(value["id"], {})
    return participant.get("team_id") or value["id"]


def normalized_team_id(value: str) -> str:
    match = TEAM_ID_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(
            "Командный идентификатор должен иметь вид AN1/A1, BE2/B2, FE1/F1, "
            "QA1/Q1 или OTHER1/O1"
        )
    prefix, number = match.groups()
    canonical_prefix, _ = TEAM_PREFIXES[prefix.upper()]
    return f"{canonical_prefix}{number}"


def role_for_team_id(value: str) -> str:
    normalized = normalized_team_id(value)
    match = TEAM_ID_PATTERN.fullmatch(normalized)
    assert match is not None
    return TEAM_PREFIXES[match.group(1).upper()][1]


def comparable_field(field: str, value: Any, provider: str, config: dict) -> Any:
    if field == "assignee":
        return canonical_participant(value, provider, config)
    if field == "issue_type" and isinstance(value, str):
        return value.strip().casefold()
    return value


def normalized_statuses(config: dict, provider: str, name: str) -> set[str]:
    values = config.get("status_rules", {}).get(provider, {}).get(name)
    if values is None:
        return set()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"status_rules.{provider}.{name} должен быть списком строк")
    return {value.strip().casefold() for value in values if value.strip()}


def validate_config(payload: Any) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая схема tracker-config.json")
    if payload.get("primary_provider") != "sbertrek":
        raise ValueError("Основным трекером должен оставаться sbertrek")
    if "issue_pairs" in payload:
        raise ValueError(
            "issue_pairs не поддерживается: задачи SberTrek и Jira сопоставляются только по точному ключу"
        )
    if not isinstance(payload.get("setup_complete", False), bool):
        raise ValueError("setup_complete должен быть логическим значением")
    if payload.get("jira_enabled") not in {True, False, None}:
        raise ValueError("jira_enabled должен быть true, false или null")
    projects = payload.get("projects", {})
    for provider in PROVIDERS:
        values = projects.get(provider) if isinstance(projects, dict) else None
        if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"projects.{provider} должен быть списком непустых строк")
    issue_types = payload.get("development_issue_types", [])
    if not isinstance(issue_types, list) or not all(isinstance(value, str) for value in issue_types):
        raise ValueError("development_issue_types должен быть списком строк")
    participants = payload.get("participants", {})
    team_roles: dict[str, str] = {}
    for provider in PROVIDERS:
        mapping = participants.get(provider, {}) if isinstance(participants, dict) else None
        if not isinstance(mapping, dict):
            raise ValueError(f"participants.{provider} должен быть объектом")
        provider_accounts: dict[str, str] = {}
        for account, participant in mapping.items():
            if not isinstance(account, str) or not isinstance(participant, dict):
                raise ValueError(f"Некорректный участник participants.{provider}")
            team_id = participant.get("team_id")
            if not isinstance(team_id, str) or normalized_team_id(team_id) != team_id:
                raise ValueError(f"Не задан нормализованный team_id participants.{provider}.{account}")
            derived_role = role_for_team_id(team_id)
            previous_account = provider_accounts.setdefault(team_id, account)
            if previous_account != account:
                raise ValueError(
                    f"team_id {team_id} назначен нескольким аккаунтам {provider}: "
                    f"{previous_account}, {account}"
                )
            previous = team_roles.setdefault(team_id, derived_role)
            if previous != derived_role:
                raise ValueError(f"Противоречащая роль командного идентификатора {team_id}")
    status_rules = payload.get("status_rules", {})
    if not isinstance(status_rules, dict):
        raise ValueError("status_rules должен быть объектом")
    for provider in PROVIDERS:
        provider_rules = status_rules.get(provider)
        if not isinstance(provider_rules, dict):
            raise ValueError(f"status_rules.{provider} должен быть объектом")
        for name in ("completed", "excluded"):
            values = provider_rules.get(name)
            if values is not None and (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value.strip() for value in values)
            ):
                raise ValueError(
                    f"status_rules.{provider}.{name} должен быть списком непустых строк или null"
                )
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
    status_rules = config.get("status_rules", {})
    for provider in PROVIDERS:
        if provider == "jira" and not config.get("jira_enabled"):
            continue
        for name in ("completed", "excluded"):
            if status_rules.get(provider, {}).get(name) is None:
                gaps.append(f"status_rules.{provider}.{name}")
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


def collection_template(provider: str, jira_enabled: bool) -> dict:
    return {
        capability: {
            "state": (
                "not-applicable"
                if capability == "cross_provider_lookup" and not jira_enabled
                else "pending"
            ),
            "reason": None,
            "failure_kind": None,
            "evidence": [],
            "checked_keys": [],
        }
        for capability in COLLECTION_CAPABILITIES
    } | {"not_found_keys": [], "not_found_evidence": [], "expanded_epic_keys": []}


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
        "collection": collection_template(provider, bool(config.get("jira_enabled"))),
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
        evidence = item.get("evidence", [])
        if item["state"] != "not-applicable":
            validate_string_list(evidence, f"collection.{capability}.evidence снимка {provider}")
            if not evidence:
                raise ValueError(
                    f"collection.{capability}={item['state']} снимка {provider} требует доказательство MCP-вызова"
                )
        failure_kind = item.get("failure_kind")
        if item["state"] == "unavailable" and failure_kind not in COLLECTION_FAILURE_KINDS:
            raise ValueError(
                f"collection.{capability}=unavailable снимка {provider} требует failure_kind"
            )
        if item["state"] != "unavailable" and failure_kind is not None:
            raise ValueError(
                f"collection.{capability} снимка {provider} не должна содержать failure_kind"
            )
        validate_string_list(
            item.get("checked_keys", []),
            f"collection.{capability}.checked_keys снимка {provider}",
        )
        if capability in {"history", "epic_links", "release_links", "epic_neighbors"} and item["state"] == "not-applicable":
            raise ValueError(f"collection.{capability} не может быть not-applicable для {provider}")
    validate_string_list(payload.get("not_found_keys", []), f"collection.not_found_keys снимка {provider}")
    not_found_evidence = payload.get("not_found_evidence", [])
    if not isinstance(not_found_evidence, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and item["key"].strip()
        and isinstance(item.get("evidence"), str)
        and item["evidence"].strip()
        for item in not_found_evidence
    ):
        raise ValueError(
            f"collection.not_found_evidence снимка {provider} должен содержать key и evidence"
        )
    evidence_keys = {item["key"] for item in not_found_evidence}
    missing_not_found_evidence = sorted(set(payload.get("not_found_keys", [])) - evidence_keys)
    if missing_not_found_evidence:
        raise ValueError(
            f"Отсутствующие ключи снимка {provider} не имеют доказательства прямого чтения: "
            + ", ".join(missing_not_found_evidence)
        )
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
        observations = issue.get("field_observations")
        if not isinstance(observations, dict) or set(observations) != set(OBSERVED_FIELDS):
            raise ValueError(
                f"Задача {issue['key']} должна явно описывать field_observations: "
                + ", ".join(OBSERVED_FIELDS)
            )
        for field in OBSERVED_FIELDS:
            state = observations.get(field)
            if state not in FIELD_OBSERVATION_STATES:
                raise ValueError(
                    f"Некорректное наблюдение {field} задачи {issue['key']}: {state}"
                )
            field_value = issue.get(field)
            has_value = present(field_value)
            if state == "value" and not has_value:
                raise ValueError(
                    f"Задача {issue['key']} объявила {field}=value без значения"
                )
            if state != "value" and has_value:
                raise ValueError(
                    f"Задача {issue['key']} содержит {field}, но observation={state}"
                )
        history = issue.get("history", [])
        if not isinstance(history, list) or not all(isinstance(event, dict) for event in history):
            raise ValueError(f"history задачи {issue['key']} должен быть списком объектов")
        for event in history:
            if not isinstance(event.get("field"), str) or not event["field"].strip():
                raise ValueError(f"Событие history задачи {issue['key']} должно иметь field")
            if timestamp_value(event.get("at")) is None:
                raise ValueError(f"Событие history задачи {issue['key']} должно иметь корректный at")
    not_found = set(payload["collection"].get("not_found_keys", []))
    both_found_and_missing = sorted(seen & not_found)
    if both_found_and_missing:
        raise ValueError(
            f"Ключи снимка {provider} одновременно найдены и отмечены отсутствующими: "
            + ", ".join(both_found_and_missing)
        )
    unresolved_seeds = sorted(set(scope["seed_keys"]) - seen - not_found)
    if unresolved_seeds:
        raise ValueError(
            f"Seed-ключи снимка {provider} не прочитаны и не отмечены not-found: "
            + ", ".join(unresolved_seeds)
        )
    if payload["collection"]["history"]["state"] == "complete":
        checked_history = set(payload["collection"]["history"].get("checked_keys", []))
        unchecked_history = sorted(seen - checked_history)
        if unchecked_history:
            raise ValueError(
                f"Снимок {provider} объявил history=complete без проверки задач: "
                + ", ".join(unchecked_history)
            )
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
        role_for_team_id(config.get("participants", {}).get(provider, {}).get(account, {}).get("team_id"))
        for provider in providers
        if config.get("participants", {}).get(provider, {}).get(account, {}).get("team_id")
    }
    roles.discard(None)
    if len(roles) != 1:
        return "unknown"
    role = roles.pop()
    return "developer" if role == "developer" else "non-developer"


def development_state(issue: dict, config: dict) -> dict:
    status = str(issue.get("status") or "").strip()
    normalized = status.casefold()
    status_provider = issue.get("field_sources", {}).get("status")
    if status_provider not in PROVIDERS:
        status_provider = "sbertrek"
    if normalized in normalized_statuses(config, status_provider, "excluded"):
        return {"state": "excluded", "reason": f"явный статус {status}"}
    if normalized in normalized_statuses(config, status_provider, "completed"):
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
    unknown: dict[tuple[str, str], dict] = {}
    for provider, snapshot in snapshots:
        mapping = config.get("participants", {}).get(provider, {})
        for issue in snapshot.get("issues", []):
            if str(issue.get("issue_type") or "").strip().casefold() not in configured_types:
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


def participant_question(participant: dict) -> str:
    return (
        "Какой командный идентификатор из planning/team.md соответствует участнику "
        f"{participant.get('name') or 'имя не указано'} "
        f"({participant['provider']}, account_id={participant['account_id']})?"
    )


def pending_participant_path(run_id: str) -> Path:
    return run_root(run_id) / "pending-participant.json"


def save_pending_participant(run_id: str, participant: dict) -> dict:
    pending = {
        "run_id": run_id,
        "provider": participant["provider"],
        "account_id": participant["account_id"],
        "name": participant.get("name"),
        "next_question": participant_question(participant),
    }
    save_json(pending_participant_path(run_id), pending)
    return pending


def run_snapshots(run_id: str) -> list[tuple[str, dict]]:
    snapshots = [("sbertrek", load_run_snapshot(run_id, "sbertrek")[1])]
    if (run_root(run_id) / "input" / "jira.json").is_file():
        snapshots.append(("jira", load_run_snapshot(run_id, "jira")[1]))
    return snapshots


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
    for field in OBSERVED_FIELDS:
        missing_count = sum(
            1
            for issue in snapshot["issues"]
            if issue.get("field_observations", {}).get(field) == "not-returned"
        )
        if missing_count:
            limitations.append(f"{provider}-{field}-not-returned:{missing_count}")
    epics = issue_group_keys(snapshot["issues"], "epic")
    releases = issue_group_keys(snapshot["issues"], "releases")
    for key in snapshot["scope"].get("expected_epic_keys", []):
        if key not in epics:
            limitations.append(f"{provider}-expected-epic-not-resolved:{key}")
    for key in snapshot["scope"].get("expected_release_keys", []):
        if key not in releases:
            limitations.append(f"{provider}-expected-release-not-resolved:{key}")
    return limitations


def validate_cross_provider_lookup(sber_snapshot: dict, jira_snapshot: dict | None) -> None:
    if jira_snapshot is None:
        return
    snapshots = {"sbertrek": sber_snapshot, "jira": jira_snapshot}
    all_keys = {
        issue["key"]
        for snapshot in snapshots.values()
        for issue in snapshot["issues"]
    }
    for provider, snapshot in snapshots.items():
        lookup = snapshot["collection"]["cross_provider_lookup"]
        if lookup["state"] == "not-applicable":
            raise ValueError(
                f"{provider}: cross_provider_lookup не может быть not-applicable при включённой Jira"
            )
        if lookup["state"] != "complete":
            continue
        found = {issue["key"] for issue in snapshot["issues"]}
        not_found = set(snapshot["collection"].get("not_found_keys", []))
        checked = set(lookup.get("checked_keys", []))
        unchecked = sorted(all_keys - checked)
        unresolved = sorted(all_keys - found - not_found)
        if unchecked:
            raise ValueError(
                f"{provider}: точная проверка ключей не выполнена для: "
                + ", ".join(unchecked)
            )
        if unresolved:
            raise ValueError(
                f"{provider}: ключи не найдены и не подтверждены как not-found: "
                + ", ".join(unresolved)
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
        jira_issue = jira_by_key.get(sber_issue["key"])
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
            not_found = set(jira_snapshot["collection"].get("not_found_keys", []))
            discrepancies.append({
                "kind": (
                    "sbertrek-only"
                    if sber_issue["key"] in not_found
                    else "cross-provider-key-not-read"
                ),
                "key": sber_issue["key"],
                "missing_provider": "jira",
            })
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
    sber_not_found = set(sber_snapshot["collection"].get("not_found_keys", []))
    for key in sorted(set(jira_by_key) - used_jira):
        issue = normalize_jira_only_issue(jira_by_key[key], config)
        jira_only_issues.append(issue)
        discrepancies.append({
            "kind": "jira-only" if key in sber_not_found else "cross-provider-key-not-read",
            "key": key,
            "missing_provider": "sbertrek",
        })

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
        "merge_policy": {
            "matching": "exact-key-only",
            "field_priority": {
                "epic": ["sbertrek", "jira"],
                "assignee": ["sbertrek", "jira"],
                "estimate": ["sbertrek", "jira"],
            },
            "estimate_unit": "story-points",
            "story_point_person_day_ratio": 1,
        },
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
        "| SberTrek | Jira | Название | Найдена через | Отношение к фиче | Исполнитель | Оценка, SP | Эпик | Релизы | Состояние разработки | Дообогащение | Конфликты |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for issue in payload["issues"]:
        enriched = ", ".join(issue["enriched_from_jira"]) or "—"
        conflicts = ", ".join(issue["conflicting_fields"]) or "—"
        assignee = markdown_cell(participant_label(issue.get("assignee")) or "Не назначен")
        estimate = markdown_cell(estimate_label(issue.get("estimate")) or "Нет оценки")
        epic = markdown_cell(group_label(issue.get("epic")) or "Без эпика")
        releases = markdown_cell(", ".join(group_label(item) for item in (issue.get("releases") or [])) or "Без релиза")
        lines.append(
            f"| {markdown_cell(issue['key'])} | {markdown_cell(issue.get('jira_key') or '—')} | "
            f"{markdown_cell(issue.get('summary') or '—')} | "
            f"{issue.get('discovery') or 'seed'} | {issue.get('feature_relevance') or 'known'} | "
            f"{assignee} | {estimate} | {epic} | {releases} | "
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


def participant_label(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    name = str(value.get("name") or "").strip()
    account = str(value.get("id") or "").strip()
    if name and account:
        return f"{name} ({account})"
    return name or account


def estimate_label(value: Any) -> str:
    if not isinstance(value, dict) or not present(value.get("value")):
        return ""
    unit = str(value.get("unit") or "").strip()
    suffix = "SP" if unit == "story-points" else unit
    return f"{value['value']} {suffix}".strip()


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
    if not isinstance(config, dict) or config.get("schema_version") not in {1, 2, 3}:
        return config, False
    if config.get("schema_version") == 3:
        migrated = dict(config)
        migrated["schema_version"] = CONFIG_SCHEMA_VERSION
        migrated.pop("issue_pairs", None)
        return migrated, True
    if config.get("schema_version") == 2:
        migrated = dict(config)
        migrated["schema_version"] = CONFIG_SCHEMA_VERSION
        migrated.pop("issue_pairs", None)
        # Version 2 allowed guessed many-to-one mappings. Recollect them through guarded runs.
        migrated["participants"] = {"sbertrek": {}, "jira": {}}
        return migrated, True
    projects = config.get("projects", {})
    looks_like_empty_v1 = (
        projects == {"sbertrek": [], "jira": []}
        and config.get("issue_pairs") == {}
        and config.get("development_issue_types") == ["development-task"]
        and config.get("participants") == {"sbertrek": {}, "jira": {}}
        and config.get("status_rules") == {
            "completed": [],
            "excluded": ["Отменена", "Удалена"],
        }
    )
    jira_enabled = config.get("jira_enabled")
    if jira_enabled not in {True, False}:
        jira_enabled = True if isinstance(projects, dict) and projects.get("jira") else None
    migrated = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "primary_provider": "sbertrek",
        "setup_complete": False,
        "jira_enabled": jira_enabled,
        "projects": projects if isinstance(projects, dict) else {"sbertrek": [], "jira": []},
        "development_issue_types": (
            [] if looks_like_empty_v1 else config.get("development_issue_types", [])
        ),
        # Legacy participant entries used free-form ids and roles; recollect them safely.
        "participants": {"sbertrek": {}, "jira": {}},
        "status_rules": {
            provider: {"completed": None, "excluded": None}
            for provider in PROVIDERS
        },
    }
    return migrated, True


def load_config() -> dict:
    config, changed = migrate_legacy_config(load_json(config_path()))
    validated = validate_config(config)
    if changed:
        save_json(config_path(), validated)
    return validated


def config_status_payload(config: dict) -> dict:
    gaps = base_config_gaps(config)
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
    must_stop = bool(gaps)
    next_question = questions.get(gaps[0]) if gaps else None
    return {
        "status": "tracker-config-ready" if not gaps else "tracker-config-incomplete",
        "path": str(config_path()),
        "gaps": gaps,
        "next_question": next_question,
        "must_stop": must_stop,
        "allowed_next_action": "ask-user" if must_stop else "begin",
        "response_contract": (
            {
                "type": "exact-single-question",
                "text": next_question,
                "additional_text_forbidden": True,
                "examples_forbidden": True,
            }
            if must_stop
            else None
        ),
    }


def print_config_status(config: dict) -> None:
    print(json.dumps(config_status_payload(config), ensure_ascii=False, indent=2))


def config_status_command(_: argparse.Namespace) -> int:
    config = load_config()
    print_config_status(config)
    return CONFIG_INCOMPLETE_EXIT if base_config_gaps(config) else 0


def set_projects_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["projects"][args.provider] = list(dict.fromkeys(args.projects))
    config["setup_complete"] = False
    save_json(config_path(), config)
    print_config_status(config)
    return 0


def set_jira_mode_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["jira_enabled"] = args.mode == "enabled"
    config["setup_complete"] = False
    save_json(config_path(), config)
    print_config_status(config)
    return 0


def set_issue_types_command(args: argparse.Namespace) -> int:
    config = load_config()
    config["development_issue_types"] = list(dict.fromkeys(args.issue_types))
    config["setup_complete"] = False
    save_json(config_path(), config)
    print_config_status(config)
    return 0


def set_statuses_command(args: argparse.Namespace) -> int:
    config = load_config()
    if args.none and args.statuses:
        raise ValueError("--none нельзя использовать одновременно со списком статусов")
    if not args.none and not args.statuses:
        raise ValueError("Укажи статусы либо используй --none")
    config["status_rules"][args.provider][args.kind] = (
        [] if args.none else list(dict.fromkeys(args.statuses))
    )
    config["setup_complete"] = False
    save_json(config_path(), config)
    print_config_status(config)
    return 0


def set_participant_command(args: argparse.Namespace) -> int:
    config = load_config()
    pending_path = pending_participant_path(args.run_id)
    if not pending_path.is_file():
        raise ValueError(
            f"Для run_id={args.run_id} нет ожидающего вопроса об участнике; "
            "сначала выполни reconcile"
        )
    pending = load_json(pending_path)
    expected = (pending.get("provider"), pending.get("account_id"))
    actual = (args.provider, args.account_id)
    if actual != expected:
        raise ValueError(
            "Разрешено сохранить только участника из текущего ожидающего вопроса: "
            f"provider={expected[0]}, account_id={expected[1]}"
        )
    team_id = normalized_team_id(args.team_id)
    config["participants"][args.provider][args.account_id] = {"team_id": team_id}
    validate_config(config)
    save_json(config_path(), config)
    pending_path.unlink()
    next_unknown = first_unknown_participant(run_snapshots(args.run_id), config)
    payload = {
        "status": "tracker-participant-saved",
        "run_id": args.run_id,
        "provider": args.provider,
        "account_id": args.account_id,
        "team_id": team_id,
        "derived_role": role_for_team_id(team_id),
        "workflow_complete": False,
        "final_response_allowed": False,
    }
    if next_unknown:
        pending = save_pending_participant(args.run_id, next_unknown)
        payload.update({
            "must_stop": True,
            "allowed_next_action": "ask-user",
            "next_question": pending["next_question"],
            "response_contract": {
                "type": "exact-single-question",
                "text": pending["next_question"],
                "additional_text_forbidden": True,
                "examples_forbidden": True,
            },
        })
    else:
        payload.update({
            "must_stop": False,
            "allowed_next_action": "reconcile",
            "next_command": f"trackerctl.py reconcile --run-id {args.run_id}",
        })
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def run_snapshot_path(run_id: str, provider: str) -> Path:
    path = run_root(run_id) / "input" / f"{provider}.json"
    if not path.is_file():
        raise ValueError(f"Снимок {provider} не создан для run_id={run_id}")
    return path


def load_run_snapshot(run_id: str, provider: str) -> tuple[Path, dict]:
    path = run_snapshot_path(run_id, provider)
    payload = load_json(path)
    if not isinstance(payload, dict) or payload.get("provider") != provider:
        raise ValueError(f"Некорректный рабочий снимок {provider}")
    return path, payload


def parse_key_value(values: list[str], label: str) -> list[dict]:
    parsed: list[dict] = []
    for value in values:
        key, separator, detail = value.partition("=")
        if not separator or not key.strip() or not detail.strip():
            raise ValueError(f"{label} должен иметь формат KEY=VALUE: {value}")
        parsed.append({"key": key.strip(), "source": detail.strip()})
    return parsed


def snapshot_metadata_command(args: argparse.Namespace) -> int:
    if timestamp_value(args.captured_at) is None:
        raise ValueError("--captured-at должен содержать временную метку с часовым поясом")
    path, snapshot = load_run_snapshot(args.run_id, args.provider)
    evidence = parse_key_value(args.seed_evidence, "--seed-evidence")
    snapshot["captured_at"] = args.captured_at
    snapshot["scope"]["query"] = args.query
    snapshot["scope"]["seed_evidence"] = evidence
    snapshot["scope"]["seed_keys"] = list(dict.fromkeys(item["key"] for item in evidence))
    snapshot["scope"]["expected_epic_keys"] = list(dict.fromkeys(args.expected_epic))
    snapshot["scope"]["expected_release_keys"] = list(dict.fromkeys(args.expected_release))
    save_json(path, snapshot)
    print(json.dumps({
        "status": "tracker-snapshot-metadata-saved",
        "run_id": args.run_id,
        "provider": args.provider,
        "path": str(path),
        "final_response_allowed": False,
        "allowed_next_action": "record-issues-and-collection",
    }, ensure_ascii=False, indent=2))
    return 0


def parse_number(value: str) -> int | float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"Некорректная оценка: {value}") from exc
    return int(number) if number.is_integer() else number


def parse_release(value: str) -> dict:
    key, separator, name = value.partition("=")
    if not key.strip():
        raise ValueError("--release требует KEY или KEY=NAME")
    return {"key": key.strip(), "name": name.strip() if separator else key.strip()}


def snapshot_issue_command(args: argparse.Namespace) -> int:
    if timestamp_value(args.updated_at) is None:
        raise ValueError("--updated-at должен содержать временную метку с часовым поясом")
    if bool(args.assignee_id) != bool(args.assignee_name):
        raise ValueError("--assignee-id и --assignee-name задаются вместе")
    if bool(args.estimate_value) != bool(args.estimate_unit):
        raise ValueError("--estimate-value и --estimate-unit задаются вместе")
    if bool(args.epic_key) != bool(args.epic_name):
        raise ValueError("--epic-key и --epic-name задаются вместе")
    observed_values = {
        "assignee": bool(args.assignee_id),
        "estimate": bool(args.estimate_value),
        "epic": bool(args.epic_key),
        "releases": bool(args.release),
    }
    observed_states = {
        "assignee": args.assignee_state,
        "estimate": args.estimate_state,
        "epic": args.epic_state,
        "releases": args.releases_state,
    }
    for field, state in observed_states.items():
        if (state == "value") != observed_values[field]:
            raise ValueError(
                f"--{field.replace('_', '-')}-state={state} противоречит переданному значению"
            )
    path, snapshot = load_run_snapshot(args.run_id, args.provider)
    existing = next((item for item in snapshot["issues"] if item.get("key") == args.key), None)
    issue = {
        "key": args.key,
        "summary": args.summary,
        "description": args.description,
        "issue_type": args.issue_type,
        "status": args.status,
        "assignee": (
            {"id": args.assignee_id, "name": args.assignee_name}
            if args.assignee_id
            else None
        ),
        "estimate": (
            {"value": parse_number(args.estimate_value), "unit": args.estimate_unit}
            if args.estimate_value
            else None
        ),
        "epic": (
            {"key": args.epic_key, "name": args.epic_name}
            if args.epic_key
            else None
        ),
        "releases": [parse_release(value) for value in args.release],
        "field_observations": observed_states,
        "discovery": args.discovery,
        "updated_at": args.updated_at,
        "history": existing.get("history", []) if existing else [],
    }
    if args.feature_relevance:
        issue["feature_relevance"] = args.feature_relevance
    if args.relevance_basis:
        issue["relevance_basis"] = args.relevance_basis
    if existing:
        snapshot["issues"][snapshot["issues"].index(existing)] = issue
    else:
        snapshot["issues"].append(issue)
    save_json(path, snapshot)
    print(json.dumps({
        "status": "tracker-snapshot-issue-saved",
        "run_id": args.run_id,
        "provider": args.provider,
        "key": args.key,
        "issue_count": len(snapshot["issues"]),
        "final_response_allowed": False,
        "allowed_next_action": "continue-collection",
    }, ensure_ascii=False, indent=2))
    return 0


def participant_value(account_id: str | None, name: str | None) -> dict | None:
    if not account_id:
        return None
    return {"id": account_id, "name": name} if name else {"id": account_id}


def snapshot_history_command(args: argparse.Namespace) -> int:
    if timestamp_value(args.at) is None:
        raise ValueError("--at должен содержать временную метку с часовым поясом")
    if args.from_value and (args.from_id or args.from_name):
        raise ValueError("--from-value нельзя сочетать с --from-id/--from-name")
    if args.to_value and (args.to_id or args.to_name):
        raise ValueError("--to-value нельзя сочетать с --to-id/--to-name")
    if bool(args.from_id) != bool(args.from_name):
        raise ValueError("--from-id и --from-name задаются вместе")
    if bool(args.to_id) != bool(args.to_name):
        raise ValueError("--to-id и --to-name задаются вместе")
    path, snapshot = load_run_snapshot(args.run_id, args.provider)
    issue = next((item for item in snapshot["issues"] if item.get("key") == args.key), None)
    if issue is None:
        raise ValueError(f"Сначала зарегистрируй задачу {args.key}")
    event = {
        "at": args.at,
        "field": args.field,
        "from": args.from_value if args.from_value is not None else participant_value(args.from_id, args.from_name),
        "to": args.to_value if args.to_value is not None else participant_value(args.to_id, args.to_name),
    }
    if event not in issue["history"]:
        issue["history"].append(event)
    save_json(path, snapshot)
    print(json.dumps({
        "status": "tracker-snapshot-history-saved",
        "run_id": args.run_id,
        "provider": args.provider,
        "key": args.key,
        "history_count": len(issue["history"]),
        "final_response_allowed": False,
        "allowed_next_action": "continue-collection",
    }, ensure_ascii=False, indent=2))
    return 0


def snapshot_not_found_command(args: argparse.Namespace) -> int:
    path, snapshot = load_run_snapshot(args.run_id, args.provider)
    if any(issue.get("key") == args.key for issue in snapshot["issues"]):
        raise ValueError(f"Задача {args.key} уже записана как найденная")
    if args.key not in snapshot["collection"]["not_found_keys"]:
        snapshot["collection"]["not_found_keys"].append(args.key)
    evidence = {"key": args.key, "evidence": args.evidence}
    existing = next(
        (
            item
            for item in snapshot["collection"]["not_found_evidence"]
            if item.get("key") == args.key
        ),
        None,
    )
    if existing:
        snapshot["collection"]["not_found_evidence"].remove(existing)
    snapshot["collection"]["not_found_evidence"].append(evidence)
    save_json(path, snapshot)
    print(json.dumps({
        "status": "tracker-snapshot-not-found-saved",
        "run_id": args.run_id,
        "provider": args.provider,
        "key": args.key,
        "final_response_allowed": False,
        "allowed_next_action": "continue-collection",
    }, ensure_ascii=False, indent=2))
    return 0


def snapshot_collection_command(args: argparse.Namespace) -> int:
    if args.state == "unavailable" and not str(args.reason or "").strip():
        raise ValueError("state=unavailable требует --reason")
    if args.state == "unavailable" and SKIPPED_COLLECTION_REASON.search(args.reason):
        raise ValueError(
            "Пропущенный MCP-вызов нельзя обозначать как unavailable; выполни вызов или зафиксируй его ошибку"
        )
    if not args.evidence:
        raise ValueError("complete/unavailable требует хотя бы одно --evidence реального MCP-вызова")
    if args.state == "unavailable" and not args.failure_kind:
        raise ValueError("state=unavailable требует --failure-kind")
    if args.state == "complete" and args.failure_kind:
        raise ValueError("state=complete нельзя сочетать с --failure-kind")
    path, snapshot = load_run_snapshot(args.run_id, args.provider)
    snapshot["collection"][args.capability] = {
        "state": args.state,
        "reason": args.reason if args.state == "unavailable" else None,
        "failure_kind": args.failure_kind if args.state == "unavailable" else None,
        "evidence": list(dict.fromkeys(args.evidence)),
        "checked_keys": list(dict.fromkeys(args.checked_key)),
    }
    for key in args.expanded_epic_key:
        if key not in snapshot["collection"]["expanded_epic_keys"]:
            snapshot["collection"]["expanded_epic_keys"].append(key)
    save_json(path, snapshot)
    print(json.dumps({
        "status": "tracker-snapshot-collection-saved",
        "run_id": args.run_id,
        "provider": args.provider,
        "capability": args.capability,
        "state": args.state,
        "final_response_allowed": False,
        "allowed_next_action": "continue-collection-or-check-run",
    }, ensure_ascii=False, indent=2))
    return 0


def snapshot_progress(snapshot: dict, provider: str) -> dict:
    missing: list[str] = []
    if timestamp_value(snapshot.get("captured_at")) is None:
        missing.append("captured_at")
    if not str(snapshot.get("scope", {}).get("query") or "").strip():
        missing.append("scope.query")
    pending = [
        capability
        for capability in COLLECTION_CAPABILITIES
        if snapshot.get("collection", {}).get(capability, {}).get("state") == "pending"
    ]
    validation_error = None
    if not missing and not pending:
        try:
            validate_snapshot(snapshot, provider)
        except ValueError as exc:
            validation_error = str(exc)
    return {
        "provider": provider,
        "issue_count": len(snapshot.get("issues", [])),
        "missing_metadata": missing,
        "pending_capabilities": pending,
        "validation_error": validation_error,
        "ready": not missing and not pending and validation_error is None,
    }


def tracker_run_status_command(args: argparse.Namespace) -> int:
    completed = completion_status_path(args.run_id)
    if completed.is_file():
        print(json.dumps(load_json(completed), ensure_ascii=False, indent=2))
        return 0
    providers = ["sbertrek"]
    if (run_root(args.run_id) / "input" / "jira.json").is_file():
        providers.append("jira")
    snapshots = {
        provider: load_run_snapshot(args.run_id, provider)[1]
        for provider in providers
    }
    progress = [
        snapshot_progress(snapshots[provider], provider)
        for provider in providers
    ]
    cross_provider_validation_error = None
    if all(item["ready"] for item in progress) and "jira" in snapshots:
        try:
            validate_cross_provider_lookup(snapshots["sbertrek"], snapshots["jira"])
        except ValueError as exc:
            cross_provider_validation_error = str(exc)
    ready = (
        all(item["ready"] for item in progress)
        and cross_provider_validation_error is None
    )
    print(json.dumps({
        "status": "tracker-run-ready" if ready else "tracker-run-incomplete",
        "run_id": args.run_id,
        "snapshots": progress,
        "cross_provider_validation_error": cross_provider_validation_error,
        "workflow_complete": False,
        "final_response_allowed": False,
        "allowed_next_action": "reconcile" if ready else "complete-snapshots",
        "required_success_status": "tracker-read-reconciled",
    }, ensure_ascii=False, indent=2))
    return 0


def tracker_result_status_command(args: argparse.Namespace) -> int:
    path = completion_status_path(args.run_id)
    if not path.is_file():
        raise ValueError(
            f"run_id={args.run_id} ещё не имеет успешного результата reconcile"
        )
    print(json.dumps(load_json(path), ensure_ascii=False, indent=2))
    return 0


def complete_config_command(_: argparse.Namespace) -> int:
    config = load_config()
    gaps = base_config_gaps(config, require_confirmation=False)
    if gaps:
        raise ValueError("Нельзя завершить настройку; не заполнены: " + ", ".join(gaps))
    config["setup_complete"] = True
    save_json(config_path(), config)
    print_config_status(config)
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
        "workflow_complete": False,
        "final_response_allowed": False,
        "allowed_next_action": "collect-and-write-snapshots",
        "recording_commands": [
            "snapshot-metadata",
            "snapshot-issue",
            "snapshot-history",
            "snapshot-not-found",
            "snapshot-collection",
            "run-status",
        ],
        "required_completion": {
            "command": f"trackerctl.py reconcile --run-id {run_id}",
            "status": "tracker-read-reconciled",
            "run_id": run_id,
        },
        "project_changed": False,
        "tracker_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    if args.run_id and completion_status_path(args.run_id).is_file():
        print(json.dumps(load_json(completion_status_path(args.run_id)), ensure_ascii=False, indent=2))
        return 0
    config = (
        validate_config(load_json(Path(args.config).resolve()))
        if args.config
        else load_config()
    )
    require_base_config(config)
    if args.run_id:
        inputs = run_root(args.run_id) / "input"
        sber_path = Path(args.sbertrek).resolve() if args.sbertrek else inputs / "sbertrek.json"
        jira_path = Path(args.jira).resolve() if args.jira else inputs / "jira.json"
        if not jira_path.is_file():
            jira_path = None
    else:
        if not args.sbertrek:
            raise ValueError("reconcile требует --run-id либо --sbertrek")
        sber_path = Path(args.sbertrek).resolve()
        jira_path = Path(args.jira).resolve() if args.jira else None
    sber = validate_snapshot(load_json(sber_path), "sbertrek")
    jira = validate_snapshot(load_json(jira_path), "jira") if jira_path else None
    if jira is not None and not config.get("jira_enabled"):
        raise ValueError("Jira отключена в tracker-config.json, но передан Jira-снимок")
    validate_snapshot_scope(sber, "sbertrek", config)
    if jira:
        validate_snapshot_scope(jira, "jira", config)
    validate_cross_provider_lookup(sber, jira)
    unknown = first_unknown_participant(
        [("sbertrek", sber)] + ([("jira", jira)] if jira else []),
        config,
    )
    if unknown:
        if not args.run_id:
            raise ValueError(
                "Неизвестных участников можно уточнять только для запуска с --run-id"
            )
        save_pending_participant(args.run_id, unknown)
        raise ValueError(
            "Не задан team_id участника: "
            f"provider={unknown['provider']}, account_id={unknown['account_id']}, "
            f"name={unknown.get('name') or 'не указано'}. "
            "Задай аналитику один вопрос и сохрани ответ командой "
            f"set-participant --run-id {args.run_id} --provider {unknown['provider']} "
            f"--account-id {unknown['account_id']} --team-id <ID>."
        )
    if args.run_id:
        pending_participant_path(args.run_id).unlink(missing_ok=True)
    result = reconcile(sber, jira, config)
    run_id = args.run_id or new_run_id()
    output_root = run_root(run_id)
    result_path = output_root / "reconciled.json"
    report_path = output_root / "report.md"
    save_json(result_path, result)
    report_path.write_text(report_text(result), encoding="utf-8")
    completion = {
        "status": "tracker-read-reconciled",
        "run_id": run_id,
        "issue_count": len(result["issues"]),
        "discrepancy_count": len(result["discrepancies"]),
        "counts": result["counts"],
        "limitations": result["limitations"],
        "result": str(result_path),
        "report": str(report_path),
        "workflow_complete": True,
        "final_response_allowed": True,
        "allowed_next_action": "present-report",
        "project_changed": False,
        "tracker_changed": False,
    }
    save_json(completion_status_path(run_id), completion)
    print(json.dumps(completion, ensure_ascii=False, indent=2))
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
    set_statuses.add_argument("--provider", choices=PROVIDERS, required=True)
    set_statuses.add_argument("--kind", choices=("completed", "excluded"), required=True)
    set_statuses.add_argument("--none", action="store_true")
    set_statuses.add_argument("statuses", nargs="*")
    set_statuses.set_defaults(handler=set_statuses_command)
    set_participant = subparsers.add_parser("set-participant")
    set_participant.add_argument("--run-id", required=True)
    set_participant.add_argument("--provider", choices=PROVIDERS, required=True)
    set_participant.add_argument("--account-id", required=True)
    set_participant.add_argument("--team-id", required=True)
    set_participant.set_defaults(handler=set_participant_command)
    snapshot_metadata = subparsers.add_parser("snapshot-metadata")
    snapshot_metadata.add_argument("--run-id", required=True)
    snapshot_metadata.add_argument("--provider", choices=PROVIDERS, required=True)
    snapshot_metadata.add_argument("--captured-at", required=True)
    snapshot_metadata.add_argument("--query", required=True)
    snapshot_metadata.add_argument("--seed-evidence", action="append", default=[])
    snapshot_metadata.add_argument("--expected-epic", action="append", default=[])
    snapshot_metadata.add_argument("--expected-release", action="append", default=[])
    snapshot_metadata.set_defaults(handler=snapshot_metadata_command)
    snapshot_issue = subparsers.add_parser("snapshot-issue")
    snapshot_issue.add_argument("--run-id", required=True)
    snapshot_issue.add_argument("--provider", choices=PROVIDERS, required=True)
    snapshot_issue.add_argument("--key", required=True)
    snapshot_issue.add_argument("--summary", required=True)
    snapshot_issue.add_argument("--description", default="")
    snapshot_issue.add_argument("--issue-type", required=True)
    snapshot_issue.add_argument("--status", required=True)
    snapshot_issue.add_argument("--assignee-id")
    snapshot_issue.add_argument("--assignee-name")
    snapshot_issue.add_argument("--assignee-state", choices=sorted(FIELD_OBSERVATION_STATES), required=True)
    snapshot_issue.add_argument("--estimate-value")
    snapshot_issue.add_argument("--estimate-unit")
    snapshot_issue.add_argument("--estimate-state", choices=sorted(FIELD_OBSERVATION_STATES), required=True)
    snapshot_issue.add_argument("--epic-key")
    snapshot_issue.add_argument("--epic-name")
    snapshot_issue.add_argument("--epic-state", choices=sorted(FIELD_OBSERVATION_STATES), required=True)
    snapshot_issue.add_argument("--release", action="append", default=[])
    snapshot_issue.add_argument("--releases-state", choices=sorted(FIELD_OBSERVATION_STATES), required=True)
    snapshot_issue.add_argument("--discovery", choices=sorted(DISCOVERY_VALUES), required=True)
    snapshot_issue.add_argument("--feature-relevance", choices=sorted(RELEVANCE_VALUES))
    snapshot_issue.add_argument("--relevance-basis")
    snapshot_issue.add_argument("--updated-at", required=True)
    snapshot_issue.set_defaults(handler=snapshot_issue_command)
    snapshot_history = subparsers.add_parser("snapshot-history")
    snapshot_history.add_argument("--run-id", required=True)
    snapshot_history.add_argument("--provider", choices=PROVIDERS, required=True)
    snapshot_history.add_argument("--key", required=True)
    snapshot_history.add_argument("--at", required=True)
    snapshot_history.add_argument("--field", required=True)
    snapshot_history.add_argument("--from-id")
    snapshot_history.add_argument("--from-name")
    snapshot_history.add_argument("--from-value")
    snapshot_history.add_argument("--to-id")
    snapshot_history.add_argument("--to-name")
    snapshot_history.add_argument("--to-value")
    snapshot_history.set_defaults(handler=snapshot_history_command)
    snapshot_not_found = subparsers.add_parser("snapshot-not-found")
    snapshot_not_found.add_argument("--run-id", required=True)
    snapshot_not_found.add_argument("--provider", choices=PROVIDERS, required=True)
    snapshot_not_found.add_argument("--key", required=True)
    snapshot_not_found.add_argument("--evidence", required=True)
    snapshot_not_found.set_defaults(handler=snapshot_not_found_command)
    snapshot_collection = subparsers.add_parser("snapshot-collection")
    snapshot_collection.add_argument("--run-id", required=True)
    snapshot_collection.add_argument("--provider", choices=PROVIDERS, required=True)
    snapshot_collection.add_argument("--capability", choices=COLLECTION_CAPABILITIES, required=True)
    snapshot_collection.add_argument("--state", choices=("complete", "unavailable"), required=True)
    snapshot_collection.add_argument("--reason")
    snapshot_collection.add_argument("--failure-kind", choices=sorted(COLLECTION_FAILURE_KINDS))
    snapshot_collection.add_argument("--evidence", action="append", default=[])
    snapshot_collection.add_argument("--checked-key", action="append", default=[])
    snapshot_collection.add_argument("--expanded-epic-key", action="append", default=[])
    snapshot_collection.set_defaults(handler=snapshot_collection_command)
    run_status = subparsers.add_parser("run-status")
    run_status.add_argument("--run-id", required=True)
    run_status.set_defaults(handler=tracker_run_status_command)
    result_status = subparsers.add_parser("result-status")
    result_status.add_argument("--run-id", required=True)
    result_status.set_defaults(handler=tracker_result_status_command)
    complete_config = subparsers.add_parser("complete-config")
    complete_config.set_defaults(handler=complete_config_command)
    begin = subparsers.add_parser("begin")
    begin.set_defaults(handler=begin_command)
    merge = subparsers.add_parser("reconcile")
    merge.add_argument("--sbertrek")
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
        if args.command in {"begin", "reconcile"}:
            run_id = getattr(args, "run_id", None)
            allowed_next_action = (
                "run-config-status" if args.command == "begin" else "run-status-and-complete-snapshots"
            )
            next_question = None
            participant_match = re.search(
                r"provider=([^,]+), account_id=([^,]+), name=(.+?)\. Задай аналитику",
                str(exc),
            )
            if participant_match:
                allowed_next_action = "ask-user"
                provider, account_id, name = participant_match.groups()
                if run_id and pending_participant_path(run_id).is_file():
                    next_question = load_json(pending_participant_path(run_id)).get("next_question")
                if not next_question:
                    next_question = participant_question({
                        "provider": provider,
                        "account_id": account_id,
                        "name": name,
                    })
            payload = {
                "status": "tracker-read-blocked",
                "run_id": run_id,
                "error": str(exc),
                "must_stop": True,
                "workflow_complete": False,
                "final_response_allowed": False,
                "allowed_next_action": allowed_next_action,
                "required_success_status": "tracker-read-reconciled",
                "final_response_contract": (
                    "Do not present tracker facts, counts or a summary. Complete the allowed next action "
                    "and retry the guarded command."
                ),
            }
            if args.command == "reconcile" and run_id and not next_question:
                payload["next_command"] = f"trackerctl.py run-status --run-id {run_id}"
            if next_question:
                payload["next_question"] = next_question
                payload["response_contract"] = {
                    "type": "exact-single-question",
                    "text": next_question,
                    "additional_text_forbidden": True,
                    "examples_forbidden": True,
                }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
