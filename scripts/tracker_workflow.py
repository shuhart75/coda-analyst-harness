#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "direct-tracker-v1"
SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 4
STOP_EXIT = 3
MAX_RESULTS = 50
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
PROVIDERS = ("sbertrek", "jira")
SCOPE_KINDS = ("epic", "tasks")
RESPONSE_SOURCES = ("mcp-file", "inline-json-capture")
ROLES = ("AN", "BE", "FE", "QA")
RESOLUTION_CHOICES = ("sbertrek", "jira", "custom")
ACTIONABLE_CONFLICT_FIELDS = {"assignee", "estimate"}
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
MISSING_SENTINELS = {"not-returned", "not returned", "unknown", "none", "null", "-", "—"}
UNASSIGNED_SENTINELS = {"unassigned", "not assigned", "не назначен", "не назначено"}
ISSUE_COLLECTION_KEYS = ("issues", "items", "results", "values", "units", "data")
ISSUE_KEY_ALIASES = ("key", "issueKey", "issue_key", "unitKey", "unit_key")
SUMMARY_ALIASES = ("summary", "name", "title")
TYPE_ALIASES = ("issue_type", "issueType", "issuetype", "type", "suit")
STATUS_ALIASES = ("status", "state")
CREATED_ALIASES = ("created_at", "createdAt", "created", "creationDate", "creation_date")
UPDATED_ALIASES = ("updated_at", "updatedAt", "updated", "updateDate", "update_date")
ASSIGNEE_ALIASES = ("assignee", "assigned_to", "assignedTo")
ESTIMATE_ALIASES = ("estimate", "story_points", "storyPoints", "story_point", "customfield_10016")
EPIC_ALIASES = ("epic", "epic_key", "epicKey", "parent")
RELEASE_ALIASES = ("releases", "release", "fixVersions", "fix_versions", "fixversion")
SBER_ATTRIBUTE_CODES = {
    "assignee": ("assigned_to", "assignee"),
    "estimate": ("story_points", "story_point", "estimate"),
    "jira_key": ("issue_key",),
    "epic": ("epic", "epic_key", "parent"),
    "releases": ("release", "releases", "fix_versions", "fixversion"),
}
JIRA_ESTIMATE_FIELDS = {
    "customfield_15014": {"name": "Оценка разработки (Back-End)", "role": "BE", "unit": "person-days"},
    "customfield_15015": {"name": "Оценка разработки (Front-End)", "role": "FE", "unit": "person-days"},
    "customfield_15016": {"name": "Оценка дизайна", "role": None, "unit": "person-days"},
    "customfield_15053": {"name": "Нагрузочное тестирование (наиболее вероятная)", "role": None, "unit": "person-days"},
    "customfield_15062": {"name": "Оценка анализа (чел.д)", "role": "AN", "unit": "person-days"},
    "customfield_15063": {"name": "Оценка разработки", "role": None, "unit": "person-days", "general": True},
    "customfield_15064": {"name": "Оценка тестирования", "role": "QA", "unit": "person-days"},
    "customfield_15065": {"name": "Оценка отчетности", "role": None, "unit": "person-days"},
    "customfield_15066": {"name": "Оценка проектирования", "role": None, "unit": "person-days"},
    "customfield_20408": {"name": "Оценка (в спринтах)", "role": None, "unit": "sprints"},
    "customfield_14937": {"name": "Оценка", "role": None, "unit": "person-days", "general": True},
    "customfield_12307": {"name": "Разработка, ч/д", "role": None, "unit": "person-days", "general": True},
}
JIRA_GENERAL_ESTIMATE_PRIORITY = ("customfield_14937", "customfield_12307", "customfield_15063")
JIRA_FIELDS = (
    "key", "summary", "status", "issuetype", "priority", "assignee", "created", "updated",
    "fixVersions", *JIRA_ESTIMATE_FIELDS.keys(),
)
SBER_FIELDS = ("key", "summary", "suit", "status", "priority", "attributes", "epic", "created_at", "updated_at")
MERGED_FIELDS = ("summary", "issue_type", "status", "assignee", "estimate", "epic", "releases", "created_at", "updated_at")
DEFAULT_CONFIG = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "primary_provider": "sbertrek",
    "setup_complete": False,
    "jira_enabled": None,
    "projects": {"sbertrek": [], "jira": []},
    "development_issue_types": [],
    "participants": {"sbertrek": {}, "jira": {}},
    "status_rules": {provider: {"completed": None, "excluded": None} for provider in PROVIDERS},
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def state_root() -> Path:
    override = os.environ.get("ANALYST_HARNESS_STATE_ROOT")
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parents[1] / ".workspace-state"


def config_path() -> Path:
    return state_root() / "tracker-config.json"


def active_path() -> Path:
    return state_root() / "tracker-active-run.json"


