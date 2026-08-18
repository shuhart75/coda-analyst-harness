# Domain Impact — <Feature Name>

Дата обновления: `<YYYY-MM-DD>`
Baseline target: `baseline/current/domain/`

## Decision registry

| Decision ID | Summary | Status | Consistency level | Source | Supersedes | Reverted by |
|---|---|---|---|---|---|---|
| DEC-YYYY-MM-DD-<FEATURE>-001 | <short decision> | proposed | local | <requirement/change request> |  |  |

## Status values
- `proposed` — решение сформулировано, но ещё не принято.
- `accepted` — решение принято и должно распространяться по артефактам.
- `deferred` — решение отложено.
- `reverted-before-release` — решение отменено до релиза.
- `released` — решение попало в release package.
- `rolled-back-after-release` — решение отменяется отдельным rollback/change после релиза.

## Consistency levels
- `local` — влияет только на текущую feature/slice.
- `cross-feature` — влияет на соседние feature requirements.
- `domain-wide` — влияет на baseline/current/domain, shared API, lifecycle, roles or business rules.

## Changed bounded contexts

## New or changed aggregates

## New or changed entities

## New or changed value objects

## New or changed domain events

## Business rules and invariants

## State transitions

## API and integration impact

## Affected requirements

| Path | Impact | Sync status |
|---|---|---|
| `features/<feature>/slices/<slice>/requirements/backend.md` | <what must change> | open |

## Affected baseline artifacts

| Path | Impact | Sync status |
|---|---|---|
| `baseline/current/domain/<file>.md` | <what must change> | open |

## Affected prototypes

### Scope prototypes
| Path | Impact | Sync status |
|---|---|---|
| `features/<feature>/planning/scope-prototype/prototype.html` | <what might be outdated> | defer-ok |

### Delivery prototypes
| Path | Impact | Sync status |
|---|---|---|
| `features/<feature>/slices/<slice>/delivery-prototype/prototype.html` | <what might be outdated> | defer-ok |

## Prototype sync status values
- `must-update-now` — prototype is an active handoff/scope artifact and must be updated.
- `defer-ok` — known drift is acceptable until user asks to actualize prototypes.
- `no-update-needed` — decision does not affect the prototype.
- `obsolete` — prototype should no longer be treated as current.

## Required consistency actions
- [ ] local feature requirements updated
- [ ] neighboring feature requirements updated or backlog item created
- [ ] domain impact reviewed by main agent
- [ ] baseline impact updated or backlog item created
- [ ] affected prototypes listed
- [ ] release package updated when applicable

## Rollback notes

### Before release
- Mark decision as `reverted-before-release`.
- Cancel related consistency backlog items.
- Revert already-propagated living requirements if needed.
- Do not touch baseline if the decision was not promoted.

### After release
- Create a new rollback/change feature or release item.
- Reference the original `Decision ID` in `Supersedes` or `Reverted by`.
- Promote the rollback as a normal baseline-changing release.

## Promotion targets
- `baseline/current/domain/`
- `baseline/current/api/`
- `baseline/current/requirements/`
