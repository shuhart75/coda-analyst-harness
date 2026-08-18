# Mode: planning

## Goal

Shape prioritized quarter features, role workstreams, HLE, capacity schedule, and immutable planning baselines.

The planning mode starts with feature intake when the user brings a candidate new feature from an external folder or an unstructured initiative.

## Main artifacts

- `planning/intake/*.md`
- `.workflow/templates/intake/feature-intake.template.md`
- `.workflow/templates/planning/planning-context.template.md`
- `.workflow/templates/planning/assumptions.template.md`
- `.workflow/templates/planning/risk-register.template.md`
- `.workflow/templates/planning/story-map.template.md`
- `.workflow/team.md`
- `features/*/feature.md`
- `features/*/planning/planning-context.md`
- `features/*/planning/assumptions.md`
- `features/*/planning/risk-register.md`
- `features/*/planning/story-map.md`
- `features/*/planning/stories/*.md`
- `features/*/planning/estimates.md`
- `features/*/planning/scope-prototype/*`
- `features/*/domain-impact.md` for preliminary DDD impact
- optional `features/*/.research/code-evidence.yaml`
- `planning/*/gantt/quarter-plan.puml`
- `planning/*/gantt/commander-plan.puml`
- `planning/*/plan-state.md`
- `planning/*/retrospective.md`

## Allowed changes

- feature intake / preflight notes
- planning stories
- preliminary domain impact
- analyst/team/agreed estimates split by `AN / FE / BE / QA`
- scope prototype
- quarter and commander gantt
- planning context, assumptions, risk register and story map
- auxiliary commit-bound code evidence when current implementation affects planning

## Planning story model

- A feature is the finished user or system outcome planned for the quarter.
- A feature has at most four planning stories: one each for `AN`, `BE`, `FE`, and `QA`.
- A missing role means no story for that role.
- Functional decomposition belongs to requirements and slices, not additional planning stories.

## Estimation rules

- Keep analyst anchor, team, and final agreed effort. Never average them automatically.
- Store effort, max parallelism, role efficiency, dependencies, and not-before constraints per role story.
- Default efficiency: `AN=0.80`, `BE=0.70`, `FE=0.65`, `QA=0.80`.
- Duration is `ceil(effort / effective parallel capacity)`.
- Personal coefficients and closed intervals come from `.workflow/team.md`.

## Priority and capacity

- Feature order in `gantt/order.txt` is top-to-bottom priority.
- Higher-priority ready work receives suitable free resources first.
- Idle roles may pipeline into the next feature, but lower-priority work must not delay higher-priority work when it becomes ready.
- Use all available capacity where possible without exceeding 100 percent.
- Planning work may use several people up to `max_parallelism`; actual tasks use one person per task.
- FE starts no earlier than three open days after BE starts. Without BE, FE starts after AN or at the first available planning window.

## Risk buffer

- Commander buffer is at least 20 percent.
- Suggest 30 percent for a high risk or external dependency, 40 percent for several high risks/new integration/unclear data, and 50 percent for critical uncertainty.
- More than 50 percent requires a manual decision.
- The buffer changes commander dates but is not rendered as a separate management-facing bar.

## Gantt planning rules

- Feature sections are the primary visual grouping in quarter, commander and actual-progress gantt views.
- When planning future not-started work for a feature, put backend/API work before frontend work.
- If exact dates are not known, plan frontend no earlier than 3 open days after backend/API work starts.
- Use `.workflow/team.md` as the team roster. Default lanes are `A1-A3`, `B1-B3`, `F1-F2`, `Q1-Q3`.
- Do not plan more than one full-time task on the same resource for the same open day. Use available resources as fully as possible before pushing work later.
- Use canonical resource lanes when a resource is known: `A<N>`, `B<N>`, `F<N>`, `Q<N>`. Keep aliases only as input shorthand, not as the preferred written form.

## Current-state actualization boundary

Planning mode owns quarter and commander baselines. It does not own current execution state.

- `draft` plans may be regenerated.
- Only the project owner may set a plan to `approved`.
- Approved quarter and commander plans are immutable.
- Scope discovered after approval is represented in actual-progress through task candidates and actual tasks.
- Quarter retrospective compares the immutable plan with actual execution and proposes future efficiency/risk calibration.

- `спланируй квартал`, `собери командирский план`, HLE and planning stories may update quarter-plan and commander-plan.
- `обнови реальный прогресс`, `обновляем прогресс`, task statuses, actual dates, execution resources and actual-progress gantt belong to `execution-update`.
- If the user asks to актуализировать текущее положение дел while planning mode is active, switch to `execution-update` before changing tasks or actual-progress.
- Do not silently edit `quarter-plan.puml` or `commander-plan.puml` while only updating current state.

## Preliminary impact

During planning, capture obvious cross-feature or domain-wide consequences in `domain-impact.md`, but keep them marked as `proposed` until requirements work confirms them.

When a planning boundary, dependency or estimate materially depends on current implementation, use the registered local `coda` clone for one bounded read-only inspection under `.workflow/code-inspection.md`. Record the inspected commit and keep technical findings as evidence or assumptions; do not derive a new business scope from code alone.

## Forbidden without mode switch

- implementation task actual dates
- execution status tracking
- actual-progress gantt


## Feature intake rule

When the user says `новая фича` or otherwise points to an external folder and says this is a new feature:

- do not scaffold `features/<slug>/` yet;
- do not create slices yet;
- inspect the source materials first;
- compare them against `baseline/current/`, existing `features/*`, and legacy/source-materials planning where relevant;
- separate existing coverage from the true new delta;
- write the result to `planning/intake/<candidate-slug>.md` using `.workflow/templates/intake/feature-intake.template.md`;
- only after intake confirmation proceed to feature scaffolding and planning stories.

## Small-context planning rules

Planning work must automatically maintain enough context for a small-window LLM to resume without rereading all source materials.

For `новая фича`, `занимаемся планированием`, `спланируй фичу`, `подготовь HLE`, quarter-plan and commander-plan work:

- summarize source materials into the intake result or `features/<feature>/planning/planning-context.md`;
- explicitly separate current-system coverage, new delta and uncertain items;
- use targeted code inspection when `baseline/current/` and source materials are insufficient to classify current-system coverage;
- keep planning assumptions in `features/<feature>/planning/assumptions.md` or a clearly named section of `planning-context.md`;
- keep planning risks in `features/<feature>/planning/risk-register.md` or a clearly named section of `planning-context.md`;
- map `source -> delta -> planning story -> slice` in `features/<feature>/planning/story-map.md` when the feature is large enough that the relationship is not obvious;
- update the run checkpoint before and after long planning passes.

Do not ask the user to request these context operations explicitly. Ask only when scope, quarter boundary, estimate basis or current-vs-new classification requires a human decision.
