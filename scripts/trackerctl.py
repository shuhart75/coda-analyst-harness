#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "targeted-tracker-v3"
SCHEMA_VERSION = 8
CONFIG_SCHEMA_VERSION = 4
STOP_EXIT = 3
PROVIDERS = ("sbertrek", "jira")
SCOPE_KINDS = ("epic", "tasks")
OBSERVATION_STATES = ("value", "absent", "not-returned")
MISSING_SENTINELS = {"not-returned", "not returned", "unknown", "none", "null", "-", "—"}
UNASSIGNED_SENTINELS = {"unassigned", "not assigned", "не назначен", "не назначено"}
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
SP_UNITS = {
    "sp", "story point", "story points", "person day", "person days",
    "человеко день", "человеко дни", "человекодень", "человекодни",
    "чел день", "чел дни",
}
HISTORY_BATCH_SIZE = 8
ESTIMATE_ROLES = ("AN", "BE", "FE", "QA")
DEVELOPMENT_DECISION_STATES = ("completed", "in-progress", "not-started", "unknown")
DEVELOPMENT_DECISION_CHOICES = ("sbertrek", "jira", "custom")
MERGED_FIELDS = (
    "summary", "issue_type", "status", "assignee", "estimate", "epic",
    "releases", "created_at", "updated_at",
)
RAW_RESPONSE_MAX_BYTES = 64 * 1024 * 1024
SBER_EXPORT_MAX_RESULTS = 50
JIRA_SEARCH_MAX_RESULTS = 50
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
JIRA_GENERAL_ESTIMATE_PRIORITY = (
    "customfield_14937",
    "customfield_12307",
    "customfield_15063",
)
JIRA_QUERY_FIELDS = (
    "key", "summary", "status", "issuetype", "priority", "assignee", "created", "updated",
    "fixVersions", "issuelinks", *JIRA_ESTIMATE_FIELDS.keys(),
)
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


def active_run_path() -> Path:
    return state_root() / "tracker-active-run.json"


