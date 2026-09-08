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

`baseline/current/` describes the deployed system. A development report alone
does not prove deployment. Before promotion, identify the deployed version,
environment and evidence, record the release and review the analyst's decisions
from `features/<feature>/development-results-state.json`.

Promote only the deployed behavior. Preserve immutable input requirements and
returns. If deployed behavior differs from the requested behavior, describe that
fact and the known limitation without implying business acceptance; link the
analyst's acceptance or rejection and any follow-up. Undeployed code remains in
release preparation. A rejected deployed change may require rollback or repair,
but must not be hidden from the description of the current system.

Before promoting a release into `baseline/current/`:
- review every included feature's `domain-impact.md`;
- review `documents/planning/consistency-backlog.md`;
- block promotion if there are unresolved `domain-wide` items that affect released scope;
- either propagate or explicitly defer cross-feature items;
- write rollback notes for decisions that supersede or revert previous released decisions.
- check auxiliary `.research/`, exchange returns, implementation-plan and test-plan artifacts for findings that were accepted but not transferred into final requirements, release notes or baseline;
- update context summaries or mark them obsolete after baseline promotion when they would otherwise point at pre-release state.

## Forbidden without mode switch

- changing quarter or commander planning baselines silently
- changing historical execution facts without explicit instruction
- rewriting raw source materials under `context/source-materials/` or imported legacy folders
