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


PROTOCOL = "targeted-tracker-v2"
SCHEMA_VERSION = 3
CONFIG_SCHEMA_VERSION = 4
STOP_EXIT = 3
PROVIDERS = ("sbertrek", "jira")
SCOPE_KINDS = ("epic", "tasks")
OBSERVATION_STATES = ("value", "absent", "not-returned")
MISSING_SENTINELS = {"not-returned", "not returned", "unknown", "none", "null", "-", "—"}
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
MERGED_FIELDS = (
    "summary", "issue_type", "status", "assignee", "estimate", "epic",
    "releases", "created_at", "updated_at",
)
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


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать JSON {path}: {exc}") from exc


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def issue_key(value: str, label: str = "Ключ задачи") -> str:
    value = value.strip().upper()
    if not ISSUE_KEY.fullmatch(value):
        raise ValueError(f"{label} должен иметь вид PROJECT-123: {value}")
    return value


def unique_keys(values: list[str]) -> list[str]:
    return sorted(set(issue_key(value) for value in values))


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
        used: dict[str, str] = {}
        for account, member in mapping.items():
            if not isinstance(account, str) or not isinstance(member, dict):
                raise ValueError(f"Некорректный participants.{provider}")
            team_id = member.get("team_id")
            if not isinstance(team_id, str) or normalized_team_id(team_id) != team_id:
                raise ValueError(f"Некорректный team_id participants.{provider}.{account}")
            previous = used.setdefault(team_id, account)
            if previous != account:
                raise ValueError(f"team_id {team_id} назначен нескольким аккаунтам {provider}")
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


def log_value(value: Any, *, limit: int = 500) -> str:
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


def unexpected_run_artifacts(run_id: str) -> list[str]:
    root = run_root(run_id)
    allowed_root = {
        "scope.json", "tracker-session-log.md", "run-status.json",
        "completion-status.json", "reconciled.json", "report.md",
        "pending-participant.json", "jobs", "providers",
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


def jql_epic(epic: str, method: str) -> str:
    if method == "parent":
        return f'parent = "{epic}"'
    if method == "epic-link":
        return f'"Epic Link" = "{epic}"'
    raise ValueError(f"Неизвестный способ раскрытия Jira-эпика: {method}")


def query_spec(provider: str, purpose: str, exact: str | None = None, *, method: str | None = None) -> dict:
    return {
        "state": "pending", "purpose": purpose,
        "language": "TQL" if provider == "sbertrek" else "JQL",
        "exact": exact, "method": method, "pages": [], "keys": [],
        "unavailable_reason": None, "unavailable_evidence": None,
    }


def primary_query(scope: dict) -> dict:
    provider, kind, ids = scope["provider"], scope["kind"], scope["ids"]
    if provider == "sbertrek" and kind == "epic":
        return query_spec(provider, "epic-members", tql_epic(ids[0]))
    if provider == "sbertrek":
        return query_spec(provider, "task-cards", tql_units(ids))
    if kind == "epic":
        return query_spec(provider, "epic-members", jql_epic(ids[0], "parent"), method="parent")
    return query_spec(provider, "task-cards", jql_keys(ids))


def snapshot_template(provider: str, scope: dict, config: dict) -> dict:
    query = primary_query(scope) if provider == scope["provider"] else query_spec(provider, "counterparts")
    return {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION,
        "provider": provider, "captured_at": None, "scope": scope,
        "projects": config["projects"][provider], "query": query,
        "issues": [], "collection_complete": False,
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


def enabled_providers(config: dict) -> tuple[str, ...]:
    return PROVIDERS if config["jira_enabled"] else ("sbertrek",)


def all_snapshots(run_id: str, config: dict, *, finalized: bool = False) -> dict[str, dict]:
    return {provider: validate_snapshot(load_json(snapshot_path(run_id, provider)), provider, finalized) for provider in enabled_providers(config)}


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
            "text": exact,
            "sha256": query_digest(exact),
        },
        "output": str(snapshot_path(run_id, provider)),
        "collector_contract": str(Path(__file__).resolve().parents[1] / "core" / "tracker-collector.md"),
        "allowed_operations": [
            "select-runtime-query-tool", "execute-exact-query", "paginate",
            "record-bounded-call", "record-page", "record-compact-card",
            "complete-job",
        ],
        "forbidden_operations": [
            "read-mcp-documentation", "probe-with-alternative-query",
            "search-by-title-or-description", "read-returned-issues-one-by-one",
            "change-tracker-or-analytical-artifacts", "continue-to-next-job",
        ],
        "required_task_fields": [
            "key", "jira_key", "jira_key_state", "summary", "issue_type", "status", "assignee",
            "estimate", "epic", "releases", "created_at", "updated_at",
        ],
        "created_at": now(),
        "completed_at": None,
    }


