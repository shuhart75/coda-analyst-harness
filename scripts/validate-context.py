#!/usr/bin/env python3
from pathlib import Path
import sys


args = [arg for arg in sys.argv[1:] if arg not in {"--strict-features", "--warnings-as-errors"}]
strict_features = "--strict-features" in sys.argv[1:]
warnings_as_errors = "--warnings-as-errors" in sys.argv[1:]
ROOT = Path(args[0]) if args else Path(".")

required_workflow = [
    ".workflow/context-policy.md",
    ".workflow/research-policy.md",
    ".workflow/templates/context/feature-context-summary.template.md",
    ".workflow/templates/context/slice-context-summary.template.md",
    ".workflow/templates/context/artifact-map.template.md",
    ".workflow/templates/context/run-state.template.md",
    ".workflow/templates/research/research-summary.template.md",
    ".workflow/templates/handoff/slice-implementation-handoff.template.md",
    ".workflow/templates/execution/implementation-plan.template.md",
    ".workflow/templates/execution/task-candidates.template.md",
    ".workflow/templates/requirements/developer-task-index.template.md",
    ".workflow/templates/requirements/developer-task.template.md",
    ".workflow/templates/testing/slice-test-plan.template.md",
    ".workflow/templates/runs/run.template.json",
]

required_research_templates = [
    "frontend.template.yaml",
    "backend.template.yaml",
    "data.template.yaml",
    "integrations.template.yaml",
    "errors-validation.template.yaml",
    "roles-access.template.yaml",
    "observability-config.template.yaml",
]

missing = []
warnings = []

for item in required_workflow:
    if not (ROOT / item).exists():
        missing.append(item)

research_dir = ROOT / ".workflow/templates/research"
for item in required_research_templates:
    if not (research_dir / item).exists():
        missing.append(str(Path(".workflow/templates/research") / item))

features_dir = ROOT / "features"
if strict_features and features_dir.exists():
    for feature_dir in sorted(p for p in features_dir.iterdir() if p.is_dir()):
        req = feature_dir / "requirements.md"
        slices = feature_dir / "slices"
        context_summary = feature_dir / "context-summary.md"
        artifact_map = feature_dir / "artifact-map.md"

        if req.exists() and not context_summary.exists():
            warnings.append(f"{context_summary.relative_to(ROOT)} missing for feature with requirements.md")
        if req.exists() and not artifact_map.exists():
            warnings.append(f"{artifact_map.relative_to(ROOT)} missing for feature with requirements.md")

        planning = feature_dir / "planning"
        if planning.exists() and any(planning.glob("stories/*.md")):
            planning_context = planning / "planning-context.md"
            if not planning_context.exists():
                warnings.append(f"{planning_context.relative_to(ROOT)} missing for feature with planning stories")

        if slices.exists():
            for slice_dir in sorted(p for p in slices.iterdir() if p.is_dir()):
                slice_card = slice_dir / "slice.md"
                if slice_card.exists() and not (slice_dir / "context-summary.md").exists():
                    warnings.append(f"{(slice_dir / 'context-summary.md').relative_to(ROOT)} missing for slice")

                research = slice_dir / ".research"
                if research.exists() and not (research / "summary.md").exists():
                    warnings.append(f"{(research / 'summary.md').relative_to(ROOT)} missing for existing research dir")

                plan = slice_dir / "execution/implementation-plan.md"
                if plan.exists():
                    text = plan.read_text(encoding="utf-8", errors="ignore")
                    if "Source Requirement" not in text and "Исходное требование" not in text:
                        warnings.append(f"{plan.relative_to(ROOT)} has no requirement reference column")

                test_plan = slice_dir / "testing/test-plan.md"
                if test_plan.exists():
                    text = test_plan.read_text(encoding="utf-8", errors="ignore")
                    if "Coverage Matrix" not in text and "Матрица покрытия" not in text:
                        warnings.append(f"{test_plan.relative_to(ROOT)} has no coverage matrix section")

if missing:
    print("Missing context workflow files:")
    for item in missing:
        print(f"- {item}")
    sys.exit(1)

if warnings:
    print("Context warnings:")
    for item in warnings[:200]:
        print(f"- {item}")
    if warnings_as_errors:
        sys.exit(1)
else:
    print("Context OK")
