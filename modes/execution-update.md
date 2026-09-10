# Mode: execution-update

## Goal

Track real work with or without a recorded planning baseline.

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
- `planning/*/gantt/actual-progress-features.json`

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
- analyst-confirmed quarter forecast exclusions under `core/forecast-exclusions.md`
- analyst-confirmed preservation of existing forecast blocks under `core/forecast-preservation.md`
- confirmed or proposed task candidates discovered after planning approval
- agreed developer task lists received through the exchange catalog
- optional materialization of selected returned tasks into task candidates or actual execution tasks

When a returned developer task is materialized into real work, keep `returns/tasks.md` as the factual source. The analyst may use or replace optional developer estimates and may link Jira keys, but does not edit the returned decomposition or block development.

QA estimates come from an explicit analyst answer or the tracker's dedicated QA
estimate. A source card with FE and QA estimates produces separate FE and QA
execution rows, each with its own estimate. If the QA estimate is lost, ask the
analyst; never default it, copy the FE estimate or silently skip QA. In visible
task titles use `QA <summary>`, without the tracker-number prefix; keep the source
key and `<key>/QA` as metadata for traceability.

QA относится к фиче целиком, а не к FE-карточке, содержащей «Оценку тестирования».
Несколько QA-задач сохраняются отдельно. При отсутствии фактического начала
прогноз QA ориентируется на первое завершение FE внутри фичи, независимо от ключа
карточки-источника: по умолчанию за один рабочий день до него (консервативный край
ориентира 1–2 дня), но не раньше начала этой FE-задачи, текущей даты и доступности
QA-ресурса. Если FE нет, ориентир — следующий рабочий день после первого завершения
другой роли фичи. Подтверждённые фактические даты не сдвигаются. Прогноз не записывается
как факт; поздняя явная плановая дата QA остаётся ограничением снизу.

## Read-only tracker evidence

Reading SberTrek and Jira follows `core/tracker-reading.md` and does not itself
switch mode or authorize changes to execution artifacts. SberTrek is primary; Jira
fills only missing values and contributes history. The read report groups the same
tasks independently by epic and by release. Applying selected facts to
actual-progress is a separate, not-yet-authorized operation.

Tracker estimates are applied per role. For every populated `AN / BE / FE / QA`
estimate, create one row in `execution/tasks.md`; the same tracker key may repeat,
but one `tracker key + role` pair may occur only once. The generator uses the
internal id `<tracker-key>/<role>` and renders a separate bar whose summary starts
with the role. If all role estimates are empty and only the general estimate is
present, use it only for one unambiguous `AN`, `BE` or `FE` prefix. Do not split a
general estimate between roles, and do not add it when any role estimate exists.

## Source and generation gate

- Resolve analytics through `workspace.py project-root`. Check Git state with `git -C "$PROJECT_ROOT"`, never from the harness root. An ignored analytics directory in the harness does not mean its files are untracked in their own repository.
- Read the canonical feature registry `features/<feature>/execution/tasks.md` and `planning/actualization.md`. Legacy slice registries remain supported; do not create slices or duplicate tasks merely to satisfy tooling.
- Individual task cards and `execution/actual-progress.md` are supporting evidence, not interchangeable generator inputs. Prepare a missing registry only from confirmed facts and explicit authorization; do not guess missing estimates, dates, story membership or resource assignments.
- Use `templates/execution/tasks.template.md` for internal `Task ID` and optional confirmed `Jira`. A local real QA task needs no tracker key. `QA-COHORT` renders as `TASK_QA_COHORT`; record the preserved alias in Notes, not as a tracker-issued identifier.
- Ролевой PLAN использует все задачи соответствующей роли внутри подтверждённой фичи; формула и legacy-совместимость описаны в `templates/planning/actualization.template.md` и `core/entity-model.md`. Принадлежность задачи к фиче не угадывается по похожему названию. Follow-up может описывать связи с существующими историями, включая список ID; это не повод создавать новые истории или дублировать QA.
- A confirmed missing story baseline is represented by `Baseline State = absent` in actualization.md with both baseline cells empty. This is independent of virtual/materialized; it does not require retrospective plan approval. Keep explicit task links and source evidence. Missing actualization.md itself still blocks generation, except for a plan-only feature with an explicit decision under `core/forecast-exclusions.md` or `core/forecast-preservation.md`.
- An absent-baseline story has no generated `PLAN` bar; its task links and computed progress remain in export comments, while the tasks render normally. Never label execution-derived dates as an approved plan. Existing approved baseline snapshots remain protected.
- Missing task estimates or required scheduling dates still block the entire run without writing outputs. A known finish is not a known start; do not infer start from Created, Updated, duration or an unverified old Gantt bar.
- Feature execution context belongs in `features/<feature>/execution-context.md`, not a second context file under execution/. Preserve and consolidate existing notes only with the relevant project edit authorization.
- A task's tracker Updated, document update date and actual completion date have distinct meanings. Do not synchronize them merely because the dates differ.
- A rendered resource lane is not proof of an actual assignee. Auto-allocation applies to not-started work, not retroactive person-to-lane attribution.
- If an overlay slug differs from the feature directory, require an explicit mapping in `planning/<quarter>/gantt/actual-progress-features.json`: `{"schema_version":1,"features":{"<overlay-slug>":"<feature-directory>"}}`. Never infer this mapping from similar names.
- To exclude a plan-only feature from the current quarter forecast, follow `core/forecast-exclusions.md`: schema version 2, explicit analyst confirmation, reason and document source. This preserves the original plan in a visibly marked section; it never invents cancellation, a new quarter, dates or progress. Existing execution sources, overlays or an approved actualization snapshot block this limited mode. Do not delete them to bypass the gate.
- When the analyst keeps an existing forecast unchanged, follow `core/forecast-preservation.md`: schema version 3, `preserved_forecasts`, an explicit source, include, checksum and aliases. Keep the entire shared block and exactly one connection in actual-progress and Confluence; do not infer a new date approval, create a fake map or substitute `outside-quarter`. Existing execution evidence remains protected.
- In execution-update run only `python3 scripts/sync-quarter-gantt.py "$PROJECT_ROOT/planning/<quarter>/gantt" --actual-only` from HARNESS_ROOT. It regenerates actual-progress includes, the view and Confluence export without writing approved quarter/commander plans.
- All selected sources and Confluence includes are checked before publication. Missing or ambiguous sources block the whole run and preserve existing outputs; no automatic stale-overlay deletion. Ordinary write errors trigger rollback of earlier writes; this is not a crash-atomic multi-file transaction.
- On a block, stop generation, show the actual project diff and missing sources. Do not patch installed generators, invent tracker keys or build an alternate slice structure during project work.
- After success inspect the diff, run applicable checks and verify Confluence parity through the standard `expand-plantuml-includes.py`. Do not commit, submit or merge unless authorized.

