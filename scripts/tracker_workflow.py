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
SCHEMA_VERSION = 2
CONFIG_SCHEMA_VERSION = 5
STOP_EXIT = 3
MAX_RESULTS = 50
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
PROVIDERS = ("sbertrek",)
SCOPE_KINDS = ("epic", "tasks", "combined", "release")
RESPONSE_SOURCES = ("mcp-file", "inline-json-capture")
ROLES = ("AN", "BE", "FE", "QA")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
MISSING_SENTINELS = {"not-returned", "not returned", "unknown", "none", "null", "-", "—"}
UNASSIGNED_SENTINELS = {"unassigned", "not assigned", "не назначен", "не назначено"}
ISSUE_COLLECTION_KEYS = ("issues", "items", "results", "values", "units", "data")
ISSUE_KEY_ALIASES = ("key", "unitKey", "unit_key")
SUMMARY_ALIASES = ("summary", "name", "title")
TYPE_ALIASES = ("issue_type", "issueType", "issuetype", "type", "suit")
STATUS_ALIASES = ("status", "state")
CREATED_ALIASES = ("created_at", "createdAt", "created", "creationDate", "creation_date")
UPDATED_ALIASES = ("updated_at", "updatedAt", "updated", "updateDate", "update_date")
ASSIGNEE_ALIASES = ("assignee", "assigned_to", "assignedTo")
ESTIMATE_ALIASES = ("estimate", "story_points", "storyPoints", "story_point")
EPIC_ALIASES = ("epic", "epic_key", "epicKey", "parent")
RELEASE_ALIASES = ("releases", "release", "fix_versions", "fixversion")
SBER_ATTRIBUTE_CODES = {
    "assignee": ("assigned_to", "assignee"),
    "estimate": ("story_points", "story_point", "estimate"),
    "epic": ("epic", "epic_key", "parent"),
    "releases": ("release", "releases", "fix_versions", "fixversion"),
}
SBER_ROLE_FIELDS = {"estimation_for_analyze": "AN", "estimation_for_dev_front": "FE",
                    "estimation_for_dev_back": "BE", "estimation_qa": "QA"}
SBER_FIELDS = ("key", "summary", "suit", "status", "priority", "attributes", "epic", "created_at", "updated_at")
SOURCE_FIELDS = ("summary", "issue_type", "status", "assignee", "estimate", "epic", "releases", "created_at", "updated_at")
DEFAULT_CONFIG = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "primary_provider": "sbertrek",
    "setup_complete": False,
    "projects": {"sbertrek": []},
    "development_issue_types": [],
    "participants": {"sbertrek": {}},
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
    if not isinstance(config, dict) or config.get("schema_version") not in {1, 2, 3, 4, CONFIG_SCHEMA_VERSION}:
        return config, False
    if config.get("schema_version") == CONFIG_SCHEMA_VERSION:
        return config, False
    # Keep the selected provider's settings; obsolete provider data stays in the backup.
    migrated = copy.deepcopy(DEFAULT_CONFIG)
    for field in ("projects", "participants", "status_rules"):
        if "sbertrek" in config.get(field, {}):
            migrated[field]["sbertrek"] = copy.deepcopy(config[field]["sbertrek"])
    for field in ("development_issue_types", "setup_complete"):
        if field in config:
            migrated[field] = copy.deepcopy(config[field])
    return migrated, True


def load_config() -> dict:
    original = load_json(config_path())
    config, changed = migrate_config(original)
    config = validate_config(config)
    if changed:
        backup = config_path().with_name("tracker-config.pre-sber-only." + digest_object(original)[:16] + ".json")
        if not backup.exists():
            save_json(backup, original)
        save_json(config_path(), config)
    return config


def config_gaps(config: dict, include_confirmation: bool = True) -> list[str]:
    gaps: list[str] = []
    if not config["projects"]["sbertrek"]:
        gaps.append("projects.sbertrek")
    if not config["development_issue_types"]:
        gaps.append("development_issue_types")
    for provider in PROVIDERS:
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
        "development_issue_types": "Какие типы объектов трекера являются единицами разработки?",
        "status_rules.sbertrek.completed": "Какие статусы SberTrek однозначно означают завершение разработки?",
        "status_rules.sbertrek.excluded": "Какие статусы SberTrek исключают задачу из выполнения?",
        "setup_complete": "Подтверждаете сохранённую базовую настройку SberTrek?",
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
    if not run_root(run_id).exists():
        path.unlink()
        return None
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
    if payload.get("scope", {}).get("provider") != "sbertrek" or any(
        step.get("provider") != "sbertrek" or "counterpart" in step.get("stage", "") or "discovery" in step.get("stage", "")
        for step in payload.get("steps", [])
    ) or payload.get("scope", {}).get("tracker_mode") == "compare":
        raise ValueError("Исторический многотрекерный run сохранён; продолжение не поддерживается. Требуется явное решение об архивировании текущего сеанса.")
    return payload


def tql_epic(key: str) -> str:
    return f"unit IN linkedUnitsOf(\"unit = '{key}'\", \"Состоит из\")"


def tql_units(keys: list[str]) -> str:
    return " or ".join(f'unit = "{key}"' for key in keys)


def make_step(provider: str, stage: str, query: str, *, requested_keys: list[str] | None = None, epic_key: str | None = None) -> dict:
    return {
        "step_id": digest_object([provider, stage, query])[:16], "provider": provider,
        "stage": stage, "query": query, "requested_keys": requested_keys or [],
        "epic_key": epic_key, "state": "pending", "attempt_errors": [],
    }


