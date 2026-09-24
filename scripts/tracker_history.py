from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path

from tracker_lifecycle import HistoryEvent, StatusRules, TaskHistory, calculate_feature, status_sets, timestamp


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


def pagination_window(payload: dict, mapping: dict, call: dict, count: int) -> tuple[int, int, int | None] | None:
    total = pointer(payload, mapping["total"]) if "total" in mapping else None
    start = pointer(payload, mapping["start"]) if "start" in mapping else None
    for name, value in (("total", total), ("start", start)):
        if name in mapping and (type(value) is not int or value < 0):
            raise ValueError("Invalid pagination metadata")
    if "request_start" in mapping:
        requested = pointer(call["arguments"], mapping["request_start"])
        if type(requested) is not int or requested < 0:
            raise ValueError("Invalid request pagination offset")
        if start is not None and start != requested:
            raise ValueError("Response pagination contradicts the recorded request")
        start = requested
    if "request_cursor" in mapping:
        cursor = pointer(call["arguments"], mapping["request_cursor"])
        if cursor in (None, ""):
            if start not in (None, 0):
                raise ValueError("Initial cursor contradicts response pagination")
            start = 0
    has_next = pointer(payload, mapping["has_next"]) if "has_next" in mapping else None
    if "has_next" in mapping and type(has_next) is not bool:
        raise ValueError("Pagination has_next must be a boolean")
    if start is None and total == count and has_next is False:
        start = 0
    if start is None:
        return None
    end = start + count
    if total is not None and (end > total or (has_next is not None and has_next != (end < total))):
        raise ValueError("Contradictory pagination metadata")
    if total is None and has_next is False:
        total = end
    return (start, end, total) if total is not None or has_next is not None else None


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