## Actual-progress scheduling rules

- Do not hand-edit generated actual-progress PlantUML for task dates. Update execution markdown, then regenerate the gantt.
- Not-started execution tasks are tasks with `Progress % = 0` and no actual dates. They may be `real` or `virtual`.
- Подтверждённое завершение к релизу без точного интервала записывай как `done / 100%` и необязательное `Completed By = YYYY-MM-DD`, с источником в Notes/Details. Не записывай границу в `Actual Finish` и не восстанавливай даты по оценке. Без полного фактического интервала генератор выводит подписанную отметку границы без резервирования ресурса; сама граница не привязывает PLAN. Если фактический интервал известен полностью, он сохраняется и не может выходить за границу.
- Отменённые `cancelled`/`canceled`, как и `superseded`, остаются в исходном реестре и комментариях экспорта, но не в расписании и не в проценте PLAN. Отмена задачи не утверждает отмену фичи и не изменяет baseline.
- Кандидаты из `task-candidates.md` не получают даты, дорожки и резерв ресурсов. Они остаются комментариями в actual-progress до явной материализации в реестр `real` или `virtual`; статус предложения сам по себе не разрешает планирование.
- Legacy-значение `Jira = KEY/ROLE` читается как ключ `KEY` и ролевой идентификатор `KEY/ROLE`, без повторного суффикса. Несовпадение суффикса с колонкой `Role` и дубли после нормализации блокируют генерацию. Исходный реестр автоматически не переписывается.
- Not-started tasks must not render before the current date marker. On each regeneration, if their planned date is stale, the generator shifts only the rendered bar to today or the next open day.
- Inside a feature, not-started backend/API tasks lead frontend tasks. Frontend bars start no earlier than 3 open days after the earliest not-started backend/API bar in the same feature.
- Not-started tasks must be capacity-scheduled by `documents/planning/team.md`: no resource lane above 100% on an open workday, and available resources should be used before pushing work later.
- If executor is empty, `TBD_*`, or a non-roster lane, let the generator auto-assign by role/task prefix/summary. Valid explicit lanes such as `B2` are preserved, with dates shifted if needed to avoid overload.
- An executor lane whose role conflicts with the work-item role is not preserved; the generator chooses a resource from the correct role roster.
- Strip square brackets from generated PlantUML labels. In particular, tracker summary `[FE] Списковая форма` must render as `FE Списковая форма`, never as nested PlantUML brackets.
- Actual started or completed tasks keep their actual dates, even when those dates are in the past.
- Сохраняй серую полосу `PLAN` с исходной длительностью. Начало привязывается к первой фактической задаче этой роли в фиче, иначе к первой прогнозной; конец — только к длительности по рабочему календарю. Без задач сохраняется исходное окно. Процент — взвешенный по точным человеко-дням прогресс всех задач роли, без кандидатов, `superseded` и `cancelled`/`canceled`. Полосы без записанного baseline не создаются.
- Never rewrite approved quarter or commander plans to absorb later scope. Render later work as task candidates or actual tasks.
- Regenerate actual-progress through `scripts/sync-quarter-gantt.py --actual-only`; it also refreshes `actual-progress-confluence.puml`.

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
