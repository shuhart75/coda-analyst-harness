#!/usr/bin/env python3
import json
from pathlib import Path
import sys

required = [
    ".workflow/llm-contract.md",
    ".workflow/agent-delegation.md",
    ".workflow/skills-policy.md",
    ".workflow/tooling-policy.md",
    ".workflow/context-policy.md",
    ".workflow/research-policy.md",
    ".workflow/code-inspection.md",
    ".workflow/run-loop.md",
    ".workflow/requirements-profile.md",
    ".workflow/harness.json",
    ".workflow/tools/switch-mode.sh",
    ".workflow/tools/start-session.sh",
    ".workflow/tools/validate-structure.py",
    ".workflow/tools/validate-links.py",
    ".workflow/tools/validate-context.py",
    ".workflow/tools/validate-workflow.py",
    ".workflow/tools/validate-planning.py",
    ".workflow/tools/validate-trace.py",
    ".workflow/tools/validate-language.py",
    ".workflow/tools/validate-requirements-profile.py",
    ".workflow/tools/validate-handoff.py",
    ".workflow/tools/handoffctl.py",
    ".workflow/tools/code-inspect.py",
    ".workflow/tools/harnessctl.py",
    ".workflow/tools/evaluate-harness.py",
    ".workflow/tools/sync-quarter-gantt.py",
    ".workflow/tools/sync-planning-gantt.py",
    ".workflow/tools/calibrate-planning.py",
    ".workflow/tools/sync-actual-progress-overlay.py",
    ".workflow/tools/find-stale-terms.py",
    ".workflow/tools/expand-plantuml-includes.py",
    ".workflow/command-catalog.md",
    ".workflow/command-cheatsheet.md",
    ".workflow/consistency-backlog.md",
    ".workflow/team.md",
    ".workflow/code-repos.json",
    ".workflow/language-policy.json",
    ".workflow/evals/golden-scenarios.json",
    ".workflow/active-mode.md",
    ".workflow/modes/planning.md",
    ".workflow/modes/requirements.md",
    ".workflow/modes/scope-prototype.md",
    ".workflow/modes/delivery-prototype.md",
    ".workflow/modes/execution-update.md",
    ".workflow/modes/release-finalization.md",
    ".workflow/templates/requirements/README.md",
    ".workflow/templates/requirements/feature-requirements.template.md",
    ".workflow/templates/requirements/slice.template.md",
    ".workflow/templates/requirements/frontend.template.md",
    ".workflow/templates/requirements/backend.template.md",
    ".workflow/templates/requirements/developer-task-index.template.md",
    ".workflow/templates/requirements/developer-task.template.md",
    ".workflow/templates/intake/README.md",
    ".workflow/templates/intake/feature-intake.template.md",
    ".workflow/templates/prototypes/README.md",
    ".workflow/templates/prototypes/prototype.html.template",
    ".workflow/templates/prototypes/feature-prototype-notes.template.md",
    ".workflow/templates/prototypes/scope-prototype-notes.template.md",
    ".workflow/templates/prototypes/delivery-prototype-notes.template.md",
    ".workflow/templates/context/feature-context-summary.template.md",
    ".workflow/templates/context/slice-context-summary.template.md",
    ".workflow/templates/context/artifact-map.template.md",
    ".workflow/templates/context/run-state.template.md",
    ".workflow/templates/research/research-summary.template.md",
    ".workflow/templates/research/frontend.template.yaml",
    ".workflow/templates/research/backend.template.yaml",
    ".workflow/templates/research/data.template.yaml",
    ".workflow/templates/research/integrations.template.yaml",
    ".workflow/templates/research/errors-validation.template.yaml",
    ".workflow/templates/research/roles-access.template.yaml",
    ".workflow/templates/research/observability-config.template.yaml",
    ".workflow/templates/research/code-evidence.template.yaml",
    ".workflow/templates/workspace/analyst-workspace-agents.template.md",
    ".workflow/templates/handoff/slice-implementation-handoff.template.md",
    ".workflow/templates/handoff/developer-request.template.md",
    ".workflow/templates/handoff/developer-package-readme.template.md",
    ".workflow/templates/handoff/developer-manifest.template.json",
    ".workflow/templates/handoff/developer-receipt.template.json",
    ".workflow/templates/handoff/analyst-receipt-review.template.json",
    ".workflow/templates/handoff/handoff-root.template.json",
    ".workflow/templates/handoff/handoff-root-readme.template.md",
    ".workflow/templates/handoff/handoff-root.feature.template.json",
    ".workflow/templates/handoff/handoff-root-feature-readme.template.md",
    ".workflow/templates/handoff/feature-package-readme.template.md",
    ".workflow/templates/handoff/feature-request.template.md",
    ".workflow/templates/handoff/feature-manifest.template.json",
    ".workflow/templates/handoff/development-tasks-instruction.template.md",
    ".workflow/templates/handoff/development-tasks-index.template.md",
    ".workflow/templates/handoff/development-task-card.template.md",
    ".workflow/templates/handoff/decomposition-receipt.template.json",
    ".workflow/templates/handoff/implementation-receipt.template.json",
    ".workflow/templates/handoff/test-receipt.template.json",
    ".workflow/templates/execution/implementation-plan.template.md",
    ".workflow/templates/execution/task-candidates.template.md",
    ".workflow/templates/planning/planning-context.template.md",
    ".workflow/templates/planning/assumptions.template.md",
    ".workflow/templates/planning/risk-register.template.md",
    ".workflow/templates/planning/story-map.template.md",
    ".workflow/templates/planning/quarter-plan-state.template.md",
    ".workflow/templates/planning/retrospective.template.md",
    ".workflow/templates/testing/slice-test-plan.template.md",
    ".workflow/templates/runs/run.template.json",
    ".workflow/skills/planning-loop/SKILL.md",
    ".workflow/skills/requirements-loop/SKILL.md",
    ".workflow/skills/implementation-loop/SKILL.md",
    ".workflow/skills/qa-loop/SKILL.md",
    "baseline/current/VERSION.md",
    "baseline/current/domain/aggregates.md",
    "baseline/current/domain",
    "baseline/current/requirements",
    "baseline/current/api",
    "baseline/current/ui",
    "baseline/current/data",
    "baseline/versions",
    "planning/intake",
    "releases",
    "context/source-materials/current-system/requirements",
    "context/source-materials/current-system/screenshots",
    "context/source-materials/change-requests",
    "features",
]

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
missing = [p for p in required if not (root / p).exists()]
if missing:
    print("Missing required paths:")
    for item in missing:
        print(f"- {item}")
    sys.exit(1)