def decode_history(payload, entry: dict, observed: datetime, snapshot=None) -> tuple[TaskHistory, list[str]]:
    from tracker_workflow import digest_object

    mapping = entry.get("mapping", JIRA_MAPPING if entry["provider"] == "jira" else None)
    if not isinstance(mapping, dict):
        raise ValueError("History mapping is required: select events or text and a task key source")
    if "key" in mapping:
        key = pointer(payload, mapping["key"])
    elif "request_key" in mapping:
        key = pointer(entry["call"]["arguments"], mapping["request_key"])
    else:
        raise ValueError("History mapping requires key or request_key from the actual call")
    if key != entry["key"]:
        raise ValueError("History response belongs to another task")
    if "request_key" in mapping and pointer(entry["call"]["arguments"], mapping["request_key"]) != key:
        raise ValueError("History request belongs to another task")
    text_only = "text" in mapping
    if 'text_extraction' in mapping and not text_only:
        raise ValueError('Text extraction requires a selected source text')
    text_window = None
    extraction_limits = []
    if text_only:
        if "events" in mapping:
            raise ValueError("Choose history events or text, not both")
        text = pointer(payload, mapping["text"])
        if not isinstance(text, str) or not text.strip():
            raise ValueError("History text must be a nonempty verbatim response")
        events = []
        if 'text_extraction' in mapping:
            from tracker_history_extraction import decode_text_extraction
            if 'text_parser' in mapping or 'changes' in mapping:
                raise ValueError('Choose one text history extraction method')
            events, text_window, extraction_limits = decode_text_extraction(text, mapping['text_extraction'])
            mapping = {**mapping, 'at': '/at', 'field': '/field', 'from': '/before', 'to': '/after',
                       'assignment_field': 'assignment', 'status_field': 'status',
                       'assignment_from': '/before', 'assignment_to': '/after',
                       'status_from': '/before', 'status_to': '/after'}
        if "text_parser" in mapping:
            from tracker_history_text import decode_text_records
            events, text_window = decode_text_records(text, mapping)
            mapping = {**mapping, "at": "/at", "field": "/field", "from": "/before", "to": "/after"}
            if "changes" in mapping:
                raise ValueError('Text history records must not override changes')
    else:
        required = {"events", "field", "assignment_field", "status_field"}
        missing = sorted(required - mapping.keys())
        if missing:
            raise ValueError("History mapping is missing: " + ", ".join(missing))
        if mapping["assignment_field"] == mapping["status_field"]:
            raise ValueError("Assignment and status fields must differ")
        events = pointer(payload, mapping["events"])
        if not isinstance(events, list):
            raise ValueError("History events must be an array")
    aliases = entry.get("status_aliases", {})
    if not isinstance(aliases, dict) or any(not isinstance(value, str) for value in aliases.values()):
        raise ValueError("Status aliases must map source values to explicit codes")
    limits = list(extraction_limits)
    window = text_window if text_only else pagination_window(payload, mapping, entry.get("call", {}), len(events))
    complete = window == (0, len(events), len(events)) and not extraction_limits
    if not complete:
        limits.append("history-completeness-not-proven")
    if text_only and not {'text_parser', 'text_extraction'}.intersection(mapping):
        limits.append("history-text-requires-dated-source")
    normalized = []
    for event in events:
        changes = pointer(event, mapping["changes"]) if "changes" in mapping else [event]
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
            values = []
            for side in ("from", "to"):
                path = mapping.get(prefix + "_" + side, mapping.get(side))
                if path is None:
                    raise ValueError(f"History mapping requires {prefix}_{side} or {side}")
                if text_only and 'text_parser' in mapping and change['before' if side == 'from' else 'after'] is not None:
                    values.append(pointer(change, path))
                else:
                    values.append(optional(change, path))
            before, after = values
            if field == mapping["status_field"]:
                if before is None or after is None:
                    raise ValueError("Status transition requires both values")
                if any(not isinstance(value, (str, int)) or isinstance(value, bool) for value in (before, after)):
                    raise ValueError("Status transition requires scalar source values; map nested codes explicitly")
                before, after = aliases.get(str(before), str(before)), aliases.get(str(after), str(after))
            elif any(value is not None and not isinstance(value, str) for value in (before, after)):
                raise ValueError("Assignment identities must be strings or null")
            elif before is None and after is None:
                raise ValueError("Assignment change contains neither old nor new identity")
            fields[field] = (before, after)
        if fields:
            value = optional(event, mapping["at"]) if "at" in mapping else None
            if value in (None, ""):
                complete = False
                if "history-event-timestamps-not-returned" not in limits:
                    limits.append("history-event-timestamps-not-returned")
                continue
            try:
                at = timestamp(datetime.fromisoformat(value))
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid history timestamp at {mapping['at']}: {value!r}") from error
            if at > observed:
                raise ValueError("History event is newer than the captured snapshot")
            normalized.append(HistoryEvent(
                "source-sha256:" + digest_object(event), at,
                fields.get(mapping["assignment_field"]), fields.get(mapping["status_field"]),
            ))
    if {"history-event-timestamps-not-returned", "history-text-extraction-unresolved"}.intersection(limits):
        normalized = []
    normalized.sort(key=lambda event: event.at)
    snapshot_mapping = entry["snapshot"]["mapping"] if snapshot is not None else mapping
    snapshot_payload = snapshot if snapshot is not None else payload
    if not isinstance(snapshot_mapping, dict):
        raise ValueError("Snapshot mapping requires source paths")
    if snapshot is not None and pointer(snapshot_payload, snapshot_mapping["key"]) != key:
        raise ValueError("Snapshot response belongs to another task")
    if "status" not in snapshot_mapping:
        raise ValueError("A current snapshot status is required; use a separate snapshot response when needed")
    status = pointer(snapshot_payload, snapshot_mapping["status"])
    try:
        assignee = pointer(snapshot_payload, snapshot_mapping["assignee"])
    except (KeyError, ValueError):
        assignee = None
        complete = False
        limits.append("snapshot-assignee-not-returned")
    if not isinstance(status, (str, int)) or isinstance(status, bool):
        raise ValueError("Snapshot status requires a source value")
    if assignee is not None and (not isinstance(assignee, str) or not assignee):
        raise ValueError("Snapshot assignee must be an identity string or null")
    history = TaskHistory(key, entry["role"], observed, assignee,
                          aliases.get(str(status), str(status)), tuple(normalized), complete)
    return history, limits