def primary_step(scope: dict) -> dict:
    provider, kind, ids = scope["provider"], scope["kind"], scope["ids"]
    if kind == "release":
        value = json.dumps(ids[0], ensure_ascii=False)
        query = f"release-members:{value}"
        return make_step(provider, f"{provider}-source-release", query)
    if kind == "combined" or (kind == "epic" and len(ids) > 1):
        epics = scope.get("epic_ids", []) if kind == "combined" else ids
        clauses = [tql_epic(key) for key in epics]
        if kind == "combined":
            clauses.append(tql_units(ids))
        return make_step(provider, f"{provider}-source-{kind}", " or ".join(f"({clause})" for clause in clauses))
    query = tql_epic(ids[0]) if kind == "epic" else tql_units(ids)
    return make_step(provider, f"{provider}-source-{kind}", query, requested_keys=ids if kind == "tasks" else [], epic_key=ids[0] if kind == "epic" else None)


def pending_step(run: dict) -> dict | None:
    return next((step for step in run["steps"] if step["state"] == "pending"), None)


def add_step(run: dict, step: dict) -> None:
    if not any(existing["stage"] == step["stage"] for existing in run["steps"]):
        run["steps"].append(step)


def card_keys(run: dict, provider: str) -> set[str]:
    return {item["key"] for item in run["cards"][provider]}


def missing_source_keys(run: dict) -> list[str]:
    scope = run["scope"]
    if scope.get("tracker_mode") != "single" or scope["kind"] not in {"tasks", "combined"}:
        return []
    return sorted(set(scope["ids"]) - card_keys(run, scope["provider"]) - set(run.get("confirmed_absent", {})))


def advance(run: dict) -> None:
    if not pending_step(run) and run["status"] == "tracker-read-collecting":
        run["status"] = "tracker-read-ready"


def next_action(run: dict) -> dict:
    if run["status"] == "tracker-read-abandoned":
        return {"type": "stop", "command": None}
    step = pending_step(run)
    ctl = str(Path(__file__).with_name("trackerctl.py"))
    if not step:
        if run["status"] == "tracker-read-ready":
            missing = missing_source_keys(run)
            if missing:
                return {"type": "verify-missing-tasks", "provider": run["scope"]["provider"],
                        "keys": missing, "absence_proven": False, "deletion_allowed": False,
                        "contract": str(Path(__file__).resolve().parents[1] / "core/tracker-adaptive.md"),
                        "retry_command": [sys.executable, ctl, "retry-missing", "--run-id", run["run_id"]]}
            return {"type": "reconcile", "command": [sys.executable, ctl, "reconcile", "--run-id", run["run_id"]]}
        if run["status"] == "tracker-read-reconciled":
            return {"type": "result-status", "command": [sys.executable, ctl, "result-status", "--run-id", run["run_id"]]}
        return {"type": "stop", "command": None}
    tool, arguments = "issue.exportJson", {"query": step["query"], "fields": list(SBER_FIELDS), "max_results": MAX_RESULTS}
    if run.get("collection_mode") == "adaptive":
        return {
            "type": "collect-tracker-data", "provider": step["provider"],
            "step_id": step["step_id"], "stage": step["stage"],
            "selection": {"query_semantics": step["query"], "requested_keys": step["requested_keys"], "epic_key": step["epic_key"]},
            "contract": str(Path(__file__).resolve().parents[1] / "core/tracker-adaptive.md"),
            "requirements": ["read-only", "exact-scope", "all-statuses", "full-response", "report-pagination",
                             "request-all-role-estimates", "request-attributes-or-equivalent-estimate-fields",
                             "verify-returned-projection", "missing-field-is-not-zero", "preserve-raw-response",
                             "task-role-from-prefix-not-assignee", "skip-outside-supported-roles",
                             "history-before-status-or-date-application"],
            **({"release_membership": {"accepted_evidence": ["card-fields", "selected-query", "links"],
                                        "mapping_location": "call-file.release_membership",
                                        "raw_response_edits_allowed": False,
                                        "contract": "core/tracker-release.md"}}
               if step["stage"].endswith("source-release") else {}),
            "ingest_command": [sys.executable, ctl, "ingest", "--run-id", run["run_id"], "--step-id", step["step_id"], "--response-file", "<full-json-path>", "--response-source", "mcp-file", "--call-file", "<call-json-path>"],
        }
    return {
        "type": "mcp-query", "provider": step["provider"], "stage": step["stage"],
        "step_id": step["step_id"], "tool": tool, "arguments": arguments,
        "ingest_command": [sys.executable, ctl, "ingest", "--run-id", run["run_id"], "--step-id", step["step_id"], "--response-file", "<full-json-path>", "--response-source", "<mcp-file|inline-json-capture>"],
    }