registry_path = root / ".workflow/code-repos.json"
try:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"Invalid code repository registry: {exc}")
    sys.exit(1)

registry_errors = []
if registry.get("schema_version") != 2:
    registry_errors.append("schema_version must be 2")
repositories = registry.get("repositories")
if not isinstance(repositories, list):
    registry_errors.append("repositories must be an array")
else:
    identifiers = [item.get("id") for item in repositories if isinstance(item, dict)]
    if len(identifiers) != len(repositories) or len(identifiers) != len(set(identifiers)):
        registry_errors.append("repository ids must be present and unique")
    coda = next((item for item in repositories if isinstance(item, dict) and item.get("id") == "coda"), None)
    if not coda:
        registry_errors.append("coda repository entry is required")
    else:
        if coda.get("access") != "read-only":
            registry_errors.append("coda access must be read-only")
        location = coda.get("location")
        if not isinstance(location, dict) or not location.get("relative_to_documents"):
            registry_errors.append("coda relative location is required")
        contours = coda.get("contours")
        if not isinstance(contours, dict) or not all(contours.get(name, {}).get("path") for name in ("backend", "frontend")):
            registry_errors.append("coda backend and frontend contours are required")
if registry_errors:
    print("Invalid code repository registry:")
    for item in registry_errors:
        print(f"- {item}")
    sys.exit(1)
print("Structure OK")
