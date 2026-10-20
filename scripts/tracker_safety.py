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
    permitted_features = (active.get('execution_scope') or {}).get('features', [active.get('feature')])
    if (state.get("mode") != "multi-user-branches" or not branch or branch in {"main", "master"}
            or active.get("branch") != branch or args.feature not in permitted_features
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