def status_payload(run: dict) -> dict:
    action = next_action(run)
    payload = {
        "protocol": PROTOCOL, "run_id": run["run_id"], "status": run["status"], "scope": run['scope'],
        "workflow_complete": run["status"] == "tracker-read-reconciled",
        "final_response_allowed": False,
        "must_stop": False,
        "allowed_next_action": action["type"], "next_action": action,
        "limitations": sorted(set(run["limitations"])),
        "paths": {"scope": str(run_root(run["run_id"]) / "scope.json"), "run_status": str(run_root(run["run_id"]) / "run-status.json"), "providers": str(run_root(run["run_id"]) / "providers")},
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
    prefix = re.match(r"\s*((?:\[[^\]]+\]\s*)+)", summary)
    bracketed = re.findall(r"\[\s*([^\]]+)\s*\]", prefix.group(1)) if prefix else []
    if bracketed:
        roles = {role for item in bracketed if (role := normalized_role_marker(item))}
        return next(iter(roles)) if len(roles) == 1 else None
    match = re.match(r"\s*([^\s:_/\-]+)(?=[\s:_/\-])", summary)
    return normalized_role_marker(match.group(1)) if match else None


def sber_estimate_definition(code: str, name: str) -> dict | None:
    if code.casefold() in SBER_ROLE_FIELDS:
        return {"name": name, "role": SBER_ROLE_FIELDS[code.casefold()], "unit": "person-days"}
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
    records, present = attribute_records(record)
    role_states = {role: "absent" if present else "not-returned" for role in ROLES}
    definitions = []
    for attribute in records:
        definition = sber_estimate_definition(attribute["code"], attribute["name"])
        if definition:
            definitions.append((attribute["code"], definition, True, attribute["value"]))
    # Some exports flatten custom attributes into the card or its fields object.
    # Keep attribute values authoritative when the same code occurs twice.
    attribute_codes = {item[0].casefold() for item in definitions}
    for code, value in fields.items():
        if code.casefold() in SBER_ROLE_FIELDS and code.casefold() not in attribute_codes:
            definition = sber_estimate_definition(code, code)
            if definition:
                definitions.append((code, definition, True, value))
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
        candidate = general_candidates[0]
        general = {"value": candidate["value"], "unit": candidate["unit"]}
        general_state = "value"
    elif general is None:
        present = attribute_records(record)[1]
        general_state = "absent" if present or alias_value(fields, ESTIMATE_ALIASES)[0] else "not-returned"
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
    if provider != "sbertrek":
        raise ValueError("Поддерживается только sbertrek")
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
    created_at, created_state = optional_text(record, CREATED_ALIASES)
    updated_at, updated_state = optional_text(record, UPDATED_ALIASES)
    return {
        "key": key,
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
        "attributes_returned": attributes_present,
    }


def response_json(raw: bytes, path: Path) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError(f"Ответ MCP не является UTF-8: {path}, байт {error.start}") from error
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Ответ MCP не является полным JSON: {path}, строка {error.lineno}, "
            f"столбец {error.colno}: {error.msg}"
        ) from error


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
        payload = response_json(raw, path)
    return path, raw, payload


def fail_run(run: dict, reason: str) -> None:
    run["status"] = "tracker-read-failed"
    run["failure"] = {"reason": reason, "at": now()}
    save_run(run)


def validate_records_for_step(step: dict, cards: list[dict]) -> None:
    if step["provider"] != "sbertrek":
        raise ValueError("Поддерживается только sbertrek")
    if step["stage"].endswith("source-tasks") or step["stage"].startswith("sbertrek-missing-"):
        unexpected = sorted({item["key"] for item in cards} - set(step["requested_keys"]))
        if unexpected:
            raise ValueError(f"Ответ содержит задачи вне исходной области: {', '.join(unexpected)}")


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
        if working.get("collection_mode") == "adaptive":
            from tracker_history import validate_call
            if not args.call_file:
                raise ValueError("Adaptive collection requires --call-file")
            _, _, call = response_file(args.call_file, args.run_id)
            validate_call(call, step["provider"])
            if call.get("selection") != step["query"]:
                raise ValueError("Call must preserve the returned selection semantics")
            step["call"] = call
        records, record_path = full_issue_records(payload)
        cards = [compact_issue(record, step["provider"], forced_epic=step.get("epic_key")) for record in records]
        if working.get("collection_mode") == "adaptive" and working["scope"]["intent"] == "update-planning":
            tool = step["call"]["tool"].casefold().replace("_", ".")
            missing_estimates = collection_diagnostics({"cards": {"sbertrek": cards}})["estimate_fields_not_returned"]
            if "exportjson" in tool and missing_estimates:
                raise ValueError(
                    "estimate-projection-not-returned: " + ", ".join(missing_estimates)
                    + ". Экспорт не содержит полей оценок. Проверь текущую схему MCP и запроси "
                    "attributes либо эквивалентные поля; повтори полный ответ на тот же pending step. "
                    "Явный null допустим, неполученное поле не доказывает отсутствия оценки."
                )
        if step["stage"].endswith("source-release"):
            from tracker_release_evidence import validate_membership
            validate_membership(working, step, cards)
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
        if step["requested_keys"] and step["stage"].endswith("source-tasks") and step["provider"] == "sbertrek":
            missing = sorted(set(step["requested_keys"]) - returned)
            working["limitations"].extend(f"sbertrek-key-not-returned:{key}" for key in missing)
        step.update({"state": "complete", "response_source": args.response_source, "response_path": str(path), "response_sha256": response_sha, "response_bytes": len(raw), "record_path": record_path, "returned_count": len(records), "completed_at": now()})
        if len(records) == MAX_RESULTS:
            working["limitations"].append(f"{step['provider']}-result-limit-reached:{MAX_RESULTS}")
        working["status"] = "tracker-read-collecting"
        working["failure"] = None
        advance(working)
        save_run(working)
    except Exception as error:
        fail_run(run, str(error))
        raise
    print(json.dumps(status_payload(working), ensure_ascii=False, indent=2))
    return 0




def retry_missing_command(args: argparse.Namespace) -> int:
    run = load_run(args.run_id)
    missing = missing_source_keys(run)
    if run["status"] != "tracker-read-ready" or not missing:
        raise ValueError("Нет неполученных ключей завершённого чтения")
    provider = run["scope"]["provider"]
    query = tql_units(missing)
    add_step(run, make_step(provider, f"{provider}-missing-{len(run['steps'])}", query, requested_keys=missing))
    run["status"] = "tracker-read-collecting"
    save_run(run)
    print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
    return 0


