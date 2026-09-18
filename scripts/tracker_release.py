from __future__ import annotations

import copy
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path

from tracker_scope import inside_project


def release_identity(value: dict) -> str:
    return str(value.get("key") or value.get("name") or "")


def release_owners(project: Path, result: dict) -> dict:
    from tracker_execution import registry_paths
    from tracker_registry import read_registry

    provider = result["scope"]["provider"]
    column = "Jira" if provider == "jira" else "SberTrek"
    owners, epics = {}, {}
    for path in registry_paths(project):
        feature = path.relative_to(project).parts[1]
        tables, _ = read_registry(path)
        for row in (row for table in tables for row in table):
            key = row.get(column, "").split("/")[0].strip()
            if key and key not in {"-", "—"}:
                owners.setdefault(key, set()).add(feature)
    for path in sorted((project / "features").glob("*/execution/tracker-scope.json")):
        inside_project(project, path)
        config = json.loads(path.read_text(encoding="utf-8"))
        if config.get("schema_version") != 1:
            raise ValueError(f"Unknown epic association schema: {path}")
        for key in config.get("epics", {}).get(provider, []):
            epics.setdefault(key, set()).add(path.relative_to(project).parts[1])
    proposals = []
    for issue in result["issues"]:
        key = issue.get(provider + "_key")
        exact = owners.get(key, set())
        epic = (issue.get("epic") or {}).get("key")
        candidates = exact or epics.get(epic, set())
        proposals.append({"key": key, "summary": issue.get("summary"),
                          "features": sorted(candidates),
                          "basis": "registry" if exact else "associated-epic" if candidates else "analyst-required",
                          "registration_required": not bool(exact),
                          "question_required": len(candidates) != 1})
    return {"items": proposals, "selected_features": sorted({feature for item in proposals for feature in item["features"]}),
            "ownership_ready": all(not item["registration_required"] and not item["question_required"] for item in proposals)}