def read_history_source(run_id: str, source: dict, provider: str, key: str, kind: str):
    from tracker_workflow import response_file, response_json, digest_bytes

    validate_call(source["call"], provider)
    source_format = source.get("format", "json")
    if source_format not in {"json", "text"}:
        raise ValueError("History source format must be json or text")
    path, raw, _ = response_file(source["response_file"], run_id, require_json=False)
    if digest_bytes(raw) != source["sha256"]:
        raise ValueError(f"History response checksum changed: {path}")
    pin_history_response(run_id, path, raw, {"provider": provider, "key": key, "call": source["call"]})
    if source_format == "json":
        payload = response_json(raw, path)
    else:
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"History text is not UTF-8: {path}, byte {error.start}") from error
    evidence = {"kind": kind, "path": str(path), "sha256": source["sha256"],
                "format": source_format, "call": source["call"], "mapping": source.get("mapping")}
    return payload, evidence


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
    if result["scope"]["kind"] == "release":
        from tracker_release import apply_scope_decisions, supplement_result
        result = apply_scope_decisions(project, result, manifest.get("release_scope_decisions"))
        result = supplement_result(args.run_id, project, result, manifest.get("supplemental_responses", []))
        result = apply_scope_decisions(project, result, manifest.get("release_scope_decisions"))
        result["scope"] = {**result["scope"], "release_decisions": manifest.get("release_decisions", {})}
        from tracker_release_evidence import review_membership
        result = review_membership(args.run_id, result, manifest.get("release_membership"))
    preview = preview_execution(project, manifest.get("quarter"), manifest.get("feature"), result,
                                manifest.get("reviewed_registries", {}), manifest.get("expected_head"))
    if not preview["ownership_ready"]:
        raise ValueError("Resolve execution-preview ownership blockers before history review")
    histories, evidence, limitations, undated_history = {}, [], [], []
    application_scope = preview.get("application_scope")
    member_keys = set(application_scope["member_keys"]) if application_scope else None
    reference_history = {f"{item.get(manifest['provider'] + '_key')}/{target['role']}"
                         for item in preview['items'] if item.get('proposed_action') == 'reference-only'
                         for target in item['targets'] if target['role'] in {'BE', 'FE'}}
    participants = manifest["participants"]
    rules = StatusRules(**{name: frozenset(values) for name, values in manifest["status_rules"].items()})
    status_sets(rules)
    provider = manifest["provider"]
    if provider not in {"jira", "sbertrek"}:
        raise ValueError("Explicit history provider is required")
    if result["scope"].get("tracker_mode") == "single" and provider != result["scope"]["provider"]:
        raise ValueError("History provider must match the selected single tracker")
    if provider == "jira" and not load_run(args.run_id)["config"]["jira_enabled"]:
        raise ValueError("Jira is disabled for this run")
    known_roles = load_run(args.run_id)["config"].get("participants", {}).get(provider, {})
    from tracker_history_text import validate_participants
    validate_participants(participants, known_roles)
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
        observed = timestamp(datetime.fromisoformat(entry["call"]["captured_at"]))
        payload, source_evidence = read_history_source(args.run_id, entry, provider, entry["key"], "history")
        sources = [source_evidence]
        snapshot = None
        if "snapshot" in entry:
            snapshot, snapshot_evidence = read_history_source(args.run_id, entry["snapshot"], provider, entry["key"], "snapshot")
            if snapshot is None:
                raise ValueError("Snapshot response cannot be null")
            sources.append(snapshot_evidence)
            observed = timestamp(datetime.fromisoformat(entry["snapshot"]["call"]["captured_at"]))
        history, limits = decode_history(payload, entry, observed, snapshot)
        if (member_keys is None or entry["key"] in member_keys) and {"history-text-requires-dated-source", "history-event-timestamps-not-returned", "history-text-extraction-unresolved"}.intersection(limits):
            undated_history.append(f"{entry['key']}/{entry['role']}")
        history = replace(history, task_key=f"{entry['key']}/{entry['role']}")
        histories[identity] = (targets[0]["feature"], history)
        evidence.append({"key": entry["key"], "role": entry["role"], "sha256": entry["sha256"], "call": entry["call"],
                         "mapping": entry.get("mapping", JIRA_MAPPING), "status_aliases": entry.get("status_aliases", {}),
                         "sources": sources, "limitations": limits})
        limitations.extend(f"{entry['key']}:{limit}" for limit in limits)
    reviews, missing_history, qa_blockers = [], [], []
    deleted_keys = {issue.get(provider + "_key") for issue in result["excluded"]
                    if issue.get("reason") == "confirmed-source-deletion"}
    skipped_keys = {issue.get(provider + "_key") for issue in result.get("skipped", [])}
    for feature in preview["selected_features"]:
        expected, local_missing = set(), []
        expected.update(f"{row[provider + '_key']}/{row['role']}"
                        for row in preview["proposed_registrations"] if row["feature"] == feature)
        for path in registry_paths(project):
            if path.relative_to(project).parts[1] != feature:
                continue
            tables, _ = read_registry(path)
            for row in (row for table in tables for row in table):
                if row.get("Role", "").upper() not in {"FE", "BE"}:
                    continue
                key = row.get("Jira" if provider == "jira" else "SberTrek", "").split("/")[0].strip()
                if member_keys is not None and key not in member_keys:
                    continue
                if key in deleted_keys | skipped_keys:
                    continue
                if not key or key in {"-", "—"}:
                    local_missing.append(row.get("Task ID", "unmapped-work"))
                else:
                    expected.add(f"{key}/{row['Role'].upper()}")
        selected = tuple(history for (source_provider, key, role), (owner, history) in histories.items()
                         if owner == feature and (member_keys is None or key in member_keys))
        missing_history.extend(sorted(expected - {history.task_key for history in selected}))
        review = calculate_feature(feature, selected, participants, rules, tuple(sorted(expected)), not local_missing)
        review["limitations"].extend(f"unmapped-feature-work:{key}" for key in local_missing)
        proposal = next(item for item in preview["feature_qa_proposals"] if item["feature"] == feature)
        partition = proposal.get("partition")
        if partition is not None:
            if not partition.get("ready"):
                qa_blockers.append({"feature": feature, "reason": partition["reason"]})
                review["limitations"].append("qa-partition-pending:" + partition["reason"])
            else:
                review["qa_groups"] = []
                for group in partition["groups"]:
                    if application_scope and group["release"] != application_scope["release"]:
                        review["qa_groups"].append({**group, "qa": None, "limitations": [],
                                                    "application_mode": "partition-only"})
                        continue
                    keys = tuple(group["history_keys"])
                    calculated = calculate_feature(feature, tuple(history for history in selected if history.task_key in keys),
                                                   participants, rules, keys, True)
                    review["qa_groups"].append({**group, "qa": calculated["qa"], "limitations": calculated["limitations"]})
        reviews.append(review)
    unavailable = manifest.get("unavailable_history", {})
    if not isinstance(unavailable, dict) or any(
        key not in set(missing_history) | reference_history
        or not isinstance(reason, str) or not reason.strip()
        for key, reason in unavailable.items()
    ):
        raise ValueError("Unavailable history requires a reason for each missing work item")
    date_sources_unavailable = manifest.get("unavailable_history_dates", {})
    if not isinstance(date_sources_unavailable, dict) or any(
        key not in set(undated_history) | reference_history
        or not isinstance(reason, str) or not reason.strip()
        for key, reason in date_sources_unavailable.items()
    ):
        raise ValueError("Unavailable history dates require a capability-check reason for each undated work item")
    pending_dates = sorted(set(undated_history) - set(date_sources_unavailable))
    pending_history = sorted((set(missing_history) - set(unavailable)) | set(pending_dates))
    from tracker_comparison import build_comparison
    comparison = build_comparison(preview, reviews, provider) if not pending_history else None
    from tracker_qa_application import confirmed_qa_updates
    if pending_history and manifest.get('qa_confirmations'):
        raise ValueError('Collect or document unavailable history before QA application review')
    if qa_blockers and manifest.get('qa_confirmations'):
        raise ValueError('Resolve QA partition blockers before approving QA application')
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
              "proposed_registrations": preview["proposed_registrations"],
              "qa_application_blockers": qa_blockers,
              "release_membership_evidence": result.get("release_membership_evidence"),
              "missing_task_candidates": preview["missing_task_candidates"],
              "deletion_proposals": [item for item in preview["items"]
                                     if item["proposed_action"] == "delete-current-execution"],
              "pending_history": pending_history, "unavailable_history": unavailable,
              "pending_history_dates": pending_dates, "unavailable_history_dates": date_sources_unavailable,
              "comparison": comparison,
              "project_root": str(project), "qa_application": qa_application,
              "application_scope": application_scope,
              "date_proposals_allowed": not pending_history,
              "fact_priority": ["analyst-confirmation", "assignment-and-status-history", "current-state-only"],
              "history_processed": bool(histories), "adapter": "source-mapped-history-v2",
              "writes_performed": False, "planning_application_allowed": False,
              "next_action": {"type": "collect-history" if pending_history else "review-with-analyst",
                              "keys": pending_history, "dated_source_required": pending_dates,
                              "contract": str(Path(__file__).resolve().parents[1] / "core/tracker-adaptive.md"),
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
