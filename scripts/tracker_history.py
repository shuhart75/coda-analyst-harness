from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path

from tracker_lifecycle import HistoryEvent, StatusRules, TaskHistory, calculate_feature, timestamp


def pointer(value, path: str):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("Expected an RFC 6901 JSON pointer")
    try:
        for part in path.split("/")[1:]:
            if isinstance(value, str):
                value = json.loads(value)
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError(f"JSON pointer not found: {path}") from error


def optional(value, path):
    try:
        return pointer(value, path)
    except ValueError:
        return None


def validate_call(call: dict, provider: str) -> None:
    if not isinstance(call, dict) or call.get("provider") != provider:
        raise ValueError("Call provider does not match collection scope")
    if not isinstance(call.get("tool"), str) or not call["tool"].strip():
        raise ValueError("The actual MCP tool name is required")
    if not isinstance(call.get("arguments"), dict) or not isinstance(call.get("capability_source"), str) or not call["capability_source"].strip():
        raise ValueError("Actual arguments and the tool capability description reference are required")
    if call.get("operation") != "read-only":
        raise ValueError("Only read-only collection is allowed")
    captured = timestamp(datetime.fromisoformat(call.get("captured_at", "")))
    if captured > datetime.now(timezone.utc):
        raise ValueError("Capture time is in the future")
    def check_credentials(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold().replace("-", "_") in {"authorization", "cookie", "cookies", "password", "token", "access_token", "api_key"}:
                    raise ValueError("Credentials must not be recorded in call arguments")
                check_credentials(child)
        elif isinstance(value, list):
            for child in value:
                check_credentials(child)
    check_credentials(call["arguments"])


JIRA_MAPPING = {
    "key": "/key", "assignee": "/assignee/key", "status": "/status/name",
    "events": "/changelogs", "at": "/created", "changes": "/items",
    "field": "/field", "from": "/from_id", "to": "/to_id",
    "assignment_field": "assignee", "status_field": "status",
}


def decode_history(payload: dict, entry: dict, observed: datetime) -> tuple[TaskHistory, list[str]]:
    from tracker_workflow import digest_object

    mapping = entry.get("mapping", JIRA_MAPPING if entry["provider"] == "jira" else None)
    if not isinstance(mapping, dict) or not set(JIRA_MAPPING).issubset(mapping):
        raise ValueError("A complete source-path mapping is required for this provider")
    key = pointer(payload, mapping["key"])
    if key != entry["key"]:
        raise ValueError("History response belongs to another task")
    events = pointer(payload, mapping["events"])
    if not isinstance(events, list):
        raise ValueError("History events must be an array")
    aliases = entry.get("status_aliases", {})
    if not isinstance(aliases, dict) or any(not isinstance(value, str) for value in aliases.values()):
        raise ValueError("Status aliases must map source values to explicit codes")
    limits = []
    complete = False
    if "total" in mapping and "start" in mapping:
        total, start = pointer(payload, mapping["total"]), pointer(payload, mapping["start"])
        if type(total) is not int or type(start) is not int or total < 0 or start < 0 or start + len(events) > total:
            raise ValueError("Invalid history pagination metadata")
        complete = start == 0 and total == len(events)
    if not complete:
        limits.append("history-completeness-not-proven")
    normalized = []
    for event in events:
        at = timestamp(datetime.fromisoformat(pointer(event, mapping["at"])))
        if at > observed:
            raise ValueError("History event is newer than the captured snapshot")
        changes = pointer(event, mapping["changes"])
        if not isinstance(changes, list):
            raise ValueError("History changes must be an array")
        fields = {}
        for change in changes:
            field = pointer(change, mapping["field"])
            if field not in {mapping["assignment_field"], mapping["status_field"]}:
                continue
            if field in fields:
                raise ValueError("Ambiguous repeated field in one history event")
            prefix = "status" if field == mapping["status_field"] else "assignment"
            before = optional(change, mapping.get(prefix + "_from", mapping["from"]))
            after = optional(change, mapping.get(prefix + "_to", mapping["to"]))
            if field == mapping["status_field"]:
                if before is None or after is None:
                    raise ValueError("Status transition requires both values")
                before, after = aliases.get(str(before), str(before)), aliases.get(str(after), str(after))
            elif any(value is not None and not isinstance(value, str) for value in (before, after)):
                raise ValueError("Assignment identities must be strings or null")
            elif before is None and after is None:
                raise ValueError("Assignment change contains neither old nor new identity")
            fields[field] = (before, after)
        if fields:
            normalized.append(HistoryEvent(
                "source-sha256:" + digest_object(event), at,
                fields.get(mapping["assignment_field"]), fields.get(mapping["status_field"]),
            ))
    normalized.sort(key=lambda event: event.at)
    status = pointer(payload, mapping["status"])
    try:
        assignee = pointer(payload, mapping["assignee"])
    except ValueError:
        assignee = None
        complete = False
        limits.append("snapshot-assignee-not-returned")
    if not isinstance(status, (str, int)) or isinstance(status, bool):
        raise ValueError("Snapshot status requires a source value")
    history = TaskHistory(key, entry["role"], observed, assignee,
                          aliases.get(str(status), str(status)), tuple(normalized), complete)
    return history, limits


def history_review_command(args) -> int:
    try:
        return review_history(args)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"Invalid history manifest or response: {error}") from error