def release_preview_command(args) -> int:
    from tracker_workflow import verified_result
    from tracker_execution import preview_execution

    completion, result = verified_result(args.run_id)
    if result["scope"]["kind"] != "release" or not completion["planning_application_allowed"]:
        raise ValueError("Expected a release actualization run")
    project = Path(args.project_root).resolve()
    ownership = release_owners(project, result)
    output = {"status": "release-ownership-preview", **ownership, "skipped": result.get("skipped", []),
              "writes_performed": False, "next_action": {"type": "resolve-release-ownership"}}
    if ownership["ownership_ready"] and ownership["selected_features"]:
        output["execution"] = preview_execution(project, None, None, result)
        output["next_action"] = {"type": "collect-feature-remainder", "provider": result["scope"]["provider"],
                                 "keys": sorted({key for proposal in output["execution"]["feature_qa_proposals"]
                                                 for key in proposal["missing_card_keys"]}),
                                 "same_run": True, "then": "history-review"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def supplement_result(run_id: str, project: Path, result: dict, responses: list[dict]) -> dict:
    from tracker_workflow import (load_run, response_file, digest_bytes, full_issue_records,
                                  compact_issue, append_cards, reconcile_data, jql_keys, tql_units)
    from tracker_history import validate_call, pin_history_response
    from tracker_execution import preview_execution

    if not responses:
        return result
    preview = preview_execution(project, None, None, result)
    allowed = {key for proposal in preview["feature_qa_proposals"] for key in proposal["missing_card_keys"]}
    provider = result["scope"]["provider"]
    run = copy.deepcopy(load_run(run_id))
    for entry in responses:
        keys = entry["keys"]
        if not keys or len(keys) != len(set(keys)) or not set(keys) <= allowed:
            raise ValueError("Supplement must read only registered affected-feature remainder keys")
        call = entry["call"]
        validate_call(call, provider)
        if call.get("selection") != (jql_keys(keys) if provider == "jira" else tql_units(keys)):
            raise ValueError("Supplement call selection differs from the exact remainder keys")
        path, raw, payload = response_file(entry["response_file"], run_id)
        if digest_bytes(raw) != entry["sha256"]:
            raise ValueError("Supplement checksum changed")
        records, _ = full_issue_records(payload)
        cards = [compact_issue(record, provider) for record in records]
        if {card["key"] for card in cards} != set(keys) or len(cards) != len(keys):
            raise ValueError("Supplement must return every requested key exactly once")
        pin_history_response(run_id, path, raw, {"provider": provider, "keys": keys, "call": call})
        append_cards(run, provider, cards)
    supplemented = reconcile_data(run)
    release = result["scope"]["ids"][0]
    supplemented["release_member_keys"] = [issue.get(provider + "_key") for issue in supplemented["issues"]
                                           if any(release in {item.get("key"), item.get("name")}
                                                  for item in issue.get("releases", []))]
    return supplemented


def qa_groups(project: Path, feature: str, rows: list[dict], issues: list[dict], scope: dict,
              release_members: set[str] | None = None) -> dict | None:
    path = inside_project(project, project / "features" / feature / "execution/qa-groups.json")
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if not saved and scope.get("kind") != "release":
        return None
    provider = scope["provider"]
    field = provider + "_key"
    targets = [row for row in rows if row["feature"] == feature and row["role"] == "QA"]
    development = [row for row in rows if row["feature"] == feature and row["role"] in {"FE", "BE"}
                   and row["kind"] == "real"]
    by_key = {issue.get(field): issue for issue in issues}
    if any(not row.get(field) or row[field] not in by_key for row in development):
        return {"ready": False, "reason": "collect-all-feature-cards-before-partition", "path": path.relative_to(project).as_posix()}
    active = [row for row in development if by_key[row[field]].get("development", {}).get("state") != "excluded"]
    if len({row[field] for row in active}) != len(active):
        raise ValueError("One development task must have one role and one registry row")
    members = {row["task_id"] for row in active}
    if not members:
        return {"ready": False, "reason": "empty-qa-scope-needs-analyst", "path": path.relative_to(project).as_posix()}
    if saved:
        if saved.get("schema_version") != 1 or saved.get("provider") != provider:
            raise ValueError("QA group schema/provider requires explicit migration")
        groups = copy.deepcopy(saved["groups"])
        seen, identities = set(), set()
        for group in groups:
            current = set(group["members"])
            if not current or len(current) != len(group["members"]) or seen & current or group["task_id"] in identities:
                raise ValueError("QA groups must have unique IDs and disjoint nonempty members")
            seen |= current
            identities.add(group["task_id"])
            estimate = Decimal(str(group["estimate"]))
            if not estimate.is_finite() or estimate < 0:
                raise ValueError("QA group estimate must be finite and nonnegative")
        if seen != members:
            return {"ready": False, "reason": "qa-membership-changed-review-partition", "path": path.relative_to(project).as_posix(),
                    "added": sorted(members - seen), "removed": sorted(seen - members)}
        if {target["task_id"] for target in targets} != identities:
            raise ValueError("Every saved QA group must match exactly one QA registry row")
        total = Decimal(str(saved["total_estimate"]))
        if sum(Decimal(str(group["estimate"])) for group in groups) != total:
            raise ValueError("QA group estimates do not preserve the total")
    else:
        if len(targets) != 1:
            return {"ready": False, "reason": "confirm-one-base-qa-before-partition", "path": path.relative_to(project).as_posix()}
        value = targets[0]["saved_facts"].get("Estimate (дн)") or targets[0]["saved_facts"].get("Estimate")
        total = Decimal(str(value).replace(",", "."))
        groups = [{"task_id": targets[0]["task_id"], "members": sorted(members), "release": None, "estimate": str(total)}]
    if not total.is_finite() or total <= 0:
        raise ValueError("QA total must be a finite positive estimate")
    if scope.get("kind") == "release":
        release = scope["ids"][0]
        choices = scope.get("release_decisions", {})
        for row in active:
            releases = by_key[row[field]].get("releases", [])
            choice = choices.get(row[field])
            if len({release_identity(item) for item in releases}) > 1 or choice is not None:
                if not choice or choice not in {item.get("key") for item in releases} | {item.get("name") for item in releases}:
                    return {"ready": False, "reason": "confirm-sole-release-owner", "key": row[field],
                            "releases": releases, "path": path.relative_to(project).as_posix()}
        selected_keys = {key for key in (release_members or set()) if key not in choices or any(
            release in {item.get("key"), item.get("name")} and choices[key] in {item.get("key"), item.get("name")}
            for item in by_key.get(key, {}).get("releases", []))}
        selected = {row["task_id"] for row in active if row[field] in selected_keys}
        if not selected:
            return {"ready": False, "reason": "release-has-no-development-members", "path": path.relative_to(project).as_posix()}
        updated = []
        for group in groups:
            included = set(group["members"]) & selected
            remaining = set(group["members"]) - selected
            if group["release"] == release and remaining or included and group["release"] not in {None, release}:
                raise ValueError("Conflicting release membership; analyst must select the sole owning release")
            if included and remaining:
                amount = Decimal(str(group["estimate"]))
                portion = (amount * len(included) / len(group["members"])).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                if portion <= 0 or amount - portion <= 0:
                    raise ValueError("QA allocation precision is insufficient; review the estimate")
                suffix = hashlib.sha256((provider + ":" + release).encode()).hexdigest()[:10]
                updated.append({**group, "members": sorted(included), "release": release, "estimate": str(portion)})
                updated.append({**group, "task_id": group["task_id"] + "-REST-" + suffix,
                                "members": sorted(remaining), "release": None, "estimate": str(amount - portion)})
            else:
                updated.append({**group, "release": release if included else group["release"]})
        groups = updated
    target_by_id = {target["task_id"]: target for target in targets}
    registry = targets[0]["registry"]
    for group in groups:
        group["registry"] = target_by_id.get(group["task_id"], {}).get("registry", registry)
        group["history_keys"] = sorted(f"{row[field]}/{row['role']}" for row in active if row["task_id"] in group["members"])
    document = {"schema_version": 1, "provider": provider, "total_estimate": str(total),
                "groups": [{key: value for key, value in group.items() if key not in {"registry", "history_keys"}} for group in groups]}
    return {"ready": True, "path": path.relative_to(project).as_posix(), "document": document, "groups": groups,
            "previous_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
            "changed": document != saved, "requires_analyst_review": True}
