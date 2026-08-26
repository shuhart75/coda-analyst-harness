# Mode: execution-update

## Goal

Track real work after planning.

## Main artifacts

- `features/*/execution/tasks.md`
- `features/*/execution/tasks/*.md`
- `features/*/execution/task-candidates.md`
- `features/*/planning/actualization.md`
- `features/*/execution-context.md`
- read-only developer results from `requirements-exchange/*/revisions/*/returns/`
- `documents/planning/team.md`
- `planning/*/gantt/actual-progress.puml`
- `planning/*/gantt/actual-progress-confluence.puml`

## Allowed changes

- task status
- task kind and progress %
- executor
- planned dates
- actual dates
- description and notes
- mapping `planning story -> implementation task`
- actualization state: `virtual` / `mixed` / `materialized`
- milestones and factual notes in actual-progress gantt
- execution context notes that explain current fact state and plan-vs-fact decisions
- confirmed or proposed task candidates discovered after planning approval
- agreed developer task lists received through the exchange catalog
- optional materialization of selected returned tasks into task candidates or actual execution tasks

When a returned developer task is materialized into real work, keep `returns/tasks.md` as the factual source. The analyst may use or replace optional developer estimates and may link Jira keys, but does not edit the returned decomposition or block development.

## Read-only tracker evidence

Reading SberTrek and Jira follows `core/tracker-reading.md` and does not itself
switch mode or authorize changes to execution artifacts. SberTrek is primary; Jira
fills only missing values and contributes history. The read report groups the same
tasks independently by epic and by release. Applying selected facts to
actual-progress is a separate, not-yet-authorized operation.

## Actual-progress scheduling rules

- Do not hand-edit generated actual-progress PlantUML for task dates. Update execution markdown, then regenerate the gantt.
- Not-started execution tasks are tasks with `Progress % = 0` and no actual dates. They may be `real` or `virtual`.
- Not-started tasks must not render before the current date marker. On each regeneration, if their planned date is stale, the generator shifts only the rendered bar to today or the next open day.
- Inside a feature, not-started backend/API tasks lead frontend tasks. Frontend bars start no earlier than 3 open days after the earliest not-started backend/API bar in the same feature.
- Not-started tasks must be capacity-scheduled by `documents/planning/team.md`: no resource lane above 100% on an open workday, and available resources should be used before pushing work later.
- If executor is empty, `TBD_*`, or a non-roster lane, let the generator auto-assign by role/task prefix/summary. Valid explicit lanes such as `B2` are preserved, with dates shifted if needed to avoid overload.
- Actual started or completed tasks keep their actual dates, even when those dates are in the past.
- Keep `PLAN ...` story bars visible; they are the commander-plan baseline, not a replacement for the execution task layer.
- Never rewrite approved quarter or commander plans to absorb later scope. Render later work as task candidates or actual tasks.
- Regenerate actual-progress through `scripts/sync-quarter-gantt.py`; it also refreshes `actual-progress-confluence.puml`.

## Small-context execution rules

Execution updates must keep enough fact context for a small-window LLM to continue safely.

For `обнови реальный прогресс`, `обновляем прогресс`, `задача X завершена`, `задачу X взял Y`, `добавь реальные задачи вместо story X`, and `сравни план и факт`, automatically:

- collect current planning stories, `actualization.md`, execution tasks and `documents/planning/team.md`;
- preserve commander/quarter plan story bars as the baseline for plan-vs-fact comparison;
- update story-to-task mapping in markdown, not only in generated PlantUML;
- avoid duplicating a real task that maps to multiple stories;
- refresh `features/<feature>/execution-context.md` or the relevant checkpoint when fact state changes materially;
- regenerate actual-progress and standalone Confluence export after gantt-related updates.

Do not change quarter-plan or commander-plan baselines while only updating real progress. Switch mode or ask for explicit confirmation if the requested fact update implies a planning baseline change.

## Resource rules

- Prefer canonical executor/resource lanes from `documents/planning/team.md`: `A1-A3`, `B1-B3`, `F1-F2`, `Q1-Q3`.
- Use `TBD_A`, `TBD_B`, `TBD_F`, `TBD_Q` when the role is known but the person/resource is not assigned yet.
- Accepted input aliases are normalized on render:
  - analyst: `A`, `AN`, `analyst`, `аналитик`;
  - backend/API: `B`, `BE`, `back`, `backend`, `api`, `бэк`, `бек`, `бэкенд`;
  - frontend: `F`, `FE`, `front`, `frontend`, `фронт`, `фронтенд`, `фронтендер`;
  - QA: `Q`, `QA`, `test`, `testing`, `тест`, `тестирование`, `тестировщик`.

## Forbidden without mode switch

- changing agreed planning estimates
- changing quarter or commander baseline gantt silently