def pin_history_response(run_id: str, path: Path, raw: bytes, provenance: dict | None = None) -> None:
    from tracker_workflow import run_root, digest_bytes, digest_object, load_json, save_json

    root = run_root(run_id) / "history-sources"
    receipts = [(root / (digest_object(str(path.resolve())) + ".json"),
                 {"path": str(path.resolve()), "sha256": digest_bytes(raw)})]
    if provenance is not None:
        receipts.append((root / (digest_object(provenance) + ".json"),
                         {"provenance": provenance, "sha256": digest_bytes(raw)}))
    for receipt, expected in receipts:
        if receipt.exists() and load_json(receipt) != expected:
            raise ValueError("Previously reviewed raw history changed; keep the original response and record analyst decisions separately")
    for receipt, expected in receipts:
        if not receipt.exists():
            save_json(receipt, expected)


def review_history(args) -> int:
    from tracker_execution import preview_execution, registry_paths
    from tracker_registry import read_registry
    from tracker_workflow import (verified_result, response_file, digest_bytes, digest_object,
                                  load_run, run_root, save_json)

    completion, result = verified_result(args.run_id)
    if not completion["planning_application_allowed"]:
        raise ValueError("Read-only run cannot enter execution review")
    if load_run(args.run_id).get("collection_mode") != "adaptive":
        raise ValueError("Legacy runs are immutable; use their existing protocol")
    _, manifest_raw, manifest = response_file(args.manifest, args.run_id)
    if manifest.get("schema_version") != 1 or manifest.get("analyst_confirmed") is not True or not manifest.get("decision_source"):
        raise ValueError("Confirmed role/status mapping and decision source are required")
    project = Path(args.project_root).resolve()
    preview = preview_execution(project, manifest.get("quarter"), manifest.get("feature"), result,
                                manifest.get("reviewed_registries", {}), manifest.get("expected_head"))
    if not preview["ownership_ready"]:
        raise ValueError("Resolve execution-preview ownership blockers before history review")
    histories, evidence, limitations = {}, [], []
    participants = manifest["participants"]
    rules = StatusRules(**{name: frozenset(values) for name, values in manifest["status_rules"].items()})
    provider = manifest["provider"]
    if provider not in {"jira", "sbertrek"}:
        raise ValueError("Explicit history provider is required")
    if result["scope"].get("tracker_mode") == "single" and provider != result["scope"]["provider"]:
        raise ValueError("History provider must match the selected single tracker")
    if provider == "jira" and not load_run(args.run_id)["config"]["jira_enabled"]:
        raise ValueError("Jira is disabled for this run")
    known_roles = load_run(args.run_id)["config"].get("participants", {}).get(provider, {})
    for identity, role in participants.items():
        known = known_roles.get(identity)
        if isinstance(known, dict):
            known = known.get("role")
        if known and known != role:
            raise ValueError(f"Participant role contradicts the configured role: {identity}")
    for entry in manifest["responses"]:
        if entry["provider"] != provider:
            raise ValueError("Do not mix participant/status namespaces in one review")
        identity = (provider, entry["key"], entry["role"])
        if identity in histories:
            raise ValueError("Duplicate history response")
        targets = [target for item in preview["items"] for target in item["targets"]
                   if item.get(provider + "_key") == entry["key"] and target["role"] == entry["role"]]
        if len(targets) != 1 or entry["role"] not in {"FE", "BE"}:
            raise ValueError("History must map to exactly one confirmed FE/BE execution work item")
        validate_call(entry["call"], provider)
        observed = timestamp(datetime.fromisoformat(entry["call"]["captured_at"]))
        if observed > datetime.now(timezone.utc):
            raise ValueError("Capture time is in the future")
        source_path, raw, payload = response_file(entry["response_file"], args.run_id)
        if digest_bytes(raw) != entry["sha256"]:
            raise ValueError("History response checksum changed")
        pin_history_response(args.run_id, source_path, raw, {"provider": provider, "key": entry["key"], "call": entry["call"]})
        history, limits = decode_history(payload, entry, observed)
        history = replace(history, task_key=f"{entry['key']}/{entry['role']}")
        histories[identity] = (targets[0]["feature"], history)
        evidence.append({"key": entry["key"], "role": entry["role"], "sha256": entry["sha256"], "call": entry["call"],
                         "mapping": entry.get("mapping", JIRA_MAPPING), "status_aliases": entry.get("status_aliases", {})})
        limitations.extend(f"{entry['key']}:{limit}" for limit in limits)
    reviews, missing_history = [], []
    deleted_keys = {issue.get(provider + "_key") for issue in result["excluded"]
                    if issue.get("reason") == "confirmed-source-deletion"}
    for feature in preview["selected_features"]:
        expected, local_missing = set(), []
        for path in registry_paths(project):
            if path.relative_to(project).parts[1] != feature:
                continue
            tables, _ = read_registry(path)
            for row in (row for table in tables for row in table):
                if row.get("Role", "").upper() not in {"FE", "BE"}:
                    continue
                key = row.get("Jira" if provider == "jira" else "SberTrek", "").split("/")[0].strip()
                if key in deleted_keys:
                    continue
                if not key or key in {"-", "—"}:
                    local_missing.append(row.get("Task ID", "unmapped-work"))
                else:
                    expected.add(f"{key}/{row['Role'].upper()}")
        selected = tuple(history for owner, history in histories.values() if owner == feature)
        missing_history.extend(sorted(expected - {history.task_key for history in selected}))
        review = calculate_feature(feature, selected, participants, rules, tuple(sorted(expected)), not local_missing)
        review["limitations"].extend(f"unmapped-feature-work:{key}" for key in local_missing)
        reviews.append(review)
    unavailable = manifest.get("unavailable_history", {})
    if not isinstance(unavailable, dict) or any(
        key not in missing_history or not isinstance(reason, str) or not reason.strip()
        for key, reason in unavailable.items()
    ):
        raise ValueError("Unavailable history requires a reason for each missing work item")
    pending_history = sorted(set(missing_history) - set(unavailable))
    from tracker_comparison import build_comparison
    comparison = build_comparison(preview, reviews, provider) if not pending_history else None
    from tracker_qa_application import confirmed_qa_updates
    if pending_history and manifest.get('qa_confirmations'):
        raise ValueError('Collect or document unavailable history before QA application review')
    qa_application = confirmed_qa_updates(comparison, manifest.get('qa_confirmations', [])) if comparison else []
    rechecked = preview_execution(project, manifest.get("quarter"), manifest.get("feature"), result,
                                  manifest.get("reviewed_registries", {}), manifest.get("expected_head"))
    if rechecked != preview:
        raise ValueError("Execution sources changed during review")
    output = {"schema_version": 1, "run_id": args.run_id,
              "status": "history-collection-incomplete" if pending_history else "history-review-ready",
              "reconciled_sha256": completion["reconciled_sha256"], "manifest_sha256": digest_bytes(manifest_raw),
              "head": preview["head"], "registry_sha256": preview["registry_sha256"],
              "features": reviews, "evidence": evidence, "limitations": limitations,
              "status_rules": manifest["status_rules"],
              "feature_qa_proposals": preview["feature_qa_proposals"],
              "missing_task_candidates": preview["missing_task_candidates"],
              "deletion_proposals": [item for item in preview["items"]
                                     if item["proposed_action"] == "delete-current-execution"],
              "pending_history": pending_history, "unavailable_history": unavailable,
              "comparison": comparison,
              "project_root": str(project), "qa_application": qa_application,
              "date_proposals_allowed": not pending_history,
              "fact_priority": ["analyst-confirmation", "assignment-and-status-history", "current-state-only"],
              "history_processed": bool(histories), "adapter": "json-pointer-history-v1",
              "writes_performed": False, "planning_application_allowed": False,
              "next_action": {"type": "collect-history" if pending_history else "review-with-analyst",
                              "provider": provider, "application_requires_analyst_command": True}}
    destination = run_root(args.run_id) / "history" / (digest_object(output) + ".json")
    save_json(destination, output)
    payload = {**output, "review_file": str(destination)}
    if not pending_history:
        payload['after_registry_application'] = {
            'type': 'qa-application-check', 'required': True,
            'command': ['python3', str(Path(__file__).with_name('trackerctl.py')), 'qa-application-check',
                        '--project-root', str(project), '--review-file', str(destination)],
        }
        payload['before_registry_application'] = {
            'type': 'application-preflight', 'required': True,
            'features': preview['selected_features'],
            'command_template': ['python3', str(Path(__file__).with_name('trackerctl.py')),
                                 'application-preflight', '--project-root', str(project), '--feature', '<owning-feature>'],
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