def history_job(run_id: str, provider: str, number: int, keys: list[str]) -> dict:
    job_id = f"history-{provider}-{number:02d}"
    return {
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "job_id": job_id,
        "kind": "provider-history",
        "state": "pending",
        "provider": provider,
        "keys": keys,
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
        ):
            raise ValueError("Контрольная сумма запроса collector-job не совпадает")
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
        "required_sequence": ["MCP call", "mcp-log", "query-page"],
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
    if assignee_events:
        latest = assignee_events[-1]
        before = participant_role(config, latest["source"], latest.get("from"))
        after = participant_role(config, latest["source"], latest.get("to"))
        if before == "developer" and after and after != "developer":
            return {"state": "completed", "basis": "developer-handoff", "at": latest["at"]}
    return {"state": "in-progress", "basis": f"{provider}-targeted-read"}


def first_unknown_participant(snapshots: dict[str, dict], config: dict) -> dict | None:
    development_types = {item.casefold() for item in config["development_issue_types"]}
    for provider in PROVIDERS:
        snapshot = snapshots.get(provider)
        if not snapshot:
            continue
        for item in snapshot["issues"]:
            if str(item.get("issue_type") or "").casefold() not in development_types:
                continue
            values = [item.get("assignee")]
            for event in item["history"]["events"]:
                if event["field"] == "assignee":
                    values.extend((event.get("from"), event.get("to")))
            for value in values:
                if isinstance(value, dict) and value.get("id") and value["id"] not in config["participants"][provider]:
                    return {"provider": provider, "account_id": value["id"], "name": value.get("name") or value["id"]}
    return None


def pending_participant_path(run_id: str) -> Path:
    return run_root(run_id) / "pending-participant.json"


def snapshot_gaps(snapshot: dict) -> list[str]:
    gaps = []
    if not snapshot["collection_complete"]:
        gaps.append(f"{snapshot['provider']}.collection.pending")
    missing = set(snapshot["query"]["keys"]) - {item["key"] for item in snapshot["issues"]}
    gaps.extend(f"{snapshot['provider']}.{item}.card.pending" for item in sorted(missing))
    for item in snapshot["issues"]:
        if item["history"]["state"] == "pending":
            gaps.append(f"{snapshot['provider']}.{item['key']}.history.pending")
    return gaps


