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
RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "primary_provider": "sbertrek",
    "projects": {"sbertrek": [], "jira": []},
    "issue_pairs": {},
    "development_issue_types": ["development-task"],
    "participants": {"sbertrek": {}, "jira": {}},
    "status_rules": {
        "completed": [],
        "excluded": ["Отменена", "Удалена"],
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
            if participant.get("role") not in {"developer", "tester", "analyst", "other"}:
                raise ValueError(f"Неизвестная роль participants.{provider}.{account}")
            previous = canonical_roles.setdefault(canonical_id, participant["role"])
            if previous != participant["role"]:
                raise ValueError(f"Противоречащие роли участника {canonical_id}")
    normalized_statuses(payload, "completed")
    normalized_statuses(payload, "excluded")
    return payload


def validate_snapshot(payload: Any, provider: str) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Неподдерживаемая схема снимка {provider}")
    if payload.get("provider") != provider:
        raise ValueError(f"Снимок должен иметь provider={provider}")
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
        history = issue.get("history", [])
        if not isinstance(history, list) or not all(isinstance(event, dict) for event in history):
            raise ValueError(f"history задачи {issue['key']} должен быть списком объектов")
        for event in history:
            if not isinstance(event.get("field"), str) or not event["field"].strip():
                raise ValueError(f"Событие history задачи {issue['key']} должно иметь field")
            if timestamp_value(event.get("at")) is None:
                raise ValueError(f"Событие history задачи {issue['key']} должно иметь корректный at")
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
            discrepancies.append({"kind": "jira-pair-missing", "key": sber_issue["key"]})
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

    for key in sorted(set(jira_by_key) - used_jira):
        discrepancies.append({"kind": "jira-only", "jira_key": key})

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "primary_provider": "sbertrek",
        "jira_used": jira_snapshot is not None,
        "limitations": [] if jira_snapshot is not None else ["jira-unavailable"],
        "issues": merged_issues,
        "discrepancies": discrepancies,
    }


def report_text(payload: dict) -> str:
    lines = [
        "# Сверка SberTrek и Jira",
        "",
        f"Сформировано: `{payload['generated_at']}`",
        "Основной источник: `SberTrek`.",
        f"Задач SberTrek: **{len(payload['issues'])}**.",
        f"Расхождений: **{len(payload['discrepancies'])}**.",
        f"Ограничения полноты: **{', '.join(payload['limitations']) or 'нет'}**.",
        "",
        "| SberTrek | Jira | Найдена через | Эпик | Релизы | Состояние разработки | Дообогащение | Конфликты |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for issue in payload["issues"]:
        enriched = ", ".join(issue["enriched_from_jira"]) or "—"
        conflicts = ", ".join(issue["conflicting_fields"]) or "—"
        epic = group_label(issue.get("epic")) or "Без эпика"
        releases = ", ".join(group_label(item) for item in (issue.get("releases") or [])) or "Без релиза"
        lines.append(
            f"| {issue['key']} | {issue.get('jira_key') or '—'} | {issue.get('discovery') or 'seed'} | {epic} | {releases} | "
            f"{issue['development_state']['state']} | {enriched} | {conflicts} |"
        )
    append_grouping(lines, payload["issues"], "Эпики", lambda issue: [group_label(issue.get("epic")) or "Без эпика"])
    append_grouping(
        lines,
        payload["issues"],
        "Релизы",
        lambda issue: [group_label(item) for item in (issue.get("releases") or [])] or ["Без релиза"],
    )
    if payload["discrepancies"]:
        lines.extend(["", "## Расхождения", ""])
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
    print(json.dumps({"status": "tracker-config-created", "path": str(path)}, ensure_ascii=False, indent=2))
    return 0


def begin_command(_: argparse.Namespace) -> int:
    run_id = new_run_id()
    inputs = run_root(run_id) / "input"
    inputs.mkdir(parents=True, exist_ok=False)
    print(json.dumps({
        "status": "tracker-read-started",
        "run_id": run_id,
        "sbertrek_input": str(inputs / "sbertrek.json"),
        "jira_input": str(inputs / "jira.json"),
        "project_changed": False,
        "tracker_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    config = validate_config(load_json(Path(args.config).resolve() if args.config else config_path()))
    sber = validate_snapshot(load_json(Path(args.sbertrek).resolve()), "sbertrek")
    jira = validate_snapshot(load_json(Path(args.jira).resolve()), "jira") if args.jira else None
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
