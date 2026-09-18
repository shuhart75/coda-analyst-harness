from __future__ import annotations

import json
from pathlib import Path
import re


def application_preflight_command(args) -> int:
    from tracker_execution import git
    from tracker_workflow import load_json, state_root

    project = Path(args.project_root).resolve()
    workspace = load_json(state_root() / "workspace.json")
    configured = workspace.get("roles", {}).get("analytics", {}).get("path")
    if not configured or Path(configured).resolve() != project:
        raise ValueError("PROJECT_ROOT differs from configured analytics")
    if Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project:
        raise ValueError("PROJECT_ROOT must be the analytics repository root")
    state = load_json(state_root() / "collaboration.json")
    active = state.get("active_work") or {}
    branch = git(project, "branch", "--show-current").strip()
    if (state.get("mode") != "multi-user-branches" or not branch or branch in {"main", "master"}
            or active.get("branch") != branch or active.get("feature") != args.feature
            or active.get("status") != "active"):
        raise ValueError("Start or resume the owning feature through collaboration before any analytics write")
    mode = (state_root() / "active-mode.md").read_text(encoding="utf-8")
    if not re.search(r"^mode:\s*execution-update\s*$", mode, re.MULTILINE):
        raise ValueError("Switch to execution-update before applying tracker changes")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        location = Path(git(project, "rev-parse", "--git-path", name).strip())
        if (location if location.is_absolute() else project / location).exists():
            raise ValueError("Finish the pending Git operation before application")
    print(json.dumps({"status": "tracker-application-preflight-ready", "feature": args.feature,
                      "branch": branch, "head": git(project, "rev-parse", "HEAD").strip(),
                      "writes_performed": False, "content_approved": False}, ensure_ascii=False, indent=2))
    return 0


def identity_lookup_command(args) -> int:
    from tracker_history import validate_call
    from tracker_workflow import (load_config, config_status_payload, STOP_EXIT, unique_keys,
                                  tql_jira_keys, tql_units, load_json, full_issue_records,
                                  record_issue_key, attribute_entries, optional_value,
                                  SBER_ATTRIBUTE_CODES, object_text, normalize_key, digest_bytes)

    gate = config_status_payload(load_config())
    if gate.get("must_stop"):
        print(json.dumps(gate, ensure_ascii=False, indent=2))
        return STOP_EXIT
    keys = unique_keys(args.key)
    query = tql_jira_keys(keys) if args.source_provider == "jira" else tql_units(keys)
    output = {"status": "tracker-identity-lookup", "source_provider": args.source_provider,
              "requested_keys": keys, "provider": "sbertrek", "query_semantics": query,
              "purpose": "identity-only", "projection": ["key", "issue_key"],
              "facts_application_allowed": False, "writes_performed": False}
    if not args.response_file:
        if args.call_file:
            raise ValueError("A call file requires its full response")
        output["next_action"] = "read-identity-links"
    else:
        if not args.call_file:
            raise ValueError("Identity evidence requires the actual call")
        call = load_json(Path(args.call_file))
        validate_call(call, "sbertrek")
        if call.get("selection") != query:
            raise ValueError("Identity call selection differs from requested keys")
        path = Path(args.response_file).resolve()
        raw = path.read_bytes()
        records, _ = full_issue_records(json.loads(raw))
        pairs, seen_source, seen_target = [], {}, {}
        for record in records:
            sber = record_issue_key(record)
            attributes, _ = attribute_entries(record)
            value, state = optional_value(record, ("issue_key",), attributes, SBER_ATTRIBUTE_CODES["jira_key"])
            jira = normalize_key(object_text(value, ("key", "code", "value", "name"))) if state == "value" else None
            source = jira if args.source_provider == "jira" else sber
            target = sber if args.source_provider == "jira" else jira
            if not source or source not in keys:
                raise ValueError("Identity response contains an unrequested source key")
            if not target:
                continue
            if (source in seen_source and seen_source[source] != target) or (target in seen_target and seen_target[target] != source):
                raise ValueError("Ambiguous tracker identity; do not select a counterpart automatically")
            if source not in seen_source:
                pairs.append({"jira": jira, "sbertrek": sber})
            seen_source[source], seen_target[target] = target, source
        output.update(status="tracker-identities-reviewed", pairs=pairs,
                      unresolved=sorted(set(keys) - set(seen_source)),
                      response_file=str(path), sha256=digest_bytes(raw), call=call,
                      next_action="confirm-scope-with-resolved-keys")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0
