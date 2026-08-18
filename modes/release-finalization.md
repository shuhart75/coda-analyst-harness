# Mode: release-finalization

## Goal

Собрать итоговые требования по релизу, зафиксировать фактическую доменную модель после внедрения и промоутить результат в новый baseline.

## Main artifacts

- `releases/*/README.md`
- `releases/*/*/release.md`
- `releases/*/*/final-requirements/*`
- `releases/*/*/final-domain-delta.md`
- `releases/*/*/final-ui-delta.md`
- `releases/*/*/final-api-delta.md`
- `releases/*/*/promotion-checklist.md`
- `releases/*/*/promoted-baseline-version.md`
- `baseline/current/**`
- `baseline/versions/**`
- `features/*/domain-impact.md`
- `features/*/context-summary.md` and `features/*/artifact-map.md` when needed for release traceability

## Allowed changes

- release package contents
- final requirements after delivery
- baseline promotion notes and version metadata
- canonical domain model, API, UI and data-model baseline files
- baseline snapshots under `baseline/versions/`
- feature deployment status notes tied to promotion


## Consistency gate

Before promoting a release into `baseline/current/`:
- review every included feature's `domain-impact.md`;
- review `.workflow/consistency-backlog.md`;
- block promotion if there are unresolved `domain-wide` items that affect released scope;
- either propagate or explicitly defer cross-feature items;
- write rollback notes for decisions that supersede or revert previous released decisions.
- check auxiliary `.research/`, handoff, implementation-plan and test-plan artifacts for findings that were accepted but not transferred into final requirements, release notes or baseline;
- update context summaries or mark them obsolete after baseline promotion when they would otherwise point at pre-release state.

## Forbidden without mode switch

- changing quarter or commander planning baselines silently
- changing historical execution facts without explicit instruction
- rewriting raw source materials under `context/source-materials/` or imported legacy folders
