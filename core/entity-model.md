# Entity Model

## Baseline snapshot

Canonical deployed-system description.

Contains:
- domain model
- canonical requirements
- API contracts
- UI structure notes
- data model notes
- decisions and baseline version metadata

## Feature

Main knowledge container for a business change.

Contains:
- planning artifacts
- requirements
- prototypes
- execution tasks
- references to exchange revisions and returned development results
- domain impact note

## Planning story

Used for:
- scope shaping
- HLE discussion
- analyst anchor estimate split by `AN / FE / BE / QA`
- team estimate split by `AN / FE / BE / QA`
- agreed estimate split by `AN / FE / BE / QA`

Planning stories do not need to match implementation tasks 1:1.

Actualization states:
- `virtual`: the story is still the current execution unit.
- `mixed`: part of the story is covered by real implementation tasks, with a residual virtual scope still visible.
- `materialized`: the story is fully covered by real implementation tasks.
- `done`: the story is complete.

Mapping fields:
- `replaced_by`: implementation task ids that replace the planning story.
- `mapping_mode`: `explicit` when the user stated the replacement, `inferred` when LLM mapped it by feature/role/summary.
- `residual_virtual_tasks`: virtual execution items that remain visible on actual-progress.

## Exchange revision

Immutable developer input selected by a feature manifest.

Contains:
- one complete `requirements.md`;
- revision number, checksum and state in `manifest.json`;
- developer-owned `returns/` with the agreed task list, task results and final coverage.

## Developer task result

Developer-owned factual report linked directly to `REQ-*` and one agreed task.

It records existing behavior, implemented behavior, differences, remaining work, commits, paths and checks. The analyst may materialize the returned task into an implementation task for actual-progress, but the returned report and analyst planning artifact remain distinct.

## Implementation task

Analyst-side actual execution tracking artifact with fields such as:
- Jira key
- summary
- kind: `real` or `virtual`
- role: `AN`, `BE`, `FE`, or `QA`
- estimate
- executor/resource lane from `PROJECT_ROOT/planning/team.md`: canonical `A<N>`, `B<N>`, `F<N>`, `Q<N>`, or `TBD_A` / `TBD_B` / `TBD_F` / `TBD_Q`
- planned dates
- actual dates
- status
- progress %
- related/replaced planning stories
- optional description

Not-started implementation tasks have `Progress % = 0` and no actual dates. In generated actual-progress gantt views, not-started tasks are rendered no earlier than the current date marker, frontend tasks are delayed until backend/API work in the same feature has had a 3-open-day lead, and resources are capacity-scheduled from `PROJECT_ROOT/planning/team.md` at no more than 100% per open workday.

## Domain impact note

Feature-local DDD delta against the current baseline.

Contains:
- changed bounded contexts
- changed aggregates/entities/value objects
- changed business rules and invariants
- lifecycle impact
- promotion targets into the next baseline

## Release package

Finalized delivery snapshot for a concrete release.

Contains:
- final requirements
- final domain delta
- final UI/API/data deltas
- promotion checklist
- promoted baseline version note