def reconcile_data(snapshots: dict[str, dict], config: dict) -> dict:
    sber = snapshots["sbertrek"]
    jira = snapshots.get("jira")
    sber_issues = {item["key"]: item for item in sber["issues"]}
    jira_issues = {item["key"]: item for item in jira["issues"]} if jira else {}
    paired_jira: set[str] = set()
    merged, discrepancies = [], []
    for sber_key, sissue in sorted(sber_issues.items()):
        jira_key = sissue.get("jira_key")
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
        assigned_at, work_started_at = assignment_dates(sissue, jissue, config)
        record.update({
            "field_sources": sources,
            "conflicts": conflicts,
            "history": merged_history(sissue, jissue),
            "assigned_at": assigned_at,
            "work_started_at": work_started_at,
            "development": development_state(sissue, jissue, config),
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
    counts = {"sbertrek": len(sber_issues), "jira": len(jira_issues), "matched": len(paired_jira), "merged": len(merged), "discrepancies": len(discrepancies)}
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
    return {"protocol": PROTOCOL, "schema_version": SCHEMA_VERSION, "status": "tracker-read-reconciled", "scope": scope, "counts": counts, "issues": merged, "groupings": groupings, "discrepancies": discrepancies, "limitations": limitations}


def render_report(result: dict) -> str:
    scope = result["scope"]
    lines = [f"# Сверка трекеров: {scope['label']}", "", "## Область", "", f"- Тип: `{scope['kind']}`", f"- Исходный трекер: `{scope['provider']}`", f"- Ключи: {', '.join(scope['ids'])}", "", "## Сводка", ""]
    labels = {"sbertrek": "Задач SberTrek", "jira": "Задач Jira", "matched": "Склеено пар", "merged": "Итоговых задач", "discrepancies": "Расхождений"}
    lines += [f"- {labels[name]}: {value}" for name, value in result["counts"].items()]
    lines += ["", "## Задачи", "", "| SberTrek | Jira | Название | Статус | Исполнитель | Оценка | В работе с | Состояние |", "|---|---|---|---|---|---|---|---|"]
    for item in result["issues"]:
        assignee, estimate = item.get("assignee") or {}, item.get("estimate") or {}
        assignee_text = assignee.get("name") or assignee.get("id") or "—" if isinstance(assignee, dict) else str(assignee)
        estimate_text = f"{estimate.get('value')} {estimate.get('unit')}" if isinstance(estimate, dict) and estimate.get("value") is not None else "—"
        cells = [item.get("sbertrek_key") or "—", item.get("jira_key") or "—", item.get("summary") or "—", item.get("status") or "—", assignee_text, estimate_text, item.get("work_started_at") or "—", item["development"]["state"]]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    lines += ["", "## Ограничения", ""] + ([f"- {item}" for item in result["limitations"]] or ["- Нет"])
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
        save_json(snapshot_path(run_id, provider), snapshot_template(provider, scope, config))
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
    append_session_log(run_id, source="trackerctl", event="command", details="command=begin; exit=0")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def mcp_log_command(args: argparse.Namespace) -> int:
    call = evidence(args.evidence, args.provider)
    if logged_mcp_details(args.run_id, call):
        raise ValueError("Этот MCP-вызов уже записан в журнале")
    if not args.summary.strip() or len(args.summary) > 500:
        raise ValueError("--summary должен содержать от 1 до 500 символов")
    if args.operation in {"capability-discovery", "issue-detail"}:
        raise ValueError("targeted-tracker-v2 запрещает exploratory и поштучные MCP-вызовы")
    config = load_config()
    if args.operation == "query":
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
    if args.operation in {"issue-detail", "history"} and not args.key:
        raise ValueError(f"operation={args.operation} требует --key")
    if args.operation not in {"issue-detail", "history"} and args.key:
        raise ValueError("--key допустим только для issue-detail и history")
    if args.operation == "history":
        job = active_job(args.run_id)
        if (
            not job
            or job.get("kind") != "provider-history"
            or job.get("provider") != args.provider
            or issue_key(args.key) not in job.get("keys", [])
        ):
            raise ValueError("Историю разрешено читать только для ключа активного history-job")
    parts = [f"operation={args.operation}", f"outcome={args.outcome}"]
    if args.query is not None:
        parts.append(f"query_sha256={query_digest(args.query)}")
    if args.page_number is not None:
        parts.append(f"page={args.page_number}")
    if args.key:
        parts.append(f"key={issue_key(args.key)}")
    if args.returned_count is not None:
        parts.append(f"returned={args.returned_count}")
    if args.query is not None:
        parts.append(f"query={args.query}")
    parts.append(f"summary={args.summary}")
    append_session_log(args.run_id, source="mcp", event="call", provider=args.provider, evidence_value=call, details="; ".join(parts))
    print(json.dumps({"status": "tracker-mcp-call-logged", "run_id": args.run_id, "provider": args.provider, "operation": args.operation, "outcome": args.outcome, "evidence": call}, ensure_ascii=False, indent=2))
    return 0


def query_page_command(args: argparse.Namespace) -> int:
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
    query["pages"].append({"number": args.page_number, "cursor": args.cursor, "next_cursor": args.next_cursor, "last_page": args.last_page, "evidence": call, "keys": page_keys})
    query["keys"] = sorted(set(query["keys"]) | set(page_keys))
    query["state"] = "complete" if args.last_page else "collecting"
    save_json(path, snapshot)
    job["state"] = "running"
    save_json(job_path_value, job)
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


def jira_epic_fallback_command(args: argparse.Namespace) -> int:
    config = load_config()
    current = current_query(args.run_id, config)
    if not current or current[0] != "jira":
        raise ValueError("Fallback допустим только для текущего Jira-запроса")
    path, snapshot = load_snapshot(args.run_id, "jira")
    query, scope = snapshot["query"], snapshot["scope"]
    if scope["provider"] != "jira" or scope["kind"] != "epic" or query.get("method") != "parent" or query["pages"]:
        raise ValueError("Fallback допустим только после отказа начального parent-запроса Jira-эпика")
    call = evidence(args.evidence, "jira")
    require_logged_mcp(args.run_id, call, outcome="error")
    query.update({"exact": jql_epic(scope["ids"][0], "epic-link"), "method": "epic-link", "state": "pending"})
    save_json(path, snapshot)
    job_file, job = load_job(args.run_id, "collection-jira")
    job["query"].update({
        "text": query["exact"],
        "sha256": query_digest(query["exact"]),
    })
    save_json(job_file, job)
    print(json.dumps({"status": "jira-epic-query-fallback-ready", **next_query_payload("jira", query)}, ensure_ascii=False, indent=2))
    return 0


def record_issue_command(args: argparse.Namespace) -> int:
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
        **values, "field_observations": observations,
        "created_at": args.created_at, "updated_at": args.updated_at,
        "history": {"state": "pending", "evidence": [], "events": [], "reason": None},
    }
    snapshot["issues"].append(item)
    save_json(path, snapshot)
    print(json.dumps({"status": "tracker-issue-recorded", "provider": args.provider, "key": key_value, "jira_key": jira_key}, ensure_ascii=False, indent=2))
    return 0


def missing_cards(snapshot: dict) -> list[str]:
    return sorted(set(snapshot["query"]["keys"]) - {item["key"] for item in snapshot["issues"]})


def create_history_jobs(run_id: str, snapshots: dict[str, dict]) -> list[dict]:
    created = []
    for provider in PROVIDERS:
        snapshot = snapshots.get(provider)
        if not snapshot:
            continue
        keys = sorted(item["key"] for item in snapshot["issues"])
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
    if secondary and secondary["query"]["exact"] is None:
        if primary_provider == "sbertrek":
            ids = sorted({item["jira_key"] for item in primary["issues"] if item.get("jira_key")})
            exact = jql_keys(ids) if ids else None
        else:
            ids = list(primary["query"]["keys"])
            exact = tql_jira_keys(ids) if ids else None
        if exact:
            secondary["query"].update({"exact": exact, "state": "pending"})
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
    require_logged_mcp(args.run_id, call, outcome="success")
    if not re.search(rf"(?<![A-Z0-9_]){re.escape(item['key'])}(?![0-9])", call, re.I):
        raise ValueError("Evidence события истории должен содержать точный ключ задачи")
    event = {"at": args.at, "field": args.field, "from": participant(args.from_id, args.from_name) if args.field == "assignee" else args.from_value, "to": participant(args.to_id, args.to_name) if args.field == "assignee" else args.to_value}
    item["history"]["events"].append(event)
    save_json(path, snapshot)
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
    require_logged_mcp(args.run_id, call, outcome="success" if args.state == "complete" else "error")
    if not re.search(rf"(?<![A-Z0-9_]){re.escape(item['key'])}(?![0-9])", call, re.I):
        raise ValueError("Evidence истории должен содержать точный ключ задачи")
    item["history"].update({"state": args.state, "evidence": [call], "reason": args.reason})
    save_json(path, snapshot)
    print(json.dumps({"status": "history-complete", "provider": args.provider, "key": args.key, "history_state": args.state}, ensure_ascii=False, indent=2))
    return 0


def history_job_complete_command(args: argparse.Namespace) -> int:
    path, job = load_job(args.run_id, args.job_id)
    if job.get("kind") != "provider-history" or job.get("state") not in {"pending", "running"}:
        raise ValueError("Завершить можно только активный history-job")
    if active_job(args.run_id) != job:
        raise ValueError("Завершить можно только текущий history-job")
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
    gaps = sum((snapshot_gaps(snapshot) for snapshot in snapshots.values()), [])
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
    if job["kind"] == "provider-collection":
        language = job["query"]["language"]
        query = job["query"]["text"]
        prompt = (
            f"Выполни в {job['provider']} ровно этот {language}-запрос без изменений:\n\n"
            f"{query}\n\n"
            "Не выполняй никаких других поисков и не заменяй запрос поиском по тексту, названию или смыслу. "
            f"Для записи компактного результата прочитай только {contract} и {path}. "
            "Не создавай скрипты или другие вспомогательные файлы. После collector-complete немедленно "
            "верни только status, job_id и пути."
        )
    else:
        keys = ", ".join(job["keys"])
        prompt = (
            f"Выполни в {job['provider']} ровно по одному запросу истории только для этих ключей: {keys}. "
            f"Для записи результата прочитай только {contract} и {path}. Не ищи другие задачи, не создавай "
            "скрипты или вспомогательные файлы. После history-job-complete немедленно верни только status, "
            "job_id и пути."
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
    for account, member in config["participants"][args.provider].items():
        if member["team_id"] == team_id and account != args.account_id:
            raise ValueError(f"team_id {team_id} уже назначен другому аккаунту {args.provider}")
    config["participants"][args.provider][args.account_id] = {"team_id": team_id}
    save_json(config_path(), config)
    pending.unlink()
    print(json.dumps({"status": "tracker-participant-saved", "provider": args.provider, "account_id": args.account_id, "team_id": team_id, "role": team_role(team_id)}, ensure_ascii=False, indent=2))
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
    gaps = sum((snapshot_gaps(snapshot) for snapshot in snapshots.values()), [])
    if gaps:
        raise ValueError("Tracker-run не завершён: " + ", ".join(gaps))
    unknown = first_unknown_participant(snapshots, config)
    if unknown:
        question = f"Какой командный team_id соответствует {unknown['provider']} account {unknown['account_id']} ({unknown['name']})?"
        save_json(pending_participant_path(args.run_id), {**unknown, "question": question})
        payload = stop_payload(question, status="tracker-reconcile-blocked", run_id=args.run_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return STOP_EXIT
    for provider, snapshot in snapshots.items():
        if not snapshot.get("captured_at"):
            snapshot["captured_at"] = now()
            save_json(snapshot_path(args.run_id, provider), snapshot)
    result = reconcile_data(snapshots, config)
    root = run_root(args.run_id)
    save_json(root / "reconciled.json", result)
    (root / "report.md").write_text(render_report(result), encoding="utf-8")
    completion = {
        "protocol": PROTOCOL, "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id, "status": "tracker-read-reconciled",
        "workflow_complete": True, "final_response_allowed": True,
        "planning_application_allowed": result["scope"].get("intent") == "update-planning",
        "counts": result["counts"], "limitations": result["limitations"],
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
    mcp = commands.add_parser("mcp-log"); mcp.add_argument("--run-id", required=True); mcp.add_argument("--provider", choices=PROVIDERS, required=True); mcp.add_argument("--operation", choices=("query", "history"), required=True); mcp.add_argument("--outcome", choices=("success", "error"), required=True); mcp.add_argument("--evidence", required=True); mcp.add_argument("--summary", required=True); mcp.add_argument("--query"); mcp.add_argument("--page-number", type=int); mcp.add_argument("--key"); mcp.add_argument("--returned-count", type=int); mcp.set_defaults(handler=mcp_log_command)
    page = commands.add_parser("query-page"); page.add_argument("--run-id", required=True); page.add_argument("--provider", choices=PROVIDERS, required=True); page.add_argument("--query", required=True); page.add_argument("--page-number", type=int, required=True); page.add_argument("--cursor"); page.add_argument("--next-cursor"); page.add_argument("--last-page", action="store_true"); page.add_argument("--evidence", required=True); page.add_argument("--key", action="append", default=[]); page.set_defaults(handler=query_page_command)
    unavailable = commands.add_parser("query-unavailable"); unavailable.add_argument("--run-id", required=True); unavailable.add_argument("--provider", choices=PROVIDERS, required=True); unavailable.add_argument("--reason", required=True); unavailable.add_argument("--evidence", required=True); unavailable.set_defaults(handler=query_unavailable_command)
    fallback = commands.add_parser("jira-epic-fallback"); fallback.add_argument("--run-id", required=True); fallback.add_argument("--evidence", required=True); fallback.set_defaults(handler=jira_epic_fallback_command)
    item = commands.add_parser("record-issue"); item.add_argument("--run-id", required=True); item.add_argument("--provider", choices=PROVIDERS, required=True); item.add_argument("--key", required=True); item.add_argument("--jira-key"); item.add_argument("--jira-key-state", choices=OBSERVATION_STATES); item.add_argument("--evidence", required=True); item.add_argument("--summary", required=True); item.add_argument("--issue-type", required=True); item.add_argument("--status", required=True); item.add_argument("--assignee-id"); item.add_argument("--assignee-name"); item.add_argument("--assignee-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--estimate", type=float); item.add_argument("--estimate-unit", default="story-points"); item.add_argument("--estimate-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--epic-key"); item.add_argument("--epic-name"); item.add_argument("--epic-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--release", action="append", default=[]); item.add_argument("--releases-state", choices=OBSERVATION_STATES, required=True); item.add_argument("--created-at"); item.add_argument("--updated-at"); item.set_defaults(handler=record_issue_command)
    collector_complete = commands.add_parser("collector-complete"); collector_complete.add_argument("--run-id", required=True); collector_complete.add_argument("--provider", choices=PROVIDERS, required=True); collector_complete.set_defaults(handler=collector_complete_command)
    event = commands.add_parser("history-event"); event.add_argument("--run-id", required=True); event.add_argument("--provider", choices=PROVIDERS, required=True); event.add_argument("--key", required=True); event.add_argument("--evidence", required=True); event.add_argument("--at", required=True); event.add_argument("--field", choices=("assignee", "status"), required=True); event.add_argument("--from-id"); event.add_argument("--from-name"); event.add_argument("--from-value"); event.add_argument("--to-id"); event.add_argument("--to-name"); event.add_argument("--to-value"); event.set_defaults(handler=history_event_command)
    history = commands.add_parser("history-complete"); history.add_argument("--run-id", required=True); history.add_argument("--provider", choices=PROVIDERS, required=True); history.add_argument("--key", required=True); history.add_argument("--state", choices=("complete", "unavailable"), required=True); history.add_argument("--reason"); history.add_argument("--evidence", required=True); history.set_defaults(handler=history_complete_command)
    history_job_done = commands.add_parser("history-job-complete"); history_job_done.add_argument("--run-id", required=True); history_job_done.add_argument("--job-id", required=True); history_job_done.set_defaults(handler=history_job_complete_command)
    run_status = commands.add_parser("run-status"); run_status.add_argument("--run-id", required=True); run_status.set_defaults(handler=run_status_command)
    collector_brief = commands.add_parser("collector-brief"); collector_brief.add_argument("--run-id", required=True); collector_brief.set_defaults(handler=collector_brief_command)
    participant_parser = commands.add_parser("set-participant"); participant_parser.add_argument("--run-id", required=True); participant_parser.add_argument("--provider", choices=PROVIDERS, required=True); participant_parser.add_argument("--account-id", required=True); participant_parser.add_argument("--team-id", required=True); participant_parser.set_defaults(handler=set_participant_command)
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
            for name in ("page_number", "key", "state"):
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