def run_root(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Некорректный run_id чтения трекеров")
    return state_root() / "tracker-runs" / run_id


def snapshot_path(run_id: str, provider: str) -> Path:
    return run_root(run_id) / "providers" / f"{provider}.json"


def jobs_root(run_id: str) -> Path:
    return run_root(run_id) / "jobs"


def job_path(run_id: str, job_id: str) -> Path:
    if not re.fullmatch(r"(?:collection-(?:sbertrek|jira)|history-(?:sbertrek|jira)-[0-9]{2})", job_id):
        raise ValueError("Некорректный job_id чтения трекеров")
    return jobs_root(run_id) / f"{job_id}.json"


def status_path(run_id: str) -> Path:
    return run_root(run_id) / "run-status.json"


def session_log_path(run_id: str) -> Path:
    return run_root(run_id) / "tracker-session-log.md"


def run_meta_path(run_id: str) -> Path:
    return run_root(run_id) / "scope.json"


def active_run_id() -> str | None:
    path = active_run_path()
    if not path.is_file():
        return None
    payload = load_json(path)
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Повреждён tracker-active-run.json")
    completion = run_root(run_id) / "completion-status.json"
    if completion.is_file():
        result = load_json(completion)
        if result.get("status") == "tracker-read-reconciled" and result.get("workflow_complete") is True:
            path.unlink()
            return None
    return run_id


def release_active_run(run_id: str) -> None:
    path = active_run_path()
    if not path.is_file():
        return
    payload = load_json(path)
    if isinstance(payload, dict) and payload.get("run_id") == run_id:
        path.unlink()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать JSON {path}: {exc}") from exc


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cards_sha256(cards: list[dict]) -> str:
    immutable_cards = [
        {key: value for key, value in card.items() if key != "history"}
        for card in cards
    ]
    canonical = json.dumps(
        sorted(immutable_cards, key=lambda item: item["key"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def object_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_json(path_value: str) -> tuple[Path, Any, int, str]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Полный JSON-ответ MCP не найден: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("Полный JSON-ответ MCP пуст")
    if size > RAW_RESPONSE_MAX_BYTES:
        raise ValueError(f"JSON-ответ MCP превышает {RAW_RESPONSE_MAX_BYTES} байт")
    payload = load_json(path)
    return path, payload, size, file_sha256(path)


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
        unit = record.get("unit")
        found, value = alias_value(unit, ISSUE_KEY_ALIASES)
        if found and isinstance(value, str) and ISSUE_KEY.fullmatch(value.strip().upper()):
            return value.strip().upper()
    return None


def issue_record_candidates(payload: Any) -> list[tuple[str, list[dict]]]:
    candidates: list[tuple[str, list[dict]]] = []
    visited_strings: set[str] = set()

    def walk(value: Any, path: str, preferred: bool = False, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(value, str):
            if value in visited_strings:
                return
            decoded = decoded_json_string(value)
            if decoded is not None:
                visited_strings.add(value)
                walk(decoded, path + "<json>", preferred, depth + 1)
            return
        if isinstance(value, list):
            if not value:
                if preferred or path == "$":
                    candidates.append((path, []))
            elif all(isinstance(item, dict) and record_issue_key(item) for item in value):
                candidates.append((path, value))
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", False, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if path == "$" and record_issue_key(value):
            candidates.append((path, [value]))
        for key, item in value.items():
            key_text = str(key)
            walk(item, f"{path}.{key_text}", key_text.casefold() in ISSUE_COLLECTION_KEYS, depth + 1)

    walk(payload, "$")
    return candidates


def full_issue_records(payload: Any) -> tuple[list[dict], str]:
    candidates = issue_record_candidates(payload)
    if not candidates:
        raise ValueError("В полном JSON-ответе MCP не найден массив карточек с ключами задач")
    max_count = max(len(records) for _, records in candidates)
    largest = [(path, records) for path, records in candidates if len(records) == max_count]
    key_sets = {tuple(sorted(record_issue_key(item) or "" for item in records)) for _, records in largest}
    if len(key_sets) > 1:
        paths = ", ".join(path for path, _ in largest)
        raise ValueError(f"JSON-ответ содержит несколько неоднозначных массивов задач: {paths}")
    path, records = largest[0]
    keys = [record_issue_key(item) for item in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Полный JSON-ответ MCP содержит повторяющиеся ключи задач")
    return records, path


def jira_page_metadata(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    folded = {str(key).casefold(): value for key, value in payload.items()}
    total = folded.get("total")
    start = folded.get("start_at", folded.get("startat"))
    maximum = folded.get("max_results", folded.get("maxresults"))
    if all(isinstance(value, int) and not isinstance(value, bool) for value in (total, start, maximum)):
        return {"total": total, "start_at": start, "max_results": maximum}
    return None


def jira_issue_links(payload: Any) -> tuple[str, list[dict], str]:
    candidates: list[tuple[str, dict]] = []

    def walk(value: Any, path: str, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, str):
            decoded = decoded_json_string(value)
            if decoded is not None:
                walk(decoded, path + "<json>", depth + 1)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", depth + 1)
            return
        if not isinstance(value, dict):
            return
        found, links = alias_value(value, ("issuelinks", "issueLinks"))
        if found and isinstance(links, list):
            candidates.append((path, value))
        for key, item in value.items():
            walk(item, f"{path}.{key}", depth + 1)

    walk(payload, "$")
    if not candidates:
        raise ValueError("В полном ответе Jira не найден массив issuelinks")
    path, container = max(candidates, key=lambda item: len(alias_value(item[1], ("issuelinks", "issueLinks"))[1]))
    found_key, raw_key = alias_value(container, ISSUE_KEY_ALIASES)
    epic_key = object_text(raw_key) if found_key else None
    if not epic_key or not ISSUE_KEY.fullmatch(epic_key.strip().upper()):
        raise ValueError("Ответ Jira с issuelinks не содержит ключ исходного эпика")
    _, links = alias_value(container, ("issuelinks", "issueLinks"))
    if not all(isinstance(item, dict) for item in links):
        raise ValueError("Jira issuelinks содержит запись неподдерживаемого формата")
    return epic_key.strip().upper(), links, path + ".issuelinks"


def jira_epic_child_keys(links: list[dict]) -> list[str]:
    result: list[str] = []
    for link in links:
        link_type = link.get("type")
        if not isinstance(link_type, dict) or str(link_type.get("name") or "").casefold() != "partof":
            continue
        inward = link.get("inward_issue") if "inward_issue" in link else link.get("inwardIssue")
        if not isinstance(inward, dict):
            continue
        key_value = record_issue_key(inward)
        if not key_value:
            raise ValueError("Дочерняя PartOf-связь Jira не содержит корректный ключ inward_issue")
        result.append(key_value)
    if len(result) != len(set(result)):
        raise ValueError("Jira issuelinks содержит повторяющиеся дочерние PartOf-ключи")
    return sorted(result)


def object_text(value: Any, aliases: tuple[str, ...] = ("code", "name", "value", "title", "key")) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    found, nested = alias_value(value, aliases)
    return object_text(nested, aliases) if found else None


def record_fields(record: dict) -> dict:
    fields = record.get("fields")
    if isinstance(fields, dict):
        return {**record, **fields}
    unit = record.get("unit")
    if isinstance(unit, dict):
        return {**unit, **{key: value for key, value in record.items() if key != "unit"}}
    return record


def attribute_records(record: dict) -> tuple[list[dict[str, Any]], bool]:
    sources = [record]
    unit = record.get("unit")
    if isinstance(unit, dict):
        sources.append(unit)
    fields = record.get("fields")
    if isinstance(fields, dict):
        sources.append(fields)
    for source in sources:
        found, raw = alias_value(source, ("attributes",))
        if not found:
            continue
        result: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            return [
                {"code": str(key), "name": str(key), "value": value}
                for key, value in raw.items()
            ], True
        if not isinstance(raw, list):
            return [], True
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
            if not found_value:
                value = None
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            found_name, name = alias_value(entry, ("name", "title", "displayName", "display_name"))
            if not found_name and isinstance(entry.get("attribute"), dict):
                found_name, name = alias_value(entry["attribute"], ("name", "title", "displayName", "display_name"))
            result.append({
                "code": code_text,
                "name": object_text(name) if found_name else code_text,
                "value": value,
            })
        return result, True
    return [], False


def attribute_entries(record: dict) -> tuple[dict[str, Any], bool]:
    records, present = attribute_records(record)
    return {item["code"].casefold(): item["value"] for item in records}, present


def attribute_value(attributes: dict[str, Any], codes: tuple[str, ...]) -> tuple[bool, Any]:
    for code in codes:
        if code.casefold() in attributes:
            return True, attributes[code.casefold()]
    return False, None


def optional_value(record: dict, aliases: tuple[str, ...], attributes: dict[str, Any], codes: tuple[str, ...]) -> tuple[Any, str]:
    fields = record_fields(record)
    field_found, field_value = alias_value(fields, aliases)
    if field_found and field_value not in (None, "", [], {}):
        return field_value, "value"
    attribute_found, attribute_value_raw = attribute_value(attributes, codes)
    if attribute_found and attribute_value_raw not in (None, "", [], {}):
        return attribute_value_raw, "value"
    if not field_found and not attribute_found:
        return None, "absent" if attributes or "attributes" in {str(key).casefold() for key in record} or isinstance(record.get("fields"), dict) else "not-returned"
    return None, "absent"


def normalized_person(value: Any) -> dict | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        text = object_text(value)
        return {"id": text, "name": text} if text else None
    found_id, account_id = alias_value(value, ("externalId", "accountId", "account_id", "login", "id", "key"))
    found_name, name = alias_value(value, ("displayName", "display_name", "fullName", "full_name", "name"))
    if not found_name:
        parts = []
        for alias in ("lastName", "firstName", "middleName"):
            found, part = alias_value(value, (alias,))
            if found and object_text(part):
                parts.append(object_text(part) or "")
        name = " ".join(parts) if parts else None
    account_text = object_text(account_id) if found_id else None
    name_text = object_text(name) if name is not None else None
    return {"id": account_text, "name": name_text or account_text} if account_text else None


def is_explicitly_unassigned(value: Any) -> bool:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        found_id, account_id = alias_value(value, ("externalId", "accountId", "account_id", "login", "id", "key"))
        found_name, name = alias_value(value, ("displayName", "display_name", "fullName", "full_name", "name"))
        account_text = object_text(account_id) if found_id else None
        name_text = object_text(name) if found_name else None
        return not account_text and bool(name_text) and name_text.casefold() in UNASSIGNED_SENTINELS
    text = object_text(value)
    return bool(text) and text.casefold() in UNASSIGNED_SENTINELS


def normalized_estimate(value: Any) -> dict | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        found, nested = alias_value(value, ("value", "amount", "estimate"))
        value = nested if found else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return {"value": number, "unit": "story-points"}


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


def normalized_estimate_text(value: str) -> str:
    return " ".join(re.sub(r"[^0-9a-zа-яё]+", " ", value.casefold()).split())


def normalized_role_marker(value: str) -> str | None:
    token = re.sub(r"[^a-zа-яё]+", "", value.casefold())
    aliases = {
        "a": "AN", "an": "AN", "ан": "AN",
        "b": "BE", "be": "BE", "бэ": "BE", "бе": "BE",
        "f": "FE", "fe": "FE", "фэ": "FE", "фе": "FE",
        "q": "QA", "qa": "QA", "тест": "QA",
    }
    visual = token.translate(str.maketrans({"а": "a", "в": "b", "е": "e"}))
    return aliases.get(token) or aliases.get(visual)


def role_from_summary_prefix(summary: str) -> str | None:
    bracketed = re.findall(r"\[\s*([^\]]+)\s*\]", summary[:64])
    if bracketed:
        roles = {role for item in bracketed if (role := normalized_role_marker(item))}
        return next(iter(roles)) if len(roles) == 1 else None
    match = re.match(r"\s*([^\s:_/\-]+)(?=[\s:_/\-])", summary)
    return normalized_role_marker(match.group(1)) if match else None


def sber_estimate_definition(code: str, name: str) -> dict | None:
    code_token = normalized_estimate_text(code)
    name_token = normalized_estimate_text(name)
    combined = f"{code_token} {name_token}"
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
    estimate_fields: list[dict] = []
    role_states = {role: "not-returned" for role in ESTIMATE_ROLES}
    role_values: dict[str, dict] = {}
    general_candidates: list[dict] = []

    if provider == "jira":
        for field_id, definition in JIRA_ESTIMATE_FIELDS.items():
            found, raw = alias_value(fields, (field_id,))
            role = definition.get("role")
            if role and found:
                role_states[role] = "absent"
            number = normalized_number(raw) if found else None
            if number is None:
                continue
            entry = {
                "field_id": field_id,
                "field_name": definition["name"],
                "value": number,
                "unit": definition["unit"],
                "role": role,
            }
            estimate_fields.append(entry)
            if role:
                role_states[role] = "value"
                role_values.setdefault(role, {
                    "value": number,
                    "unit": definition["unit"],
                    "source_field": {"id": field_id, "name": definition["name"]},
                    "inferred_from_general": False,
                })
            if definition.get("general"):
                general_candidates.append(entry)
    else:
        records, attributes_present = attribute_records(record)
        role_states = {role: "absent" if attributes_present else "not-returned" for role in ESTIMATE_ROLES}
        for attribute in records:
            definition = sber_estimate_definition(attribute["code"], attribute["name"])
            if not definition:
                continue
            number = normalized_number(attribute["value"])
            role = definition.get("role")
            if number is None:
                continue
            entry = {
                "field_id": attribute["code"],
                "field_name": definition["name"],
                "value": number,
                "unit": definition["unit"],
                "role": role,
            }
            estimate_fields.append(entry)
            if role:
                role_states[role] = "value"
                role_values.setdefault(role, {
                    "value": number,
                    "unit": definition["unit"],
                    "source_field": {"id": attribute["code"], "name": definition["name"]},
                    "inferred_from_general": False,
                })
            if definition.get("general"):
                general_candidates.append(entry)

    general = None
    general_state = "not-returned"
    estimate_raw, legacy_state = optional_value(record, ESTIMATE_ALIASES, {}, ())
    legacy = normalized_estimate(estimate_raw) if legacy_state == "value" else None
    if legacy:
        general = legacy
        general_state = "value"
    elif general_candidates:
        if provider == "jira":
            priorities = {field_id: index for index, field_id in enumerate(JIRA_GENERAL_ESTIMATE_PRIORITY)}
            general_candidates.sort(key=lambda item: priorities.get(item["field_id"], len(priorities)))
        candidate = general_candidates[0]
        general = {"value": candidate["value"], "unit": candidate["unit"]}
        general_state = "value"
    elif provider == "jira":
        general_state = "absent" if any(alias_value(fields, (field_id,))[0] for field_id, item in JIRA_ESTIMATE_FIELDS.items() if item.get("general")) else "not-returned"
    else:
        _, attributes_present = attribute_records(record)
        general_state = "absent" if attributes_present else "not-returned"

    if not role_values and general is not None:
        inferred_role = role_from_summary_prefix(summary)
        if inferred_role in {"AN", "BE", "FE"}:
            source = general_candidates[0] if general_candidates else {
                "field_id": "estimate",
                "field_name": "Общая оценка",
            }
            role_values[inferred_role] = {
                **general,
                "source_field": {"id": source["field_id"], "name": source["field_name"]},
                "inferred_from_general": True,
            }
            role_states[inferred_role] = "value"

    return general, general_state, role_values, role_states, estimate_fields


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
    found_name, name_value = alias_value(value, ("name", "summary", "title"))
    return {"key": key.strip().upper(), "name": object_text(name_value) if found_name else key.strip().upper()}


def normalized_releases(value: Any) -> list[dict]:
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        release = normalized_epic(item)
        if release:
            result.append(release)
            continue
        text = object_text(item)
        if text:
            result.append({"key": text, "name": text})
    return result


def required_record_text(record: dict, aliases: tuple[str, ...], label: str, key_value: str) -> str:
    found, value = alias_value(record_fields(record), aliases)
    text = object_text(value, ("code", "name", "value", "title", "key")) if found else None
    if not text or text.casefold() in MISSING_SENTINELS:
        raise ValueError(f"Карточка {key_value} не содержит обязательное поле {label}")
    return text


def compact_issue_from_response(record: dict, provider: str, scope: dict) -> dict:
    key_value = record_issue_key(record)
    if not key_value:
        raise ValueError("JSON-объект карточки не содержит ключ задачи")
    summary = required_record_text(record, SUMMARY_ALIASES, "summary", key_value)
    attributes, _ = attribute_entries(record)
    assignee_raw, assignee_state = optional_value(record, ASSIGNEE_ALIASES, attributes, SBER_ATTRIBUTE_CODES["assignee"])
    estimate, estimate_state, role_estimates, role_estimate_observations, estimate_fields = source_estimates(
        record, provider, summary
    )
    epic_raw, epic_state = optional_value(record, EPIC_ALIASES, attributes, SBER_ATTRIBUTE_CODES["epic"])
    releases_raw, releases_state = optional_value(record, RELEASE_ALIASES, attributes, SBER_ATTRIBUTE_CODES["releases"])
    assignee_unassigned = assignee_state == "value" and is_explicitly_unassigned(assignee_raw)
    assignee = None if assignee_unassigned else normalized_person(assignee_raw)
    if assignee_unassigned:
        assignee_state = "absent"
    epic = normalized_epic(epic_raw)
    releases = normalized_releases(releases_raw) if releases_state == "value" else []
    if assignee_state == "value" and assignee is None:
        raise ValueError(f"Карточка {key_value}: исполнитель имеет неподдерживаемый формат")
    if epic_state == "value" and epic is None:
        raise ValueError(f"Карточка {key_value}: эпик имеет неподдерживаемый формат")
    if provider == scope.get("provider") and scope.get("kind") == "epic":
        epic = {"key": scope["ids"][0], "name": scope["ids"][0]}
        epic_state = "value"
    if provider == "sbertrek":
        jira_raw, jira_key_state = optional_value(record, ("issue_key",), attributes, SBER_ATTRIBUTE_CODES["jira_key"])
        jira_text = object_text(jira_raw, ("key", "code", "value", "name")) if jira_key_state == "value" else None
        jira_key = issue_key(jira_text, "Объект Jira") if jira_text else None
        if jira_key_state == "value" and jira_key is None:
            raise ValueError(f"Карточка {key_value}: Объект Jira имеет неподдерживаемый формат")
    else:
        jira_key, jira_key_state = None, "absent"
    observations = {
        "assignee": assignee_state if assignee is not None or assignee_state != "value" else "not-returned",
        "estimate": estimate_state if estimate is not None or estimate_state != "value" else "not-returned",
        "epic": epic_state if epic is not None or epic_state != "value" else "not-returned",
        "releases": releases_state,
    }
    return {
        "key": key_value,
        "jira_key": jira_key,
        "jira_key_state": jira_key_state,
        "summary": summary,
        "issue_type": required_record_text(record, TYPE_ALIASES, "issue_type", key_value),
        "status": required_record_text(record, STATUS_ALIASES, "status", key_value),
        "assignee": assignee,
        "estimate": estimate,
        "role_estimates": role_estimates,
        "role_estimate_observations": role_estimate_observations,
        "estimate_fields": estimate_fields,
        "epic": epic,
        "releases": releases,
        "field_observations": observations,
        "created_at": required_record_text(record, CREATED_ALIASES, "created_at", key_value),
        "updated_at": required_record_text(record, UPDATED_ALIASES, "updated_at", key_value),
        "history": {"state": "pending", "evidence": [], "events": [], "reason": None},
    }


def issue_key(value: str, label: str = "Ключ задачи") -> str:
    value = value.strip().upper()
    if not ISSUE_KEY.fullmatch(value):
        raise ValueError(f"{label} должен иметь вид PROJECT-123: {value}")
    return value


def unique_keys(values: list[str]) -> list[str]:
    return sorted(set(issue_key(value) for value in values))


def keys_sha256(values: list[str]) -> str:
    return object_sha256(unique_keys(values))


def evidence(value: str, provider: str) -> str:
    if not value.startswith(f"mcp:{provider}:") or value.casefold().count("mcp:") != 1:
        raise ValueError(f"Evidence должен описывать один вызов и начинаться с mcp:{provider}:")
    if any(mark in value for mark in ("\n", "\r", ";", "|", "`")) or value.endswith(":none"):
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
        for account, member in mapping.items():
            if not isinstance(account, str) or not isinstance(member, dict):
                raise ValueError(f"Некорректный participants.{provider}")
            team_id = member.get("team_id")
            if not isinstance(team_id, str) or normalized_team_id(team_id) != team_id:
                raise ValueError(f"Некорректный team_id participants.{provider}.{account}")
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
    return {"status": "tracker-config-ready", "path": str(config_path()), "gaps": [], "must_stop": False, "allowed_next_action": "begin"}


def log_value(value: Any, *, limit: int = 8000) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def initialize_session_log(run_id: str, scope: dict, providers: list[str]) -> None:
    path = session_log_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Журнал чтения трекеров\n\n"
        f"- Протокол: `{PROTOCOL}`\n"
        f"- Run ID: `{run_id}`\n"
        f"- Область: `{scope['kind']}` в `{scope['provider']}`\n"
        f"- Контекст: `{log_value(scope['label'])}`\n"
        f"- Создан: `{now()}`\n"
        f"- Провайдеры: `{', '.join(providers)}`\n"
        f"- Входные ключи: `{', '.join(scope['ids'])}`\n"
        f"- Источник области: `{log_value(scope['source'])}`\n\n"
        "Журнал создаётся автоматически и не заменяет входные снимки.\n\n"
        "| Время UTC | Источник | Событие | Провайдер | Evidence | Детали |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )


def append_session_log(run_id: str, *, source: str, event: str, provider: str | None = None, evidence_value: str | None = None, details: str = "") -> None:
    path = session_log_path(run_id)
    if not path.is_file():
        raise ValueError(f"Для tracker-run отсутствует диагностический журнал: {path}")
    row = (
        f"| {now()} | {log_value(source)} | {log_value(event)} | {log_value(provider or '-')} | "
        f"{f'`{log_value(evidence_value)}`' if evidence_value else '-'} | {log_value(details)} |\n"
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(row)


def logged_mcp_details(run_id: str, call: str) -> str | None:
    path = session_log_path(run_id)
    if not path.is_file():
        return None
    provider = call.split(":", 2)[1] if call.startswith("mcp:") else ""
    marker = f"| mcp | call | {provider} | `{call}` |"
    for line in path.read_text(encoding="utf-8").splitlines():
        if marker in line:
            return line
    return None


def require_logged_mcp(run_id: str, call: str, *, outcome: str | None = None) -> str:
    details = logged_mcp_details(run_id, call)
    if details is None:
        raise ValueError("Evidence MCP-вызова сначала нужно записать через mcp-log")
    if outcome and f"outcome={outcome}" not in details:
        raise ValueError(f"MCP-вызов должен иметь outcome={outcome}")
    return details


def logged_query_calls(run_id: str, provider: str, query: str, page_number: int) -> list[str]:
    path = session_log_path(run_id)
    if not path.is_file():
        return []
    marker = f"| mcp | call | {provider} |"
    query_marker = f"query_sha256={query_digest(query)}; page={page_number};"
    return [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if marker in line and "operation=query;" in line and query_marker in line
    ]


def logged_tracker_commands(
    run_id: str, command: str, *, provider: str | None = None,
    evidence_value: str | None = None, detail_markers: tuple[str, ...] = (),
    any_evidence: bool = False,
) -> list[str]:
    path = session_log_path(run_id)
    if not path.is_file():
        return []
    marker = f"| trackerctl | command | {provider or '-'} | "
    evidence_marker = f"`{evidence_value}`" if evidence_value else "-"
    required = (f"command={command}; exit=0", *detail_markers)
    return [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if marker in line
        and (any_evidence or f"| {evidence_marker} |" in line)
        and all(item in line for item in required)
    ]


def unexpected_run_artifacts(run_id: str) -> list[str]:
    root = run_root(run_id)
    allowed_root = {
        "scope.json", "tracker-session-log.md", "run-status.json",
        "completion-status.json", "reconciled.json", "report.md",
        "pending-participant.json", "pending-development-decision.json",
        "development-decisions.json", "jobs", "providers",
    }
    unexpected: list[str] = []
    for path in root.iterdir():
        if path.name not in allowed_root:
            unexpected.append(path.name)
    if (root / "jobs").is_dir():
        for path in (root / "jobs").iterdir():
            if not path.is_file() or not re.fullmatch(
                r"(?:collection-(?:sbertrek|jira)|history-(?:sbertrek|jira)-[0-9]{2})\.json",
                path.name,
            ):
                unexpected.append(str(path.relative_to(root)))
    if (root / "providers").is_dir():
        for path in (root / "providers").iterdir():
            if not path.is_file() or path.name not in {"sbertrek.json", "jira.json"}:
                unexpected.append(str(path.relative_to(root)))
    return sorted(unexpected)


def tql_epic(epic: str) -> str:
    return f"unit IN linkedUnitsOf(\"unit = '{epic}'\", \"Состоит из\")"


def tql_units(keys: list[str]) -> str:
    return " or ".join(f'unit = "{item}"' for item in keys)


def tql_jira_keys(keys: list[str]) -> str:
    return " or ".join(f'issue_key = "{item}"' for item in keys)


def jql_keys(keys: list[str]) -> str:
    return "key IN (" + ", ".join(f'"{item}"' for item in keys) + ")"


def jira_epic_links_call(epic: str) -> str:
    return f'jira_get_issue(issue_key="{epic}", fields="issuelinks")'


def query_spec(
    provider: str, purpose: str, exact: str | None = None, *,
    method: str | None = None, language: str | None = None,
) -> dict:
    return {
        "state": "pending", "purpose": purpose,
        "language": language or ("TQL" if provider == "sbertrek" else "JQL"),
        "exact": exact, "initial_exact": exact, "method": method,
        "pages": [], "keys": [], "requested_keys": [], "confirmed_absent": [],
        "discovery": None,
        "unavailable_reason": None, "unavailable_evidence": None,
    }


def primary_query(scope: dict) -> dict:
    provider, kind, ids = scope["provider"], scope["kind"], scope["ids"]
    if provider == "sbertrek" and kind == "epic":
        return query_spec(provider, "epic-members", tql_epic(ids[0]))
    if provider == "sbertrek":
        return query_spec(provider, "task-cards", tql_units(ids))
    if kind == "epic":
        return query_spec(
            provider, "epic-links", jira_epic_links_call(ids[0]),
            method="issuelinks", language="JIRA_API",
        )
    return query_spec(provider, "task-cards", jql_keys(ids))


def snapshot_template(run_id: str, provider: str, scope: dict, config: dict) -> dict:
    query = primary_query(scope) if provider == scope["provider"] else query_spec(provider, "counterparts")
    return {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "provider": provider, "captured_at": None, "scope": scope,
        "projects": config["projects"][provider], "query": query,
        "issues": [], "collection_complete": False,
    }


def validate_snapshot(snapshot: Any, run_id: str, provider: str, finalized: bool = False) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("protocol") != PROTOCOL or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Снимок создан старым протоколом; начни новый tracker-run")
    if snapshot.get("provider") != provider:
        raise ValueError(f"Ожидался снимок {provider}")
    if snapshot.get("run_id") != run_id:
        raise ValueError(f"Снимок {provider} относится к другому tracker-run")
    if finalized and not snapshot.get("captured_at"):
        raise ValueError(f"Снимок {provider} не финализирован")
    return snapshot


def validate_issue_card(item: Any, provider: str) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"Карточка {provider} должна быть JSON-объектом")
    key_value = item.get("key")
    if not isinstance(key_value, str) or not ISSUE_KEY.fullmatch(key_value):
        raise ValueError(f"Карточка {provider} содержит некорректный key")
    for field in ("summary", "issue_type", "status", "created_at", "updated_at", "evidence"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"Карточка {key_value} не содержит строковое поле {field}")
    if item["summary"].casefold() == key_value.casefold():
        raise ValueError(f"Карточка {key_value} содержит placeholder вместо summary")
    assignee = item.get("assignee")
    if assignee is not None and (
        not isinstance(assignee, dict)
        or not isinstance(assignee.get("id"), str)
        or not assignee["id"]
    ):
        raise ValueError(f"Карточка {key_value}: assignee должен быть структурированным объектом")
    estimate = item.get("estimate")
    if estimate is not None and (
        not isinstance(estimate, dict)
        or not isinstance(estimate.get("value"), (int, float))
        or isinstance(estimate.get("value"), bool)
        or not isinstance(estimate.get("unit"), str)
    ):
        raise ValueError(f"Карточка {key_value}: estimate должен содержать value и unit")
    role_estimates = item.get("role_estimates")
    if not isinstance(role_estimates, dict) or any(role not in ESTIMATE_ROLES for role in role_estimates):
        raise ValueError(f"Карточка {key_value}: role_estimates имеет некорректные роли")
    for role, role_estimate in role_estimates.items():
        if (
            not isinstance(role_estimate, dict)
            or not isinstance(role_estimate.get("value"), (int, float))
            or isinstance(role_estimate.get("value"), bool)
            or not isinstance(role_estimate.get("unit"), str)
            or not isinstance(role_estimate.get("source_field"), dict)
            or not isinstance(role_estimate["source_field"].get("id"), str)
            or not isinstance(role_estimate.get("inferred_from_general"), bool)
        ):
            raise ValueError(f"Карточка {key_value}: оценка роли {role} имеет неполную схему")
    role_observations = item.get("role_estimate_observations")
    if not isinstance(role_observations, dict) or set(role_observations) != set(ESTIMATE_ROLES):
        raise ValueError(f"Карточка {key_value}: role_estimate_observations имеет неполную схему")
    for role, state in role_observations.items():
        if state not in OBSERVATION_STATES or ((state == "value") != (role in role_estimates)):
            raise ValueError(f"Карточка {key_value}: состояние оценки {role} не соответствует значению")
    estimate_fields = item.get("estimate_fields")
    if not isinstance(estimate_fields, list):
        raise ValueError(f"Карточка {key_value}: estimate_fields должен быть списком")
    for field in estimate_fields:
        if (
            not isinstance(field, dict)
            or not isinstance(field.get("field_id"), str)
            or not isinstance(field.get("field_name"), str)
            or not isinstance(field.get("value"), (int, float))
            or isinstance(field.get("value"), bool)
            or not isinstance(field.get("unit"), str)
            or field.get("role") not in {*ESTIMATE_ROLES, None}
        ):
            raise ValueError(f"Карточка {key_value}: estimate_fields содержит некорректную запись")
    epic = item.get("epic")
    if epic is not None and (
        not isinstance(epic, dict)
        or not isinstance(epic.get("key"), str)
        or not ISSUE_KEY.fullmatch(epic["key"])
    ):
        raise ValueError(f"Карточка {key_value}: epic должен быть структурированным объектом")
    releases = item.get("releases")
    if not isinstance(releases, list) or any(
        not isinstance(release, dict) or not isinstance(release.get("key"), str) or not release["key"]
        for release in releases
    ):
        raise ValueError(f"Карточка {key_value}: releases должен быть списком объектов")
    observations = item.get("field_observations")
    if not isinstance(observations, dict) or set(observations) != {"assignee", "estimate", "epic", "releases"}:
        raise ValueError(f"Карточка {key_value}: field_observations имеет неполную схему")
    for field, state in observations.items():
        if state not in OBSERVATION_STATES:
            raise ValueError(f"Карточка {key_value}: некорректное состояние {field}")
        populated = item.get(field) not in (None, [], {})
        if (state == "value") != populated:
            raise ValueError(f"Карточка {key_value}: состояние {field} не соответствует значению")
    jira_state = item.get("jira_key_state")
    jira_key_value = item.get("jira_key")
    if provider == "sbertrek":
        if jira_state not in OBSERVATION_STATES:
            raise ValueError(f"Карточка {key_value}: отсутствует jira_key_state")
        if (jira_state == "value") != (jira_key_value is not None):
            raise ValueError(f"Карточка {key_value}: jira_key_state не соответствует Объекту Jira")
        if jira_key_value is not None and (not isinstance(jira_key_value, str) or not ISSUE_KEY.fullmatch(jira_key_value)):
            raise ValueError(f"Карточка {key_value}: некорректный Объект Jira")
    elif jira_key_value is not None or jira_state != "absent":
        raise ValueError(f"Карточка {key_value}: Jira-карточка не должна содержать Объект Jira")
    history = item.get("history")
    if not isinstance(history, dict) or history.get("state") not in {"pending", "complete", "unavailable"}:
        raise ValueError(f"Карточка {key_value}: некорректное состояние истории")
    if not isinstance(history.get("events"), list) or not isinstance(history.get("evidence"), list):
        raise ValueError(f"Карточка {key_value}: история имеет неполную схему")
    for event in history["events"]:
        if not isinstance(event, dict) or event.get("field") not in {"assignee", "status"} or not isinstance(event.get("at"), str):
            raise ValueError(f"Карточка {key_value}: событие истории имеет неполную схему")
        evidence(event.get("evidence") or "", provider)


def confirmed_absent_map(snapshot: dict | None) -> dict[str, str]:
    if not snapshot:
        return {}
    result: dict[str, str] = {}
    for item in snapshot.get("query", {}).get("confirmed_absent", []):
        if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("evidence"), str):
            result[item["key"]] = item["evidence"]
    return result


def excluded_sbertrek_keys(snapshots: dict[str, dict]) -> set[str]:
    jira_absent = set(confirmed_absent_map(snapshots.get("jira")))
    if not jira_absent:
        return set()
    return {
        item["key"]
        for item in snapshots["sbertrek"]["issues"]
        if item.get("jira_key_state") == "value" and item.get("jira_key") in jira_absent
    }


def validate_collection_integrity(run_id: str, snapshot: dict, provider: str) -> None:
    query = snapshot.get("query")
    if not isinstance(query, dict) or query.get("state") not in {"complete", "skipped", "unavailable"}:
        raise ValueError(f"Коллекция {provider} не завершена")
    pages = query.get("pages")
    issues = snapshot.get("issues")
    if not isinstance(pages, list) or not isinstance(issues, list) or not isinstance(query.get("keys"), list):
        raise ValueError(f"Коллекция {provider} имеет неполную схему")
    requested_keys = query.get("requested_keys")
    confirmed_absent = query.get("confirmed_absent")
    if not isinstance(requested_keys, list) or len(requested_keys) != len(set(requested_keys)):
        raise ValueError(f"Коллекция {provider} содержит некорректный requested_keys")
    if not isinstance(confirmed_absent, list):
        raise ValueError(f"Коллекция {provider} содержит некорректный confirmed_absent")
    jira_epic_discovery = (
        provider == "jira"
        and snapshot.get("scope", {}).get("provider") == "jira"
        and snapshot.get("scope", {}).get("kind") == "epic"
    )
    if jira_epic_discovery:
        discovery = query.get("discovery")
        expected_query = jira_epic_links_call(snapshot["scope"]["ids"][0])
        if (
            not isinstance(discovery, dict)
            or discovery.get("method") != "issuelinks-PartOf-inward_issue"
            or discovery.get("query") != expected_query
            or discovery.get("child_keys") != unique_keys(discovery.get("child_keys") or [])
            or not isinstance(discovery.get("link_count"), int)
            or discovery["link_count"] < len(discovery["child_keys"])
            or not re.fullmatch(r"[a-f0-9]{64}", str(discovery.get("response_sha256") or ""))
        ):
            raise ValueError("Коллекция Jira-эпика не содержит проверенный issuelinks discovery")
        call = evidence(str(discovery.get("evidence") or ""), "jira")
        details = require_logged_mcp(run_id, call, outcome="success")
        if f"operation=epic-links; outcome=success; query_sha256={query_digest(expected_query)};" not in details:
            raise ValueError("Evidence Jira issuelinks не совпадает с исходным эпиком")
        require_tracker_command(
            run_id, "jira-ingest-epic-links", provider="jira", evidence_value=call,
            detail_markers=(f"record_sha256={object_sha256(discovery)}",),
        )
        if query.get("requested_keys") != discovery["child_keys"]:
            raise ValueError("Jira epic JQL не соответствует PartOf inward_issue из issuelinks")
        expected_exact = jql_keys(discovery["child_keys"]) if discovery["child_keys"] else None
        if query.get("initial_exact") != expected_exact or query.get("exact") != expected_exact:
            raise ValueError("Точный Jira epic JQL изменён после структурного разбора issuelinks")
    sbertrek_counterpart_epic_discovery = (
        provider == "sbertrek"
        and snapshot.get("scope", {}).get("provider") == "jira"
        and snapshot.get("scope", {}).get("kind") == "epic"
    )
    if sbertrek_counterpart_epic_discovery and query["state"] != "unavailable":
        discovery = query.get("discovery")
        jira_epic_key = snapshot["scope"]["ids"][0]
        expected_lookup = tql_jira_keys([jira_epic_key])
        sbertrek_epic_key = discovery.get("sbertrek_epic_key") if isinstance(discovery, dict) else None
        if (
            not isinstance(discovery, dict)
            or discovery.get("method") != "issue_key-to-sbertrek-epic"
            or discovery.get("query") != expected_lookup
            or discovery.get("jira_epic_key") != jira_epic_key
            or discovery.get("returned_count") not in {0, 1}
            or (sbertrek_epic_key is not None and (
                not isinstance(sbertrek_epic_key, str)
                or not ISSUE_KEY.fullmatch(sbertrek_epic_key)
                or discovery["returned_count"] != 1
            ))
            or (sbertrek_epic_key is None and discovery.get("returned_count") != 0)
            or not re.fullmatch(r"[a-f0-9]{64}", str(discovery.get("response_sha256") or ""))
        ):
            raise ValueError("Коллекция Jira-эпика не содержит проверенный поиск SberTrek-эпика")
        call = evidence(str(discovery.get("evidence") or ""), "sbertrek")
        details = require_logged_mcp(run_id, call, outcome="success")
        if f"operation=counterpart-epic; outcome=success; query_sha256={query_digest(expected_lookup)};" not in details:
            raise ValueError("Evidence поиска SberTrek-эпика не совпадает с исходным Jira-эпиком")
        require_tracker_command(
            run_id, "sbertrek-ingest-counterpart-epic", provider="sbertrek", evidence_value=call,
            detail_markers=(f"record_sha256={object_sha256(discovery)}",),
        )
        job = load_job(run_id, "collection-sbertrek")[1]
        if job["query"].get("initial_text") != expected_lookup:
            raise ValueError("Исходный поиск SberTrek-эпика изменён после создания")
        expected_exact = tql_epic(sbertrek_epic_key) if sbertrek_epic_key else None
        if (
            query.get("purpose") != "epic-members"
            or query.get("method") != "linkedUnitsOf"
            or query.get("requested_keys") != []
            or query.get("initial_exact") != expected_exact
            or query.get("exact") != expected_exact
            or (expected_exact is not None and job["query"].get("text") != expected_exact)
        ):
            raise ValueError("TQL состава SberTrek-эпика изменён после структурного поиска контрпары")
    absent_keys: list[str] = []
    for item in confirmed_absent:
        if (
            provider != "jira"
            or query.get("purpose") != "counterparts"
            or not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or not ISSUE_KEY.fullmatch(item["key"])
            or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("batch_sha256") or ""))
        ):
            raise ValueError(f"Коллекция {provider} содержит некорректное подтверждение отсутствия")
        call = evidence(str(item.get("evidence") or ""), "jira")
        require_logged_mcp(run_id, call, outcome="error")
        require_tracker_command(
            run_id, "jira-record-absent-counterparts", provider="jira",
            evidence_value=call,
            detail_markers=(f"record_sha256={item['batch_sha256']}",),
        )
        absent_keys.append(item["key"])
    if len(absent_keys) != len(set(absent_keys)) or not set(absent_keys).issubset(set(requested_keys)):
        raise ValueError("Подтверждённые отсутствующие Jira-ключи не соответствуют исходному counterpart-запросу")
    all_counterparts_absent = (
        provider == "jira"
        and query.get("purpose") == "counterparts"
        and bool(requested_keys)
        and set(absent_keys) == set(requested_keys)
    )
    empty_jira_epic = jira_epic_discovery and not query.get("requested_keys")
    empty_sbertrek_counterpart_epic = (
        sbertrek_counterpart_epic_discovery
        and isinstance(query.get("discovery"), dict)
        and query["discovery"].get("sbertrek_epic_key") is None
    )
    if (
        query["state"] == "complete"
        and not pages
        and not all_counterparts_absent
        and not empty_jira_epic
        and not empty_sbertrek_counterpart_epic
    ):
        raise ValueError(f"Коллекция {provider} помечена complete без зарегистрированной страницы MCP")
    if query["state"] in {"skipped", "unavailable"}:
        if pages or issues or query["keys"]:
            raise ValueError(f"Коллекция {provider} {query['state']} не должна содержать страницы или карточки")
        return
    page_keys: list[str] = []
    page_evidence: dict[str, set[str]] = {}
    structural_pages: list[dict] = []
    for expected_number, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or page.get("number") != expected_number:
            raise ValueError(f"Коллекция {provider}: нарушена нумерация страниц")
        keys = page.get("keys")
        if not isinstance(keys, list) or len(keys) != len(set(keys)) or any(not isinstance(key, str) or not ISSUE_KEY.fullmatch(key) for key in keys):
            raise ValueError(f"Коллекция {provider}: страница {expected_number} содержит некорректные ключи")
        call = evidence(str(page.get("evidence") or ""), provider)
        details = require_logged_mcp(run_id, call, outcome="success")
        expected_details = (
            f"operation=query; outcome=success; query_sha256={query_digest(query['exact'])}; "
            f"page={expected_number}; returned={len(keys)};"
        )
        if expected_details not in details:
            raise ValueError(f"Коллекция {provider}: evidence страницы {expected_number} не совпадает с bulk-запросом")
        if page.get("recording_method") == "structural-json-import":
            if not re.fullmatch(r"[a-f0-9]{64}", str(page.get("response_sha256") or "")):
                raise ValueError(f"Коллекция {provider}: страница {expected_number} не содержит SHA-256 ответа")
            if not isinstance(page.get("response_bytes"), int) or page["response_bytes"] <= 0:
                raise ValueError(f"Коллекция {provider}: страница {expected_number} не содержит размер ответа")
            if page.get("returned_count") != len(keys):
                raise ValueError(f"Коллекция {provider}: машинное число карточек страницы не совпадает с ключами")
            expected_max = SBER_EXPORT_MAX_RESULTS if provider == "sbertrek" else JIRA_SEARCH_MAX_RESULTS
            if page.get("requested_max_results") != expected_max:
                raise ValueError(f"Коллекция {provider} должна быть запрошена с max_results={expected_max}")
            if not re.fullmatch(r"[a-f0-9]{64}", str(page.get("cards_sha256") or "")):
                raise ValueError(f"Коллекция {provider}: страница {expected_number} не содержит SHA-256 карточек")
            structural_pages.append(page)
        elif provider in PROVIDERS:
            raise ValueError(f"{provider}-страница должна быть записана только структурным импортом полного JSON")
        page_keys.extend(keys)
        for key_value in keys:
            page_evidence.setdefault(key_value, set()).add(call)
    if pages and (not pages[-1].get("last_page") or any(page.get("last_page") for page in pages[:-1])):
        raise ValueError(f"Коллекция {provider}: пагинация не имеет единственной последней страницы")
    if len(page_keys) != len(set(page_keys)):
        raise ValueError(f"Коллекция {provider}: ключ задачи повторяется на нескольких страницах")
    if sorted(page_keys) != sorted(query["keys"]):
        raise ValueError(f"Коллекция {provider}: query.keys не совпадает с машинными страницами")
    if provider == "jira" and query.get("purpose") == "counterparts":
        if sorted(set(page_keys) | set(absent_keys)) != sorted(requested_keys):
            raise ValueError("Jira counterpart-коллекция не покрывает все исходно запрошенные ключи")
    issue_keys = []
    for item in issues:
        validate_issue_card(item, provider)
        issue_keys.append(item["key"])
        if item["evidence"] not in page_evidence.get(item["key"], set()):
            raise ValueError(f"Карточка {item['key']} не связана с evidence своей bulk-страницы")
    if len(issue_keys) != len(set(issue_keys)) or sorted(issue_keys) != sorted(page_keys):
        raise ValueError(f"Коллекция {provider}: набор карточек не совпадает с полным bulk-ответом")
    for page in structural_pages:
        page_cards = [item for item in issues if item["evidence"] == page["evidence"]]
        if cards_sha256(page_cards) != page["cards_sha256"]:
            raise ValueError(f"Коллекция {provider}: компактные карточки страницы изменены после структурного импорта")


def require_tracker_command(
    run_id: str, command: str, *, provider: str | None = None,
    evidence_value: str | None = None, detail_markers: tuple[str, ...] = (),
) -> None:
    if not logged_tracker_commands(
        run_id, command, provider=provider, evidence_value=evidence_value,
        detail_markers=detail_markers,
    ):
        raise ValueError(f"Tracker-run не содержит успешную команду {command} для зарегистрированных данных")


def validate_run_provenance(run_id: str, snapshots: dict[str, dict]) -> None:
    meta = load_json(run_meta_path(run_id))
    if not isinstance(meta, dict) or meta.get("run_id") != run_id or meta.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("scope.json не соответствует tracker-run")
    validate_counterpart_contract(run_id, snapshots)
    excluded_sber = excluded_sbertrek_keys(snapshots)
    for provider, snapshot in snapshots.items():
        query = snapshot["query"]
        for absent in query.get("confirmed_absent", []):
            require_tracker_command(
                run_id, "jira-record-absent-counterparts", provider="jira",
                evidence_value=absent["evidence"],
                detail_markers=(f"record_sha256={absent['batch_sha256']}",),
            )
        if query["state"] == "complete":
            for page in query["pages"]:
                command = "ingest-query-response"
                markers = [f"page_number={page['number']}"]
                require_tracker_command(
                    run_id, command, provider=provider, evidence_value=page["evidence"],
                    detail_markers=tuple(markers),
                )
        collection_path = job_path(run_id, f"collection-{provider}")
        if collection_path.is_file():
            require_tracker_command(run_id, "collector-complete", provider=provider)
        for item in snapshot["issues"]:
            if provider == "sbertrek" and item["key"] in excluded_sber:
                continue
            event_groups: dict[tuple[str, str], int] = {}
            for event in item["history"]["events"]:
                call = evidence(event["evidence"], provider)
                require_logged_mcp(run_id, call, outcome="success")
                group = (call, object_sha256(event))
                event_groups[group] = event_groups.get(group, 0) + 1
            for (call, record_digest), expected_count in event_groups.items():
                commands = logged_tracker_commands(
                    run_id, "history-event", provider=provider, evidence_value=call,
                    detail_markers=(f"key={item['key']}", f"record_sha256={record_digest}"),
                )
                if len(commands) != expected_count:
                    raise ValueError(f"История {provider}:{item['key']} изменена вне trackerctl")
            all_event_commands = logged_tracker_commands(
                run_id, "history-event", provider=provider, any_evidence=True,
                detail_markers=(f"key={item['key']}",),
            )
            if len(all_event_commands) != len(item["history"]["events"]):
                raise ValueError(f"История {provider}:{item['key']} не совпадает с журналом trackerctl")
            history = item["history"]
            if history["state"] not in {"complete", "unavailable"} or len(history["evidence"]) != 1:
                raise ValueError(f"История {provider}:{item['key']} не имеет завершённого provenance")
            call = evidence(history["evidence"][0], provider)
            require_logged_mcp(run_id, call, outcome="success" if history["state"] == "complete" else "error")
            require_tracker_command(
                run_id, "history-complete", provider=provider, evidence_value=call,
                detail_markers=(
                    f"key={item['key']}", f"state={history['state']}",
                    "record_sha256=" + object_sha256({name: history[name] for name in ("state", "evidence", "reason")}),
                ),
            )
    for job in all_jobs(run_id):
        if job.get("state") != "complete":
            raise ValueError(f"Job {job.get('job_id')} не завершён")
        if job.get("kind") == "provider-history":
            validate_history_job_calls(run_id, job, require_complete=True)
        command = "collector-complete" if job["kind"] == "provider-collection" else "history-job-complete"
        markers = () if command == "collector-complete" else (f"job_id={job['job_id']}",)
        require_tracker_command(run_id, command, provider=job["provider"] if command == "collector-complete" else None, detail_markers=markers)


def load_snapshot(run_id: str, provider: str) -> tuple[Path, dict]:
    path = snapshot_path(run_id, provider)
    if not path.is_file():
        raise ValueError(f"Снимок {provider} не создан для run_id={run_id}")
    return path, validate_snapshot(load_json(path), run_id, provider)


def enabled_providers(config: dict) -> tuple[str, ...]:
    return PROVIDERS if config["jira_enabled"] else ("sbertrek",)


def all_snapshots(run_id: str, config: dict, *, finalized: bool = False) -> dict[str, dict]:
    return {provider: validate_snapshot(load_json(snapshot_path(run_id, provider)), run_id, provider, finalized) for provider in enabled_providers(config)}


def validate_counterpart_contract(run_id: str, snapshots: dict[str, dict]) -> None:
    jira = snapshots.get("jira")
    sber = snapshots.get("sbertrek")
    if not jira or not sber or sber["scope"]["provider"] != "sbertrek":
        return
    query = jira["query"]
    if query.get("purpose") != "counterparts" or query.get("initial_exact") is None:
        return
    expected_keys = sorted({
        item["jira_key"] for item in sber["issues"]
        if item.get("jira_key_state") == "value" and item.get("jira_key")
    })
    expected_initial = jql_keys(expected_keys) if expected_keys else None
    if query.get("requested_keys") != expected_keys or query.get("initial_exact") != expected_initial:
        raise ValueError("Исходный Jira counterpart-запрос не соответствует Объектам Jira из SberTrek")
    absent_keys = set(confirmed_absent_map(jira))
    remaining = sorted(set(expected_keys) - absent_keys)
    expected_active = jql_keys(remaining) if remaining else None
    if query.get("state") not in {"skipped", "unavailable"} and query.get("exact") != expected_active:
        raise ValueError("Активный Jira counterpart-запрос изменён вне управляемого исключения отсутствующих ключей")
    job_file = job_path(run_id, "collection-jira")
    if not job_file.is_file():
        return
    _, job = load_job(run_id, "collection-jira")
    if job["query"].get("initial_text") != expected_initial:
        raise ValueError("Исходный Jira collector-job изменён после создания")
    if expected_active is not None and job["query"].get("text") != expected_active:
        raise ValueError("Активный Jira collector-job не соответствует управляемому counterpart-запросу")


def ensure_mutable(snapshot: dict) -> None:
    if snapshot.get("captured_at"):
        raise ValueError("Финализированный снимок неизменяем")


def issue_by_key(snapshot: dict, key_value: str) -> dict | None:
    return next((item for item in snapshot["issues"] if item["key"] == key_value), None)


def current_query(run_id: str, config: dict) -> tuple[str, dict] | None:
    snapshots = all_snapshots(run_id, config)
    scope = next(iter(snapshots.values()))["scope"]
    primary = snapshots[scope["provider"]]
    if primary["query"]["state"] in {"pending", "collecting"}:
        return scope["provider"], primary["query"]
    secondary_provider = "jira" if scope["provider"] == "sbertrek" else "sbertrek"
    secondary = snapshots.get(secondary_provider)
    if secondary and secondary["query"]["state"] in {"pending", "collecting"} and secondary["query"]["exact"]:
        return secondary_provider, secondary["query"]
    return None


def query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def collection_job(run_id: str, provider: str, query: dict) -> dict:
    exact = query.get("exact")
    if not exact:
        raise ValueError("Нельзя создать collector-job без точного запроса")
    initial_exact = query.get("initial_exact") or exact
    job_id = f"collection-{provider}"
    return {
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "job_id": job_id,
        "kind": "provider-collection",
        "state": "pending",
        "provider": provider,
        "query": {
            "language": query["language"],
            "purpose": query["purpose"],
            "method": query.get("method"),
            "text": exact,
            "sha256": query_digest(exact),
            "initial_text": initial_exact,
            "initial_sha256": query_digest(initial_exact),
        },
        "output": str(snapshot_path(run_id, provider)),
        "collector_contract": str(Path(__file__).resolve().parents[1] / "core" / "tracker-collector.md"),
        "allowed_operations": [
            "select-runtime-json-export-tool" if provider == "sbertrek" else "select-runtime-query-tool",
            "execute-exact-query", "paginate",
            "record-bounded-call", "structurally-import-full-json-response",
            "record-page", "record-compact-card",
            *(["record-confirmed-absent-counterparts"] if provider == "jira" else []),
            *(["record-counterpart-epic-discovery"] if query.get("method") == "jira-epic-counterpart" else []),
            *(["record-query-unavailable"] if query.get("purpose") in {"counterparts", "counterpart-epic"} else []),
            "complete-job",
        ],
        "forbidden_operations": [
            "read-mcp-documentation", "probe-with-alternative-query",
            "search-by-title-or-description", "read-returned-issues-one-by-one",
            "issue.search", "link.list",
            *([] if provider == "jira" and query.get("method") == "issuelinks" else ["issue.getByKey"]),
            "change-tracker-or-analytical-artifacts", "continue-to-next-job",
        ],
        "required_task_fields": [
            "key", "jira_key", "jira_key_state", "summary", "issue_type", "status", "assignee",
            "estimate", "role_estimates", "estimate_fields", "epic", "releases", "created_at", "updated_at",
        ],
        "response_contract": {
            "full_json_required": True,
            "rendered_preview_is_not_data": True,
            "structural_import_command": "ingest-query-response",
            "mcp_tool_contract": {
                "required_capability": "exact-tql-bulk-json-export",
                "preferred_operation": "issue.exportJson",
                "query_parameter": "query",
                "max_results_parameter": "max_results",
                "max_results": SBER_EXPORT_MAX_RESULTS,
                "forbidden_operations": ["issue.search", "issue.getByKey", "link.list"],
            } if provider == "sbertrek" else {
                "required_capability": "jira-exact-read",
                "preferred_operation": "jira_get_issue" if query.get("method") == "issuelinks" else "jira_search",
                "limit_parameter": "limit",
                "max_results": JIRA_SEARCH_MAX_RESULTS,
                "epic_links_fields": ["issuelinks"],
            },
            "preferred_fields": [
                "key", "summary", "suit", "status", "attributes", "epic",
                "created_at", "updated_at",
            ] if provider == "sbertrek" else list(JIRA_QUERY_FIELDS),
            "jira_estimate_fields": JIRA_ESTIMATE_FIELDS if provider == "jira" else None,
        },
        "created_at": now(),
        "completed_at": None,
    }


def canonical_history_calls(job: dict) -> list[dict]:
    provider = job["provider"]
    job_id = job["job_id"]
    keys = unique_keys(job["keys"])
    return [
        {
            "evidence": f"mcp:{provider}:history:{key}",
            "keys": [key],
            "keys_sha256": keys_sha256([key]),
        }
        for key in keys
    ]


def history_call_for_key(job: dict, key: str, evidence_value: str, outcome: str) -> dict:
    normalized_key = issue_key(key)
    for call in job.get("calls", []):
        if call["evidence"] == evidence_value and normalized_key in call["keys"]:
            if call["outcome"] != outcome:
                raise ValueError(f"History-вызов должен иметь outcome={outcome}")
            return call
    raise ValueError("Ключ истории не связан с зарегистрированным каноническим вызовом активного job")


def validate_history_job_calls(run_id: str, job: dict, *, require_complete: bool) -> None:
    expected = canonical_history_calls(job)
    actual = job.get("calls", [])
    if require_complete and len(actual) != len(expected):
        raise ValueError("History-job не содержит полного набора канонических MCP-вызовов")
    for call in actual:
        details = require_logged_mcp(run_id, call["evidence"], outcome=call["outcome"])
        markers = (
            "operation=history;",
            f"keys_sha256={call['keys_sha256']};",
            f"keys={','.join(call['keys'])};",
        )
        if not all(marker in details for marker in markers):
            raise ValueError("History-job не совпадает с машинным журналом MCP-вызовов")


def history_job(run_id: str, provider: str, number: int, keys: list[str]) -> dict:
    job_id = f"history-{provider}-{number:02d}"
    keys = unique_keys(keys)
    return {
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "job_id": job_id,
        "kind": "provider-history",
        "state": "pending",
        "provider": provider,
        "keys": keys,
        "call_mode": "per-key",
        "calls": [],
        "output": str(snapshot_path(run_id, provider)),
        "collector_contract": str(Path(__file__).resolve().parents[1] / "core" / "tracker-collector.md"),
        "allowed_operations": [
            "read-exact-key-history", "record-bounded-call",
            "record-assignee-or-status-events", "complete-key-history", "complete-job",
        ],
        "forbidden_operations": [
            "read-mcp-documentation", "search-for-other-issues",
            "record-comments-or-description-history", "change-tracker-or-analytical-artifacts",
            "continue-to-next-job",
        ],
        "created_at": now(),
        "completed_at": None,
    }


def save_job(run_id: str, job: dict) -> Path:
    path = job_path(run_id, job["job_id"])
    save_json(path, job)
    return path


def load_job(run_id: str, job_id: str) -> tuple[Path, dict]:
    path = job_path(run_id, job_id)
    if not path.is_file():
        raise ValueError(f"Collector-job не найден: {job_id}")
    job = load_json(path)
    if (
        not isinstance(job, dict)
        or job.get("protocol") != PROTOCOL
        or job.get("schema_version") != SCHEMA_VERSION
        or job.get("run_id") != run_id
        or job.get("job_id") != job_id
    ):
        raise ValueError("Collector-job создан старым или повреждённым протоколом")
    if job.get("state") not in {"pending", "running", "complete"}:
        raise ValueError("Collector-job содержит некорректное состояние")
    if job.get("kind") == "provider-collection":
        query = job.get("query")
        if (
            not isinstance(query, dict)
            or not isinstance(query.get("text"), str)
            or query.get("sha256") != query_digest(query["text"])
            or not isinstance(query.get("initial_text"), str)
            or query.get("initial_sha256") != query_digest(query["initial_text"])
        ):
            raise ValueError("Контрольная сумма запроса collector-job не совпадает")
    elif job.get("kind") == "provider-history":
        keys = job.get("keys")
        expected_mode = "per-key"
        if (
            not isinstance(keys, list)
            or keys != unique_keys(keys)
            or job.get("call_mode") != expected_mode
            or not isinstance(job.get("calls"), list)
        ):
            raise ValueError("History-job содержит некорректный контракт вызовов")
        expected_calls = canonical_history_calls(job)
        seen: set[str] = set()
        for call in job["calls"]:
            if not isinstance(call, dict) or call.get("evidence") in seen:
                raise ValueError("History-job содержит повреждённые или повторные вызовы")
            seen.add(call["evidence"])
            expected = next((item for item in expected_calls if item["evidence"] == call.get("evidence")), None)
            if (
                expected is None
                or call.get("keys") != expected["keys"]
                or call.get("keys_sha256") != expected["keys_sha256"]
                or call.get("outcome") not in {"success", "error"}
                or set(call) != {"evidence", "keys", "keys_sha256", "outcome"}
            ):
                raise ValueError("History-job содержит вызов, не соответствующий каноническому контракту")
    return path, job


def all_jobs(run_id: str) -> list[dict]:
    root = jobs_root(run_id)
    if not root.is_dir():
        return []
    return [load_job(run_id, path.stem)[1] for path in sorted(root.glob("*.json"))]


def active_job(run_id: str) -> dict | None:
    pending = [job for job in all_jobs(run_id) if job.get("state") in {"pending", "running"}]
    pending.sort(key=lambda job: (
        0 if job.get("kind") == "provider-collection" else 1,
        0 if job.get("provider") == "sbertrek" else 1,
        job.get("job_id", ""),
    ))
    return pending[0] if pending else None


def next_job_payload(run_id: str) -> dict:
    job = active_job(run_id)
    if not job:
        return {}
    return {
        "allowed_next_action": "delegate-collector-job",
        "delegation_required": True,
        "next_job": {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "provider": job["provider"],
            "path": str(job_path(run_id, job["job_id"])),
            "collector_contract": job["collector_contract"],
            "return_contract": "status-and-paths-only",
        },
    }


def next_query_payload(provider: str, query: dict) -> dict:
    return {
        "next_query": {
            "provider": provider, "purpose": query["purpose"],
            "language": query["language"], "query": query["exact"],
            "method": query.get("method"), "exact_query_required": True,
            "pagination_required": True,
        },
        "required_sequence": ["MCP call", "ingest-query-response"],
    }


def write_status(run_id: str, status: str, *, gaps: list[str] | None = None, allowed: str | None = None, complete: bool = False, extra: dict | None = None) -> dict:
    payload = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "status": status, "workflow_complete": complete,
        "final_response_allowed": complete, "gaps": gaps or [],
        "allowed_next_action": allowed,
        "paths": {
            "run_status": str(status_path(run_id)),
            "session_log": str(session_log_path(run_id)),
            "scope": str(run_meta_path(run_id)),
            "jobs": str(jobs_root(run_id)),
            "providers": str(run_root(run_id) / "providers"),
        },
        **(extra or {}),
    }
    save_json(status_path(run_id), payload)
    return payload


def canonical_estimate(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    normalized = " ".join(str(result.get("unit")).casefold().replace("-", " ").replace("_", " ").split())
    if normalized in SP_UNITS:
        result["unit"] = "story-points"
    return result


def canonical_role_estimate(value: Any) -> Any:
    result = canonical_estimate(value)
    return result if isinstance(result, dict) else value


def merged_role_estimates(
    sber: dict | None, jira: dict | None,
) -> tuple[dict[str, dict], dict[str, str | None], list[dict]]:
    result: dict[str, dict] = {}
    sources: dict[str, str | None] = {}
    conflicts: list[dict] = []
    for role in ESTIMATE_ROLES:
        svalue = canonical_role_estimate((sber or {}).get("role_estimates", {}).get(role))
        jvalue = canonical_role_estimate((jira or {}).get("role_estimates", {}).get(role))
        chosen, source = (svalue, "sbertrek") if svalue not in (None, {}, "") else (jvalue, "jira")
        if chosen not in (None, {}, ""):
            result[role] = {**chosen, "source": source}
            sources[role] = source
        else:
            sources[role] = None
        if svalue not in (None, {}, "") and jvalue not in (None, {}, ""):
            comparable_sber = {key: svalue.get(key) for key in ("value", "unit")}
            comparable_jira = {key: jvalue.get(key) for key in ("value", "unit")}
            if comparable_sber != comparable_jira:
                conflicts.append({
                    "field": f"role_estimates.{role}",
                    "sbertrek": svalue,
                    "jira": jvalue,
                    "resolution": "sbertrek-preserved",
                })
    return result, sources, conflicts


def merged_estimate_fields(sber: dict | None, jira: dict | None) -> list[dict]:
    result = []
    for provider, item in (("sbertrek", sber), ("jira", jira)):
        for field in (item or {}).get("estimate_fields", []):
            result.append({**field, "provider": provider})
    return result


def prefixed_work_summary(role: str, summary: str) -> str:
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


def execution_work_items(issues: list[dict]) -> list[dict]:
    result: list[dict] = []
    for issue in issues:
        if issue.get("development", {}).get("state") == "excluded":
            continue
        identity = issue.get("jira_key") or issue.get("sbertrek_key")
        if not identity:
            continue
        assignee = issue.get("assignee") if isinstance(issue.get("assignee"), dict) else None
        assignee_team_id = assignee.get("team_id") if assignee else None
        for role in ESTIMATE_ROLES:
            estimate = issue.get("role_estimates", {}).get(role)
            if not isinstance(estimate, dict) or not isinstance(estimate.get("value"), (int, float)) or estimate["value"] <= 0:
                continue
            role_assignee = assignee if isinstance(assignee_team_id, str) and assignee_team_id.startswith(role) else None
            result.append({
                "work_item_id": f"{identity}/{role}",
                "tracker_key": identity,
                "sbertrek_key": issue.get("sbertrek_key"),
                "jira_key": issue.get("jira_key"),
                "role": role,
                "summary": prefixed_work_summary(role, str(issue.get("summary") or identity)),
                "estimate": canonical_role_estimate(estimate),
                "assignee": role_assignee,
                "status": issue.get("status"),
                "development": issue.get("development"),
            })
    return result


def parse_release(value: str) -> dict:
    release_key, sep, name = value.partition("=")
    return {"key": release_key.strip(), "name": name.strip()} if sep else {"key": value.strip(), "name": value.strip()}


def participant(account_id: str | None, name: str | None) -> dict | None:
    return {"id": account_id, "name": name} if account_id else None


def normalized_status_set(config: dict, provider: str, kind: str) -> set[str]:
    return {item.strip().casefold() for item in config["status_rules"][provider][kind] or []}


def participant_role(config: dict, provider: str, value: Any) -> str | None:
    if not isinstance(value, dict) or not value.get("id"):
        return None
    mapping = config["participants"][provider].get(value["id"])
    return team_role(mapping["team_id"]) if mapping else None


def normalized_participant_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def suggested_team_id_for_name(snapshots: dict[str, dict], config: dict, name: str) -> str | None:
    target = normalized_participant_name(name)
    if not target:
        return None
    candidates: set[str] = set()
    for provider, snapshot in snapshots.items():
        values = []
        for item in snapshot["issues"]:
            values.append(item.get("assignee"))
            for event in item["history"]["events"]:
                if event["field"] == "assignee":
                    values.extend((event.get("from"), event.get("to")))
        for value in values:
            if not isinstance(value, dict) or normalized_participant_name(value.get("name")) != target:
                continue
            member = config["participants"][provider].get(value.get("id"))
            if member:
                candidates.add(member["team_id"])
    return next(iter(candidates)) if len(candidates) == 1 else None


def merged_value(field: str, sber: dict | None, jira: dict | None) -> tuple[Any, str | None, dict | None]:
    svalue = sber.get(field) if sber else None
    jvalue = jira.get(field) if jira else None
    if field == "estimate":
        svalue, jvalue = canonical_estimate(svalue), canonical_estimate(jvalue)
    chosen, source = (svalue, "sbertrek") if svalue not in (None, "", [], {}) else (jvalue, "jira")
    conflict = None
    comparable_equal = svalue == jvalue
    if field == "issue_type" and isinstance(svalue, str) and isinstance(jvalue, str):
        comparable_equal = svalue.casefold() == jvalue.casefold()
    if svalue not in (None, "", [], {}) and jvalue not in (None, "", [], {}) and not comparable_equal:
        conflict = {"field": field, "sbertrek": svalue, "jira": jvalue, "resolution": "sbertrek-preserved"}
    return chosen, source if chosen not in (None, "", [], {}) else None, conflict


def merged_history(sber: dict | None, jira: dict | None) -> list[dict]:
    seen: set[str] = set()
    result = []
    for provider, item in (("sbertrek", sber), ("jira", jira)):
        if not item:
            continue
        for event in item["history"]["events"]:
            fingerprint = json.dumps({name: event.get(name) for name in ("at", "field", "from", "to")}, ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            result.append({**event, "source": provider})
    return sorted(result, key=lambda item: item["at"])


def assignment_dates(sber: dict | None, jira: dict | None, config: dict) -> tuple[str | None, str | None]:
    assigned_at = None
    work_started_at = None
    for event in merged_history(sber, jira):
        if event["field"] != "assignee" or not isinstance(event.get("to"), dict) or not event["to"].get("id"):
            continue
        assigned_at = assigned_at or event["at"]
        if work_started_at is None and participant_role(config, event["source"], event["to"]) == "developer":
            work_started_at = event["at"]
    return assigned_at, work_started_at


def enrich_assignee(value: Any, source: str | None, config: dict) -> Any:
    if not isinstance(value, dict) or not value.get("id") or source not in PROVIDERS:
        return value
    member = config["participants"][source].get(value["id"])
    if not member:
        return value
    return {**value, "team_id": member["team_id"], "role": team_role(member["team_id"])}


def provider_development_state(item: dict, provider: str, config: dict) -> dict:
    if str(item.get("issue_type") or "").casefold() not in {value.casefold() for value in config["development_issue_types"]}:
        return {"state": "not-development-unit", "basis": "issue-type"}
    status = str(item.get("status") or "")
    if status.casefold() in normalized_status_set(config, provider, "excluded"):
        return {"state": "excluded", "basis": f"{provider}-status", "status": status}
    if status.casefold() in normalized_status_set(config, provider, "completed"):
        return {"state": "completed", "basis": f"{provider}-status", "status": status}
    assignee_events = [
        {**event, "source": provider}
        for event in item["history"]["events"]
        if event["field"] == "assignee"
    ]
    latest_handoff = None
    for event in assignee_events:
        before = participant_role(config, event["source"], event.get("from"))
        after = participant_role(config, event["source"], event.get("to"))
        if after == "developer":
            latest_handoff = None
        if before == "developer" and after and after != "developer":
            latest_handoff = event
    if latest_handoff:
        return {"state": "completed", "basis": "developer-handoff", "at": latest_handoff["at"]}
    current_assignee = item.get("assignee")
    if participant_role(config, provider, current_assignee) == "developer":
        return {"state": "in-progress", "basis": f"{provider}-developer-assignee"}
    if any(
        participant_role(config, event["source"], event.get("to")) == "developer"
        for event in assignee_events
    ):
        return {"state": "in-progress", "basis": "developer-assignment-history"}
    if current_assignee is None and item["history"]["state"] == "complete":
        return {"state": "not-started", "basis": "complete-history-without-developer-assignment"}
    return {"state": "unknown", "basis": "insufficient-development-evidence"}


def development_state(sber: dict | None, jira: dict | None, config: dict) -> dict:
    item = sber or jira or {}
    provider = "sbertrek" if sber else "jira"
    if str(item.get("issue_type") or "").casefold() not in {value.casefold() for value in config["development_issue_types"]}:
        return {"state": "not-development-unit", "basis": "issue-type"}
    status = str(item.get("status") or "")
    if status.casefold() in normalized_status_set(config, provider, "excluded"):
        return {"state": "excluded", "basis": f"{provider}-status", "status": status}
    if status.casefold() in normalized_status_set(config, provider, "completed"):
        return {"state": "completed", "basis": f"{provider}-status", "status": status}
    assignee_events = [event for event in merged_history(sber, jira) if event["field"] == "assignee"]
    latest_handoff = None
    for event in assignee_events:
        before = participant_role(config, event["source"], event.get("from"))
        after = participant_role(config, event["source"], event.get("to"))
        if after == "developer":
            latest_handoff = None
        if before == "developer" and after and after != "developer":
            latest_handoff = event
    if latest_handoff:
        return {"state": "completed", "basis": "developer-handoff", "at": latest_handoff["at"]}
    current_assignee, assignee_source, _ = merged_value("assignee", sber, jira)
    if assignee_source and participant_role(config, assignee_source, current_assignee) == "developer":
        return {"state": "in-progress", "basis": f"{assignee_source}-developer-assignee"}
    if any(
        participant_role(config, event["source"], event.get("to")) == "developer"
        for event in assignee_events
    ):
        return {"state": "in-progress", "basis": "developer-assignment-history"}
    histories = [candidate["history"]["state"] for candidate in (sber, jira) if candidate]
    if current_assignee is None and histories and all(state == "complete" for state in histories):
        return {"state": "not-started", "basis": "complete-history-without-developer-assignment"}
    return {"state": "unknown", "basis": "insufficient-development-evidence"}


def development_conflicts(snapshots: dict[str, dict], config: dict) -> list[dict]:
    jira = snapshots.get("jira")
    if not jira:
        return []
    jira_issues = {item["key"]: item for item in jira["issues"]}
    excluded_sber = excluded_sbertrek_keys(snapshots)
    development_types = {value.casefold() for value in config["development_issue_types"]}
    result = []
    for sber in sorted(snapshots["sbertrek"]["issues"], key=lambda item: item["key"]):
        if sber["key"] in excluded_sber:
            continue
        jira_key = sber.get("jira_key")
        jira_issue = jira_issues.get(jira_key) if jira_key else None
        if not jira_issue or str(sber.get("issue_type") or "").casefold() not in development_types:
            continue
        primary_status = str(sber.get("status") or "").casefold()
        if primary_status in normalized_status_set(config, "sbertrek", "excluded") | normalized_status_set(config, "sbertrek", "completed"):
            continue
        sber_role = participant_role(config, "sbertrek", sber.get("assignee"))
        jira_role = participant_role(config, "jira", jira_issue.get("assignee"))
        if not sber_role or not jira_role or (sber_role == "developer") == (jira_role == "developer"):
            continue
        sber_development = provider_development_state(sber, "sbertrek", config)
        jira_development = provider_development_state(jira_issue, "jira", config)
        if (
            sber_development["state"] == jira_development["state"]
            or sber_development["state"] not in DEVELOPMENT_DECISION_STATES
            or jira_development["state"] not in DEVELOPMENT_DECISION_STATES
        ):
            continue
        result.append({
            "sbertrek_key": sber["key"],
            "jira_key": jira_issue["key"],
            "summary": sber.get("summary") or jira_issue.get("summary") or sber["key"],
            "sbertrek_assignee": enrich_assignee(sber.get("assignee"), "sbertrek", config),
            "jira_assignee": enrich_assignee(jira_issue.get("assignee"), "jira", config),
            "sbertrek_state": sber_development["state"],
            "jira_state": jira_development["state"],
        })
    return result


def first_unknown_participant(snapshots: dict[str, dict], config: dict) -> dict | None:
    development_types = {item.casefold() for item in config["development_issue_types"]}
    excluded_sber = excluded_sbertrek_keys(snapshots)
    for provider in PROVIDERS:
        snapshot = snapshots.get(provider)
        if not snapshot:
            continue
        for item in snapshot["issues"]:
            if provider == "sbertrek" and item["key"] in excluded_sber:
                continue
            if str(item.get("issue_type") or "").casefold() not in development_types:
                continue
            values = [item.get("assignee")]
            for event in item["history"]["events"]:
                if event["field"] == "assignee":
                    values.extend((event.get("from"), event.get("to")))
            for value in values:
                if isinstance(value, dict) and value.get("id") and value["id"] not in config["participants"][provider]:
                    name = value.get("name") or value["id"]
                    return {
                        "provider": provider,
                        "account_id": value["id"],
                        "name": name,
                        "suggested_team_id": suggested_team_id_for_name(snapshots, config, name),
                    }
    return None


def pending_participant_path(run_id: str) -> Path:
    return run_root(run_id) / "pending-participant.json"


def pending_development_decision_path(run_id: str) -> Path:
    return run_root(run_id) / "pending-development-decision.json"


def development_decisions_path(run_id: str) -> Path:
    return run_root(run_id) / "development-decisions.json"


def empty_development_decisions(run_id: str) -> dict:
    return {
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "default_choice": None,
        "decisions": [],
    }


def load_development_decisions(run_id: str) -> dict:
    path = development_decisions_path(run_id)
    payload = load_json(path) if path.is_file() else empty_development_decisions(run_id)
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != PROTOCOL
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("run_id") != run_id
        or payload.get("default_choice") not in (None, "sbertrek", "jira")
        or not isinstance(payload.get("decisions"), list)
    ):
        raise ValueError("Повреждён development-decisions.json")
    keys: set[str] = set()
    all_choices = []
    for item in payload["decisions"]:
        if not isinstance(item, dict):
            raise ValueError("Повреждена запись решения по ЖЦ")
        core = {name: item.get(name) for name in (
            "sbertrek_key", "jira_key", "choice", "state", "apply_to_all",
            "sbertrek_state", "jira_state",
        )}
        key_value = core["sbertrek_key"]
        if (
            not isinstance(key_value, str)
            or not ISSUE_KEY.fullmatch(key_value)
            or key_value in keys
            or not isinstance(core["jira_key"], str)
            or not ISSUE_KEY.fullmatch(core["jira_key"])
            or core["choice"] not in DEVELOPMENT_DECISION_CHOICES
            or core["state"] not in DEVELOPMENT_DECISION_STATES
            or not isinstance(core["apply_to_all"], bool)
            or core["sbertrek_state"] not in DEVELOPMENT_DECISION_STATES
            or core["jira_state"] not in DEVELOPMENT_DECISION_STATES
            or item.get("record_sha256") != object_sha256(core)
        ):
            raise ValueError("Повреждена запись решения по ЖЦ")
        if core["apply_to_all"]:
            if core["choice"] not in {"sbertrek", "jira"}:
                raise ValueError("Массовое решение по ЖЦ допустимо только для приоритета трекера")
            all_choices.append(core["choice"])
        require_tracker_command(
            run_id, "set-development-decision",
            detail_markers=(f"key={key_value}", f"record_sha256={item['record_sha256']}"),
        )
        keys.add(key_value)
    expected_default = all_choices[-1] if all_choices else None
    if payload["default_choice"] != expected_default or len(set(all_choices)) > 1:
        raise ValueError("Массовая политика решений по ЖЦ противоречива")
    return payload


def validate_development_decisions(conflicts: list[dict], payload: dict) -> dict[str, dict]:
    conflicts_by_key = {item["sbertrek_key"]: item for item in conflicts}
    applied: dict[str, dict] = {}
    for decision in payload["decisions"]:
        conflict = conflicts_by_key.get(decision["sbertrek_key"])
        if not conflict:
            raise ValueError("Решение по ЖЦ не соответствует текущим конфликтам tracker-run")
        if any(decision[name] != conflict[name] for name in ("jira_key", "sbertrek_state", "jira_state")):
            raise ValueError("Решение по ЖЦ не соответствует сохранённым данным трекеров")
        if decision["choice"] in {"sbertrek", "jira"}:
            expected_state = conflict[f"{decision['choice']}_state"]
            if decision["state"] != expected_state:
                raise ValueError("Решение по ЖЦ не соответствует выбранному приоритету трекера")
        elif decision["apply_to_all"]:
            raise ValueError("Пользовательский вариант ЖЦ нельзя применять ко всем задачам")
        applied[decision["sbertrek_key"]] = decision
    default_choice = payload["default_choice"]
    if default_choice:
        for conflict in conflicts:
            if conflict["sbertrek_key"] in applied:
                continue
            applied[conflict["sbertrek_key"]] = {
                "sbertrek_key": conflict["sbertrek_key"],
                "jira_key": conflict["jira_key"],
                "choice": default_choice,
                "state": conflict[f"{default_choice}_state"],
                "apply_to_all": True,
                "sbertrek_state": conflict["sbertrek_state"],
                "jira_state": conflict["jira_state"],
                "inherited_run_default": True,
            }
    return applied


def development_assignee_label(value: dict) -> str:
    return (
        f"{value.get('name') or value['id']} "
        f"(account {value['id']}, team_id {value['team_id']}, role {value['role']})"
    )


def development_decision_question(conflict: dict) -> str:
    return (
        f"По задаче {conflict['sbertrek_key']} / {conflict['jira_key']} расходится состояние разработки. "
        f"SberTrek: {development_assignee_label(conflict['sbertrek_assignee'])}, "
        f"состояние {conflict['sbertrek_state']}. "
        f"Jira: {development_assignee_label(conflict['jira_assignee'])}, "
        f"состояние {conflict['jira_state']}. Как разрешить конфликт?\n"
        "1. Приоритет SberTrek только для этой задачи.\n"
        "2. Приоритет SberTrek для этой и всех последующих конфликтующих задач текущей сверки.\n"
        "3. Приоритет Jira только для этой задачи.\n"
        "4. Приоритет Jira для этой и всех последующих конфликтующих задач текущей сверки.\n"
        "5. Свой вариант: completed, in-progress, not-started или unknown."
    )


def snapshot_gaps(snapshot: dict, excluded_keys: set[str] | None = None) -> list[str]:
    gaps = []
    excluded_keys = excluded_keys or set()
    if not snapshot["collection_complete"]:
        gaps.append(f"{snapshot['provider']}.collection.pending")
    missing = set(snapshot["query"]["keys"]) - {item["key"] for item in snapshot["issues"]}
    gaps.extend(f"{snapshot['provider']}.{item}.card.pending" for item in sorted(missing))
    for item in snapshot["issues"]:
        if item["key"] in excluded_keys:
            continue
        if item["history"]["state"] == "pending":
            gaps.append(f"{snapshot['provider']}.{item['key']}.history.pending")
    return gaps


def count_values(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def machine_summary(issues: list[dict], discrepancies: list[dict]) -> dict:
    story_points_total = 0.0
    role_totals = {role: 0.0 for role in ESTIMATE_ROLES}
    unestimated = 0
    non_story_point_estimates = 0
    inferred_general = 0
    for item in issues:
        role_values = item.get("role_estimates", {})
        role_total_used = False
        for role in ESTIMATE_ROLES:
            role_estimate = role_values.get(role) if isinstance(role_values, dict) else None
            if not isinstance(role_estimate, dict):
                continue
            if role_estimate.get("unit") == "story-points" and isinstance(role_estimate.get("value"), (int, float)):
                value = float(role_estimate["value"])
                role_totals[role] += value
                story_points_total += value
                role_total_used = True
                if role_estimate.get("inferred_from_general"):
                    inferred_general += 1
            else:
                non_story_point_estimates += 1
        estimate = item.get("estimate")
        if role_total_used:
            continue
        if isinstance(estimate, dict) and estimate.get("unit") == "story-points" and isinstance(estimate.get("value"), (int, float)):
            story_points_total += float(estimate["value"])
        elif estimate in (None, {}, ""):
            unestimated += 1
        else:
            non_story_point_estimates += 1
    missing = sorted(
        item["jira_key"] for item in discrepancies
        if item.get("kind") == "jira-counterpart-not-returned" and item.get("jira_key")
    )
    excluded = sorted(
        item["sbertrek_key"] for item in discrepancies
        if item.get("kind") == "jira-counterpart-absent-excluded" and item.get("sbertrek_key")
    )
    absent = sorted(
        item["jira_key"] for item in discrepancies
        if item.get("kind") == "jira-counterpart-absent-excluded" and item.get("jira_key")
    )
    return {
        "story_points_total": story_points_total,
        "role_estimate_totals": role_totals,
        "role_work_item_count": sum(1 for item in issues for role in ESTIMATE_ROLES if role in item.get("role_estimates", {})),
        "general_estimate_role_inference_count": inferred_general,
        "unestimated_issue_count": unestimated,
        "non_story_point_estimate_count": non_story_point_estimates,
        "status_counts": count_values([str(item.get("status") or "unassigned") for item in issues]),
        "development_state_counts": count_values([str(item["development"]["state"]) for item in issues]),
        "discrepancy_kind_counts": count_values([str(item["kind"]) for item in discrepancies]),
        "missing_jira_counterparts": missing,
        "absent_jira_counterparts": absent,
        "excluded_sbertrek_issue_count": len(excluded),
        "excluded_sbertrek_issues": excluded,
    }


def reconcile_data(snapshots: dict[str, dict], config: dict, development_decisions: dict[str, dict] | None = None) -> dict:
    development_decisions = development_decisions or {}
    sber = snapshots["sbertrek"]
    jira = snapshots.get("jira")
    sber_issues = {item["key"]: item for item in sber["issues"]}
    jira_issues = {item["key"]: item for item in jira["issues"]} if jira else {}
    absent_jira = confirmed_absent_map(jira)
    paired_jira: set[str] = set()
    merged, discrepancies, excluded = [], [], []
    for sber_key, sissue in sorted(sber_issues.items()):
        jira_key = sissue.get("jira_key")
        if jira_key and jira_key in absent_jira:
            exclusion = {
                "sbertrek_key": sber_key,
                "jira_key": jira_key,
                "reason": "jira-counterpart-absent",
                "evidence": absent_jira[jira_key],
            }
            excluded.append(exclusion)
            discrepancies.append({"kind": "jira-counterpart-absent-excluded", **exclusion})
            continue
        jissue = jira_issues.get(jira_key) if jira_key else None
        if jissue:
            paired_jira.add(jira_key)
        record: dict[str, Any] = {"sbertrek_key": sber_key, "jira_key": jira_key}
        sources, conflicts = {}, []
        for field in MERGED_FIELDS:
            value, source, conflict = merged_value(field, sissue, jissue)
            record[field], sources[field] = value, source
            if conflict:
                conflicts.append(conflict)
                discrepancies.append({"kind": "field-conflict", "sbertrek_key": sber_key, "jira_key": jira_key, **conflict})
        record["assignee"] = enrich_assignee(record.get("assignee"), sources.get("assignee"), config)
        role_estimates, role_sources, role_conflicts = merged_role_estimates(sissue, jissue)
        record["role_estimates"] = role_estimates
        record["estimate_fields"] = merged_estimate_fields(sissue, jissue)
        sources["role_estimates"] = role_sources
        for conflict in role_conflicts:
            conflicts.append(conflict)
            discrepancies.append({"kind": "field-conflict", "sbertrek_key": sber_key, "jira_key": jira_key, **conflict})
        assigned_at, work_started_at = assignment_dates(sissue, jissue, config)
        development = development_state(sissue, jissue, config)
        if sber_key in development_decisions:
            decision = development_decisions[sber_key]
            development = {
                "state": decision["state"],
                "basis": "user-decision",
                "choice": decision["choice"],
                "apply_to_all": decision["apply_to_all"],
                "inherited_run_default": decision.get("inherited_run_default", False),
                "sbertrek_state": decision["sbertrek_state"],
                "jira_state": decision["jira_state"],
            }
        record.update({
            "field_sources": sources,
            "conflicts": conflicts,
            "history": merged_history(sissue, jissue),
            "assigned_at": assigned_at,
            "work_started_at": work_started_at,
            "development": development,
        })
        merged.append(record)
        if jira_key and not jissue:
            discrepancies.append({"kind": "jira-counterpart-not-returned", "sbertrek_key": sber_key, "jira_key": jira_key})
    for jira_key, jissue in sorted(jira_issues.items()):
        if jira_key in paired_jira:
            continue
        record = {"sbertrek_key": None, "jira_key": jira_key, "history": merged_history(None, jissue), "conflicts": []}
        for field in MERGED_FIELDS:
            record[field] = canonical_estimate(jissue.get(field)) if field == "estimate" else jissue.get(field)
        record["field_sources"] = {field: "jira" if record.get(field) not in (None, "", [], {}) else None for field in MERGED_FIELDS}
        record["role_estimates"] = {
            role: {**canonical_role_estimate(value), "source": "jira"}
            for role, value in jissue.get("role_estimates", {}).items()
        }
        record["estimate_fields"] = merged_estimate_fields(None, jissue)
        record["field_sources"]["role_estimates"] = {
            role: "jira" if role in record["role_estimates"] else None for role in ESTIMATE_ROLES
        }
        record["assignee"] = enrich_assignee(record.get("assignee"), "jira", config)
        record["assigned_at"], record["work_started_at"] = assignment_dates(None, jissue, config)
        record["development"] = development_state(None, jissue, config)
        merged.append(record)
        discrepancies.append({"kind": "jira-only", "jira_key": jira_key})
    scope = sber["scope"]
    primary = snapshots[scope["provider"]]
    limitations = []
    if scope["kind"] == "tasks":
        limitations.extend(f"scope-key-not-returned:{scope['provider']}:{item}" for item in sorted(set(scope["ids"]) - set(primary["query"]["keys"])))
    for provider, snapshot in snapshots.items():
        if snapshot["query"]["state"] == "unavailable":
            limitations.append(f"{provider}-targeted-query-unavailable")
        for item in snapshot["issues"]:
            if item["history"]["state"] == "unavailable":
                limitations.append(f"{provider}-history-unavailable:{item['key']}")
        if provider == "sbertrek" and any(
            page.get("returned_count") == SBER_EXPORT_MAX_RESULTS
            for page in snapshot["query"].get("pages", [])
        ):
            limitations.append(f"sbertrek-export-limit-reached:{SBER_EXPORT_MAX_RESULTS}")
        if provider == "jira" and any(
            page.get("returned_count") == JIRA_SEARCH_MAX_RESULTS and not page.get("page_metadata")
            for page in snapshot["query"].get("pages", [])
        ):
            limitations.append(f"jira-search-limit-reached:{JIRA_SEARCH_MAX_RESULTS}")
    limitations.extend(
        f"general-estimate-role-unresolved:{item.get('jira_key') or item.get('sbertrek_key')}"
        for item in merged
        if item.get("estimate") not in (None, {}, "") and not item.get("role_estimates")
    )
    work_items = execution_work_items(merged)
    counts = {
        "sbertrek": len(sber_issues),
        "jira": len(jira_issues),
        "matched": len(paired_jira),
        "excluded": len(excluded),
        "merged": len(merged),
        "work_items": len(work_items),
        "discrepancies": len(discrepancies),
    }
    groupings: dict[str, dict[str, list[str]]] = {"epics": {}, "releases": {}}
    for item in merged:
        identity = item.get("sbertrek_key") or item.get("jira_key")
        epic = item.get("epic")
        epic_key = epic.get("key") if isinstance(epic, dict) else None
        groupings["epics"].setdefault(epic_key or "unassigned", []).append(identity)
        releases = item.get("releases") or []
        if not releases:
            groupings["releases"].setdefault("unassigned", []).append(identity)
        for release in releases:
            release_key = release.get("key") if isinstance(release, dict) else str(release)
            groupings["releases"].setdefault(release_key or "unassigned", []).append(identity)
    summary = machine_summary(merged, discrepancies)
    return {
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "status": "tracker-read-reconciled",
        "scope": scope,
        "counts": counts,
        "summary": summary,
        "issues": merged,
        "work_items": work_items,
        "excluded_issues": excluded,
        "groupings": groupings,
        "discrepancies": discrepancies,
        "limitations": limitations,
    }


def render_report(result: dict) -> str:
    scope = result["scope"]
    lines = [f"# Сверка трекеров: {scope['label']}", "", "## Область", "", f"- Тип: `{scope['kind']}`", f"- Исходный трекер: `{scope['provider']}`", f"- Ключи: {', '.join(scope['ids'])}", "", "## Сводка", ""]
    labels = {"sbertrek": "Задач SberTrek", "jira": "Задач Jira", "matched": "Склеено пар", "excluded": "Исключено SberTrek-задач", "merged": "Итоговых задач", "work_items": "Ролевых полос", "discrepancies": "Расхождений"}
    lines += [f"- {labels[name]}: {value}" for name, value in result["counts"].items()]
    summary = result["summary"]
    lines += [
        f"- Суммарная оценка: {summary['story_points_total']} story-points",
        "- По ролям: " + ", ".join(f"{role}={summary['role_estimate_totals'][role]}" for role in ESTIMATE_ROLES),
        f"- Общих оценок распределено по префиксу: {summary['general_estimate_role_inference_count']}",
        f"- Без оценки: {summary['unestimated_issue_count']}",
        f"- Оценка в других единицах: {summary['non_story_point_estimate_count']}",
        "- Статусы: " + ", ".join(f"{name}={value}" for name, value in summary["status_counts"].items()),
        "- Состояния разработки: " + ", ".join(f"{name}={value}" for name, value in summary["development_state_counts"].items()),
        "- Виды расхождений: " + ", ".join(f"{name}={value}" for name, value in summary["discrepancy_kind_counts"].items()),
        "- Не найдены в Jira: " + (", ".join(summary["missing_jira_counterparts"]) or "нет"),
        "- Подтверждённо отсутствуют в Jira: " + (", ".join(summary["absent_jira_counterparts"]) or "нет"),
        "- Исключены задачи SberTrek: " + (", ".join(summary["excluded_sbertrek_issues"]) or "нет"),
    ]
    lines += ["", "## Задачи", "", "| SberTrek | Jira | Название | Статус | Исполнитель | Общая оценка | AN | BE | FE | QA | В работе с | Состояние |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for item in result["issues"]:
        assignee, estimate = item.get("assignee") or {}, item.get("estimate") or {}
        if isinstance(assignee, dict):
            team_id, name = assignee.get("team_id"), assignee.get("name")
            assignee_text = f"{team_id} ({name})" if team_id and name else team_id or name or assignee.get("id") or "—"
        else:
            assignee_text = str(assignee)
        estimate_text = f"{estimate.get('value')} {estimate.get('unit')}" if isinstance(estimate, dict) and estimate.get("value") is not None else "—"
        role_cells = []
        for role in ESTIMATE_ROLES:
            role_estimate = item.get("role_estimates", {}).get(role) or {}
            role_cells.append(f"{role_estimate.get('value')} {role_estimate.get('unit')}" if role_estimate else "—")
        cells = [item.get("sbertrek_key") or "—", item.get("jira_key") or "—", item.get("summary") or "—", item.get("status") or "—", assignee_text, estimate_text, *role_cells, item.get("work_started_at") or "—", item["development"]["state"]]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines += ["", "## Ролевые полосы для Ганта", "", "| Work item | Задача | Роль | Название | Оценка | Исполнитель |", "|---|---|---|---|---:|---|"]
    for item in result["work_items"]:
        estimate = item["estimate"]
        assignee = item.get("assignee") or {}
        cells = [
            item["work_item_id"], item["tracker_key"], item["role"], item["summary"],
            f"{estimate.get('value')} {estimate.get('unit')}", assignee.get("team_id") or "—",
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    if not result["work_items"]:
        lines.append("| — | — | — | — | — | — |")
    lines += ["", "## Ограничения", ""] + ([f"- {item}" for item in result["limitations"]] or ["- Нет"])
    lines += ["", "## Исключённые задачи", ""]
    lines += [
        f"- `{item['sbertrek_key']}` исключена: Jira-контрагент `{item['jira_key']}` подтверждённо отсутствует."
        for item in result["excluded_issues"]
    ] or ["- Нет"]
    for group, title in (("epics", "Группировка по эпикам"), ("releases", "Группировка по релизам")):
        lines += ["", f"## {title}", ""]
        lines += [f"- **{name}**: {', '.join(keys)}" for name, keys in sorted(result["groupings"][group].items())] or ["- Нет"]
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
    if config_gaps(config):
        payload = config_status(config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    existing_run = active_run_id()
    if existing_run:
        raise ValueError(
            f"Уже существует незавершённый tracker-run {existing_run}; "
            "новый begin запрещён. Продолжай существующий run через run-status"
        )
    ids = unique_keys(args.scope_id)
    if args.scope_kind == "epic" and len(ids) != 1:
        raise ValueError("Для scope-kind=epic требуется ровно один --scope-id")
    if args.scope_provider == "jira" and not config["jira_enabled"]:
        raise ValueError("Jira отключена в tracker-config.json; Jira не может быть исходной областью")
    scope = {
        "kind": args.scope_kind,
        "provider": args.scope_provider,
        "ids": ids,
        "label": args.label.strip(),
        "source": args.scope_source.strip(),
        "intent": args.intent,
    }
    if not scope["label"] or not scope["source"]:
        raise ValueError("--label и --scope-source не могут быть пустыми")
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    providers = list(enabled_providers(config))
    save_json(run_meta_path(run_id), {"protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id, "scope": scope, "created_at": now()})
    for provider in providers:
        save_json(snapshot_path(run_id, provider), snapshot_template(run_id, provider, scope, config))
    initialize_session_log(run_id, scope, providers)
    provider, query = current_query(run_id, config) or (None, None)
    assert provider and query
    save_job(run_id, collection_job(run_id, provider, query))
    status = write_status(
        run_id,
        "tracker-read-awaiting-collector",
        gaps=[f"{provider}.collection-job.pending"],
        extra=next_job_payload(run_id),
    )
    save_json(active_run_path(), {"protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": now()})
    append_session_log(run_id, source="trackerctl", event="command", details="command=begin; exit=0")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def jira_ingest_epic_links_command(args: argparse.Namespace) -> int:
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != "jira":
        raise ValueError("Сейчас нет разрешённого запроса связей Jira-эпика")
    path, snapshot = load_snapshot(args.run_id, "jira")
    ensure_mutable(snapshot)
    query = snapshot["query"]
    job_path_value, job = load_job(args.run_id, "collection-jira")
    if (
        snapshot["scope"].get("provider") != "jira"
        or snapshot["scope"].get("kind") != "epic"
        or query.get("purpose") != "epic-links"
        or query.get("method") != "issuelinks"
        or job.get("state") not in {"pending", "running"}
        or active_job(args.run_id) != job
    ):
        raise ValueError("Импорт issuelinks разрешён только активному Jira epic collector-job")
    call = evidence(args.evidence, "jira")
    if logged_mcp_details(args.run_id, call):
        raise ValueError("Этот MCP-вызов уже записан в журнале")
    _, payload, response_size, response_digest = response_json(args.response_file)
    epic_key, links, links_path = jira_issue_links(payload)
    expected_epic = snapshot["scope"]["ids"][0]
    if epic_key != expected_epic:
        raise ValueError(f"Ответ issuelinks относится к {epic_key}, ожидался {expected_epic}")
    children = jira_epic_child_keys(links)
    discovery_query = query["exact"]
    details = (
        f"operation=epic-links; outcome=success; query_sha256={query_digest(discovery_query)}; "
        f"returned={len(links)}; child_count={len(children)}; response_sha256={response_digest}; "
        f"response_bytes={response_size}; parser_path={links_path}; query={discovery_query}; "
        "summary=Jira issuelinks structurally imported"
    )
    append_session_log(
        args.run_id, source="mcp", event="call", provider="jira",
        evidence_value=call, details=details,
    )
    query["discovery"] = {
        "method": "issuelinks-PartOf-inward_issue",
        "query": discovery_query,
        "evidence": call,
        "response_sha256": response_digest,
        "response_bytes": response_size,
        "records_path": links_path,
        "link_count": len(links),
        "child_keys": children,
    }
    if children:
        exact = jql_keys(children)
        query.update({
            "purpose": "epic-members",
            "language": "JQL",
            "exact": exact,
            "initial_exact": exact,
            "requested_keys": children,
            "state": "pending",
        })
        job["query"].update({"language": "JQL", "text": exact, "sha256": query_digest(exact)})
        status = "jira-epic-members-query-ready"
    else:
        query.update({
            "purpose": "epic-members",
            "language": "JQL",
            "exact": None,
            "initial_exact": None,
            "requested_keys": [],
            "state": "complete",
        })
        status = "jira-epic-empty"
    job["state"] = "running"
    save_json(path, snapshot)
    save_json(job_path_value, job)
    args.record_sha256 = object_sha256(query["discovery"])
    payload_out = {
        "status": status,
        "run_id": args.run_id,
        "epic": epic_key,
        "link_count": len(links),
        "child_count": len(children),
        "preferred_fields": list(JIRA_QUERY_FIELDS),
        "allowed_next_action": "execute-returned-jql" if children else "collector-complete",
    }
    if children:
        payload_out.update(next_query_payload("jira", query))
    print(json.dumps(payload_out, ensure_ascii=False, indent=2))
    return 0


def sbertrek_ingest_counterpart_epic_command(args: argparse.Namespace) -> int:
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != "sbertrek":
        raise ValueError("Сейчас нет разрешённого поиска SberTrek-эпика")
    path, snapshot = load_snapshot(args.run_id, "sbertrek")
    ensure_mutable(snapshot)
    query = snapshot["query"]
    job_path_value, job = load_job(args.run_id, "collection-sbertrek")
    if (
        snapshot["scope"].get("provider") != "jira"
        or snapshot["scope"].get("kind") != "epic"
        or query.get("purpose") != "counterpart-epic"
        or query.get("method") != "jira-epic-counterpart"
        or job.get("state") not in {"pending", "running"}
        or active_job(args.run_id) != job
    ):
        raise ValueError("Поиск SberTrek-эпика разрешён только активному counterpart epic collector-job")
    if args.max_results != SBER_EXPORT_MAX_RESULTS:
        raise ValueError(f"Поиск SberTrek-эпика должен использовать --max-results {SBER_EXPORT_MAX_RESULTS}")
    call = evidence(args.evidence, "sbertrek")
    if logged_mcp_details(args.run_id, call):
        raise ValueError("Этот MCP-вызов уже записан в журнале")
    _, payload, response_size, response_digest = response_json(args.response_file)
    records, records_path = full_issue_records(payload)
    if len(records) > 1:
        raise ValueError("Одному Jira-эпику соответствует несколько SberTrek-эпиков")
    cards = [compact_issue_from_response(record, "sbertrek", snapshot["scope"]) for record in records]
    jira_epic_key = snapshot["scope"]["ids"][0]
    sbertrek_epic_key = None
    if cards:
        card = cards[0]
        if str(card["issue_type"]).casefold() != "epic":
            raise ValueError("Объект SberTrek, найденный по Jira-эпику, не является эпиком")
        if card.get("jira_key_state") != "value" or card.get("jira_key") != jira_epic_key:
            raise ValueError("Найденный SberTrek-эпик не подтверждает Объект Jira исходного эпика")
        sbertrek_epic_key = card["key"]
    discovery_query = query["exact"]
    details = (
        f"operation=counterpart-epic; outcome=success; query_sha256={query_digest(discovery_query)}; "
        f"returned={len(cards)}; max_results={args.max_results}; response_sha256={response_digest}; "
        f"response_bytes={response_size}; parser_path={records_path}; query={discovery_query}; "
        "summary=SberTrek counterpart epic structurally imported"
    )
    append_session_log(
        args.run_id, source="mcp", event="call", provider="sbertrek",
        evidence_value=call, details=details,
    )
    query["discovery"] = {
        "method": "issue_key-to-sbertrek-epic",
        "query": discovery_query,
        "evidence": call,
        "response_sha256": response_digest,
        "response_bytes": response_size,
        "records_path": records_path,
        "returned_count": len(cards),
        "jira_epic_key": jira_epic_key,
        "sbertrek_epic_key": sbertrek_epic_key,
    }
    if sbertrek_epic_key:
        exact = tql_epic(sbertrek_epic_key)
        query.update({
            "purpose": "epic-members",
            "language": "TQL",
            "exact": exact,
            "initial_exact": exact,
            "method": "linkedUnitsOf",
            "requested_keys": [],
            "state": "pending",
        })
        job["query"].update({
            "language": "TQL", "purpose": "epic-members", "method": "linkedUnitsOf",
            "text": exact, "sha256": query_digest(exact),
        })
        status = "sbertrek-counterpart-epic-members-query-ready"
    else:
        query.update({
            "purpose": "epic-members",
            "language": "TQL",
            "exact": None,
            "initial_exact": None,
            "method": "linkedUnitsOf",
            "requested_keys": [],
            "state": "complete",
        })
        status = "sbertrek-counterpart-epic-not-found"
    job["state"] = "running"
    save_json(path, snapshot)
    save_json(job_path_value, job)
    args.record_sha256 = object_sha256(query["discovery"])
    payload_out = {
        "status": status,
        "run_id": args.run_id,
        "jira_epic": jira_epic_key,
        "sbertrek_epic": sbertrek_epic_key,
        "allowed_next_action": "execute-returned-tql" if sbertrek_epic_key else "collector-complete",
    }
    if sbertrek_epic_key:
        payload_out.update(next_query_payload("sbertrek", query))
    print(json.dumps(payload_out, ensure_ascii=False, indent=2))
    return 0


def ingest_query_response_command(args: argparse.Namespace) -> int:
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != args.provider:
        raise ValueError("Сейчас нет разрешённого поискового запроса для этого провайдера")
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    job_path_value, job = load_job(args.run_id, f"collection-{args.provider}")
    if job.get("state") not in {"pending", "running"} or active_job(args.run_id) != job:
        raise ValueError("JSON-ответ разрешено импортировать только активному collector-job")
    query = snapshot["query"]
    exact_query = job["query"]["text"]
    if query["exact"] != exact_query:
        raise ValueError("Точный запрос снимка не совпадает с активным collector-job")
    tool_contract = job["response_contract"]["mcp_tool_contract"]
    expected_max_results = tool_contract["max_results"]
    if args.max_results != expected_max_results:
        raise ValueError(f"Bulk-запрос {args.provider} должен использовать --max-results {expected_max_results}")
    expected_page = len(query["pages"]) + 1
    if args.page_number != expected_page:
        raise ValueError(f"Ожидалась страница {expected_page}")
    if args.last_page and args.next_cursor:
        raise ValueError("Последняя страница не может иметь --next-cursor")
    if not args.last_page and not args.next_cursor:
        raise ValueError("Непоследняя страница требует --next-cursor")
    if query["pages"] and args.cursor != query["pages"][-1]["next_cursor"]:
        raise ValueError("--cursor должен совпадать с next_cursor предыдущей страницы")
    if not query["pages"] and args.cursor:
        raise ValueError("Первая страница не может иметь --cursor")
    call = evidence(args.evidence, args.provider)
    if logged_mcp_details(args.run_id, call):
        raise ValueError("Этот MCP-вызов уже записан в журнале")
    if logged_query_calls(args.run_id, args.provider, exact_query, args.page_number):
        raise ValueError("Для одной страницы точного bulk-запроса разрешён ровно один MCP-вызов")
    _, payload, response_size, response_digest = response_json(args.response_file)
    records, records_path = full_issue_records(payload)
    page_metadata = jira_page_metadata(payload) if args.provider == "jira" else None
    if page_metadata:
        if page_metadata["max_results"] != args.max_results:
            raise ValueError("Jira max_results в ответе не совпадает с запрошенным limit")
        expected_last = page_metadata["start_at"] + len(records) >= page_metadata["total"]
        if args.last_page != expected_last:
            raise ValueError("Признак последней Jira-страницы не совпадает с total/start_at ответа")
        expected_cursor = None if expected_last else str(page_metadata["start_at"] + len(records))
        if args.next_cursor != expected_cursor:
            raise ValueError("Jira next-cursor должен совпадать со следующим start_at из ответа")
    cards = [compact_issue_from_response(record, args.provider, snapshot["scope"]) for record in records]
    discovered_sbertrek_epic = (
        query.get("discovery", {}).get("sbertrek_epic_key")
        if args.provider == "sbertrek" and isinstance(query.get("discovery"), dict)
        else None
    )
    if discovered_sbertrek_epic:
        for card in cards:
            card["epic"] = {"key": discovered_sbertrek_epic, "name": discovered_sbertrek_epic}
            card["field_observations"]["epic"] = "value"
    keys = [item["key"] for item in cards]
    existing_keys = set(query["keys"])
    repeated = sorted(existing_keys & set(keys))
    if repeated:
        raise ValueError("JSON-ответ повторяет ключи предыдущей страницы: " + ", ".join(repeated))
    details = (
        f"operation=query; outcome=success; query_sha256={query_digest(exact_query)}; "
        f"page={args.page_number}; returned={len(keys)}; max_results={args.max_results}; response_sha256={response_digest}; "
        f"response_bytes={response_size}; parser_path={records_path}; query={exact_query}; "
        "summary=full JSON structurally imported"
    )
    append_session_log(
        args.run_id, source="mcp", event="call", provider=args.provider,
        evidence_value=call, details=details,
    )
    for item in cards:
        item["evidence"] = call
        validate_issue_card(item, args.provider)
    compact_digest = cards_sha256(cards)
    query["pages"].append({
        "number": args.page_number,
        "cursor": args.cursor,
        "next_cursor": args.next_cursor,
        "last_page": args.last_page,
        "evidence": call,
        "keys": keys,
        "recording_method": "structural-json-import",
        "response_sha256": response_digest,
        "response_bytes": response_size,
        "returned_count": len(keys),
        "requested_max_results": args.max_results,
        "records_path": records_path,
        "page_metadata": page_metadata,
        "cards_sha256": compact_digest,
    })
    query["keys"] = sorted(existing_keys | set(keys))
    query["state"] = "complete" if args.last_page else "collecting"
    snapshot["issues"].extend(cards)
    save_json(path, snapshot)
    job["state"] = "running"
    save_json(job_path_value, job)
    print(json.dumps({
        "status": "tracker-query-response-imported",
        "provider": args.provider,
        "page": args.page_number,
        "query_state": query["state"],
        "returned_count": len(keys),
        "response_sha256": response_digest,
    }, ensure_ascii=False, indent=2))
    return 0


def mcp_log_command(args: argparse.Namespace) -> int:
    call = evidence(args.evidence, args.provider)
    if logged_mcp_details(args.run_id, call):
        raise ValueError("Этот MCP-вызов уже записан в журнале")
    if not args.summary.strip() or len(args.summary) > 8000:
        raise ValueError("--summary должен содержать от 1 до 8000 символов")
    if args.operation in {"capability-discovery", "issue-detail"}:
        raise ValueError(f"{PROTOCOL} запрещает exploratory и поштучные MCP-вызовы")
    config = load_config()
    if args.operation == "query":
        if args.outcome == "success":
            raise ValueError("Успешный bulk-ответ регистрируется только через ingest-query-response")
        current = current_query(args.run_id, config)
        if not current or current[0] != args.provider:
            raise ValueError("Сейчас нет разрешённого поискового запроса для этого провайдера")
        job = active_job(args.run_id)
        if not job or job.get("kind") != "provider-collection" or job.get("provider") != args.provider:
            raise ValueError("Поисковый MCP-вызов разрешён только активному collector-job")
        if args.query != current[1]["exact"]:
            raise ValueError(f"Разрешён только точный {current[1]['language']} активного collector-job")
        if args.page_number is None or args.page_number < 1 or args.returned_count is None or args.returned_count < 0:
            raise ValueError("operation=query требует --page-number и --returned-count")
        if logged_query_calls(args.run_id, args.provider, args.query, args.page_number):
            raise ValueError("Для одной страницы точного bulk-запроса разрешён ровно один MCP-вызов")
    elif args.query is not None or args.page_number is not None or args.returned_count is not None:
        raise ValueError("--query, --page-number и --returned-count допустимы только для operation=query")
    if args.operation == "history" and not args.key:
        raise ValueError("operation=history требует хотя бы один --key")
    if args.operation != "history" and args.key:
        raise ValueError("--key допустим только для operation=history")
    if args.operation == "history":
        job = active_job(args.run_id)
        if not job or job.get("kind") != "provider-history" or job.get("provider") != args.provider:
            raise ValueError("Историю разрешено читать только для активного history-job")
        keys = unique_keys(args.key)
        expected = canonical_history_calls(job)
        expected_call = next((item for item in expected if item["evidence"] == call), None)
        if expected_call is None or expected_call["keys"] != keys:
            raise ValueError("History MCP-вызов должен использовать канонический evidence и точный набор ключей job")
        if any(item["evidence"] == call or set(item["keys"]) & set(keys) for item in job["calls"]):
            raise ValueError("Для каждого канонического набора ключей разрешён ровно один history MCP-вызов")
        job["calls"].append({**expected_call, "outcome": args.outcome})
        save_json(job_path(args.run_id, job["job_id"]), job)
    parts = [f"operation={args.operation}", f"outcome={args.outcome}"]
    if args.query is not None:
        parts.append(f"query_sha256={query_digest(args.query)}")
    if args.page_number is not None:
        parts.append(f"page={args.page_number}")
    if args.key:
        normalized_keys = unique_keys(args.key)
        parts.append(f"keys_sha256={keys_sha256(normalized_keys)}")
        parts.append(f"keys={','.join(normalized_keys)}")
    if args.returned_count is not None:
        parts.append(f"returned={args.returned_count}")
    if args.query is not None:
        parts.append(f"query={args.query}")
    parts.append(f"summary={args.summary}")
    append_session_log(args.run_id, source="mcp", event="call", provider=args.provider, evidence_value=call, details="; ".join(parts))
    print(json.dumps({"status": "tracker-mcp-call-logged", "run_id": args.run_id, "provider": args.provider, "operation": args.operation, "outcome": args.outcome, "evidence": call}, ensure_ascii=False, indent=2))
    return 0


def jira_record_absent_counterparts_command(args: argparse.Namespace) -> int:
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != "jira":
        raise ValueError("Сейчас нет активного Jira counterpart-запроса")
    path, snapshot = load_snapshot(args.run_id, "jira")
    ensure_mutable(snapshot)
    query = snapshot["query"]
    job_path_value, job = load_job(args.run_id, "collection-jira")
    if (
        query.get("purpose") != "counterparts"
        or snapshot["scope"]["provider"] != "sbertrek"
        or job.get("state") not in {"pending", "running"}
        or active_job(args.run_id) != job
    ):
        raise ValueError("Отсутствующие Jira-контрагенты регистрируются только активным counterpart-job")
    if query["pages"] or snapshot["issues"]:
        raise ValueError("Подтверждение отсутствия допустимо до первой успешной страницы Jira")
    call = evidence(args.evidence, "jira")
    details = require_logged_mcp(args.run_id, call, outcome="error")
    if f"query_sha256={query_digest(query['exact'])};" not in details:
        raise ValueError("Ошибка Jira относится не к текущему точному counterpart-запросу")
    keys = unique_keys(args.key)
    if not keys:
        raise ValueError("Укажи хотя бы один подтверждённо отсутствующий Jira-ключ")
    already_absent = set(confirmed_absent_map(snapshot))
    requested = set(query.get("requested_keys") or [])
    invalid = sorted(set(keys) - requested | set(keys) & already_absent)
    if invalid:
        raise ValueError("Jira-ключи отсутствия не входят в текущий исходный counterpart-запрос: " + ", ".join(invalid))
    reported = set(re.findall(
        r"An issue with key ['\"]([A-Z][A-Z0-9_]*-[1-9][0-9]*)['\"] does not exist for field ['\"]key['\"]",
        details,
        flags=re.I,
    ))
    if set(keys) != reported:
        raise ValueError("Список отсутствующих ключей должен точно совпадать с ошибкой Jira")
    batch_digest = object_sha256({"evidence": call, "keys": keys})
    query["confirmed_absent"].extend({
        "key": key_value,
        "evidence": call,
        "batch_sha256": batch_digest,
    } for key_value in keys)
    remaining = sorted(requested - already_absent - set(keys))
    if remaining:
        retry_query = jql_keys(remaining)
        query.update({"exact": retry_query, "state": "pending"})
        job["query"].update({"text": retry_query, "sha256": query_digest(retry_query)})
        status = "jira-counterpart-retry-ready"
    else:
        query.update({"exact": None, "state": "complete"})
        status = "jira-counterpart-all-absent"
    job["state"] = "running"
    save_json(path, snapshot)
    save_json(job_path_value, job)
    args.record_sha256 = batch_digest
    payload = {
        "status": status,
        "run_id": args.run_id,
        "confirmed_absent": keys,
        "remaining_key_count": len(remaining),
    }
    if remaining:
        payload.update(next_query_payload("jira", query))
    else:
        payload["allowed_next_action"] = "collector-complete"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def query_page_command(args: argparse.Namespace) -> int:
    if args.provider in PROVIDERS:
        raise ValueError(f"{args.provider}-страница регистрируется только через ingest-query-response")
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != args.provider:
        raise ValueError("Эта страница не соответствует текущему разрешённому запросу")
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    job_path_value, job = load_job(args.run_id, f"collection-{args.provider}")
    if job.get("state") not in {"pending", "running"} or active_job(args.run_id) != job:
        raise ValueError("Страница разрешена только активному collector-job")
    query = snapshot["query"]
    if args.query != query["exact"]:
        raise ValueError(f"Разрешён только точный {query['language']} активного collector-job")
    expected_page = len(query["pages"]) + 1
    if args.page_number != expected_page:
        raise ValueError(f"Ожидалась страница {expected_page}")
    if args.last_page and args.next_cursor:
        raise ValueError("Последняя страница не может иметь --next-cursor")
    if not args.last_page and not args.next_cursor:
        raise ValueError("Непоследняя страница требует --next-cursor")
    if query["pages"] and args.cursor != query["pages"][-1]["next_cursor"]:
        raise ValueError("--cursor должен совпадать с next_cursor предыдущей страницы")
    if not query["pages"] and args.cursor:
        raise ValueError("Первая страница не может иметь --cursor")
    call = evidence(args.evidence, args.provider)
    details = require_logged_mcp(args.run_id, call, outcome="success")
    if any(page["evidence"] == call for page in query["pages"]):
        raise ValueError("Один MCP-вызов нельзя записать как две страницы")
    page_keys = unique_keys(args.key)
    expected_details = (
        f"operation=query; outcome=success; query_sha256={query_digest(args.query)}; "
        f"page={args.page_number}; returned={len(page_keys)};"
    )
    if expected_details not in details:
        raise ValueError("Evidence страницы не совпадает с запросом, номером страницы или числом возвращённых ключей")
    page_record = {
        "number": args.page_number, "cursor": args.cursor, "next_cursor": args.next_cursor,
        "last_page": args.last_page, "evidence": call, "keys": page_keys,
        "recording_method": "bounded-inline-recording",
    }
    query["pages"].append(page_record)
    query["keys"] = sorted(set(query["keys"]) | set(page_keys))
    query["state"] = "complete" if args.last_page else "collecting"
    save_json(path, snapshot)
    job["state"] = "running"
    save_json(job_path_value, job)
    args.record_sha256 = object_sha256(page_record)
    print(json.dumps({"status": "tracker-query-page-recorded", "provider": args.provider, "page": args.page_number, "query_state": query["state"], "key_count": len(query["keys"])}, ensure_ascii=False, indent=2))
    return 0


def query_unavailable_command(args: argparse.Namespace) -> int:
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != args.provider:
        raise ValueError("Сейчас нет разрешённого запроса для этого провайдера")
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    if snapshot["query"]["pages"]:
        raise ValueError("Нельзя объявить недоступным уже начатый запрос")
    if args.provider == snapshot["scope"]["provider"]:
        raise ValueError("Недоступность исходного трекера блокирует запуск")
    call = evidence(args.evidence, args.provider)
    require_logged_mcp(args.run_id, call, outcome="error")
    snapshot["query"].update({"state": "unavailable", "unavailable_reason": args.reason, "unavailable_evidence": call})
    save_json(path, snapshot)
    job_file, job = load_job(args.run_id, f"collection-{args.provider}")
    job["state"] = "running"
    save_json(job_file, job)
    print(json.dumps({"status": "tracker-query-unavailable-recorded", "provider": args.provider}, ensure_ascii=False, indent=2))
    return 0


def record_issue_command(args: argparse.Namespace) -> int:
    if args.provider == "sbertrek":
        raise ValueError("SberTrek-карточки создаются только структурным импортом полного JSON")
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    if snapshot["collection_complete"]:
        raise ValueError("Завершённую коллекцию нельзя дополнять")
    job = active_job(args.run_id)
    if not job or job.get("kind") != "provider-collection" or job.get("provider") != args.provider:
        raise ValueError("Карточки разрешено записывать только активному collector-job")
    key_value = issue_key(args.key)
    summary = args.summary.strip()
    issue_type = args.issue_type.strip()
    status = args.status.strip()
    essential = {"summary": summary, "issue_type": issue_type, "status": status}
    invalid = [name for name, value in essential.items() if not value or value.casefold() in MISSING_SENTINELS]
    if summary.casefold() == key_value.casefold():
        invalid.append("summary")
    if not args.created_at or not args.updated_at:
        invalid.extend(name for name, value in (("created_at", args.created_at), ("updated_at", args.updated_at)) if not value)
    if invalid:
        raise ValueError("Карточка не содержит обязательные данные bulk-ответа: " + ", ".join(sorted(set(invalid))))
    if key_value not in snapshot["query"]["keys"]:
        raise ValueError("Карточка разрешена только для ключа текущего точного запроса")
    if issue_by_key(snapshot, key_value):
        raise ValueError(f"Задача {key_value} уже записана")
    call = evidence(args.evidence, args.provider)
    require_logged_mcp(args.run_id, call, outcome="success")
    page_evidence = {page["evidence"] for page in snapshot["query"]["pages"] if key_value in page["keys"]}
    if call not in page_evidence:
        raise ValueError("Evidence карточки должно быть страницей точного bulk-запроса")
    values = {
        "assignee": participant(args.assignee_id, args.assignee_name),
        "estimate": {"value": args.estimate, "unit": args.estimate_unit} if args.estimate is not None else None,
        "epic": {"key": issue_key(args.epic_key), "name": args.epic_name} if args.epic_key else None,
        "releases": [parse_release(item) for item in args.release],
    }
    role_estimates: dict[str, dict] = {}
    role_estimate_observations = {role: "absent" for role in ESTIMATE_ROLES}
    inferred_role = role_from_summary_prefix(summary) if values["estimate"] else None
    if inferred_role in {"AN", "BE", "FE"}:
        role_estimates[inferred_role] = {
            **values["estimate"],
            "source_field": {"id": "estimate", "name": "Общая оценка"},
            "inferred_from_general": True,
        }
        role_estimate_observations[inferred_role] = "value"
    observations = {"assignee": args.assignee_state, "estimate": args.estimate_state, "epic": args.epic_state, "releases": args.releases_state}
    for field, state in observations.items():
        if (state == "value") != (values[field] not in (None, [], {})):
            raise ValueError(f"{field}: состояние value должно точно соответствовать переданному значению")
    jira_key_state = args.jira_key_state or ("absent" if args.provider == "jira" else None)
    if jira_key_state is None:
        raise ValueError("Карточка SberTrek требует явный --jira-key-state")
    jira_key = issue_key(args.jira_key, "Объект Jira") if args.jira_key else None
    if (jira_key_state == "value") != (jira_key is not None):
        raise ValueError("jira_key_state=value должен точно соответствовать переданному --jira-key")
    if args.provider != "sbertrek" and (jira_key or jira_key_state != "absent"):
        raise ValueError("Объект Jira и его состояние записываются только для карточки SberTrek")
    item = {
        "key": key_value, "jira_key": jira_key, "jira_key_state": jira_key_state, "evidence": call,
        "summary": summary, "issue_type": issue_type, "status": status,
        **values, "role_estimates": role_estimates,
        "role_estimate_observations": role_estimate_observations,
        "estimate_fields": [], "field_observations": observations,
        "created_at": args.created_at, "updated_at": args.updated_at,
        "history": {"state": "pending", "evidence": [], "events": [], "reason": None},
    }
    snapshot["issues"].append(item)
    save_json(path, snapshot)
    args.record_sha256 = cards_sha256([item])
    print(json.dumps({"status": "tracker-issue-recorded", "provider": args.provider, "key": key_value, "jira_key": jira_key}, ensure_ascii=False, indent=2))
    return 0


def missing_cards(snapshot: dict) -> list[str]:
    return sorted(set(snapshot["query"]["keys"]) - {item["key"] for item in snapshot["issues"]})


def create_history_jobs(run_id: str, snapshots: dict[str, dict]) -> list[dict]:
    created = []
    excluded_sber = excluded_sbertrek_keys(snapshots)
    for provider in PROVIDERS:
        snapshot = snapshots.get(provider)
        if not snapshot:
            continue
        keys = sorted(
            item["key"] for item in snapshot["issues"]
            if provider != "sbertrek" or item["key"] not in excluded_sber
        )
        for offset in range(0, len(keys), HISTORY_BATCH_SIZE):
            job = history_job(run_id, provider, offset // HISTORY_BATCH_SIZE + 1, keys[offset:offset + HISTORY_BATCH_SIZE])
            save_job(run_id, job)
            created.append(job)
    return created


def advance_after_collection(run_id: str) -> dict:
    config = load_config()
    snapshots = all_snapshots(run_id, config)
    scope = next(iter(snapshots.values()))["scope"]
    primary_provider = scope["provider"]
    primary = snapshots[primary_provider]
    primary_job_file = job_path(run_id, f"collection-{primary_provider}")
    if not primary_job_file.is_file() or load_job(run_id, f"collection-{primary_provider}")[1].get("state") != "complete":
        raise ValueError(f"Исходный collector-job {primary_provider} не завершён")
    missing = missing_cards(primary)
    if missing:
        raise ValueError("Не записаны карточки исходного запроса: " + ", ".join(missing))
    secondary_provider = "jira" if primary_provider == "sbertrek" else "sbertrek"
    secondary = snapshots.get(secondary_provider)
    if (
        secondary
        and secondary["query"].get("initial_exact") is None
        and secondary["query"].get("discovery") is None
    ):
        if primary_provider == "sbertrek":
            ids = sorted({item["jira_key"] for item in primary["issues"] if item.get("jira_key")})
            exact = jql_keys(ids) if ids else None
            purpose = "counterparts"
            method = None
        elif scope["kind"] == "epic":
            ids = [scope["ids"][0]]
            exact = tql_jira_keys(ids)
            purpose = "counterpart-epic"
            method = "jira-epic-counterpart"
        else:
            ids = list(primary["query"]["keys"])
            exact = tql_jira_keys(ids) if ids else None
            purpose = "counterparts"
            method = None
        if exact:
            secondary["query"].update({
                "purpose": purpose,
                "exact": exact,
                "initial_exact": exact,
                "method": method,
                "requested_keys": ids,
                "confirmed_absent": [],
                "state": "pending",
            })
        else:
            secondary["query"].update({"state": "skipped"})
        save_json(snapshot_path(run_id, secondary_provider), secondary)
        snapshots[secondary_provider] = secondary
        if exact:
            save_job(run_id, collection_job(run_id, secondary_provider, secondary["query"]))
            return write_status(
                run_id,
                "tracker-read-awaiting-collector",
                gaps=[f"{secondary_provider}.collection-job.pending"],
                extra=next_job_payload(run_id),
            )
    if secondary and secondary["query"]["state"] not in {"complete", "skipped", "unavailable"}:
        raise ValueError(f"Counterpart-запрос {secondary_provider} не завершён")
    if secondary and secondary["query"]["state"] in {"complete", "unavailable"}:
        secondary_job_file = job_path(run_id, f"collection-{secondary_provider}")
        if not secondary_job_file.is_file() or load_job(run_id, f"collection-{secondary_provider}")[1].get("state") != "complete":
            raise ValueError(f"Counterpart collector-job {secondary_provider} не завершён")
    if secondary:
        missing = missing_cards(secondary)
        if missing:
            raise ValueError("Не записаны карточки counterpart-запроса: " + ", ".join(missing))
        if secondary_provider == "sbertrek":
            allowed_jira = set(primary["query"]["keys"])
            if scope["kind"] == "tasks":
                invalid = [f"{item['key']}={item['jira_key']}" for item in secondary["issues"] if item.get("jira_key") not in allowed_jira]
                if invalid:
                    raise ValueError("SberTrek Объект Jira вне исходной Jira-области: " + ", ".join(invalid))
    for provider, snapshot in snapshots.items():
        snapshot["collection_complete"] = True
        save_json(snapshot_path(run_id, provider), snapshot)
    history_jobs = create_history_jobs(run_id, snapshots)
    if history_jobs:
        return write_status(
            run_id,
            "tracker-read-awaiting-history-collector",
            gaps=[f"{job['job_id']}.pending" for job in history_jobs],
            extra=next_job_payload(run_id),
        )
    return write_status(run_id, "tracker-read-ready-to-reconcile", allowed="reconcile")


def collector_complete_command(args: argparse.Namespace) -> int:
    job_id = f"collection-{args.provider}"
    path, job = load_job(args.run_id, job_id)
    if job.get("state") not in {"pending", "running"} or active_job(args.run_id) != job:
        raise ValueError("Завершить можно только активный collector-job")
    _, snapshot = load_snapshot(args.run_id, args.provider)
    if snapshot["query"]["state"] not in {"complete", "unavailable"}:
        raise ValueError("Точный запрос collector-job не завершён")
    validate_collection_integrity(args.run_id, snapshot, args.provider)
    validate_counterpart_contract(args.run_id, all_snapshots(args.run_id, load_config()))
    missing = missing_cards(snapshot)
    if missing:
        raise ValueError("Не записаны компактные карточки: " + ", ".join(missing))
    if args.provider == "sbertrek":
        unknown_jira_keys = [item["key"] for item in snapshot["issues"] if item.get("jira_key_state") == "not-returned"]
        if unknown_jira_keys:
            raise ValueError(
                "Поле Объект Jira не было прочитано; absent допустим, not-returned блокирует сверку: "
                + ", ".join(unknown_jira_keys)
            )
        unknown_fields = [
            f"{item['key']}.{field}"
            for item in snapshot["issues"]
            for field, state in item["field_observations"].items()
            if state == "not-returned"
        ]
        if unknown_fields:
            raise ValueError(
                "Полный ответ SberTrek должен различать value и absent; not-returned блокирует сверку: "
                + ", ".join(unknown_fields)
            )
    unexpected = unexpected_run_artifacts(args.run_id)
    if unexpected:
        raise ValueError("Tracker-run содержит незарегистрированные вспомогательные файлы: " + ", ".join(unexpected))
    job["state"] = "complete"
    job["completed_at"] = now()
    save_json(path, job)
    advance_after_collection(args.run_id)
    print(json.dumps({
        "protocol": PROTOCOL,
        "run_id": args.run_id,
        "status": "collector-job-complete",
        "completed_job": job_id,
        "collector_must_return": True,
        "paths": {
            "run_status": str(status_path(args.run_id)),
            "jobs": str(jobs_root(args.run_id)),
            "providers": str(run_root(args.run_id) / "providers"),
        },
    }, ensure_ascii=False, indent=2))
    return 0


def history_event_command(args: argparse.Namespace) -> int:
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    if not snapshot["collection_complete"]:
        raise ValueError("История разрешена только после завершения collection-jobs")
    job = active_job(args.run_id)
    if (
        not job
        or job.get("kind") != "provider-history"
        or job.get("provider") != args.provider
        or issue_key(args.key) not in job.get("keys", [])
    ):
        raise ValueError("Событие разрешено только для ключа активного history-job")
    item = issue_by_key(snapshot, issue_key(args.key))
    if not item:
        raise ValueError("Задача отсутствует в целевой коллекции")
    call = evidence(args.evidence, args.provider)
    history_call_for_key(job, item["key"], call, "success")
    require_logged_mcp(args.run_id, call, outcome="success")
    event = {
        "at": args.at,
        "field": args.field,
        "from": participant(args.from_id, args.from_name) if args.field == "assignee" else args.from_value,
        "to": participant(args.to_id, args.to_name) if args.field == "assignee" else args.to_value,
        "evidence": call,
    }
    item["history"]["events"].append(event)
    save_json(path, snapshot)
    args.record_sha256 = object_sha256(event)
    print(json.dumps({"status": "history-event-recorded", "provider": args.provider, "key": args.key}, ensure_ascii=False, indent=2))
    return 0


def history_complete_command(args: argparse.Namespace) -> int:
    path, snapshot = load_snapshot(args.run_id, args.provider)
    ensure_mutable(snapshot)
    if not snapshot["collection_complete"]:
        raise ValueError("История разрешена только после завершения collection-jobs")
    job = active_job(args.run_id)
    if (
        not job
        or job.get("kind") != "provider-history"
        or job.get("provider") != args.provider
        or issue_key(args.key) not in job.get("keys", [])
    ):
        raise ValueError("Историю разрешено завершать только в активном history-job")
    item = issue_by_key(snapshot, issue_key(args.key))
    if not item:
        raise ValueError("Задача отсутствует в целевой коллекции")
    if args.state == "unavailable" and not args.reason:
        raise ValueError("Недоступная история требует --reason")
    call = evidence(args.evidence, args.provider)
    outcome = "success" if args.state == "complete" else "error"
    history_call_for_key(job, item["key"], call, outcome)
    require_logged_mcp(args.run_id, call, outcome=outcome)
    item["history"].update({"state": args.state, "evidence": [call], "reason": args.reason})
    save_json(path, snapshot)
    args.record_sha256 = object_sha256({name: item["history"][name] for name in ("state", "evidence", "reason")})
    print(json.dumps({"status": "history-complete", "provider": args.provider, "key": args.key, "history_state": args.state}, ensure_ascii=False, indent=2))
    return 0


def history_job_complete_command(args: argparse.Namespace) -> int:
    path, job = load_job(args.run_id, args.job_id)
    if job.get("kind") != "provider-history" or job.get("state") not in {"pending", "running"}:
        raise ValueError("Завершить можно только активный history-job")
    if active_job(args.run_id) != job:
        raise ValueError("Завершить можно только текущий history-job")
    validate_history_job_calls(args.run_id, job, require_complete=True)
    _, snapshot = load_snapshot(args.run_id, job["provider"])
    pending = []
    for key in job["keys"]:
        item = issue_by_key(snapshot, key)
        if not item or item.get("history", {}).get("state") == "pending":
            pending.append(key)
    if pending:
        raise ValueError("Не завершена история ключей: " + ", ".join(pending))
    job["state"] = "complete"
    job["completed_at"] = now()
    save_json(path, job)
    next_payload = next_job_payload(args.run_id)
    if next_payload:
        payload = write_status(
            args.run_id,
            "tracker-read-awaiting-history-collector",
            gaps=[f"{item['job_id']}.pending" for item in all_jobs(args.run_id) if item.get("state") in {"pending", "running"}],
            extra=next_payload,
        )
    else:
        payload = write_status(args.run_id, "tracker-read-ready-to-reconcile", allowed="reconcile")
    print(json.dumps({
        "protocol": PROTOCOL,
        "run_id": args.run_id,
        "status": "history-job-complete",
        "completed_job": args.job_id,
        "collector_must_return": True,
        "paths": {
            "run_status": str(status_path(args.run_id)),
            "jobs": str(jobs_root(args.run_id)),
            "providers": str(run_root(args.run_id) / "providers"),
        },
    }, ensure_ascii=False, indent=2))
    return 0


def run_status_command(args: argparse.Namespace) -> int:
    completion = run_root(args.run_id) / "completion-status.json"
    if completion.is_file():
        payload = load_json(completion)
        if payload.get("protocol") != PROTOCOL:
            raise ValueError("Completion-status создан старым протоколом")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    extra = next_job_payload(args.run_id)
    if extra:
        job_id = extra["next_job"]["job_id"]
        payload = write_status(
            args.run_id,
            "tracker-read-awaiting-collector",
            gaps=[f"{job_id}.pending"],
            allowed="delegate-collector-job",
            extra=extra,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    snapshots = all_snapshots(args.run_id, load_config())
    excluded_sber = excluded_sbertrek_keys(snapshots)
    gaps = sum((
        snapshot_gaps(snapshot, excluded_sber if snapshot["provider"] == "sbertrek" else set())
        for snapshot in snapshots.values()
    ), [])
    payload = write_status(
        args.run_id,
        "tracker-read-ready-to-reconcile" if not gaps else "tracker-read-incomplete",
        gaps=sorted(set(gaps)),
        allowed="reconcile" if not gaps else "fix-reported-gap",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if gaps else 0


def collector_brief_command(args: argparse.Namespace) -> int:
    job = active_job(args.run_id)
    if not job:
        raise ValueError("У tracker-run нет ожидающего collector-job")
    path = job_path(args.run_id, job["job_id"])
    contract = job["collector_contract"]
    run_guard = (
        f"Работай только в существующем run_id={args.run_id} и только с job_id={job['job_id']}. "
        "Не запускай begin, не создавай другой tracker-run и не меняй run_id или job path. "
        "Не редактируй scope.json, provider JSON, job JSON, run-status или tracker-session-log напрямую; "
        "записывай результат только командами trackerctl из контракта. "
    )
    if job["kind"] == "provider-collection":
        language = job["query"]["language"]
        query = job["query"]["text"]
        if job["provider"] == "sbertrek":
            export_contract = (
                "Выбери только MCP-операцию issue.exportJson либо эквивалентную операцию bulk JSON export, "
                "которая принимает точный TQL в параметре query и возвращает полный JSON как файл. Имя MCP-сервера "
                f"может отличаться. Для каждого export передай max_results={SBER_EXPORT_MAX_RESULTS}; другое значение "
                "запрещено. Не используй issue.search, параметр text, issue.getByKey или link.list. Запроси только "
                "поля из response_contract.preferred_fields, если MCP-инструмент поддерживает проекцию; поле "
                "attributes обязательно. Не передавай fields=null и не читай отображённый preview. "
            )
            if job["query"].get("method") == "jira-epic-counterpart":
                prompt = (
                    run_guard +
                    f"Выполни в sbertrek ровно этот {language}-запрос без изменений:\n\n{query}\n\n" +
                    export_contract +
                    "Этот первый bulk-запрос ищет только SberTrek-эпик, связанный с исходным Jira-эпиком. Передай "
                    "полный исходный JSON-файл команде sbertrek-ingest-counterpart-epic с "
                    f"--max-results {SBER_EXPORT_MAX_RESULTS}. Только эта команда проверит единственность и тип эпика "
                    "и, если он найден, вернёт точный TQL linkedUnitsOf по его SberTrek-ключу. Выполни возвращённый "
                    "TQL вторым bulk export без изменений и передай его полный JSON в ingest-query-response "
                    f"--provider sbertrek --max-results {SBER_EXPORT_MAX_RESULTS}. Если эпик не найден и команда "
                    "разрешила collector-complete, не выполняй второй запрос. Не ищи SberTrek-контрпары отдельно "
                    "по ключам дочерних Jira-задач. При ошибке реального MCP-вызова сначала зарегистрируй её через "
                    "mcp-log, затем выполни query-unavailable; не повторяй и не изменяй запрос. "
                    f"Прочитай только {contract} и {path}. После collector-complete немедленно верни только "
                    "status, job_id и пути."
                )
            else:
                prompt = (
                    run_guard +
                    f"Выполни в sbertrek ровно этот {language}-запрос без изменений:\n\n{query}\n\n" +
                    export_contract +
                    "Полученный полный исходный JSON-файл передай в trackerctl.py ingest-query-response с "
                    f"--max-results {SBER_EXPORT_MAX_RESULTS}: команда сама структурно извлечёт все карточки. "
                    "Не выполняй других поисков, detail-вызовов или ручного record-issue. Не заменяй запрос поиском "
                    "по тексту, названию или смыслу. При ошибке counterpart-вызова зарегистрируй её через mcp-log "
                    "и query-unavailable; ошибку исходного SberTrek-запроса зарегистрируй и остановись. "
                    f"Прочитай только {contract} и {path}. После collector-complete немедленно верни только "
                    "status, job_id и пути."
                )
        else:
            fields = ",".join(job["response_contract"]["preferred_fields"])
            if language == "JIRA_API":
                prompt = (
                    run_guard +
                    f"Выполни в Jira ровно этот вызов без изменений:\n\n{query}\n\n"
                    "Это единственный разрешённый поштучный вызов: он читает только поле issuelinks исходного эпика. "
                    "Сохрани полный JSON-ответ без пересказа и передай файл команде jira-ingest-epic-links. Команда "
                    "сама выберет только связи type.name=PartOf с inward_issue и вернёт точный JQL по всем дочерним "
                    "ключам без фильтра по типу или статусу. Если JQL возвращён, выполни один jira_search с ним, "
                    f"fields=\"{fields}\" и limit={JIRA_SEARCH_MAX_RESULTS}; сохрани полный JSON и передай его в "
                    f"ingest-query-response --provider jira --max-results {JIRA_SEARCH_MAX_RESULTS}. Не вызывай "
                    "jira_get_issue для дочерних задач и не фильтруй Done/Resolved/Closed. Если дочерних ключей нет, "
                    "сразу выполни collector-complete. "
                    f"Прочитай только {contract} и {path}. После collector-complete немедленно верни только status, "
                    "job_id и пути."
                )
            else:
                prompt = (
                    run_guard +
                    f"Выполни в Jira ровно этот {language}-запрос без изменений:\n\n{query}\n\n"
                    f"Используй jira_search с fields=\"{fields}\" и limit={JIRA_SEARCH_MAX_RESULTS}. Не добавляй "
                    "фильтр по статусу или типу и не выполняй поштучные jira_get_issue. Сохрани полный JSON-ответ "
                    f"и передай его в ingest-query-response --provider jira --max-results {JIRA_SEARCH_MAX_RESULTS}; "
                    "эта команда сама извлечёт карточки и все ролевые оценки. Для реальной пагинации продолжай тем же "
                    "точным JQL и полученным cursor. Если точный counterpart-запрос завершился только ошибками вида "
                    "`An issue with key 'KEY' does not exist for field 'key'`, запиши один ошибочный вызов через "
                    "mcp-log, передав полный текст ошибки в --summary, затем зарегистрируй все перечисленные ключи "
                    "одной командой jira-record-absent-counterparts. Выполни только новый JQL, который вернёт эта "
                    "команда. При любой другой ошибке остановись. "
                    f"Прочитай только {contract} и {path}. После collector-complete немедленно верни только status, "
                    "job_id и пути."
                )
    else:
        keys = ", ".join(job["keys"])
        calls = canonical_history_calls(job)
        if job["call_mode"] == "batch":
            call = calls[0]
            history_instruction = (
                f"Выполни в {job['provider']} ровно один batch-запрос истории сразу для полного набора ключей: "
                f"{keys}. Зарегистрируй этот единственный вызов через mcp-log с evidence "
                f"{call['evidence']} и передай каждый ключ отдельным --key. Один batch-ответ нельзя "
                "переименовывать или повторно регистрировать как несколько вызовов. "
            )
        else:
            call_list = "; ".join(f"{item['keys'][0]} -> {item['evidence']}" for item in calls)
            exact_history = (
                " Для Jira каждый вызов должен иметь вид jira_get_issue(issue_key=\"KEY\", "
                "fields=\"key,status,assignee\", expand=\"changelog\"); сохрани только события assignee и status."
                if job["provider"] == "jira" else ""
            )
            history_instruction = (
                f"Выполни в {job['provider']} ровно по одному запросу истории на каждый ключ: {keys}. "
                f"Для mcp-log используй только эти соответствия ключа и evidence: {call_list}."
                f"{exact_history} "
            )
        prompt = (
            run_guard + history_instruction +
            f"Для записи результата прочитай только {contract} и {path}. Не придумывай evidence, не ищи "
            "другие задачи, не создавай скрипты или вспомогательные файлы. После history-job-complete "
            "немедленно верни только status, job_id и пути."
        )
    print(json.dumps({
        "protocol": PROTOCOL,
        "run_id": args.run_id,
        "status": "collector-brief-ready",
        "job_id": job["job_id"],
        "job_path": str(path),
        "collector_contract": contract,
        "prompt": prompt,
    }, ensure_ascii=False, indent=2))
    return 0


def set_participant_command(args: argparse.Namespace) -> int:
    pending = pending_participant_path(args.run_id)
    if not pending.is_file():
        raise ValueError("Нет ожидающего вопроса о соответствии участника")
    expected = load_json(pending)
    if (args.provider, args.account_id) != (expected["provider"], expected["account_id"]):
        raise ValueError("Разрешено ответить только на текущий вопрос об участнике")
    config = load_config()
    team_id = normalized_team_id(args.team_id)
    config["participants"][args.provider][args.account_id] = {"team_id": team_id}
    save_json(config_path(), config)
    pending.unlink()
    print(json.dumps({"status": "tracker-participant-saved", "provider": args.provider, "account_id": args.account_id, "team_id": team_id, "role": team_role(team_id)}, ensure_ascii=False, indent=2))
    return 0


def set_development_decision_command(args: argparse.Namespace) -> int:
    pending_path = pending_development_decision_path(args.run_id)
    if not pending_path.is_file():
        raise ValueError("Нет ожидающего вопроса о конфликте ЖЦ")
    pending = load_json(pending_path)
    conflict = pending.get("conflict") if isinstance(pending, dict) else None
    if (
        not isinstance(conflict, dict)
        or pending.get("protocol") != PROTOCOL
        or pending.get("schema_version") != SCHEMA_VERSION
        or pending.get("run_id") != args.run_id
        or pending.get("conflict_sha256") != object_sha256(conflict)
        or pending.get("question") != development_decision_question(conflict)
    ):
        raise ValueError("Повреждён pending-development-decision.json")
    if args.key != conflict.get("sbertrek_key"):
        raise ValueError("Разрешено ответить только на текущий вопрос о конфликте ЖЦ")
    config = load_config()
    snapshots = all_snapshots(args.run_id, config)
    for provider, snapshot in snapshots.items():
        validate_collection_integrity(args.run_id, snapshot, provider)
    validate_run_provenance(args.run_id, snapshots)
    decisions = load_development_decisions(args.run_id)
    conflicts = development_conflicts(snapshots, config)
    applied = validate_development_decisions(conflicts, decisions)
    unresolved = [item for item in conflicts if item["sbertrek_key"] not in applied]
    if not unresolved or unresolved[0] != conflict:
        raise ValueError("Текущий вопрос о конфликте ЖЦ не соответствует данным tracker-run")
    if args.choice == "custom":
        if args.state is None:
            raise ValueError("Пользовательский вариант требует --state")
        if args.apply_to_all:
            raise ValueError("Пользовательский вариант нельзя применять ко всем задачам")
        state = args.state
    else:
        if args.state is not None:
            raise ValueError("При выборе приоритета трекера --state не указывается")
        state = conflict[f"{args.choice}_state"]
    core = {
        "sbertrek_key": conflict["sbertrek_key"],
        "jira_key": conflict["jira_key"],
        "choice": args.choice,
        "state": state,
        "apply_to_all": args.apply_to_all,
        "sbertrek_state": conflict["sbertrek_state"],
        "jira_state": conflict["jira_state"],
    }
    digest = object_sha256(core)
    decisions["decisions"].append({**core, "record_sha256": digest})
    if args.apply_to_all:
        decisions["default_choice"] = args.choice
    save_json(development_decisions_path(args.run_id), decisions)
    pending_path.unlink()
    args.record_sha256 = digest
    print(json.dumps({
        "status": "tracker-development-decision-saved",
        "run_id": args.run_id,
        "sbertrek_key": conflict["sbertrek_key"],
        "jira_key": conflict["jira_key"],
        "choice": args.choice,
        "state": state,
        "apply_to_all": args.apply_to_all,
        "default_scope": "current-run" if args.apply_to_all else None,
        "allowed_next_action": "reconcile",
    }, ensure_ascii=False, indent=2))
    return 0


def abandon_run_command(args: argparse.Namespace) -> int:
    current = active_run_id()
    if current != args.run_id:
        raise ValueError("Отказаться можно только от текущего незавершённого tracker-run")
    if (run_root(args.run_id) / "completion-status.json").is_file():
        raise ValueError("Завершённый tracker-run нельзя пометить abandoned")
    reason = args.reason.strip()
    if not reason:
        raise ValueError("abandon-run требует непустую причину")
    payload = write_status(
        args.run_id, "tracker-read-abandoned", gaps=[reason],
        allowed="begin", complete=False,
    )
    release_active_run(args.run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    config = load_config()
    unexpected = unexpected_run_artifacts(args.run_id)
    if unexpected:
        raise ValueError("Tracker-run содержит незарегистрированные вспомогательные файлы: " + ", ".join(unexpected))
    pending_jobs = [job["job_id"] for job in all_jobs(args.run_id) if job.get("state") in {"pending", "running"}]
    if pending_jobs:
        raise ValueError("Collector-jobs не завершены: " + ", ".join(pending_jobs))
    snapshots = all_snapshots(args.run_id, config)
    for provider, snapshot in snapshots.items():
        validate_collection_integrity(args.run_id, snapshot, provider)
    validate_run_provenance(args.run_id, snapshots)
    excluded_sber = excluded_sbertrek_keys(snapshots)
    gaps = sum((
        snapshot_gaps(snapshot, excluded_sber if snapshot["provider"] == "sbertrek" else set())
        for snapshot in snapshots.values()
    ), [])
    if gaps:
        raise ValueError("Tracker-run не завершён: " + ", ".join(gaps))
    unknown = first_unknown_participant(snapshots, config)
    if unknown:
        question = f"Какой командный team_id соответствует {unknown['provider']} account {unknown['account_id']} ({unknown['name']})?"
        if unknown.get("suggested_team_id"):
            question += f" Другие аккаунты участника уже соответствуют {unknown['suggested_team_id']}; укажи этот team_id, если это тот же человек."
        save_json(pending_participant_path(args.run_id), {**unknown, "question": question})
        payload = stop_payload(question, status="tracker-reconcile-blocked", run_id=args.run_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    decisions = load_development_decisions(args.run_id)
    conflicts = development_conflicts(snapshots, config)
    applied_decisions = validate_development_decisions(conflicts, decisions)
    unresolved = [item for item in conflicts if item["sbertrek_key"] not in applied_decisions]
    if unresolved:
        conflict = unresolved[0]
        question = development_decision_question(conflict)
        pending_payload = {
            "protocol": PROTOCOL,
            "schema_version": SCHEMA_VERSION,
            "run_id": args.run_id,
            "conflict": conflict,
            "conflict_sha256": object_sha256(conflict),
            "question": question,
        }
        save_json(pending_development_decision_path(args.run_id), pending_payload)
        payload = stop_payload(question, status="tracker-reconcile-blocked", run_id=args.run_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    for provider, snapshot in snapshots.items():
        if not snapshot.get("captured_at"):
            snapshot["captured_at"] = now()
            save_json(snapshot_path(args.run_id, provider), snapshot)
    result = reconcile_data(snapshots, config, applied_decisions)
    root = run_root(args.run_id)
    save_json(root / "reconciled.json", result)
    (root / "report.md").write_text(render_report(result), encoding="utf-8")
    completion = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id, "status": "tracker-read-reconciled",
        "workflow_complete": True, "final_response_allowed": True,
        "planning_application_allowed": result["scope"].get("intent") == "update-planning",
        "counts": result["counts"], "summary": result["summary"], "limitations": result["limitations"],
        "paths": {
            "session_log": str(session_log_path(args.run_id)),
            "run_status": str(status_path(args.run_id)),
            "scope": str(run_meta_path(args.run_id)),
            "jobs": str(jobs_root(args.run_id)),
            "providers": str(root / "providers"),
            "reconciled": str(root / "reconciled.json"),
            "report": str(root / "report.md"),
            "completion_status": str(root / "completion-status.json"),
        },
    }
    save_json(root / "completion-status.json", completion)
    save_json(status_path(args.run_id), completion)
    release_active_run(args.run_id)
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
    root = argparse.ArgumentParser(description="Guarded targeted tracker reconciliation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-config"); init.add_argument("--force", action="store_true"); init.set_defaults(handler=init_config_command)
    status = commands.add_parser("config-status"); status.set_defaults(handler=config_status_command)
    projects = commands.add_parser("set-projects"); projects.add_argument("--provider", choices=PROVIDERS, required=True); projects.add_argument("projects", nargs="+"); projects.set_defaults(handler=update_config)
    jira = commands.add_parser("set-jira-mode"); jira.add_argument("mode", choices=("enabled", "disabled")); jira.set_defaults(handler=update_config)
    types = commands.add_parser("set-issue-types"); types.add_argument("issue_types", nargs="+"); types.set_defaults(handler=update_config)
    statuses = commands.add_parser("set-statuses"); statuses.add_argument("--provider", choices=PROVIDERS, required=True); statuses.add_argument("--kind", choices=("completed", "excluded"), required=True); statuses.add_argument("--none", action="store_true"); statuses.add_argument("statuses", nargs="*"); statuses.set_defaults(handler=update_config)
    complete = commands.add_parser("complete-config"); complete.set_defaults(handler=complete_config_command)
    begin = commands.add_parser("begin"); begin.add_argument("--scope-kind", choices=SCOPE_KINDS, required=True); begin.add_argument("--scope-provider", choices=PROVIDERS, required=True); begin.add_argument("--scope-id", action="append", required=True); begin.add_argument("--label", required=True); begin.add_argument("--scope-source", required=True); begin.add_argument("--intent", choices=("read-only", "update-planning"), default="read-only"); begin.set_defaults(handler=begin_command)
    links = commands.add_parser("jira-ingest-epic-links"); links.add_argument("--run-id", required=True); links.add_argument("--evidence", required=True); links.add_argument("--response-file", required=True); links.set_defaults(handler=jira_ingest_epic_links_command, provider="jira")
    sber_epic = commands.add_parser("sbertrek-ingest-counterpart-epic"); sber_epic.add_argument("--run-id", required=True); sber_epic.add_argument("--evidence", required=True); sber_epic.add_argument("--response-file", required=True); sber_epic.add_argument("--max-results", type=int, required=True); sber_epic.set_defaults(handler=sbertrek_ingest_counterpart_epic_command, provider="sbertrek")
    ingest = commands.add_parser("ingest-query-response"); ingest.add_argument("--run-id", required=True); ingest.add_argument("--provider", choices=PROVIDERS, required=True); ingest.add_argument("--page-number", type=int, required=True); ingest.add_argument("--cursor"); ingest.add_argument("--next-cursor"); ingest.add_argument("--last-page", action="store_true"); ingest.add_argument("--evidence", required=True); ingest.add_argument("--response-file", required=True); ingest.add_argument("--max-results", type=int, required=True); ingest.set_defaults(handler=ingest_query_response_command)
    mcp = commands.add_parser("mcp-log"); mcp.add_argument("--run-id", required=True); mcp.add_argument("--provider", choices=PROVIDERS, required=True); mcp.add_argument("--operation", choices=("query", "history"), required=True); mcp.add_argument("--outcome", choices=("success", "error"), required=True); mcp.add_argument("--evidence", required=True); mcp.add_argument("--summary", required=True); mcp.add_argument("--query"); mcp.add_argument("--page-number", type=int); mcp.add_argument("--key", action="append", default=[]); mcp.add_argument("--returned-count", type=int); mcp.set_defaults(handler=mcp_log_command)
    absent = commands.add_parser("jira-record-absent-counterparts"); absent.add_argument("--run-id", required=True); absent.add_argument("--evidence", required=True); absent.add_argument("--key", action="append", default=[]); absent.set_defaults(handler=jira_record_absent_counterparts_command, provider="jira")
    page = commands.add_parser("query-page"); page.add_argument("--run-id", required=True); page.add_argument("--provider", choices=PROVIDERS, required=True); page.add_argument("--query", required=True); page.add_argument("--page-number", type=int, required=True); page.add_argument("--cursor"); page.add_argument("--next-cursor"); page.add_argument("--last-page", action="store_true"); page.add_argument("--evidence", required=True); page.add_argument("--key", action="append", default=[]); page.set_defaults(handler=query_page_command)
    unavailable = commands.add_parser("query-unavailable"); unavailable.add_argument("--run-id", required=True); unavailable.add_argument("--provider", choices=PROVIDERS, required=True); unavailable.add_argument("--reason", required=True); unavailable.add_argument("--evidence", required=True); unavailable.set_defaults(handler=query_unavailable_command)
    item = commands.add_parser("record-issue"); item.add_argument("--run-id", required=True); item.add_argument("--provider", choices=PROVIDERS, required=True); item.add_argument("--key", required=True); item.add_argument("--jira-key"); item.add_argument("--jira-key-state", choices=OBSERVATION_STATES); item.add_argument("--evidence", required=True); item.add_argument("--summary", required=True); item.add_argument("--issue-type", required=True); item.add_argument("--status", required=True); item.add_argument("--assignee-id"); item.add_argument("--assignee-name"); item.add_argument("--assignee-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--estimate", type=float); item.add_argument("--estimate-unit", default="story-points"); item.add_argument("--estimate-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--epic-key"); item.add_argument("--epic-name"); item.add_argument("--epic-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--release", action="append", default=[]); item.add_argument("--releases-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--created-at"); item.add_argument("--updated-at"); item.set_defaults(handler=record_issue_command)
    collector_complete = commands.add_parser("collector-complete"); collector_complete.add_argument("--run-id", required=True); collector_complete.add_argument("--provider", choices=PROVIDERS, required=True); collector_complete.set_defaults(handler=collector_complete_command)
    event = commands.add_parser("history-event"); event.add_argument("--run-id", required=True); event.add_argument("--provider", choices=PROVIDERS, required=True); event.add_argument("--key", required=True); event.add_argument("--evidence", required=True); event.add_argument("--at", required=True); event.add_argument("--field", choices=("assignee", "status"), required=True); event.add_argument("--from-id"); event.add_argument("--from-name"); event.add_argument("--from-value"); event.add_argument("--to-id"); event.add_argument("--to-name"); event.add_argument("--to-value"); event.set_defaults(handler=history_event_command)
    history = commands.add_parser("history-complete"); history.add_argument("--run-id", required=True); history.add_argument("--provider", choices=PROVIDERS, required=True); history.add_argument("--key", required=True); history.add_argument("--state", choices=("complete", "unavailable"), required=True); history.add_argument("--reason"); history.add_argument("--evidence", required=True); history.set_defaults(handler=history_complete_command)
    history_job_done = commands.add_parser("history-job-complete"); history_job_done.add_argument("--run-id", required=True); history_job_done.add_argument("--job-id", required=True); history_job_done.set_defaults(handler=history_job_complete_command)
    run_status = commands.add_parser("run-status"); run_status.add_argument("--run-id", required=True); run_status.set_defaults(handler=run_status_command)
    collector_brief = commands.add_parser("collector-brief"); collector_brief.add_argument("--run-id", required=True); collector_brief.set_defaults(handler=collector_brief_command)
    participant_parser = commands.add_parser("set-participant"); participant_parser.add_argument("--run-id", required=True); participant_parser.add_argument("--provider", choices=PROVIDERS, required=True); participant_parser.add_argument("--account-id", required=True); participant_parser.add_argument("--team-id", required=True); participant_parser.set_defaults(handler=set_participant_command)
    decision_parser = commands.add_parser("set-development-decision"); decision_parser.add_argument("--run-id", required=True); decision_parser.add_argument("--key", required=True); decision_parser.add_argument("--choice", choices=DEVELOPMENT_DECISION_CHOICES, required=True); decision_parser.add_argument("--state", choices=DEVELOPMENT_DECISION_STATES); decision_parser.add_argument("--apply-to-all", action="store_true"); decision_parser.set_defaults(handler=set_development_decision_command)
    abandon = commands.add_parser("abandon-run"); abandon.add_argument("--run-id", required=True); abandon.add_argument("--reason", required=True); abandon.set_defaults(handler=abandon_run_command)
    reconcile = commands.add_parser("reconcile"); reconcile.add_argument("--run-id", required=True); reconcile.set_defaults(handler=reconcile_command)
    result = commands.add_parser("result-status"); result.add_argument("--run-id", required=True); result.set_defaults(handler=result_status_command)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        run_id = getattr(args, "run_id", None)
        if run_id and not session_log_path(run_id).is_file():
            raise ValueError("Tracker-run не содержит обязательный tracker-session-log.md; начни новый run")
        result = args.handler(args)
        run_id = getattr(args, "run_id", None)
        if run_id:
            details = f"command={args.command}; exit={result}"
            for name in ("page_number", "key", "state", "job_id", "record_sha256"):
                value = getattr(args, name, None)
                if value is not None:
                    details += f"; {name}={value}"
            append_session_log(run_id, source="trackerctl", event="command", provider=getattr(args, "provider", None), evidence_value=getattr(args, "evidence", None), details=details)
        return result
    except ValueError as exc:
        run_id = getattr(args, "run_id", None)
        if run_id and session_log_path(run_id).is_file():
            append_session_log(run_id, source="trackerctl", event="error", provider=getattr(args, "provider", None), evidence_value=getattr(args, "evidence", None), details=f"command={args.command}; error={exc}")
        payload = {"status": "tracker-read-blocked", "run_id": run_id, "error": str(exc), "must_stop": True, "workflow_complete": False, "final_response_allowed": False, "allowed_next_action": "fix-reported-gap", "required_success_status": "tracker-read-reconciled"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
