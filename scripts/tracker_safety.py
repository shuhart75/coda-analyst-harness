from __future__ import annotations

import json
from pathlib import Path
import re


def require_write_context(project, *, modes, feature=None) -> dict:
    from tracker_execution import git
    from tracker_workflow import load_json, state_root

    project = Path(project).resolve()
    workspace = load_json(state_root() / "workspace.json")
    configured = workspace.get("roles", {}).get("analytics", {}).get("path")
    if not configured or Path(configured).resolve() != project:
        raise ValueError("PROJECT_ROOT differs from configured analytics")
    if Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project:
        raise ValueError("PROJECT_ROOT must be the analytics repository root")
    state = load_json(state_root() / "collaboration.json")
    active = state.get("active_work") or {}
    branch = git(project, "branch", "--show-current").strip()
    permitted_features = (active.get('execution_scope') or {}).get('features', [active.get('feature')])
    if (state.get("mode") != "multi-user-branches" or not branch or branch in {"main", "master"}
            or active.get("branch") != branch or not active.get("feature")
            or (feature is not None and feature not in permitted_features)
            or active.get("status") != "active"):
        raise ValueError("Start or resume the owning feature through collaboration before any analytics write")
    mode_path = state_root() / "active-mode.md"
    mode = mode_path.read_text(encoding="utf-8") if mode_path.is_file() else ""
    configured_modes = re.findall(r"^mode:[ \t]*([^\r\n]+)", mode, re.MULTILINE)
    if len(configured_modes) != 1 or configured_modes[0].strip() not in modes:
        raise ValueError("Switch to " + " or ".join(modes) + " before any analytics write")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        location = Path(git(project, "rev-parse", "--git-path", name).strip())
        if (location if location.is_absolute() else project / location).exists():
            raise ValueError("Finish the pending Git operation before application")
    return {"branch": branch, "feature": active["feature"], "mode": configured_modes[0].strip()}


def application_preflight_command(args) -> int:
    from tracker_execution import git

    project = Path(args.project_root).resolve()
    context = require_write_context(project, modes=("execution-update",), feature=args.feature)
    print(json.dumps({"status": "tracker-application-preflight-ready", **context, "feature": args.feature,
                      "head": git(project, "rev-parse", "HEAD").strip(),
                      "writes_performed": False, "content_approved": False}, ensure_ascii=False, indent=2))
    return 0


def application_write_gate(project, features) -> dict:
    """Read-only diagnostics; never prevent history collection on main."""
    features = sorted(set(features))
    blockers = []
    if not features:
        blockers.append({"feature": None, "reason": "Resolve source ownership before any analytics write"})
    for feature in features:
        try:
            require_write_context(project, modes=("execution-update",), feature=feature)
        except (ValueError, OSError, KeyError) as exc:
            blockers.append({"feature": feature, "reason": str(exc)})
    return {"ready": not blockers, "blockers": blockers, "writes_performed": False,
            "required_before": ["registry-edit", "qa-partition-edit", "qa-remainder-registration", "association-edit"],
            "commands": [["python3", str(Path(__file__).with_name("trackerctl.py")), "application-preflight",
                          "--project-root", str(project), "--feature", feature] for feature in features],
            "recheck_before_each_write": True, "content_approved": False,
            "collection_may_continue": True}
