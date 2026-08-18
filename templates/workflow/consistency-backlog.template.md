# Consistency Backlog

This backlog tracks known cross-feature, domain-wide, baseline, and prototype consistency work.

## Status values
- `open` — known impact, not yet propagated.
- `in-progress` — propagation is underway.
- `propagated` — required artifacts were updated.
- `deferred` — consciously postponed.
- `cancelled` — source decision was cancelled before release.
- `rolled-back` — propagated change was reversed by later work.
- `rollback-propagation-required` — rollback happened and dependent artifacts still need sync.

## Consistency levels
- `local`
- `cross-feature`
- `domain-wide`

## Items

| ID | Decision ID | Source feature | Level | Status | Affected requirements | Affected baseline | Affected prototypes | Notes |
|---|---|---|---|---|---|---|---|---|
| CONS-YYYY-MM-DD-001 | DEC-YYYY-MM-DD-FEATURE-001 | `features/<feature>/` | cross-feature | open | `features/<other>/...` | `baseline/current/...` | `features/<feature>/.../prototype.html` | <short note> |