def confirm_absence_command(args: argparse.Namespace) -> int:
    from tracker_history import validate_call

    run = load_run(args.run_id)
    scope = run["scope"]
    if scope.get("tracker_mode") != "single" or scope["kind"] not in {"tasks", "combined"}:
        raise ValueError("Подтверждение отсутствия требует однотрекерной области tasks")
    if scope["kind"] == "combined" and pending_step(run) and pending_step(run)["stage"].endswith("source-combined"):
        raise ValueError("Сначала получи объединённую область; отсутствие проверяется отдельным повторным чтением задач")
    if run["status"] not in {"tracker-read-collecting", "tracker-read-ready", "tracker-read-failed"}:
        raise ValueError("Нельзя менять завершённый run")
    key = normalize_key(args.key)
    if key not in scope["ids"] or key in card_keys(run, scope["provider"]):
        raise ValueError("Ключ вне области или карточка уже получена")
    if not args.analyst_confirmed or not args.decision_source.strip():
        raise ValueError("Требуется явное подтверждение удаления аналитиком и источник решения")
    _, raw, _ = response_file(args.response_file, args.run_id, require_json=False)
    if not re.search(r"(?<![A-Z0-9_-])" + re.escape(key) + r"(?![A-Z0-9_-])", raw.decode("utf-8")):
        raise ValueError("Ответ не содержит подтверждаемого ключа")
    _, _, call = response_file(args.call_file, args.run_id)
    validate_call(call, scope["provider"])
    confirmation = {"key": key, "provider": scope["provider"], "sha256": digest_bytes(raw),
                    "call": call, "decision_source": args.decision_source,
                    "analyst_confirmed": True}
    previous = run.setdefault("confirmed_absent", {}).get(key)
    if previous and previous != confirmation:
        raise ValueError("Подтверждение уже сохранено с другим доказательством")
    run["confirmed_absent"][key] = confirmation
    step = pending_step(run)
    if step:
        remaining = [item for item in step["requested_keys"] if item not in run["confirmed_absent"]]
        step["requested_keys"] = remaining
        step["query"] = tql_units(remaining)
        if not remaining:
            step.update(state="complete", returned_count=0, completed_at=now())
    run["status"] = "tracker-read-collecting"
    run["failure"] = None
    advance(run)
    save_run(run)
    print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
    return 0


