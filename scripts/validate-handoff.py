#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REQ_RE = re.compile(r"\bREQ-[A-Z0-9-]+\b")
SCN_RE = re.compile(r"\bSCN-[A-Z0-9-]+\b")
ADD_RE = re.compile(r"ADD-[A-Z0-9-]+")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")

PACKAGE_STATUSES = {
    "pending",
    "delivered",
    "delivered-with-deviations",
    "partially-delivered",
    "no-change-required",
    "not-delivered",
    "rejected-package",
}
COVERAGE_STATUSES = {
    "pending",
    "already-implemented",
    "implemented-as-required",
    "implemented-with-deviation",
    "implemented-with-scope-change",
    "partially-implemented",
    "not-implemented",
    "deferred",
    "blocked-dependency",
    "blocked-input-ambiguity",
    "not-applicable",
}
FOLLOW_UP_RECOMMENDATIONS = {
    "pending",
    "no-action",
    "promote-to-baseline",
    "update-requirement",
    "keep-open",
    "defer",
    "move-to-other-change",
    "cancel",
    "investigate",
}
DELIVERED_STATUSES = {
    "implemented-as-required",
    "implemented-with-deviation",
    "implemented-with-scope-change",
    "partially-implemented",
}
UNFINISHED_STATUSES = {
    "partially-implemented",
    "not-implemented",
    "deferred",
    "blocked-dependency",
    "blocked-input-ambiguity",
}
DEVIATION_STATUSES = {
    "implemented-with-deviation",
    "implemented-with-scope-change",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_ids(entries: Any, key: str, expected: list[str], label: str) -> list[str]:
    if not isinstance(entries, list):
        return [f"{label} must be an array"]
    actual = [entry.get(key) for entry in entries if isinstance(entry, dict)]
    errors: list[str] = []
    if len(actual) != len(entries) or any(not isinstance(value, str) for value in actual):
        errors.append(f"{label} contains an invalid {key}")
    if len(actual) != len(set(actual)):
        errors.append(f"{label} contains duplicate ids")
    if set(actual) != set(expected):
        errors.append(f"{label} does not exactly match manifest ids")
    return errors


def nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_evidence(entries: Any, label: str, final: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(entries, list):
        return [f"{label} evidence must be an array"]
    if final and not entries:
        errors.append(f"{label} has no evidence")
    for evidence in entries:
        if not isinstance(evidence, dict):
            errors.append(f"{label} evidence must be an object")
            continue
        if not nonempty_text(evidence.get("path")):
            errors.append(f"{label} evidence path is required")
        if not nonempty_text(evidence.get("observation")):
            errors.append(f"{label} evidence observation is required")
        if not nonempty_text(evidence.get("symbol")) and not isinstance(evidence.get("line"), int):
            errors.append(f"{label} evidence requires symbol or line")
    return errors


def validate_source(value: Any, label: str, final: bool) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} must be an object"]
    errors: list[str] = []
    if final and not COMMIT_RE.fullmatch(str(value.get("commit", ""))):
        errors.append(f"{label}.commit must be a full 40-character hash")
    if final and not nonempty_text(value.get("branch")):
        errors.append(f"{label}.branch is required")
    if value.get("working_tree_state") not in {"unknown", "clean", "dirty"}:
        errors.append(f"{label}.working_tree_state is invalid")
    if final and value.get("working_tree_state") == "unknown":
        errors.append(f"{label}.working_tree_state cannot be unknown in a final receipt")
    paths = value.get("relevant_uncommitted_paths")
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        errors.append(f"{label}.relevant_uncommitted_paths must be an array of paths")
    return errors


def validate_coverage_entry(entry: dict[str, Any], item_id: str, final: bool) -> list[str]:
    errors: list[str] = []
    status = entry.get("status")
    recommendation = entry.get("follow_up_recommendation")
    if status not in COVERAGE_STATUSES:
        errors.append(f"{item_id}: invalid coverage status {status!r}")
    if recommendation not in FOLLOW_UP_RECOMMENDATIONS:
        errors.append(f"{item_id}: invalid follow_up_recommendation {recommendation!r}")
    if final and "pending" in {status, recommendation}:
        errors.append(f"{item_id}: final receipt contains pending values")
    if final and status != "not-applicable" and not nonempty_text(entry.get("behavior_before")):
        errors.append(f"{item_id}: behavior_before is required")
    if final and status in DELIVERED_STATUSES and not nonempty_text(entry.get("delivered_behavior")):
        errors.append(f"{item_id}: delivered_behavior is required for {status}")
    if final and status in DEVIATION_STATUSES and not nonempty_text(entry.get("deviation_from_input")):
        errors.append(f"{item_id}: deviation_from_input is required for {status}")
    if final and status in UNFINISHED_STATUSES and not nonempty_text(entry.get("remaining_work")):
        errors.append(f"{item_id}: remaining_work is required for {status}")
    commits = entry.get("commit_sha256")
    if not isinstance(commits, list) or any(not COMMIT_RE.fullmatch(str(commit)) for commit in commits):
        errors.append(f"{item_id}: commit_sha256 must contain full commit hashes")
    if final and status in DELIVERED_STATUSES - {"partially-implemented"} and isinstance(commits, list) and not commits:
        errors.append(f"{item_id}: {status} requires at least one delivery commit; use already-implemented for pre-existing behavior")
    verification = entry.get("verification")
    if not isinstance(verification, list):
        errors.append(f"{item_id}: verification must be an array")
    errors.extend(validate_evidence(entry.get("evidence"), item_id, final))
    return errors


def validate(package: Path, receipt_name: str) -> list[str]:
    errors: list[str] = []
    manifest_path = package / "manifest.json"
    request_path = package / "request.md"
    receipt_path = package / receipt_name
    manifest = load_json(manifest_path)
    receipt = load_json(receipt_path)

    if manifest.get("schema_version") != 6:
        errors.append("manifest schema_version must be 6")
    policy = manifest.get("delivery_policy", {})
    if policy.get("input") != "immutable-comparison-point":
        errors.append("delivery_policy.input must be immutable-comparison-point")
    if policy.get("feedback") != "receipt-then-analyst-review":
        errors.append("delivery_policy.feedback must be receipt-then-analyst-review")
    if set(manifest.get("allowed_package_statuses", [])) != PACKAGE_STATUSES:
        errors.append("manifest package statuses do not match the canonical protocol")
    if set(manifest.get("allowed_coverage_statuses", [])) != COVERAGE_STATUSES:
        errors.append("manifest coverage statuses do not match the canonical protocol")
    if set(manifest.get("allowed_follow_up_recommendations", [])) != FOLLOW_UP_RECOMMENDATIONS:
        errors.append("manifest follow-up recommendations do not match the canonical protocol")

    payload_paths: set[str] = set()
    for item in manifest.get("payload", []):
        if not isinstance(item, dict):
            errors.append("manifest payload entry must be an object")
            continue
        relative = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            errors.append(f"invalid payload path: {relative!r}")
            continue
        if relative in payload_paths:
            errors.append(f"duplicate payload path: {relative}")
            continue
        payload_paths.add(relative)
        payload_file = package / relative
        if not payload_file.is_file():
            errors.append(f"missing payload file: {relative}")
        elif sha256(payload_file) != expected_hash:
            errors.append(f"payload checksum mismatch: {relative}")

    request_text = request_path.read_text(encoding="utf-8")
    requirements = manifest.get("requirements", [])
    scenarios = manifest.get("scenarios", [])
    if set(REQ_RE.findall(request_text)) != set(requirements):
        errors.append("request requirement ids do not match manifest")
    if set(SCN_RE.findall(request_text)) != set(scenarios):
        errors.append("request scenario ids do not match manifest")
    scenario_map = manifest.get("scenario_requirement_map", {})
    if set(scenario_map) != set(scenarios):
        errors.append("scenario_requirement_map does not exactly cover manifest scenarios")
    for scenario, covered in scenario_map.items():
        if not isinstance(covered, list) or not covered or not set(covered) <= set(requirements):
            errors.append(f"invalid scenario_requirement_map entry: {scenario}")

    if receipt.get("schema_version") != 4:
        errors.append("receipt schema_version must be 4")
    for key in ("package_id", "package_revision"):
        if receipt.get(key) != manifest.get(key):
            errors.append(f"receipt {key} does not match manifest")
    if receipt.get("request_id") != manifest.get("request", {}).get("id"):
        errors.append("receipt request_id does not match manifest")
    if receipt.get("request_version") != manifest.get("request", {}).get("version"):
        errors.append("receipt request_version does not match manifest")
    if receipt.get("request_sha256") != sha256(request_path):
        errors.append("receipt request_sha256 does not match request.md")
    if receipt.get("target_repository") != manifest.get("target", {}).get("repository"):
        errors.append("receipt target_repository does not match manifest")
    if receipt.get("target_contour") != manifest.get("target", {}).get("contour"):
        errors.append("receipt target_contour does not match manifest")
    if set(receipt.get("allowed_statuses", [])) != PACKAGE_STATUSES:
        errors.append("receipt package statuses do not match the canonical protocol")
    if set(receipt.get("allowed_coverage_statuses", [])) != COVERAGE_STATUSES:
        errors.append("receipt coverage statuses do not match the canonical protocol")
    if set(receipt.get("allowed_follow_up_recommendations", [])) != FOLLOW_UP_RECOMMENDATIONS:
        errors.append("receipt follow-up recommendations do not match the canonical protocol")

    package_status = receipt.get("status")
    if package_status not in PACKAGE_STATUSES:
        errors.append(f"invalid receipt status: {package_status}")
    final = package_status != "pending"
    if final and package_status != "rejected-package":
        if not nonempty_text(receipt.get("received_at")) or not nonempty_text(receipt.get("completed_at")):
            errors.append("final receipt requires received_at and completed_at")
        errors.extend(validate_source(receipt.get("source_before"), "source_before", True))
        errors.extend(validate_source(receipt.get("source_after"), "source_after", True))

    requirement_entries = receipt.get("requirement_coverage", [])
    scenario_entries = receipt.get("scenario_coverage", [])
    errors.extend(exact_ids(requirement_entries, "requirement", requirements, "requirement_coverage"))
    errors.extend(exact_ids(scenario_entries, "scenario", scenarios, "scenario_coverage"))
    coverage_entries = [
        entry for entry in requirement_entries + scenario_entries if isinstance(entry, dict)
    ] if isinstance(requirement_entries, list) and isinstance(scenario_entries, list) else []
    for entry in coverage_entries:
        item_id = entry.get("requirement") or entry.get("scenario") or "?"
        if "scenario" in entry and entry.get("covered_by") != scenario_map.get(item_id):
            errors.append(f"{item_id}: covered_by does not exactly match manifest")
        errors.extend(validate_coverage_entry(entry, item_id, final and package_status != "rejected-package"))

    additional = receipt.get("additional_deliveries")
    if not isinstance(additional, list):
        errors.append("receipt additional_deliveries must be an array")
        additional = []
    additional_ids = [item.get("id") for item in additional if isinstance(item, dict)]
    if len(additional_ids) != len(additional) or len(additional_ids) != len(set(additional_ids)):
        errors.append("additional_deliveries contains invalid or duplicate ids")
    for item in additional:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not ADD_RE.fullmatch(item_id):
            errors.append(f"invalid additional delivery id: {item_id!r}")
        if final and package_status != "rejected-package":
            for field in ("title", "reason", "delivered_behavior"):
                if not nonempty_text(item.get(field)):
                    errors.append(f"{item_id}: {field} is required")
            if item.get("follow_up_recommendation") not in FOLLOW_UP_RECOMMENDATIONS - {"pending"}:
                errors.append(f"{item_id}: invalid follow_up_recommendation")
            item_commits = item.get("commit_sha256")
            if not isinstance(item_commits, list) or any(not COMMIT_RE.fullmatch(str(commit)) for commit in item_commits):
                errors.append(f"{item_id}: commit_sha256 must contain full commit hashes")
            if not isinstance(item.get("verification"), list):
                errors.append(f"{item_id}: verification must be an array")
            errors.extend(validate_evidence(item.get("evidence"), item_id or "ADD", True))

    commits = receipt.get("commits")
    if not isinstance(commits, list):
        errors.append("receipt commits must be an array")
        commits = []
    known_ids = set(requirements) | set(scenarios) | set(additional_ids)
    for commit in commits:
        if not isinstance(commit, dict) or not COMMIT_RE.fullmatch(str(commit.get("sha256", ""))):
            errors.append("receipt commit must contain a full sha256")
            continue
        if not nonempty_text(commit.get("summary")):
            errors.append(f"commit {commit.get('sha256')}: summary is required")
        item_ids = commit.get("item_ids")
        if not isinstance(item_ids, list) or not set(item_ids) <= known_ids:
            errors.append(f"commit {commit.get('sha256')}: item_ids contain unknown ids")
    declared_commits = {commit.get("sha256") for commit in commits if isinstance(commit, dict)}
    for entry in coverage_entries + [item for item in additional if isinstance(item, dict)]:
        item_id = entry.get("requirement") or entry.get("scenario") or entry.get("id") or "?"
        referenced_commits = entry.get("commit_sha256", [])
        if isinstance(referenced_commits, list) and not set(referenced_commits) <= declared_commits:
            errors.append(f"{item_id}: commit_sha256 references an undeclared commit")

    for key in (
        "generated_or_changed_artifacts",
        "remaining_work",
        "baseline_feedback",
        "requirements_feedback",
        "verification",
    ):
        if not isinstance(receipt.get(key), list):
            errors.append(f"receipt {key} must be an array")

    if final and package_status not in {"rejected-package", "no-change-required", "not-delivered"} and not commits:
        errors.append("delivered receipt must list at least one commit")

    if final and package_status != "rejected-package" and coverage_entries:
        statuses = {entry.get("status") for entry in coverage_entries}
        has_delivered = any(entry.get("commit_sha256") for entry in coverage_entries) or bool(additional)
        has_unfinished = bool(statuses & UNFINISHED_STATUSES)
        has_deviation = bool(statuses & DEVIATION_STATUSES) or bool(additional)
        all_no_change = statuses <= {"already-implemented", "not-applicable"}
        expected_status = (
            "no-change-required" if all_no_change and not additional
            else "partially-delivered" if has_delivered and has_unfinished
            else "delivered-with-deviations" if has_delivered and has_deviation
            else "delivered" if has_delivered
            else "not-delivered"
        )
        if package_status != expected_status:
            errors.append(f"receipt status must be {expected_status} based on item coverage")
        remaining = receipt.get("remaining_work")
        if has_unfinished and isinstance(remaining, list) and not remaining:
            errors.append("receipt remaining_work is empty while input items remain unfinished")
        if not has_unfinished and isinstance(remaining, list) and remaining:
            errors.append("receipt remaining_work is filled although no input item remains unfinished")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an analyst-to-development handoff package")
    parser.add_argument("package", type=Path)
    parser.add_argument("--receipt", default="receipt.template.json")
    args = parser.parse_args()
    try:
        errors = validate(args.package.resolve(), args.receipt)
    except (OSError, ValueError) as exc:
        print(f"Handoff validation failed: {exc}")
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Handoff package OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
