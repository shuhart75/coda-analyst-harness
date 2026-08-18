# Artifact Map — <feature-slug>

Status: draft
Updated: <YYYY-MM-DD>

## Authoritative Artifacts

| Artifact | Role |
|---|---|
| `features/<feature-slug>/requirements.md` | Primary authored requirements |
| `features/<feature-slug>/feature.md` | Feature planning/control card |
| `features/<feature-slug>/domain-impact.md` | Impact and consistency tracking |

## Derived Artifacts

| Artifact | Derived From | Refresh Trigger |
|---|---|---|
| `slices/*/slice.md` | Root requirements | Slice scope changes |
| `slices/*/requirements/frontend.md` | Root requirements + prototype | UI behavior changes |
| `slices/*/requirements/backend.md` | Root requirements | API/data/integration changes |
| `slices/*/delivery-prototype/*` | Confirmed root prototype + requirements | Prototype handoff changes |

## Auxiliary Artifacts

| Artifact | Purpose | Source Of Truth? |
|---|---|---|
| `context-summary.md` | Small-window feature context | No |
| `slices/*/context-summary.md` | Small-window slice context | No |
| `.research/*` | Temporary or auditable research | No |
| `implementation-handoff.md` | Development handoff | No, unless referenced by execution |
| `testing/test-plan.md` | QA draft/check plan | No, unless accepted by QA process |

## Obsolete Or Deferred

| Artifact | Status | Action |
|---|---|---|
|  |  |  |