def canonical_estimate(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    normalized = " ".join(str(result.get("unit")).casefold().replace("-", " ").replace("_", " ").split())
    if normalized in {"sp", "story point", "story points", "person day", "person days", "человекодень", "человекодни", "чел день", "чел дни"}:
        result["unit"] = "story-points"
    return result


def development_state(item: dict, config: dict) -> dict:
    provider = "sbertrek"
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
        identity = issue.get("sbertrek_key")
        for role in ROLES:
            if issue.get("task_role") and role in {"BE", "FE"} and role != issue["task_role"]:
                continue
            estimate = issue.get("role_estimates", {}).get(role)
            if not isinstance(estimate, dict) or not isinstance(estimate.get("value"), (int, float)) or estimate["value"] <= 0:
                continue
            result.append({
                "work_item_id": f"{identity}/{role}", "tracker_key": identity,
                "sbertrek_key": issue.get("sbertrek_key"),
                "role": role, "summary": prefixed_summary(role, issue["summary"]),
                "estimate": estimate, "assignee": issue.get("assignee"),
                "status": issue.get("status"), "development": issue["development"],
            })
    return result


def reconcile_data(run: dict) -> dict:
    sber_items = run["cards"]["sbertrek"]
    issues, excluded = [], []
    for card in sber_items:
        values = {field: canonical_estimate(card.get(field)) if field == "estimate" else card.get(field)
                  for field in SOURCE_FIELDS}
        roles = {role: {**canonical_estimate(value), "source": "sbertrek"}
                 for role, value in card.get("role_estimates", {}).items() if value is not None}
        issues.append({
            "sbertrek_key": card["key"], **values,
            "sources": {field: "sbertrek" if value not in (None, "", [], {}) else None
                        for field, value in values.items()},
            "role_estimates": roles,
            "role_estimate_sources": {role: "sbertrek" if role in roles else None for role in ROLES},
            "estimate_fields": [{**field, "provider": "sbertrek"} for field in card.get("estimate_fields", [])],
            "development": development_state(card, run["config"]),
        })
    skipped = []
    if run.get("role_policy") == "prefix-v1":
        skipped = [{**issue, "reason": "outside-supported-roles"} for issue in issues
                   if role_from_summary(issue.get("summary") or "") is None]
        issues = [issue for issue in issues if role_from_summary(issue.get("summary") or "") is not None]
        for issue in issues:
            issue["task_role"] = role_from_summary(issue["summary"])
    issues.sort(key=lambda item: item["sbertrek_key"])
    for key, evidence in run.get("confirmed_absent", {}).items():
        excluded.append({"sbertrek_key": key, "reason": "confirmed-source-deletion", "evidence": evidence})
    role_totals = {role: 0.0 for role in ROLES}
    overall_total = 0.0
    for issue in issues:
        has_role_estimate = False
        for role, estimate in issue.get("role_estimates", {}).items():
            if estimate.get("unit") in {"story-points", "person-days"}:
                value = float(estimate["value"])
                role_totals[role] += value
                overall_total += value
                has_role_estimate = True
        estimate = issue.get("estimate")
        if not has_role_estimate and isinstance(estimate, dict) and estimate.get("unit") == "story-points":
            overall_total += float(estimate["value"])
    limitations = sorted(set(run["limitations"] + ["history-not-collected"] + [
        f"general-estimate-role-unresolved:{item.get('sbertrek_key')}"
        for item in issues if item.get("estimate") and not item.get("role_estimates")
    ]))
    counts = {
        "sbertrek": len(sber_items),
        "issues": len(issues), "excluded": len(excluded),
    }
    role_work_items = work_items(issues)
    counts["work_items"] = len(role_work_items)
    summary = {"story_points_total": overall_total, "role_totals_person_days": role_totals}
    if run.get("estimate_reporting") == "known-values-v1":
        coverage = {role: sum(role in issue.get("role_estimates", {}) for issue in issues) for role in ROLES}
        summary["estimate_coverage"] = coverage
        summary["role_totals_person_days"] = {role: role_totals[role] if coverage[role] else None for role in ROLES}
        if not any(coverage.values()) and not any(issue.get("estimate") for issue in issues):
            summary["story_points_total"] = None
    return {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run["run_id"],
        "scope": run["scope"], "issues": issues, "work_items": role_work_items,
        "excluded": excluded, "counts": counts,
        **({"skipped": skipped} if run.get("role_policy") == "prefix-v1" else {}),
        "summary": summary,
        "limitations": limitations,
    }


def render_report(result: dict) -> str:
    lines = [
        "# Проверка задач SberTrek", "", f"Run ID: `{result['run_id']}`", "",
        f"- SberTrek: {result['counts']['sbertrek']}",
        f"- Итоговых задач: {result['counts']['issues']}",
        f"- Исключено: {result['counts']['excluded']}",
        f"- Общая оценка: {result['summary']['story_points_total'] if result['summary']['story_points_total'] is not None else 'неизвестна'} story-points", "",
        "| SberTrek | Название | Статус | Исполнитель | AN | BE | FE | QA |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for item in result["issues"]:
        assignee = (item.get("assignee") or {}).get("name") or "—"
        values = [str(item.get("role_estimates", {}).get(role, {}).get("value", "—")) for role in ROLES]
        lines.append(f"| {item.get('sbertrek_key') or '—'} | {item['summary']} | {item['status']} | {assignee} | {' | '.join(values)} |")
    for item in result.get("skipped", []):
        lines.append(f"Пропущено вне AN/BE/FE/QA: {item.get('sbertrek_key')} — {item.get('summary')}")
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
    estimate_line = f"Общая оценка: {result['summary']['story_points_total']} SP. Ролевые оценки: AN {totals['AN']}, BE {totals['BE']}, FE {totals['FE']}, QA {totals['QA']} человекодней."
    if "estimate_coverage" in result["summary"]:
        rendered = ", ".join(f"{role} {totals[role] if totals[role] is not None else 'неизвестно'}" for role in ROLES)
        overall = result["summary"]["story_points_total"]
        estimate_line = (f"Сумма известных оценок: {overall if overall is not None else 'неизвестно'} SP. "
                         f"Ролевые оценки: {rendered} человекодней. Пустые оценки не равны нулю; полнота оценки не подтверждена.")
    lines = [
        f"Сверка завершена. Run ID: {result['run_id']}",
        f"SberTrek: {counts['sbertrek']}; исходных задач: {counts['issues']}; ролевых задач: {counts['work_items']}; исключено: {counts['excluded']}.",
        estimate_line,
        "Задачи:",
    ]
    for item in result["issues"]:
        sbertrek_key = item.get("sbertrek_key") or "-"
        summary = " ".join(str(item.get("summary") or "-").split())
        status = " ".join(str(item.get("status") or "-").split())
        assignee = " ".join(str((item.get("assignee") or {}).get("name") or "-").split())
        estimates = ", ".join(
            f"{role} {item.get('role_estimates', {}).get(role, {}).get('value', '-')}"
            for role in ROLES
        )
        lines.append(
            f"- SberTrek {sbertrek_key}; {summary}; "
            f"статус: {status}; исполнитель: {assignee}; оценки: {estimates}."
        )
    for item in result.get("skipped", []):
        lines.append(f"Пропущено вне AN/BE/FE/QA: {item.get('sbertrek_key')} — {item.get('summary')}")
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
                f"- SberTrek {item.get('sbertrek_key') or '-'}; "
                f"причина: {item['reason']}."
            )
    if result["limitations"]:
        lines.append("Ограничения: " + ", ".join(result["limitations"]) + ".")
    return "\n".join(lines)


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


def scope_preview_command(args: argparse.Namespace) -> int:
    config = load_config()
    status = config_status_payload(config)
    if status.get("must_stop"):
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return STOP_EXIT
    if active_path().is_file():
        marker = load_json(active_path())
        active = marker.get("run_id") if isinstance(marker, dict) else None
        if not isinstance(active, str) or not RUN_ID.fullmatch(active):
            raise ValueError("Повреждён tracker-active-run.json")
        if run_root(active).exists():
            existing = load_run(active)
            if existing["status"] != "tracker-read-reconciled":
                payload = status_payload(existing)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return STOP_EXIT if payload.get("must_stop") else 0
    from tracker_scope import preview_scope

    payload = preview_scope(Path(args.project_root), args.provider, args.quarter, args.feature, args.delivery)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def update_config_command(args: argparse.Namespace) -> int:
    config = load_config()
    if args.command == "set-projects":
        config["projects"][args.provider] = sorted(set(args.projects))
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
    if args.scope_kind == 'release' and args.intent is None:
        raise ValueError('Release begin requires explicit --intent: update-planning for actualization, read-only for inspection')
    args.intent = args.intent or 'read-only'
    if args.scope_kind == "release":
        if not args.adaptive or len(args.scope_id) != 1:
            raise ValueError("release requires one release and a single-provider adaptive run")
        ids = args.scope_id
        if not ids[0].strip() or ids[0] != ids[0].strip() or any(ord(char) < 32 for char in ids[0]):
            raise ValueError("Release identity must be a nonempty single-line name or ID")
    else:
        ids = unique_keys(args.scope_id)
    if args.scope_kind == "epic" and len(ids) != 1 and not args.adaptive:
        raise ValueError("Для epic требуется ровно один scope-id")
    scope = {"kind": args.scope_kind, "provider": args.scope_provider, "ids": ids, "label": args.label, "source": args.scope_source, "intent": args.intent}
    if args.scope_kind == "combined":
        if not args.adaptive or not args.epic_id:
            raise ValueError("combined требует однотрекерный adaptive run и --epic-id")
        scope["epic_ids"] = unique_keys(args.epic_id)
    elif args.epic_id:
        raise ValueError("--epic-id используется только с combined")
    scope["tracker_mode"] = "single"
    active = active_run_id()
    if active:
        existing = load_run(active)
        identity_fields = ("kind", "provider", "ids", "intent", "epic_ids")
        if all(existing["scope"].get(field) == scope.get(field) for field in identity_fields):
            if existing["scope"].get("tracker_mode", "single") != "single":
                raise ValueError("Режим существующего run отличается; не заменяй его без явного abandon-run")
            payload = status_payload(existing)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return STOP_EXIT if payload.get("must_stop") else 0
        raise ValueError(f"Уже есть незавершённый tracker-run {active}; сначала явно abandon-run")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "created_at": now(), "status": "tracker-read-collecting", "scope": scope,
        "config": config, "steps": [primary_step(scope)], "cards": {provider: [] for provider in PROVIDERS},
        "limitations": [], "failure": None,
        "collection_mode": "adaptive" if args.adaptive else "legacy",
        **({"role_policy": "prefix-v1", "estimate_reporting": "known-values-v1"} if args.adaptive else {}),
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
    if run["status"] != "tracker-read-ready" or missing_source_keys(run):
        print(json.dumps(status_payload(run), ensure_ascii=False, indent=2))
        return 2
    result = reconcile_data(run)
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


def verified_result(run_id: str) -> tuple[dict, dict]:
    root = run_root(run_id)
    run = load_run(run_id)
    if run.get("status") != "tracker-read-reconciled":
        raise ValueError("Сверка tracker-run ещё не завершена")
    completion = load_json(root / "completion-status.json")
    if completion.get("protocol") != PROTOCOL or completion.get("run_id") != run_id:
        raise ValueError("Официальный completion-status повреждён")
    if completion.get("reconciled_sha256") != file_digest(root / "reconciled.json") or completion.get("report_sha256") != file_digest(root / "report.md"):
        raise ValueError("Итоговые файлы изменены после reconciliation")
    result = load_json(root / "reconciled.json")
    if result.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Исторический результат сохранён без изменений; применение результата прежней схемы не поддерживается. Новый сбор требует отдельного запроса.")
    if result.get("protocol") != PROTOCOL or result.get("run_id") != run_id or result.get("scope") != run["scope"]:
        raise ValueError("Область результата не совпадает с tracker-run")
    if result != reconcile_data(run):
        raise ValueError("Результат не воспроизводится из сохранённой сверки")
    planning_allowed = run["scope"]["intent"] == "update-planning"
    expected = {
        "status": "tracker-read-reconciled",
        "workflow_complete": True,
        "final_response_allowed": True,
        "planning_application_allowed": planning_allowed,
        "counts": result["counts"],
        "summary": result["summary"],
        "limitations": result["limitations"],
    }
    if any(
        completion.get(field) is not value if isinstance(value, bool) else completion.get(field) != value
        for field, value in expected.items()
    ):
        raise ValueError("Разрешения или сводка completion-status не совпадают с результатом сверки")
    if completion.get("response_contract", {}).get("text") != official_text(result):
        raise ValueError("Официальный текст результата изменён")
    application = run_root(run_id) / "application-state.json"
    if application.exists():
        saved = load_json(application)
        if saved.get("reconciled_sha256") != completion["reconciled_sha256"]:
            raise ValueError("Application decision belongs to another reconciled result")
        completion["application_state"] = saved
        if saved["state"] == "paused":
            completion["planning_application_allowed"] = False
    from tracker_sessions import applied_execution
    applied = applied_execution(run_id)
    if applied:
        completion['application_state'] = applied
        completion['planning_application_allowed'] = False
    completion['scope'] = run['scope']
    return completion, result


def collection_diagnostics(run: dict) -> dict:
    cards = run["cards"]["sbertrek"]
    missing = [card["key"] for card in cards
               if role_from_summary(card.get("summary", ""))
               and card.get("field_observations", {}).get("estimate") == "not-returned"
               and all(card.get("role_estimate_observations", {}).get(role) == "not-returned"
                       for role in ROLES)]
    return {
        "estimate_fields_not_returned": sorted(missing),
        "projection_review_required": bool(missing),
        "message": (
            "Поля оценок не получены; это не пустые оценки в трекере. Проверь текущую схему MCP "
            "и проекцию: attributes либо поддерживаемые поля оценок. Не копируй параметры "
            "старого вызова без проверки. Не заменяй ответ завершённого шага. Для уже завершённого сбора "
            "сохрани ограничение оценок; отдельные снимки истории не дополняют оценки результата. "
            "Проверка проекции не отменяет сбор истории, включая задачи created/todo."
        ) if missing else None,
    }


def result_status_command(args: argparse.Namespace) -> int:
    completion, result = verified_result(args.run_id)
    root = run_root(args.run_id)
    planning_allowed = completion["planning_application_allowed"]
    preview_command = "release-preview" if result["scope"]["kind"] == "release" else "execution-preview"
    completion["planning_update"] = {
        "state": completion.get("application_state", {}).get("state", "pending" if planning_allowed else "not-requested"),
        "actualization_complete": completion.get('application_state', {}).get('state') == 'applied',
        "next_action": {
            "type": preview_command,
            "command": [
                sys.executable, str(Path(__file__).with_name("trackerctl.py")),
                preview_command, "--run-id", args.run_id,
                "--project-root", "<resolved-project-root>",
            ],
            "required_selection": "release members" if preview_command == "release-preview" else "--quarter YYYY-QN or --feature feature",
            "mode": "execution-update",
            "contract": str(Path(__file__).resolve().parents[1] / "core" / "tracker-actualization.md"),
            "run_id": args.run_id,
            "reconciled": str(root / "reconciled.json"),
            "reconciled_sha256": completion["reconciled_sha256"],
        } if planning_allowed else None,
    }
    # Stored completion describes collection only. Expose the requested workflow
    # separately, without rewriting historical results or their checksums.
    completion["collection_complete"] = True
    completion["report_allowed"] = True
    completion["collection_diagnostics"] = collection_diagnostics(load_run(args.run_id))
    completion["next_action"] = completion["planning_update"]["next_action"]
    completion["workflow_complete"] = result["scope"]["intent"] == "read-only" or completion["planning_update"]["actualization_complete"]
    completion["final_response_allowed"] = not planning_allowed
    completion["response_contract"] = {
        **completion["response_contract"],
        "delivery": "progress" if planning_allowed else "final",
        "must_continue": planning_allowed,
    }
    print(json.dumps(completion, ensure_ascii=False, indent=2))
    return 0


def application_state_command(args: argparse.Namespace) -> int:
    if not args.analyst_confirmed or not args.reason.strip():
        raise ValueError("Application state requires the analyst's explicit decision and reason")
    completion, result = verified_result(args.run_id)
    if completion.get('application_state', {}).get('state') == 'applied':
        raise ValueError('Run application was completed by collaboration finish; it cannot be resumed')
    if result["scope"]["intent"] != "update-planning":
        raise ValueError("Read-only run has no application phase")
    decision = {"state": args.state, "reason": args.reason, "at": now(),
                "reconciled_sha256": completion["reconciled_sha256"]}
    root = run_root(args.run_id)
    save_json(root / "application-decisions" / (digest_object(decision) + ".json"), decision)
    save_json(root / "application-state.json", decision)
    print(json.dumps({"status": "tracker-application-" + args.state, "collection_preserved": True,
                      "analytics_writes_performed": False, "decision": decision}, ensure_ascii=False, indent=2))
    return 0


def execution_preview_command(args: argparse.Namespace) -> int:
    completion, result = verified_result(args.run_id)
    if completion["planning_application_allowed"] is not True:
        raise ValueError("Read-only сверка не разрешает фазу актуализации")
    from tracker_execution import preview_execution

    reviewed = dict(args.reviewed_registry)
    if len(reviewed) != len(args.reviewed_registry):
        raise ValueError("Реестр указан для подтверждения несколько раз")
    if reviewed and (not args.analyst_confirmed or not args.expected_head):
        raise ValueError("Проверка изменённых реестров требует явного подтверждения аналитика и --expected-head")
    if not reviewed and (args.analyst_confirmed or args.expected_head):
        raise ValueError("Подтверждение требует точных --reviewed-registry")
    payload = preview_execution(
        Path(args.project_root), args.quarter, args.feature, result, reviewed, args.expected_head, args.delivery,
    )
    payload["run_id"] = args.run_id
    payload["reconciled_sha256"] = completion["reconciled_sha256"]
    if payload["ownership_ready"] and load_run(args.run_id).get("collection_mode") == "adaptive":
        payload["date_proposals_allowed"] = False
        payload["fact_priority"] = ["analyst-confirmation", "assignment-and-status-history", "current-state-only"]
        payload["next_action"] = {
            "type": "collect-history", "contract": str(Path(__file__).resolve().parents[1] / "core/tracker-adaptive.md"),
            "provider": result["scope"]["provider"],
            "required_before_date_proposals": True,
            "required_before_status_proposals": True,
            "command": [sys.executable, str(Path(__file__).with_name("trackerctl.py")), "history-review",
                        "--run-id", args.run_id, "--project-root", args.project_root,
                        "--manifest", "<history-manifest-json>"],
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Direct deterministic tracker reconciliation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-config"); init.add_argument("--force", action="store_true"); init.set_defaults(handler=init_config_command)
    commands.add_parser("config-status").set_defaults(handler=config_status_command)
    preview = commands.add_parser("scope-preview")
    preview.add_argument("--project-root", required=True)
    preview.add_argument("--provider", choices=PROVIDERS, default="sbertrek")
    preview.add_argument("--quarter")
    preview.add_argument("--feature")
    preview.add_argument('--delivery', action='append', help='Exact delivery key; repeat for several deliveries')
    preview.set_defaults(handler=scope_preview_command)
    execution = commands.add_parser("execution-preview")
    execution.add_argument("--run-id", required=True)
    execution.add_argument("--project-root", required=True)
    execution.add_argument("--quarter")
    execution.add_argument("--feature")
    execution.add_argument('--delivery', action='append', help='Exact delivery key; repeat for several deliveries')
    execution.add_argument("--reviewed-registry", nargs=2, action="append", default=[], metavar=("PATH", "SHA256"))
    execution.add_argument("--expected-head")
    execution.add_argument("--analyst-confirmed", action="store_true")
    execution.set_defaults(handler=execution_preview_command)
    projects = commands.add_parser("set-projects"); projects.add_argument("--provider", choices=PROVIDERS, default="sbertrek"); projects.add_argument("projects", nargs="+"); projects.set_defaults(handler=update_config_command)
    issue_types = commands.add_parser("set-issue-types"); issue_types.add_argument("issue_types", nargs="+"); issue_types.set_defaults(handler=update_config_command)
    statuses = commands.add_parser("set-statuses"); statuses.add_argument("--provider", choices=PROVIDERS, default="sbertrek"); statuses.add_argument("--kind", choices=("completed", "excluded"), required=True); statuses.add_argument("--none", action="store_true"); statuses.add_argument("statuses", nargs="*"); statuses.set_defaults(handler=update_config_command)
    commands.add_parser("complete-config").set_defaults(handler=complete_config_command)
    from tracker_sessions import resume_command
    resume = commands.add_parser('resume')
    resume.add_argument('--scope-kind', choices=SCOPE_KINDS, required=True)
    resume.add_argument('--scope-provider', choices=PROVIDERS, default="sbertrek")
    resume.add_argument('--scope-id', action='append', required=True)
    resume.add_argument('--intent', choices=('read-only', 'update-planning'), required=True)
    resume.add_argument('--epic-id', action='append', default=[])
    resume.add_argument('--run-id')
    resume.set_defaults(handler=resume_command)
    begin = commands.add_parser("begin"); begin.add_argument("--scope-kind", choices=SCOPE_KINDS, required=True); begin.add_argument("--scope-provider", choices=PROVIDERS, default="sbertrek"); begin.add_argument("--scope-id", action="append", required=True); begin.add_argument("--label", required=True); begin.add_argument("--scope-source", required=True); begin.add_argument("--intent", choices=("read-only", "update-planning")); begin.set_defaults(handler=begin_command)
    collection = begin.add_mutually_exclusive_group()
    collection.add_argument("--adaptive", dest="adaptive", action="store_true", default=True)
    collection.add_argument("--legacy", dest="adaptive", action="store_false")
    begin.add_argument("--epic-id", action="append", default=[])
    from tracker_safety import application_preflight_command
    preflight = commands.add_parser("application-preflight")
    preflight.add_argument("--project-root", required=True)
    preflight.add_argument("--feature", "--delivery", dest='feature', required=True)
    preflight.set_defaults(handler=application_preflight_command)
    absence = commands.add_parser("confirm-absence")
    absence.add_argument("--run-id", required=True)
    absence.add_argument("--key", required=True)
    absence.add_argument("--response-file", required=True)
    absence.add_argument("--call-file", required=True)
    absence.add_argument("--decision-source", required=True)
    absence.add_argument("--analyst-confirmed", action="store_true")
    absence.set_defaults(handler=confirm_absence_command)
    retry = commands.add_parser("retry-missing")
    retry.add_argument("--run-id", required=True)
    retry.set_defaults(handler=retry_missing_command)
    status = commands.add_parser("run-status"); status.add_argument("--run-id", required=True); status.set_defaults(handler=run_status_command)
    ingest = commands.add_parser("ingest"); ingest.add_argument("--run-id", required=True); ingest.add_argument("--step-id", required=True); ingest.add_argument("--response-file", required=True); ingest.add_argument("--response-source", choices=RESPONSE_SOURCES, required=True); ingest.set_defaults(handler=ingest_command)
    ingest.add_argument("--call-file")
    history = commands.add_parser("history-review")
    history.add_argument("--run-id", required=True)
    history.add_argument("--project-root", required=True)
    history.add_argument("--manifest", required=True)
    from tracker_history import history_review_command
    history.set_defaults(handler=history_review_command)
    qa_check = commands.add_parser('qa-application-check')
    qa_check.add_argument('--review-file', required=True)
    qa_check.add_argument('--project-root', required=True)
    from tracker_qa_application import check_qa_application
    qa_check.set_defaults(handler=check_qa_application)
    release = commands.add_parser("release-preview")
    release.add_argument("--run-id", required=True)
    release.add_argument("--project-root", required=True)
    release.add_argument("--decisions")
    release.add_argument("--membership")
    from tracker_release import release_preview_command
    release.set_defaults(handler=release_preview_command)
    reconcile = commands.add_parser("reconcile"); reconcile.add_argument("--run-id", required=True); reconcile.set_defaults(handler=reconcile_command)
    result = commands.add_parser("result-status"); result.add_argument("--run-id", required=True); result.set_defaults(handler=result_status_command)
    application = commands.add_parser("application-state")
    application.add_argument("--run-id", required=True)
    application.add_argument("--state", choices=("paused", "pending"), required=True)
    application.add_argument("--reason", required=True)
    application.add_argument("--analyst-confirmed", action="store_true")
    application.set_defaults(handler=application_state_command)
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