def run_root(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Некорректный run_id")
    return state_root() / "tracker-runs" / run_id


def run_path(run_id: str) -> Path:
    return run_root(run_id) / "run.json"


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Файл не найден: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Повреждён JSON: {path}") from error


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_object(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest_bytes(raw)


def file_digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def normalize_key(value: str, label: str = "Ключ задачи") -> str:
    result = value.strip().upper()
    if not ISSUE_KEY.fullmatch(result):
        raise ValueError(f"{label} должен иметь вид PROJECT-123: {result}")
    return result


def unique_keys(values: list[str]) -> list[str]:
    return sorted(set(normalize_key(value) for value in values))


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
    if not isinstance(config.get("development_issue_types"), list):
        raise ValueError("development_issue_types должен быть списком")
    return config


def migrate_config(config: Any) -> tuple[dict, bool]:
    if not isinstance(config, dict):
        return config, False
    if config.get("schema_version") == CONFIG_SCHEMA_VERSION:
        migrated = dict(config)
        changed = "issue_pairs" in migrated
        migrated.pop("issue_pairs", None)
        return migrated, changed
    if config.get("schema_version") not in {1, 2, 3}:
        return config, False
    projects = config.get("projects") if isinstance(config.get("projects"), dict) else {"sbertrek": [], "jira": []}
    migrated = {
        **DEFAULT_CONFIG,
        "jira_enabled": config.get("jira_enabled") if config.get("jira_enabled") in {True, False} else bool(projects.get("jira")) or None,
        "projects": {provider: list(projects.get(provider, [])) for provider in PROVIDERS},
        "development_issue_types": list(config.get("development_issue_types", [])),
        "participants": config.get("participants", {"sbertrek": {}, "jira": {}}) if config.get("schema_version") == 3 else {"sbertrek": {}, "jira": {}},
        "status_rules": config.get("status_rules", DEFAULT_CONFIG["status_rules"]) if config.get("schema_version") == 3 else DEFAULT_CONFIG["status_rules"],
        "setup_complete": bool(config.get("setup_complete")) if config.get("schema_version") == 3 else False,
    }
    return migrated, True


def load_config() -> dict:
    config, changed = migrate_config(load_json(config_path()))
    config = validate_config(config)
    if changed:
        save_json(config_path(), config)
    return config


def config_gaps(config: dict, include_confirmation: bool = True) -> list[str]:
    gaps: list[str] = []
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
        **extra, "must_stop": True, "workflow_complete": False,
        "final_response_allowed": False, "allowed_next_action": "ask-user",
        "next_question": question,
        "response_contract": {"type": "exact-single-question", "text": question, "additional_text_forbidden": True},
    }


def config_status_payload(config: dict) -> dict:
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
    return {"status": "tracker-config-ready", "path": str(config_path()), "gaps": [], "must_stop": False, "allowed_next_action": "begin"}


def active_run_id() -> str | None:
    path = active_path()
    if not path.is_file():
        return None
    payload = load_json(path)
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Повреждён tracker-active-run.json")
    if (run_root(run_id) / "completion-status.json").is_file():
        path.unlink()
        return None
    return run_id


def release_active(run_id: str) -> None:
    if active_path().is_file() and active_run_id() == run_id:
        active_path().unlink()


def load_run(run_id: str) -> dict:
    payload = load_json(run_path(run_id))
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL or payload.get("run_id") != run_id:
        raise ValueError("Tracker-run создан другим протоколом или повреждён")
    return payload


def tql_epic(key: str) -> str:
    return f"unit IN linkedUnitsOf(\"unit = '{key}'\", \"Состоит из\")"


def tql_units(keys: list[str]) -> str:
    return " or ".join(f'unit = "{key}"' for key in keys)


def tql_jira_keys(keys: list[str]) -> str:
    return " or ".join(f'issue_key = "{key}"' for key in keys)


def jql_keys(keys: list[str]) -> str:
    return "key IN (" + ", ".join(f'"{key}"' for key in keys) + ")"


def jql_epic(key: str) -> str:
    return f'"Epic Link" = "{key}"'


def make_step(provider: str, stage: str, query: str, *, requested_keys: list[str] | None = None, epic_key: str | None = None) -> dict:
    return {
        "step_id": digest_object([provider, stage, query])[:16], "provider": provider,
        "stage": stage, "query": query, "requested_keys": requested_keys or [],
        "epic_key": epic_key, "state": "pending", "attempt_errors": [],
    }


def primary_step(scope: dict) -> dict:
    provider, kind, ids = scope["provider"], scope["kind"], scope["ids"]
    query = (jql_epic(ids[0]) if kind == "epic" else jql_keys(ids)) if provider == "jira" else (tql_epic(ids[0]) if kind == "epic" else tql_units(ids))
    return make_step(provider, f"{provider}-source-{kind}", query, requested_keys=ids if kind == "tasks" else [], epic_key=ids[0] if kind == "epic" else None)


def pending_step(run: dict) -> dict | None:
    return next((step for step in run["steps"] if step["state"] == "pending"), None)


def add_step(run: dict, step: dict) -> None:
    if not any(existing["stage"] == step["stage"] for existing in run["steps"]):
        run["steps"].append(step)


def card_keys(run: dict, provider: str) -> set[str]:
    return {item["key"] for item in run["cards"][provider]}


def sber_jira_keys(run: dict) -> list[str]:
    return sorted({item["jira_key"] for item in run["cards"]["sbertrek"] if item.get("jira_key_state") == "value"})


def advance(run: dict) -> None:
    if pending_step(run) or run["status"] != "tracker-read-collecting":
        return
    scope = run["scope"]
    stages = {step["stage"] for step in run["steps"] if step["state"] == "complete"}
    if scope["provider"] == "sbertrek":
        if "jira-counterparts" not in stages:
            keys = sber_jira_keys(run)
            if keys and run["config"]["jira_enabled"]:
                add_step(run, make_step("jira", "jira-counterparts", jql_keys(keys), requested_keys=keys))
                return
        run["status"] = "tracker-read-ready"
        return
    if scope["kind"] == "tasks":
        if "sbertrek-counterparts" not in stages:
            keys = sorted(card_keys(run, "jira"))
            if keys:
                add_step(run, make_step("sbertrek", "sbertrek-counterparts", tql_jira_keys(keys), requested_keys=keys))
                return
        run["status"] = "tracker-read-ready"
        return
    if "sbertrek-epic-discovery" not in stages:
        epic = scope["ids"][0]
        add_step(run, make_step("sbertrek", "sbertrek-epic-discovery", tql_jira_keys([epic]), requested_keys=[epic]))
        return
    discovery = next(step for step in run["steps"] if step["stage"] == "sbertrek-epic-discovery")
    counterpart = discovery.get("counterpart_epic")
    if counterpart and "sbertrek-epic-members" not in stages:
        add_step(run, make_step("sbertrek", "sbertrek-epic-members", tql_epic(counterpart), epic_key=counterpart))
        return
    if counterpart and "sbertrek-epic-members" in stages and "jira-extra-counterparts" not in stages:
        missing = sorted(set(sber_jira_keys(run)) - card_keys(run, "jira"))
        if missing:
            add_step(run, make_step("jira", "jira-extra-counterparts", jql_keys(missing), requested_keys=missing))
            return
    run["status"] = "tracker-read-ready"


def next_action(run: dict) -> dict:
    step = pending_step(run)
    ctl = str(Path(__file__).with_name("trackerctl.py"))
    if not step:
        if run["status"] == "tracker-read-needs-resolution":
            conflict = first_unresolved_conflict(reconcile_data(run))
            if conflict is None:
                return {"type": "reconcile", "command": [sys.executable, ctl, "reconcile", "--run-id", run["run_id"]]}
            task_key = conflict["sbertrek_key"]
            base = [sys.executable, ctl, "resolve-conflict", "--run-id", run["run_id"], "--task-key", task_key]
            return {
                "type": "resolve-conflict", "task": conflict,
                "next_question": conflict_question(conflict),
                "custom_file_contract": {
                    "format": "json-object", "required_fields": [item["field"] for item in conflict["conflicts"]],
                    "allowed_field_values": ["sbertrek", "jira", "explicit-normalized-value"],
                },
                "commands": {
                    "sbertrek": [*base, "--choice", "sbertrek"],
                    "sbertrek_for_following": [*base, "--choice", "sbertrek", "--apply-to-following"],
                    "jira": [*base, "--choice", "jira"],
                    "jira_for_following": [*base, "--choice", "jira", "--apply-to-following"],
                    "custom": [*base, "--choice", "custom", "--custom-file", "<resolution-json-path>"],
                },
            }
        if run["status"] == "tracker-read-ready":
            return {"type": "reconcile", "command": [sys.executable, ctl, "reconcile", "--run-id", run["run_id"]]}
        if run["status"] == "tracker-read-reconciled":
            return {"type": "result-status", "command": [sys.executable, ctl, "result-status", "--run-id", run["run_id"]]}
        return {"type": "stop", "command": None}
    if step["provider"] == "jira":
        tool, arguments = "jira_search", {"jql": step["query"], "fields": ",".join(JIRA_FIELDS), "limit": MAX_RESULTS}
    else:
        tool, arguments = "issue.exportJson", {"query": step["query"], "fields": list(SBER_FIELDS), "max_results": MAX_RESULTS}
    return {
        "type": "mcp-query", "provider": step["provider"], "stage": step["stage"],
        "step_id": step["step_id"], "tool": tool, "arguments": arguments,
        "ingest_command": [sys.executable, ctl, "ingest", "--run-id", run["run_id"], "--step-id", step["step_id"], "--response-file", "<full-json-path>", "--response-source", "<mcp-file|inline-json-capture>"],
        "error_command": [sys.executable, ctl, "ingest-error", "--run-id", run["run_id"], "--step-id", step["step_id"], "--error-file", "<full-error-path>"] if step["stage"] in {"jira-counterparts", "jira-extra-counterparts"} else None,
    }


def status_payload(run: dict) -> dict:
    action = next_action(run)
    payload = {
        "protocol": PROTOCOL, "run_id": run["run_id"], "status": run["status"],
        "workflow_complete": run["status"] == "tracker-read-reconciled",
        "final_response_allowed": False,
        "must_stop": action["type"] == "resolve-conflict",
        "allowed_next_action": action["type"], "next_action": action,
        "limitations": sorted(set(run["limitations"])),
        "paths": {"scope": str(run_root(run["run_id"]) / "scope.json"), "run_status": str(run_root(run["run_id"]) / "run-status.json"), "providers": str(run_root(run["run_id"]) / "providers")},
    }
    if action["type"] == "resolve-conflict":
        payload["next_question"] = action["next_question"]
        payload["response_contract"] = {
            "type": "ask-one", "text": action["next_question"],
            "additional_commentary_allowed": False,
        }
    return payload


def write_provider_snapshots(run: dict) -> None:
    root = run_root(run["run_id"])
    for provider in PROVIDERS:
        responses = []
        for step in run["steps"]:
            if step["provider"] == provider and step["state"] == "complete":
                responses.append({key: step.get(key) for key in ("step_id", "stage", "query", "response_source", "response_sha256", "response_bytes", "returned_count")})
        save_json(root / "providers" / f"{provider}.json", {"protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run["run_id"], "provider": provider, "scope": run["scope"], "issues": run["cards"][provider], "responses": responses})


def save_run(run: dict) -> None:
    save_json(run_path(run["run_id"]), run)
    write_provider_snapshots(run)
    save_json(run_root(run["run_id"]) / "run-status.json", status_payload(run))


def decoded_json_string(value: str) -> Any | None:
    text = value.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def alias_value(container: Any, aliases: tuple[str, ...]) -> tuple[bool, Any]:
    if not isinstance(container, dict):
        return False, None
    folded = {str(key).casefold(): value for key, value in container.items()}
    for alias in aliases:
        if alias.casefold() in folded:
            return True, folded[alias.casefold()]
    return False, None


def record_issue_key(record: Any) -> str | None:
    found, value = alias_value(record, ISSUE_KEY_ALIASES)
    if found and isinstance(value, str) and ISSUE_KEY.fullmatch(value.strip().upper()):
        return value.strip().upper()
    if isinstance(record, dict):
        found, value = alias_value(record.get("unit"), ISSUE_KEY_ALIASES)
        if found and isinstance(value, str) and ISSUE_KEY.fullmatch(value.strip().upper()):
            return value.strip().upper()
    return None


def full_issue_records(payload: Any) -> tuple[list[dict], str]:
    candidates: list[tuple[str, list[dict]]] = []
    visited: set[str] = set()

    def walk(value: Any, path: str, preferred: bool = False, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(value, str):
            if value in visited:
                return
            decoded = decoded_json_string(value)
            if decoded is not None:
                visited.add(value)
                walk(decoded, path + "<json>", preferred, depth + 1)
            return
        if isinstance(value, list):
            if not value and (preferred or path == "$"):
                candidates.append((path, []))
            elif value and all(isinstance(item, dict) and record_issue_key(item) for item in value):
                candidates.append((path, value))
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", False, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if path == "$" and record_issue_key(value):
            candidates.append((path, [value]))
        for key, item in value.items():
            walk(item, f"{path}.{key}", str(key).casefold() in ISSUE_COLLECTION_KEYS, depth + 1)

    walk(payload, "$")
    if not candidates:
        raise ValueError("В полном JSON-ответе MCP не найден массив карточек с ключами задач")
    maximum = max(len(records) for _, records in candidates)
    largest = [(path, records) for path, records in candidates if len(records) == maximum]
    key_sets = {tuple(sorted(record_issue_key(item) or "" for item in records)) for _, records in largest}
    if len(key_sets) > 1:
        raise ValueError("JSON-ответ содержит несколько неоднозначных массивов задач")
    path, records = largest[0]
    keys = [record_issue_key(item) for item in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Полный JSON-ответ MCP содержит повторяющиеся ключи задач")
    return records, path


def jira_metadata(payload: Any) -> dict | None:
    found: set[tuple[int, int, int]] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(value, str):
            decoded = decoded_json_string(value)
            if decoded is not None:
                walk(decoded, depth + 1)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        folded = {str(key).casefold(): item for key, item in value.items()}
        values = (folded.get("total"), folded.get("start_at", folded.get("startat")), folded.get("max_results", folded.get("maxresults")))
        if all(isinstance(item, int) and not isinstance(item, bool) for item in values):
            found.add(values)
        for item in value.values():
            walk(item, depth + 1)

    walk(payload)
    if len(found) > 1:
        raise ValueError("Jira JSON-ответ содержит неоднозначные metadata пагинации")
    if not found:
        return None
    total, start_at, max_results = next(iter(found))
    return {"total": total, "start_at": start_at, "max_results": max_results}


def record_fields(record: dict) -> dict:
    if isinstance(record.get("fields"), dict):
        return {**record, **record["fields"]}
    if isinstance(record.get("unit"), dict):
        return {**record["unit"], **{key: value for key, value in record.items() if key != "unit"}}
    return record


def object_text(value: Any, aliases: tuple[str, ...] = ("code", "name", "value", "title", "key")) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    found, nested = alias_value(value, aliases)
    return object_text(nested, aliases) if found else None


def attribute_records(record: dict) -> tuple[list[dict[str, Any]], bool]:
    sources = [record]
    if isinstance(record.get("unit"), dict):
        sources.append(record["unit"])
    if isinstance(record.get("fields"), dict):
        sources.append(record["fields"])
    for source in sources:
        found, raw = alias_value(source, ("attributes",))
        if not found:
            continue
        if isinstance(raw, dict):
            return [{"code": str(key), "name": str(key), "value": value} for key, value in raw.items()], True
        if not isinstance(raw, list):
            return [], True
        result: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            found_code, code = alias_value(entry, ("code", "attributeCode", "attribute_code"))
            if not found_code and isinstance(entry.get("attribute"), dict):
                found_code, code = alias_value(entry["attribute"], ("code", "key", "name"))
            code_text = object_text(code, ("code", "key", "name", "value")) if found_code else None
            if not code_text:
                continue
            found_value, value = alias_value(entry, ("value", "values", "data"))
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            found_name, name = alias_value(entry, ("name", "title", "displayName", "display_name"))
            if not found_name and isinstance(entry.get("attribute"), dict):
                found_name, name = alias_value(entry["attribute"], ("name", "title", "displayName", "display_name"))
            result.append({"code": code_text, "name": object_text(name) if found_name else code_text, "value": value if found_value else None})
        return result, True
    return [], False


def attribute_entries(record: dict) -> tuple[dict[str, Any], bool]:
    records, present = attribute_records(record)
    return {item["code"].casefold(): item["value"] for item in records}, present


def optional_value(record: dict, aliases: tuple[str, ...], attributes: dict[str, Any], codes: tuple[str, ...]) -> tuple[Any, str]:
    fields = record_fields(record)
    found, value = alias_value(fields, aliases)
    if found and value not in (None, "", [], {}):
        return value, "value"
    for code in codes:
        if code.casefold() in attributes:
            attribute = attributes[code.casefold()]
            return (attribute, "value") if attribute not in (None, "", [], {}) else (None, "absent")
    containers_present = bool(attributes) or any(str(key).casefold() == "attributes" for key in record) or isinstance(record.get("fields"), dict)
    return None, "absent" if found or containers_present else "not-returned"


def normalized_person(value: Any) -> dict | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        text = object_text(value)
        return {"id": text, "name": text} if text else None
    found_id, account = alias_value(value, ("externalId", "accountId", "account_id", "login", "id", "key"))
    found_name, name = alias_value(value, ("displayName", "display_name", "fullName", "full_name", "name"))
    if not found_name:
        parts = []
        for alias in ("lastName", "firstName", "middleName"):
            present, part = alias_value(value, (alias,))
            if present and object_text(part):
                parts.append(object_text(part) or "")
        name = " ".join(parts) if parts else None
    account_text = object_text(account) if found_id else None
    name_text = object_text(name) if name is not None else None
    if not account_text and not name_text:
        return None
    return {"id": account_text, "name": name_text or account_text}


def explicitly_unassigned(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(item).casefold() in UNASSIGNED_SENTINELS for item in value.values() if isinstance(item, str))
    text = object_text(value)
    return bool(text) and text.casefold() in UNASSIGNED_SENTINELS


def normalized_number(value: Any) -> float | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        found, nested = alias_value(value, ("value", "amount", "estimate"))
        value = nested if found else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalized_estimate(value: Any) -> dict | None:
    number = normalized_number(value)
    return {"value": number, "unit": "story-points"} if number is not None else None


def normalized_role_marker(value: str) -> str | None:
    token = re.sub(r"[^a-zа-яё]+", "", value.casefold())
    aliases = {"a": "AN", "an": "AN", "ан": "AN", "b": "BE", "be": "BE", "бэ": "BE", "бе": "BE", "f": "FE", "fe": "FE", "фэ": "FE", "фе": "FE", "q": "QA", "qa": "QA", "тест": "QA"}
    visual = token.translate(str.maketrans({"а": "a", "в": "b", "е": "e"}))
    return aliases.get(token) or aliases.get(visual)


def role_from_summary(summary: str) -> str | None:
    bracketed = re.findall(r"\[\s*([^\]]+)\s*\]", summary[:64])
    if bracketed:
        roles = {role for item in bracketed if (role := normalized_role_marker(item))}
        return next(iter(roles)) if len(roles) == 1 else None
    match = re.match(r"\s*([^\s:_/\-]+)(?=[\s:_/\-])", summary)
    return normalized_role_marker(match.group(1)) if match else None


def sber_estimate_definition(code: str, name: str) -> dict | None:
    combined = " ".join(re.sub(r"[^0-9a-zа-яё]+", " ", f"{code} {name}".casefold()).split())
    if code.casefold() in {item.casefold() for item in SBER_ATTRIBUTE_CODES["estimate"]}:
        return {"name": name, "role": None, "unit": "story-points", "general": True}
    if any(marker in combined for marker in ("front end", "frontend", "разработк fe", "fe разработк")):
        return {"name": name, "role": "FE", "unit": "person-days"}
    if any(marker in combined for marker in ("back end", "backend", "разработк be", "be разработк")):
        return {"name": name, "role": "BE", "unit": "person-days"}
    if "анализ" in combined or "analysis" in combined:
        return {"name": name, "role": "AN", "unit": "person-days"}
    if "тестирован" in combined or "testing" in combined or re.search(r"\bqa\b", combined):
        return {"name": name, "role": "QA", "unit": "person-days"}
    if any(marker in combined for marker in ("общ", "чд", "чел д", "story point", "story points", "total estimate")):
        return {"name": name, "role": None, "unit": "person-days", "general": True}
    return None


def source_estimates(record: dict, provider: str, summary: str) -> tuple[dict | None, str, dict[str, dict], dict[str, str], list[dict]]:
    fields = record_fields(record)
    entries: list[dict] = []
    role_states = {role: "not-returned" for role in ROLES}
    role_values: dict[str, dict] = {}
    general_candidates: list[dict] = []
    if provider == "jira":
        definitions = [
            (field_id, definition, alias_value(fields, (field_id,))[0], alias_value(fields, (field_id,))[1])
            for field_id, definition in JIRA_ESTIMATE_FIELDS.items()
        ]
    else:
        records, present = attribute_records(record)
        role_states = {role: "absent" if present else "not-returned" for role in ROLES}
        definitions = []
        for attribute in records:
            definition = sber_estimate_definition(attribute["code"], attribute["name"])
            if definition:
                definitions.append((attribute["code"], definition, True, attribute["value"]))
    for field_id, definition, found, raw in definitions:
        role = definition.get("role")
        if role and found:
            role_states[role] = "absent"
        number = normalized_number(raw) if found else None
        if number is None:
            continue
        entry = {"field_id": field_id, "field_name": definition["name"], "value": number, "unit": definition["unit"], "role": role}
        entries.append(entry)
        if role:
            role_states[role] = "value"
            role_values.setdefault(role, {"value": number, "unit": definition["unit"], "source_field": {"id": field_id, "name": definition["name"]}, "inferred_from_general": False})
        if definition.get("general"):
            general_candidates.append(entry)
    raw_general, legacy_state = optional_value(record, ESTIMATE_ALIASES, {}, ())
    general = normalized_estimate(raw_general) if legacy_state == "value" else None
    general_state = "value" if general else "not-returned"
    if general is None and general_candidates:
        if provider == "jira":
            priority = {field: index for index, field in enumerate(JIRA_GENERAL_ESTIMATE_PRIORITY)}
            general_candidates.sort(key=lambda item: priority.get(item["field_id"], len(priority)))
        candidate = general_candidates[0]
        general = {"value": candidate["value"], "unit": candidate["unit"]}
        general_state = "value"
    elif general is None:
        present = any(alias_value(fields, (field,))[0] for field, definition in JIRA_ESTIMATE_FIELDS.items() if definition.get("general")) if provider == "jira" else attribute_records(record)[1]
        general_state = "absent" if present else "not-returned"
    if not role_values and general is not None:
        role = role_from_summary(summary)
        if role in {"AN", "BE", "FE"}:
            source = general_candidates[0] if general_candidates else {"field_id": "estimate", "field_name": "Общая оценка"}
            role_values[role] = {**general, "source_field": {"id": source["field_id"], "name": source["field_name"]}, "inferred_from_general": True}
            role_states[role] = "value"
    return general, general_state, role_values, role_states, entries


def normalized_epic(value: Any) -> dict | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        key = value.strip().upper()
        return {"key": key, "name": key} if ISSUE_KEY.fullmatch(key) else None
    if not isinstance(value, dict):
        return None
    found, key_value = alias_value(value, ISSUE_KEY_ALIASES + ("code", "id"))
    key = object_text(key_value) if found else None
    if not key or not ISSUE_KEY.fullmatch(key.strip().upper()):
        return None
    found_name, name = alias_value(value, ("name", "summary", "title"))
    return {"key": key.strip().upper(), "name": object_text(name) if found_name else key.strip().upper()}


def normalized_releases(value: Any) -> list[dict]:
    result = []
    for item in value if isinstance(value, list) else [value]:
        release = normalized_epic(item)
        if release:
            result.append(release)
        elif object_text(item):
            result.append({"key": object_text(item), "name": object_text(item)})
    return result


def required_text(record: dict, aliases: tuple[str, ...], label: str, key: str) -> str:
    found, value = alias_value(record_fields(record), aliases)
    text = object_text(value, ("code", "name", "value", "title", "key")) if found else None
    if not text or text.casefold() in MISSING_SENTINELS:
        raise ValueError(f"Карточка {key} не содержит обязательное поле {label}")
    return text


def optional_text(record: dict, aliases: tuple[str, ...]) -> tuple[str | None, str]:
    value, state = optional_value(record, aliases, {}, ())
    text = object_text(value, ("code", "name", "value", "title", "key")) if state == "value" else None
    if not text or text.casefold() in MISSING_SENTINELS:
        return None, "absent" if state == "value" else state
    return text, "value"


def compact_issue(record: dict, provider: str, *, forced_epic: str | None = None) -> dict:
    key = record_issue_key(record)
    if not key:
        raise ValueError("JSON-объект карточки не содержит ключ задачи")
    summary = required_text(record, SUMMARY_ALIASES, "summary", key)
    attributes, attributes_present = attribute_entries(record)
    assignee_raw, assignee_state = optional_value(record, ASSIGNEE_ALIASES, attributes, SBER_ATTRIBUTE_CODES["assignee"])
    assignee = None if explicitly_unassigned(assignee_raw) else normalized_person(assignee_raw)
    if explicitly_unassigned(assignee_raw):
        assignee_state = "absent"
    if assignee_state == "value" and assignee is None:
        raise ValueError(f"Карточка {key}: исполнитель имеет неподдерживаемый формат")
    estimate, estimate_state, role_values, role_states, estimate_fields = source_estimates(record, provider, summary)
    epic_raw, epic_state = optional_value(record, EPIC_ALIASES, attributes, SBER_ATTRIBUTE_CODES["epic"])
    epic = normalized_epic(epic_raw)
    if forced_epic:
        epic, epic_state = {"key": forced_epic, "name": forced_epic}, "value"
    releases_raw, releases_state = optional_value(record, RELEASE_ALIASES, attributes, SBER_ATTRIBUTE_CODES["releases"])
    releases = normalized_releases(releases_raw) if releases_state == "value" else []
    if provider == "sbertrek":
        jira_raw, jira_state = optional_value(record, ("issue_key",), attributes, SBER_ATTRIBUTE_CODES["jira_key"])
        jira_text = object_text(jira_raw, ("key", "code", "value", "name")) if jira_state == "value" else None
        jira_key = normalize_key(jira_text, "Объект Jira") if jira_text else None
    else:
        jira_key, jira_state = None, "absent"
    created_at, created_state = optional_text(record, CREATED_ALIASES)
    updated_at, updated_state = optional_text(record, UPDATED_ALIASES)
    return {
        "key": key, "jira_key": jira_key, "jira_key_state": jira_state,
        "summary": summary, "issue_type": required_text(record, TYPE_ALIASES, "issue_type", key),
        "status": required_text(record, STATUS_ALIASES, "status", key),
        "assignee": assignee, "estimate": estimate, "role_estimates": role_values,
        "role_estimate_observations": role_states, "estimate_fields": estimate_fields,
        "epic": epic, "releases": releases,
        "field_observations": {
            "assignee": assignee_state, "estimate": estimate_state, "epic": epic_state,
            "releases": releases_state, "created_at": created_state, "updated_at": updated_state,
        },
        "created_at": created_at,
        "updated_at": updated_at,
        "attributes_returned": attributes_present if provider == "sbertrek" else None,
    }


def response_file(path_value: str, run_id: str, *, require_json: bool = True) -> tuple[Path, bytes, Any | None]:
    path = Path(path_value).expanduser().resolve()
    try:
        path.relative_to(run_root(run_id))
    except ValueError:
        pass
    else:
        raise ValueError("Ответ MCP должен находиться вне каталога tracker-run")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ответ MCP пуст или превышает допустимый размер")
    payload = None
    if require_json:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Ответ MCP не является полным JSON") from error
    return path, raw, payload


def fail_run(run: dict, reason: str) -> None:
    run["status"] = "tracker-read-failed"
    run["failure"] = {"reason": reason, "at": now()}
    save_run(run)


def validate_records_for_step(step: dict, cards: list[dict]) -> None:
    if step["stage"].endswith("source-tasks"):
        unexpected = sorted({item["key"] for item in cards} - set(step["requested_keys"]))
        if unexpected:
            raise ValueError(f"Ответ содержит задачи вне исходной области: {', '.join(unexpected)}")
    if step["stage"] in {"jira-counterparts", "jira-extra-counterparts"}:
        unexpected = sorted({item["key"] for item in cards} - set(step["requested_keys"]))
        if unexpected:
            raise ValueError(f"Jira вернула незапрошенные ключи: {', '.join(unexpected)}")
    if step["stage"] == "sbertrek-counterparts":
        invalid = [item["key"] for item in cards if item.get("jira_key_state") != "value" or item.get("jira_key") not in step["requested_keys"]]
        if invalid:
            raise ValueError(f"SberTrek counterpart-ответ не подтверждает issue_key: {', '.join(invalid)}")


def append_cards(run: dict, provider: str, cards: list[dict]) -> None:
    existing = {item["key"]: item for item in run["cards"][provider]}
    for card in cards:
        if card["key"] in existing and digest_object(existing[card["key"]]) != digest_object(card):
            raise ValueError(f"Карточка {card['key']} повторно получена с другим содержимым")
        if card["key"] not in existing:
            run["cards"][provider].append(card)
    run["cards"][provider].sort(key=lambda item: item["key"])


def ingest_command(args: argparse.Namespace) -> int:
    run = load_run(args.run_id)
    working = copy.deepcopy(run)
    step = next((item for item in working["steps"] if item["step_id"] == args.step_id), None)
    if not step:
        raise ValueError("step_id не принадлежит tracker-run")
    try:
        path, raw, payload = response_file(args.response_file, args.run_id)
        response_sha = digest_bytes(raw)
        if step["state"] == "complete":
            if step.get("response_sha256") == response_sha:
                if working["status"] == "tracker-read-failed":
                    working["status"] = "tracker-read-collecting"
                    working["failure"] = None
                    advance(working)
                    save_run(working)
                print(json.dumps({**status_payload(working), "ingest": "idempotent-no-op"}, ensure_ascii=False, indent=2))
                return 0
            raise ValueError("Для уже завершённого шага передан другой ответ")
        if step is not pending_step(working):
            raise ValueError("Можно импортировать только ответ текущего next_action")
        records, record_path = full_issue_records(payload)
        if step["stage"] == "sbertrek-epic-discovery":
            if len(records) > 1:
                raise ValueError("Для Jira-эпика найдено больше одного SberTrek-эпика")
            counterpart = None
            if records:
                record = records[0]
                counterpart = record_issue_key(record)
                attributes, _ = attribute_entries(record)
                raw_link, state = optional_value(record, ("issue_key",), attributes, SBER_ATTRIBUTE_CODES["jira_key"])
                linked = object_text(raw_link, ("key", "code", "value", "name")) if state == "value" else None
                if not counterpart or not linked or normalize_key(linked) != step["requested_keys"][0]:
                    raise ValueError("Найденный SberTrek-эпик не подтверждает issue_key исходного Jira-эпика")
                found_type, raw_type = alias_value(record_fields(record), TYPE_ALIASES)
                if found_type and object_text(raw_type) and object_text(raw_type).casefold() != "epic":
                    raise ValueError("SberTrek counterpart исходного Jira-эпика не является эпиком")
            step["counterpart_epic"] = counterpart
            if counterpart is None:
                working["limitations"].append(f"sbertrek-counterpart-epic-not-found:{step['requested_keys'][0]}")
            cards: list[dict] = []
        else:
            cards = [compact_issue(record, step["provider"], forced_epic=step.get("epic_key")) for record in records]
            evidence = f"{step['step_id']}:{response_sha}"
            for card in cards:
                card["evidence"] = evidence
            validate_records_for_step(step, cards)
            append_cards(working, step["provider"], cards)
            for card in cards:
                for field in ("created_at", "updated_at"):
                    if card["field_observations"][field] != "value":
                        working["limitations"].append(f"{step['provider']}-{field.replace('_', '-')}-not-returned:{card['key']}")
            returned = {item["key"] for item in cards}
            if step["requested_keys"] and step["provider"] == "jira":
                missing = sorted(set(step["requested_keys"]) - returned - set(working["absent_jira_keys"]))
                working["limitations"].extend(f"jira-key-not-returned:{key}" for key in missing)
            if step["requested_keys"] and step["stage"].endswith("source-tasks") and step["provider"] == "sbertrek":
                missing = sorted(set(step["requested_keys"]) - returned)
                working["limitations"].extend(f"sbertrek-key-not-returned:{key}" for key in missing)
        step.update({"state": "complete", "response_source": args.response_source, "response_path": str(path), "response_sha256": response_sha, "response_bytes": len(raw), "record_path": record_path, "returned_count": len(records), "completed_at": now()})
        if len(records) == MAX_RESULTS:
            working["limitations"].append(f"{step['provider']}-result-limit-reached:{MAX_RESULTS}")
        metadata = jira_metadata(payload) if step["provider"] == "jira" else None
        if metadata and metadata["total"] > metadata["start_at"] + len(records):
            working["limitations"].append(f"jira-result-incomplete:{len(records)}-of-{metadata['total']}")
        working["status"] = "tracker-read-collecting"
        working["failure"] = None
        advance(working)
        save_run(working)
    except Exception as error:
        fail_run(run, str(error))
        raise
    print(json.dumps(status_payload(working), ensure_ascii=False, indent=2))
    return 0


def all_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(all_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(all_text(item) for item in value)
    return str(value)


def ingest_error_command(args: argparse.Namespace) -> int:
    run = load_run(args.run_id)
    working = copy.deepcopy(run)
    step = next((item for item in working["steps"] if item["step_id"] == args.step_id), None)
    if not step or step is not pending_step(working):
        raise ValueError("Ошибка относится не к текущему next_action")
    try:
        _, raw, _ = response_file(args.error_file, args.run_id, require_json=False)
        response_sha = digest_bytes(raw)
        if any(item["sha256"] == response_sha for item in step["attempt_errors"]):
            print(json.dumps({**status_payload(run), "ingest": "idempotent-no-op"}, ensure_ascii=False, indent=2))
            return 0
        try:
            text = all_text(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = raw.decode("utf-8", errors="replace")
        if step["stage"] not in {"jira-counterparts", "jira-extra-counterparts"}:
            raise ValueError("Ошибка исходного запроса не допускает автоматического продолжения")
        absent = unique_keys(re.findall(r"issue with key ['\"]([A-Z][A-Z0-9_]*-[1-9][0-9]*)['\"] does not exist", text, flags=re.I))
        if not absent or not set(absent).issubset(set(step["requested_keys"])):
            raise ValueError("Jira-ошибка не подтверждает отсутствие запрошенных counterpart-ключей")
        step["attempt_errors"].append({"sha256": response_sha, "bytes": len(raw), "absent_keys": absent, "at": now()})
        working["absent_jira_keys"] = sorted(set(working["absent_jira_keys"]) | set(absent))
        remaining = sorted(set(step["requested_keys"]) - set(absent))
        if remaining:
            step["query"] = jql_keys(remaining)
            step["requested_keys"] = remaining
        else:
            step["state"] = "complete"
            step["returned_count"] = 0
            step["completed_at"] = now()
        working["status"] = "tracker-read-collecting"
        working["failure"] = None
        advance(working)
        save_run(working)
    except Exception as error:
        fail_run(run, str(error))
        raise
    print(json.dumps(status_payload(working), ensure_ascii=False, indent=2))
    return 0


def canonical_estimate(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    normalized = " ".join(str(result.get("unit")).casefold().replace("-", " ").replace("_", " ").split())
    if normalized in {"sp", "story point", "story points", "person day", "person days", "человекодень", "человекодни", "чел день", "чел дни"}:
        result["unit"] = "story-points"
    return result


def comparable_person(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    name = " ".join(str(value.get("name") or "").casefold().split())
    return ("name", name) if name else ("id", str(value.get("id") or "").casefold())


def merged_value(field: str, sber: dict | None, jira: dict | None) -> tuple[Any, str | None, dict | None]:
    svalue = sber.get(field) if sber else None
    jvalue = jira.get(field) if jira else None
    if field == "estimate":
        svalue, jvalue = canonical_estimate(svalue), canonical_estimate(jvalue)
    chosen, source = (svalue, "sbertrek") if svalue not in (None, "", [], {}) else (jvalue, "jira")
    if field == "assignee":
        equal = comparable_person(svalue) == comparable_person(jvalue)
    else:
        equal = svalue == jvalue or (field == "issue_type" and isinstance(svalue, str) and isinstance(jvalue, str) and svalue.casefold() == jvalue.casefold())
    conflict = None
    if field in ACTIONABLE_CONFLICT_FIELDS and svalue not in (None, "", [], {}) and jvalue not in (None, "", [], {}) and not equal:
        conflict = {"field": field, "sbertrek": svalue, "jira": jvalue, "resolution": "unresolved"}
    return chosen, source if chosen not in (None, "", [], {}) else None, conflict


def merged_roles(sber: dict | None, jira: dict | None) -> tuple[dict, dict, list[dict]]:
    values, sources, conflicts = {}, {}, []
    for role in ROLES:
        svalue = canonical_estimate((sber or {}).get("role_estimates", {}).get(role))
        jvalue = canonical_estimate((jira or {}).get("role_estimates", {}).get(role))
        chosen, source = (svalue, "sbertrek") if svalue not in (None, {}, "") else (jvalue, "jira")
        if chosen not in (None, {}, ""):
            values[role] = {**chosen, "source": source}
        sources[role] = source if chosen not in (None, {}, "") else None
        if svalue not in (None, {}, "") and jvalue not in (None, {}, "") and {key: svalue.get(key) for key in ("value", "unit")} != {key: jvalue.get(key) for key in ("value", "unit")}:
            conflicts.append({"field": f"role_estimates.{role}", "sbertrek": svalue, "jira": jvalue, "resolution": "unresolved"})
    return values, sources, conflicts


def resolution_for(run: dict, task_key: str, field: str, conflict: dict) -> tuple[Any, str | None, str]:
    resolution = run.get("conflict_resolutions", {}).get(task_key)
    choice = resolution.get("choice") if isinstance(resolution, dict) else None
    if choice is None:
        choice = run.get("following_conflict_choice")
    if choice in PROVIDERS:
        return conflict[choice], choice, choice
    if choice != "custom" or not isinstance(resolution, dict):
        return conflict["sbertrek"], "sbertrek", "unresolved"
    custom = resolution.get("values", {}).get(field)
    if custom in PROVIDERS:
        return conflict[custom], custom, f"custom:{custom}"
    return custom, "custom", "custom"


def display_conflict_value(field: str, value: Any) -> str:
    if field == "assignee" and isinstance(value, dict):
        return str(value.get("name") or value.get("id") or "-")
    if isinstance(value, dict) and isinstance(value.get("value"), (int, float)):
        return f"{value['value']} {value.get('unit') or ''}".strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def conflict_field_label(field: str) -> str:
    if field == "assignee":
        return "исполнитель"
    if field == "estimate":
        return "общая оценка"
    if field.startswith("role_estimates."):
        return f"оценка {field.split('.', 1)[1]}"
    return field


def first_unresolved_conflict(result: dict) -> dict | None:
    grouped: dict[str, dict] = {}
    for item in result["discrepancies"]:
        if item.get("resolution") != "unresolved":
            continue
        key = item["sbertrek_key"]
        grouped.setdefault(key, {
            "sbertrek_key": key, "jira_key": item.get("jira_key"), "conflicts": [],
        })["conflicts"].append({
            field: item[field] for field in ("field", "sbertrek", "jira")
        })
    return next(iter(grouped.values()), None)


def conflict_question(task: dict) -> str:
    lines = [
        f"В задаче SberTrek {task['sbertrek_key']} / Jira {task.get('jira_key') or '-'} обнаружены разные заполненные значения:",
    ]
    for item in task["conflicts"]:
        lines.append(
            f"- {conflict_field_label(item['field'])}: SberTrek = {display_conflict_value(item['field'], item['sbertrek'])}; "
            f"Jira = {display_conflict_value(item['field'], item['jira'])}."
        )
    lines.extend([
        "Выберите один вариант:",
        "1. Приоритет SberTrek для этой задачи.",
        "2. Приоритет SberTrek для этой и всех последующих конфликтующих задач текущего run.",
        "3. Приоритет Jira для этой задачи.",
        "4. Приоритет Jira для этой и всех последующих конфликтующих задач текущего run.",
        "5. Свой вариант по перечисленным полям.",
    ])
    return "\n".join(lines)


def development_state(sber: dict | None, jira: dict | None, config: dict) -> dict:
    item, provider = (sber, "sbertrek") if sber else (jira, "jira")
    status = str((item or {}).get("status") or "")
    completed = {str(value).casefold() for value in config["status_rules"][provider]["completed"]}
    excluded = {str(value).casefold() for value in config["status_rules"][provider]["excluded"]}
    if status.casefold() in excluded:
        state = "excluded"
    elif status.casefold() in completed:
        state = "completed"
    else:
        state = "unknown"
    return {"state": state, "source": provider, "reason": "current-status" if state != "unknown" else "history-not-collected"}


def prefixed_summary(role: str, summary: str) -> str:
    value = summary
    while True:
        match = re.match(r"^\s*\[\s*([^\]]+)\s*\]\s*", value)
        if not match or not normalized_role_marker(match.group(1)):
            break
        value = value[match.end():]
    match = re.match(r"^\s*([^\s:_/\-]+)(?=[\s:_/\-])\s*[:_\-/]?\s*", value)
    if match and normalized_role_marker(match.group(1)):
        value = value[match.end():]
    return f"{role} {value.strip() or summary.strip()}"


def work_items(issues: list[dict]) -> list[dict]:
    result = []
    for issue in issues:
        if issue["development"]["state"] == "excluded":
            continue
        identity = issue.get("jira_key") or issue.get("sbertrek_key")
        for role in ROLES:
            estimate = issue.get("role_estimates", {}).get(role)
            if not isinstance(estimate, dict) or not isinstance(estimate.get("value"), (int, float)) or estimate["value"] <= 0:
                continue
            result.append({
                "work_item_id": f"{identity}/{role}", "tracker_key": identity,
                "sbertrek_key": issue.get("sbertrek_key"), "jira_key": issue.get("jira_key"),
                "role": role, "summary": prefixed_summary(role, issue["summary"]),
                "estimate": estimate, "assignee": issue.get("assignee"),
                "status": issue.get("status"), "development": issue["development"],
            })
    return result


def reconcile_data(run: dict) -> dict:
    sber_items = run["cards"]["sbertrek"]
    jira_items = run["cards"]["jira"]
    jira_by_key = {item["key"]: item for item in jira_items}
    paired_jira: set[str] = set()
    issues, discrepancies, excluded = [], [], []
    for sber in sber_items:
        jira_key = sber.get("jira_key") if sber.get("jira_key_state") == "value" else None
        if jira_key in run["absent_jira_keys"]:
            excluded.append({"sbertrek_key": sber["key"], "jira_key": jira_key, "reason": "jira-counterpart-absent"})
            continue
        jira = jira_by_key.get(jira_key)
        if jira:
            paired_jira.add(jira["key"])
        merged, sources, local = {}, {}, []
        for field in MERGED_FIELDS:
            merged[field], sources[field], conflict = merged_value(field, sber, jira)
            if conflict:
                value, source, resolution = resolution_for(run, sber["key"], field, conflict)
                conflict["resolution"] = resolution
                if resolution != "unresolved":
                    merged[field], sources[field] = value, source
                local.append(conflict)
        roles, role_sources, role_conflicts = merged_roles(sber, jira)
        for conflict in role_conflicts:
            role = conflict["field"].split(".", 1)[1]
            value, source, resolution = resolution_for(run, sber["key"], conflict["field"], conflict)
            conflict["resolution"] = resolution
            if resolution != "unresolved":
                if value in (None, "", {}):
                    roles.pop(role, None)
                    role_sources[role] = None
                else:
                    roles[role] = {**value, "source": source}
                    role_sources[role] = source
        local.extend(role_conflicts)
        for conflict in local:
            discrepancies.append({"type": "field-conflict", "sbertrek_key": sber["key"], "jira_key": jira_key, **conflict})
        issues.append({
            "sbertrek_key": sber["key"], "jira_key": jira_key, **merged,
            "sources": sources, "role_estimates": roles, "role_estimate_sources": role_sources,
            "estimate_fields": [{**field, "provider": provider} for provider, item in (("sbertrek", sber), ("jira", jira)) for field in (item or {}).get("estimate_fields", [])],
            "development": development_state(sber, jira, run["config"]),
        })
    for jira in jira_items:
        if jira["key"] in paired_jira:
            continue
        roles, role_sources, _ = merged_roles(None, jira)
        issue = {"sbertrek_key": None, "jira_key": jira["key"], **{field: jira.get(field) for field in MERGED_FIELDS}}
        issue.update({
            "sources": {field: "jira" if jira.get(field) not in (None, "", [], {}) else None for field in MERGED_FIELDS},
            "role_estimates": roles, "role_estimate_sources": role_sources,
            "estimate_fields": [{**field, "provider": "jira"} for field in jira.get("estimate_fields", [])],
            "development": development_state(None, jira, run["config"]),
        })
        issues.append(issue)
    issues.sort(key=lambda item: (item.get("sbertrek_key") or "", item.get("jira_key") or ""))
    role_totals = {role: 0.0 for role in ROLES}
    general_total = 0.0
    for issue in issues:
        estimate = issue.get("estimate")
        if isinstance(estimate, dict) and estimate.get("unit") == "story-points":
            general_total += float(estimate["value"])
        for role, estimate in issue.get("role_estimates", {}).items():
            if estimate.get("unit") in {"story-points", "person-days"}:
                role_totals[role] += float(estimate["value"])
    limitations = sorted(set(run["limitations"] + ["history-not-collected"] + [
        f"general-estimate-role-unresolved:{item.get('jira_key') or item.get('sbertrek_key')}"
        for item in issues if item.get("estimate") and not item.get("role_estimates")
    ]))
    counts = {
        "sbertrek": len(sber_items), "jira": len(jira_items), "matched": len(paired_jira),
        "issues": len(issues), "excluded": len(excluded), "discrepancies": len(discrepancies),
    }
    role_work_items = work_items(issues)
    counts["work_items"] = len(role_work_items)
    return {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run["run_id"],
        "scope": run["scope"], "issues": issues, "work_items": role_work_items,
        "excluded": excluded, "discrepancies": discrepancies, "counts": counts,
        "summary": {"story_points_total": general_total, "role_totals_person_days": role_totals},
        "limitations": limitations,
    }


def render_report(result: dict) -> str:
    lines = [
        "# Сверка задач трекеров", "", f"Run ID: `{result['run_id']}`", "",
        f"- SberTrek: {result['counts']['sbertrek']}", f"- Jira: {result['counts']['jira']}",
        f"- Склеено пар: {result['counts']['matched']}", f"- Итоговых задач: {result['counts']['issues']}",
        f"- Исключено: {result['counts']['excluded']}", f"- Расхождений: {result['counts']['discrepancies']}",
        f"- Общая оценка: {result['summary']['story_points_total']} story-points", "",
        "| SberTrek | Jira | Название | Статус | Исполнитель | AN | BE | FE | QA |",
        "|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for item in result["issues"]:
        assignee = (item.get("assignee") or {}).get("name") or "—"
        values = [str(item.get("role_estimates", {}).get(role, {}).get("value", "—")) for role in ROLES]
        lines.append(f"| {item.get('sbertrek_key') or '—'} | {item.get('jira_key') or '—'} | {item['summary']} | {item['status']} | {assignee} | {' | '.join(values)} |")
    lines.extend(["", f"## Ролевые задачи ({result['counts']['work_items']})", ""])
    for item in result["work_items"]:
        assignee = (item.get("assignee") or {}).get("name") or "—"
        lines.append(
            f"- `{item['work_item_id']}` — {item['summary']}; "
            f"{item['estimate']['value']} {item['estimate']['unit']}; исполнитель: {assignee}."
        )
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines) + "\n"


def official_text(result: dict) -> str:
    counts, totals = result["counts"], result["summary"]["role_totals_person_days"]
    lines = [
        f"Сверка завершена. Run ID: {result['run_id']}",
        f"SberTrek: {counts['sbertrek']}; Jira: {counts['jira']}; склеено: {counts['matched']}; исходных задач: {counts['issues']}; ролевых задач: {counts['work_items']}; исключено: {counts['excluded']}; расхождений: {counts['discrepancies']}.",
        f"Общая оценка: {result['summary']['story_points_total']} SP. Ролевые оценки: AN {totals['AN']}, BE {totals['BE']}, FE {totals['FE']}, QA {totals['QA']} человекодней.",
        "Задачи:",
    ]
    for item in result["issues"]:
        sbertrek_key = item.get("sbertrek_key") or "-"
        jira_key = item.get("jira_key") or "-"
        summary = " ".join(str(item.get("summary") or "-").split())
        status = " ".join(str(item.get("status") or "-").split())
        assignee = " ".join(str((item.get("assignee") or {}).get("name") or "-").split())
        estimates = ", ".join(
            f"{role} {item.get('role_estimates', {}).get(role, {}).get('value', '-')}"
            for role in ROLES
        )
        lines.append(
            f"- SberTrek {sbertrek_key}; Jira {jira_key}; {summary}; "
            f"статус: {status}; исполнитель: {assignee}; оценки: {estimates}."
        )
    lines.append("Ролевые задачи:")
    for item in result["work_items"]:
        assignee = " ".join(str((item.get("assignee") or {}).get("name") or "-").split())
        lines.append(
            f"- {item['work_item_id']}; {item['summary']}; оценка: "
            f"{item['estimate']['value']} {item['estimate']['unit']}; исполнитель: {assignee}."
        )
    if result["excluded"]:
        lines.append("Исключены:")
        for item in result["excluded"]:
            lines.append(
                f"- SberTrek {item.get('sbertrek_key') or '-'}; Jira {item.get('jira_key') or '-'}; "
                f"причина: {item['reason']}."
            )
    if result["limitations"]:
        lines.append("Ограничения: " + ", ".join(result["limitations"]) + ".")
    return "\n".join(lines)


def validated_custom_resolution(task: dict, path_value: str) -> dict:
    payload = load_json(Path(path_value).expanduser().resolve())
    if not isinstance(payload, dict):
        raise ValueError("Пользовательский вариант должен быть JSON-объектом")
    expected = {item["field"] for item in task["conflicts"]}
    if set(payload) != expected:
        raise ValueError("Пользовательский вариант должен задавать ровно все поля текущего конфликта")
    result = {}
    for field, value in payload.items():
        if value in PROVIDERS:
            result[field] = value
            continue
        if field == "assignee":
            person = normalized_person(value)
            if person is None:
                raise ValueError("Пользовательский исполнитель должен содержать имя или идентификатор")
            result[field] = person
            continue
        if not isinstance(value, dict) or not isinstance(value.get("value"), (int, float)) or not isinstance(value.get("unit"), str):
            raise ValueError(f"Пользовательская оценка {field} должна содержать числовые value и строковый unit")
        result[field] = canonical_estimate(value)
    return result


def init_config_command(args: argparse.Namespace) -> int:
    path = config_path()
    if path.exists() and not args.force:
        raise ValueError("tracker-config.json уже существует")
    save_json(path, DEFAULT_CONFIG)
    print(json.dumps({"status": "tracker-config-created", "path": str(path)}, ensure_ascii=False, indent=2))
    return 0


def config_status_command(_: argparse.Namespace) -> int:
    payload = config_status_payload(load_config())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return STOP_EXIT if payload.get("must_stop") else 0


def update_config_command(args: argparse.Namespace) -> int:
    config = load_config()
    if args.command == "set-projects":
        config["projects"][args.provider] = sorted(set(args.projects))
    elif args.command == "set-jira-mode":
        config["jira_enabled"] = args.mode == "enabled"
    elif args.command == "set-issue-types":
        config["development_issue_types"] = sorted(set(args.issue_types))
    elif args.command == "set-statuses":
        config["status_rules"][args.provider][args.kind] = [] if args.none else sorted(set(args.statuses))
    save_json(config_path(), config)
    payload = config_status_payload(config)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return STOP_EXIT if payload.get("must_stop") else 0


def complete_config_command(_: argparse.Namespace) -> int:
    config = load_config()
    gaps = config_gaps(config, include_confirmation=False)
    if gaps:
        payload = config_status_payload(config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    config["setup_complete"] = True
    save_json(config_path(), config)
    print(json.dumps(config_status_payload(config), ensure_ascii=False, indent=2))
    return 0


def begin_command(args: argparse.Namespace) -> int:
    config = load_config()
    status = config_status_payload(config)
    if status.get("must_stop"):
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return STOP_EXIT
    if args.scope_provider == "jira" and not config["jira_enabled"]:
        raise ValueError("Jira отключена в tracker-config.json")
    ids = unique_keys(args.scope_id)
    if args.scope_kind == "epic" and len(ids) != 1:
        raise ValueError("Для epic требуется ровно один scope-id")
    scope = {"kind": args.scope_kind, "provider": args.scope_provider, "ids": ids, "label": args.label, "source": args.scope_source, "intent": args.intent}
    active = active_run_id()
    if active:
        existing = load_run(active)
        identity_fields = ("kind", "provider", "ids", "intent")
        if all(existing["scope"].get(field) == scope[field] for field in identity_fields):
            payload = status_payload(existing)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return STOP_EXIT if payload.get("must_stop") else 0
        raise ValueError(f"Уже есть незавершённый tracker-run {active}; сначала явно abandon-run")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "created_at": now(), "status": "tracker-read-collecting", "scope": scope,
        "config": config, "steps": [primary_step(scope)], "cards": {provider: [] for provider in PROVIDERS},
        "absent_jira_keys": [], "limitations": [], "failure": None,
        "conflict_resolutions": {}, "following_conflict_choice": None,
    }
    root = run_root(run_id)
    root.mkdir(parents=True)
    save_json(root / "scope.json", {"protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id, "scope": scope})
    save_json(active_path(), {"run_id": run_id})
    save_run(run)
    print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
    return 0


def run_status_command(args: argparse.Namespace) -> int:
    payload = status_payload(load_run(args.run_id))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return STOP_EXIT if payload.get("must_stop") else 0


def abandon_command(args: argparse.Namespace) -> int:
    if not args.analyst_confirmed:
        raise ValueError("abandon-run требует явного подтверждения аналитика через --analyst-confirmed")
    root = run_root(args.run_id)
    if run_path(args.run_id).is_file():
        try:
            run = load_json(run_path(args.run_id))
        except ValueError:
            run = None
        if isinstance(run, dict) and run.get("protocol") == PROTOCOL and run.get("run_id") == args.run_id:
            if run.get("status") == "tracker-read-reconciled":
                raise ValueError("Завершённый tracker-run нельзя abandon")
            run["status"] = "tracker-read-abandoned"
            run["abandoned_at"] = now()
            run["abandon_reason"] = args.reason
            save_run(run)
        else:
            previous_protocol = run.get("protocol", "unknown") if isinstance(run, dict) else "unknown"
            save_json(root / "abandoned.json", {"run_id": args.run_id, "reason": args.reason, "at": now(), "previous_protocol": previous_protocol})
    elif root.is_dir():
        save_json(root / "abandoned.json", {"run_id": args.run_id, "reason": args.reason, "at": now(), "previous_protocol": "unknown"})
    else:
        raise ValueError("Tracker-run не найден")
    release_active(args.run_id)
    print(json.dumps({"status": "tracker-read-abandoned", "run_id": args.run_id, "allowed_next_action": "begin"}, ensure_ascii=False, indent=2))
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    run = load_run(args.run_id)
    if run["status"] != "tracker-read-ready":
        print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
        return 2
    result = reconcile_data(run)
    if first_unresolved_conflict(result):
        run["status"] = "tracker-read-needs-resolution"
        save_run(run)
        print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
        return STOP_EXIT
    root = run_root(args.run_id)
    save_json(root / "reconciled.json", result)
    (root / "report.md").write_text(render_report(result), encoding="utf-8")
    completion = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": args.run_id,
        "status": "tracker-read-reconciled", "workflow_complete": True, "final_response_allowed": True,
        "planning_application_allowed": run["scope"]["intent"] == "update-planning",
        "counts": result["counts"], "summary": result["summary"], "limitations": result["limitations"],
        "reconciled_sha256": file_digest(root / "reconciled.json"), "report_sha256": file_digest(root / "report.md"),
        "response_contract": {"type": "emit-verbatim", "text": official_text(result), "additional_commentary_allowed": False},
        "paths": {"reconciled": str(root / "reconciled.json"), "report": str(root / "report.md"), "completion_status": str(root / "completion-status.json")},
    }
    save_json(root / "completion-status.json", completion)
    run["status"] = "tracker-read-reconciled"
    run["completed_at"] = now()
    save_run(run)
    release_active(args.run_id)
    print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
    return 0


def resolve_conflict_command(args: argparse.Namespace) -> int:
    run = load_run(args.run_id)
    if run["status"] != "tracker-read-needs-resolution":
        raise ValueError("Tracker-run не ожидает решения конфликта")
    task = first_unresolved_conflict(reconcile_data(run))
    if task is None or args.task_key != task["sbertrek_key"]:
        raise ValueError("Решение относится не к текущей конфликтующей задаче")
    if args.apply_to_following and args.choice == "custom":
        raise ValueError("Пользовательский вариант нельзя автоматически применять к последующим задачам")
    if args.choice == "custom":
        if not args.custom_file:
            raise ValueError("Для пользовательского варианта требуется --custom-file")
        values = validated_custom_resolution(task, args.custom_file)
    else:
        if args.custom_file:
            raise ValueError("--custom-file допустим только для пользовательского варианта")
        values = {}
    run.setdefault("conflict_resolutions", {})[args.task_key] = {
        "choice": args.choice, "values": values, "resolved_at": now(),
    }
    if args.apply_to_following:
        run["following_conflict_choice"] = args.choice
    run["status"] = "tracker-read-needs-resolution" if first_unresolved_conflict(reconcile_data(run)) else "tracker-read-ready"
    save_run(run)
    payload = status_payload(run)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return STOP_EXIT if payload.get("must_stop") else 0


def result_status_command(args: argparse.Namespace) -> int:
    root = run_root(args.run_id)
    completion = load_json(root / "completion-status.json")
    if completion.get("protocol") != PROTOCOL or completion.get("run_id") != args.run_id:
        raise ValueError("Официальный completion-status повреждён")
    if completion.get("reconciled_sha256") != file_digest(root / "reconciled.json") or completion.get("report_sha256") != file_digest(root / "report.md"):
        raise ValueError("Итоговые файлы изменены после reconciliation")
    result = load_json(root / "reconciled.json")
    if completion.get("response_contract", {}).get("text") != official_text(result):
        raise ValueError("Официальный текст результата изменён")
    print(json.dumps(completion, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Direct deterministic tracker reconciliation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-config"); init.add_argument("--force", action="store_true"); init.set_defaults(handler=init_config_command)
    commands.add_parser("config-status").set_defaults(handler=config_status_command)
    projects = commands.add_parser("set-projects"); projects.add_argument("--provider", choices=PROVIDERS, required=True); projects.add_argument("projects", nargs="+"); projects.set_defaults(handler=update_config_command)
    jira = commands.add_parser("set-jira-mode"); jira.add_argument("mode", choices=("enabled", "disabled")); jira.set_defaults(handler=update_config_command)
    issue_types = commands.add_parser("set-issue-types"); issue_types.add_argument("issue_types", nargs="+"); issue_types.set_defaults(handler=update_config_command)
    statuses = commands.add_parser("set-statuses"); statuses.add_argument("--provider", choices=PROVIDERS, required=True); statuses.add_argument("--kind", choices=("completed", "excluded"), required=True); statuses.add_argument("--none", action="store_true"); statuses.add_argument("statuses", nargs="*"); statuses.set_defaults(handler=update_config_command)
    commands.add_parser("complete-config").set_defaults(handler=complete_config_command)
    begin = commands.add_parser("begin"); begin.add_argument("--scope-kind", choices=SCOPE_KINDS, required=True); begin.add_argument("--scope-provider", choices=PROVIDERS, required=True); begin.add_argument("--scope-id", action="append", required=True); begin.add_argument("--label", required=True); begin.add_argument("--scope-source", required=True); begin.add_argument("--intent", choices=("read-only", "update-planning"), default="read-only"); begin.set_defaults(handler=begin_command)
    status = commands.add_parser("run-status"); status.add_argument("--run-id", required=True); status.set_defaults(handler=run_status_command)
    ingest = commands.add_parser("ingest"); ingest.add_argument("--run-id", required=True); ingest.add_argument("--step-id", required=True); ingest.add_argument("--response-file", required=True); ingest.add_argument("--response-source", choices=RESPONSE_SOURCES, required=True); ingest.set_defaults(handler=ingest_command)
    error = commands.add_parser("ingest-error"); error.add_argument("--run-id", required=True); error.add_argument("--step-id", required=True); error.add_argument("--error-file", required=True); error.set_defaults(handler=ingest_error_command)
    reconcile = commands.add_parser("reconcile"); reconcile.add_argument("--run-id", required=True); reconcile.set_defaults(handler=reconcile_command)
    resolve = commands.add_parser("resolve-conflict"); resolve.add_argument("--run-id", required=True); resolve.add_argument("--task-key", required=True); resolve.add_argument("--choice", choices=RESOLUTION_CHOICES, required=True); resolve.add_argument("--apply-to-following", action="store_true"); resolve.add_argument("--custom-file"); resolve.set_defaults(handler=resolve_conflict_command)
    result = commands.add_parser("result-status"); result.add_argument("--run-id", required=True); result.set_defaults(handler=result_status_command)
    abandon = commands.add_parser("abandon-run"); abandon.add_argument("--run-id", required=True); abandon.add_argument("--reason", required=True); abandon.add_argument("--analyst-confirmed", action="store_true"); abandon.set_defaults(handler=abandon_command)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return args.handler(args)
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
