from __future__ import annotations

import os
from pathlib import Path


def harness_root() -> Path:
    return Path(__file__).resolve().parents[1]


def state_root() -> Path:
    override = os.environ.get("CODA_ANALYST_STATE_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else harness_root() / ".workspace-state"


def active_mode_path() -> Path:
    return state_root() / "active-mode.md"


def run_state_path() -> Path:
    return state_root() / "run-state"


def runs_path() -> Path:
    return state_root() / "runs"


def source_mirror_path(root: Path | None = None) -> Path:
    workspace = root.resolve() if root else harness_root()
    return workspace / ".workspace-state" / "repositories" / "changeswork-copy.git"


def retired_repositories_path(root: Path | None = None) -> Path:
    workspace = root.resolve() if root else harness_root()
    return workspace / ".workspace-state" / "retired-repositories"


def code_registry_path() -> Path:
    return harness_root() / "templates/workflow/code-repos.template.json"


def language_policy_path() -> Path:
    return harness_root() / "templates/workflow/language-policy.template.json"


def team_path(project: Path) -> Path:
    return project / "planning/team.md"


def consistency_backlog_path(project: Path) -> Path:
    return project / "planning/consistency-backlog.md"


def project_rules_path(project: Path) -> Path:
    return project / "context/project-rules"


def approved_plans_path(project: Path) -> Path:
    return project / "planning/approved-plans"


def eval_config_path(project: Path) -> Path:
    return project / "context/evals/golden-scenarios.json"


def ensure_local_state(default_mode: str = "requirements") -> None:
    state_root().mkdir(parents=True, exist_ok=True)
    run_state_path().mkdir(parents=True, exist_ok=True)
    runs_path().mkdir(parents=True, exist_ok=True)
    active = active_mode_path()
    if not active.exists():
        active.write_text(
            "# Active Mode\n\n"
            f"mode: {default_mode}\n\n"
            "## Mode File\n"
            f"modes/{default_mode}.md\n",
            encoding="utf-8",
        )
