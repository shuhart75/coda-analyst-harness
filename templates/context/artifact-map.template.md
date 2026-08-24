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
| `requirements-exchange/<feature>/revisions/<NNN>/requirements.md` | Root requirements | Explicit developer transfer only |
| `delivery-prototype/*` | Confirmed root prototype + requirements | Explicit delivery-prototype update |

## Auxiliary Artifacts

| Artifact | Purpose | Source Of Truth? |
|---|---|---|
| `context-summary.md` | Small-window feature context | No |
| `.research/*` | Temporary or auditable research | No |
| `execution-context.md` | Current execution context | No, unless accepted into execution artifacts |

## Obsolete Or Deferred

| Artifact | Status | Action |
|---|---|---|
|  |  |  |
