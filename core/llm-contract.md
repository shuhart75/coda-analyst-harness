# LLM Contract

This contract is CLI-neutral. It applies to Codex CLI, Claude Code, Qwen CLI, VSCodium agents, and other LLM assistants working in a project scaffolded with this harness.

## Session bootstrap

Before changing artifacts, read:

1. `AGENTS.md`
2. `core/llm-contract.md`
3. `core/agent-delegation.md`
4. `core/skills-policy.md`
5. `core/tooling-policy.md`
6. `core/context-policy.md`
7. `core/research-policy.md`
8. `core/run-loop.md`
9. `.workspace-state/run-state/session-brief.md` when present
10. `.workspace-state/active-mode.md`
11. `modes/<active-mode>.md`
12. `PROJECT_ROOT/README.md`
13. `PROJECT_ROOT/planning/team.md` before planning resources or regenerating actual-progress
14. relevant `PROJECT_ROOT/context/project-rules/*.md`
15. relevant templates for the current action
16. `PROJECT_ROOT/baseline/current/` for the canonical deployed state when it exists
17. relevant feature/slice source artifacts under `PROJECT_ROOT`
18. relevant `PROJECT_ROOT/releases/` artifacts when finalizing a delivered change

If the user points to a folder with current-system docs/screenshots/change requests, inspect that folder first and keep source references in the produced artifacts.

`HARNESS_ROOT` is the `coda-analyst-harness` repository. Resolve `PROJECT_ROOT` only through `python3 HARNESS_ROOT/scripts/workspace.py --root HARNESS_ROOT project-root`. Unless a path explicitly starts with a harness directory such as `core/`, `modes/`, `scripts/`, `skills/`, `templates/` or `.workspace-state/`, resolve project paths such as `baseline/`, `context/`, `planning/`, `features/` and `releases/` under `PROJECT_ROOT`, regardless of the directory from which the LLM was launched.


## Small-context operating rules

Treat context management as an internal harness responsibility. Users should not have to request context summaries, checkpoints, research, completeness sweeps, or prototype/slice alignment checks.

When work is broad, long-running, or likely to exceed a small context window:
- read existing `context-summary.md`, `artifact-map.md`, planning context and run-state files before broad source artifacts;
- create or refresh feature/planning/execution context summaries when their source-of-truth artifacts change substantially; refresh slice summaries only when the active mode owns a slice change or requirements package preparation has been explicitly authorized;
- update `.workspace-state/run-state/current.md` or an equivalent checkpoint before and after long passes;
- keep facts, inferences, assumptions and open questions separate;
- transfer accepted research findings into authoritative artifacts instead of leaving them only in `.research/` or chat.

External memory systems are optional accelerators. Repository markdown remains the source of truth.


## Command interpretation rules

Treat short natural-language workflow commands from `templates/workflow/command-catalog.template.md` as first-class instructions.

When the user uses a command from that catalog or a near-equivalent phrase:
- map it to the intended workflow mode and action;
- switch mode if needed;
- read the target mode file before editing;
- execute the implied workflow, not just the literal words;
- preserve the user's concrete names, dates, task ids and paths.

If the command references impacted requirements, prototypes, or rollback of a known decision, consult:
- `features/*/domain-impact.md`;
- `PROJECT_ROOT/planning/consistency-backlog.md`;
- `releases/*` and `baseline/current/` when relevant.

If multiple commands conflict, prioritize the most recent explicit user instruction and state the assumption briefly.

## Feature intake rule

Treat `новая фича` as a first-class planning command.

When the user says `новая фича`, or gives a folder and says this is a new feature:
- switch into `planning` mode if needed;
- do not scaffold `features/<slug>/` yet;
- inspect the folder first;
- compare the candidate change against `baseline/current/`;
- compare it against existing `features/*`;
- compare it against legacy planning and source materials when relevant;
- separate existing system coverage from the true new delta;
- write the result to `planning/intake/<candidate-slug>.md` using `templates/intake/feature-intake.template.md`;
- return proposed feature slug, proposed slices, affected baseline artifacts, affected existing features, Q2 scope draft, and workflow gaps before any scaffold step.

Only create the feature structure after the intake result is accepted or the user explicitly asks to proceed.

## Modes are guardrails

Treat the active mode as a write boundary.

- `planning`: owns feature scope, planning stories, estimates, scope prototype, quarter-plan and commander-plan.
- `requirements`: owns the root requirements and, only during authorized package preparation, derived slices and FE/BE packs.
- `scope-prototype`: owns planning-stage live prototypes for customer scope alignment.
- `delivery-prototype`: owns slice-level React + MUI prototypes for frontend handoff.
- `execution-update`: owns implementation tasks, actualization mapping, and actual-progress gantt.
- `release-finalization`: owns release packages, final requirements, baseline promotion, and canonical baseline updates.

If the user asks for work outside the active mode, either switch mode explicitly or state the cross-mode change before editing.

## Canonical entities

- `baseline/current` is the canonical deployed-system description.
- `planning story` is a planning/HLE unit. It has Summary, Description, estimates split by `AN / FE / BE / QA`, and may not match implementation tasks 1:1.
- `implementation task` is an execution tracking unit. It should match Jira naming where possible and includes estimate, dates, executor, status and progress.
- `requirement pack` is grouped by feature and, when useful, by slice and then FE/BE.
- `feature delivery package` lives under `features/<feature>/handoffs/<package-id>/` and is the preferred shared unit for development and testing. The immutable input contains root requirements and slices; developers return their technical decomposition inside the same package.
- `development task card` is created and confirmed by developers under `returns/development-tasks/`. It describes one future Jira task and one contour, is self-contained, and may have an estimate or Jira key, but neither is required.
- Each development task card must be created from the package template, keep every required section populated, and retain the full `Короткие команды разработчика` block after every edit.
- Receiver-side context is disclosed progressively: active `handoff.json`, compact request and manifest, one relevant contour of packaged requirements, that contour's local SDD, matched code, then nearby tests. Do not load all of `coda` or both contours without a recorded cross-contour dependency.
- Analyst-side code access is progressive and strictly read-only. Resolve role `code` through `.workspace-state/code-repos.json`, require its generated write allowlist to be empty, snapshot it with `code-inspect.py begin`, inspect one contour and bounded matches, then prove that files, index, branch, `HEAD` and repository configuration are unchanged with `code-inspect.py verify`. No user wording implicitly grants write access.
- A feature manifest may use atomic `REQ-*`/`SCN-*` traceability or explicit `legacy-sections` compatibility. Never manufacture identifiers missing from an older source document.
- `features/<feature>/requirements.md` is the primary control page and authored source for requirements; each slice must have its own ordered section there.
- `slice card` and slice FE/BE packs are derived transmission artifacts cut from the root feature requirements only during authorized package preparation, not parallel independent sources. Slices remain the primary testing units; development cards and implementation receipts provide supporting context.
- `common feature prototype` lives in `features/<feature>/prototype.html`; the user iterates on it first as the visual source of truth.
- `delivery prototype` is a slice-level schematic handoff artifact derived from the confirmed common feature prototype and root requirements.
- `release package` captures the final delivered state before promotion into a new baseline.

## Gantt rules

- `quarter-plan` and `commander-plan` are built from planning stories.
- Planning story estimates must be stored in `features/<feature>/planning/estimates.md` with explicit `AN / FE / BE / QA` role splits and an agreed total.
- A feature is the quarter-level outcome. Planning stories are role workstreams, with at most one story per `AN`, `BE`, `FE`, and `QA`.
- Approved quarter and commander plans are immutable. Later scope belongs to task candidates and actual-progress.
- Planning maximizes team utilization without exceeding 100 percent and honors personal closed intervals.
- Default efficiency is `AN=0.80`, `BE=0.70`, `FE=0.65`, `QA=0.80`; story and personal overrides are explicit.
- FE starts no earlier than three open days after BE starts. If BE is absent, FE starts after AN or at the first available window.
- Commander risk buffer is at least 20 percent and remains internal rather than a separate management-facing bar.
- `commander-plan` is the quarter plan with management buffer, normally 20-30%.
- `actual-progress` must show two useful layers:
  - `PLAN <TYPE> <summary>` bars from commander-plan planning stories;
  - current execution tasks, virtual or real.
- Do not put square brackets in PlantUML task labels. Use `PLAN FE ...`, not `PLAN [FE] ...`.
- Feature sections on generated root gantt files must be separated by `-- Feature title --`.
- Root gantt files must include the marker `Мы сейчас здесь`.
- Project start is quarter start, unless a visible task starts earlier.
- Do not hide baseline planning stories from actual-progress; the diagram exists to compare plan vs fact.
- Put hand-authored milestones in view-specific preamble files, for example `planning/<quarter>/gantt/preamble/actual-progress.puml`, not in generated root gantt files.
- When a milestone uses `happens at YYYY/MM/DD` in a preamble, `sync-quarter-gantt.py` should highlight that day in the generated view.
- If the user asks for a standalone PlantUML export without includes, expand the generated view into a separate file and leave the include-based source intact.
- For actual-progress execution tasks, tasks that have `Progress % = 0` and no actual dates are not allowed to render in the past. On each regeneration, the generator moves their rendered start to today, or the next open day, without changing the markdown source dates.
- Within one feature section, not-started backend/API tasks should lead not-started frontend tasks. Frontend tasks may render no earlier than 3 open days after the earliest not-started backend task in the same feature.
- Not-started execution tasks must not overload resource lanes. Use `PROJECT_ROOT/planning/team.md` as the roster, keep each resource at no more than one full-time task per open day, and use available resources as fully as possible before pushing work later.
- If a not-started task has no explicit executor, has a `TBD_*` executor, or references a non-roster resource lane, assign it by role from `Role`, task id prefix, executor alias or summary. Preserve explicit valid roster lanes, but still shift dates if needed to avoid overload.
- Keep baseline `PLAN ...` story bars visible for plan-vs-fact comparison even when execution tasks are shifted forward by the current date.

## Resource naming

- Role estimates and semantic task roles use `AN / BE / FE / QA`.
- The project-local roster lives in `PROJECT_ROOT/planning/team.md`.
- Default PlantUML resource lanes are `A1`, `A2`, `A3`, `B1`, `B2`, `B3`, `F1`, `F2`, `Q1`, `Q2`, `Q3`.
- Accepted aliases for resource/executor input:
  - analyst: `A`, `AN`, `analyst`, `аналитик`;
  - backend/API: `B`, `BE`, `back`, `backend`, `api`, `бэк`, `бек`, `бэкенд`;
  - frontend: `F`, `FE`, `front`, `frontend`, `фронт`, `фронтенд`, `фронтендер`;
  - QA: `Q`, `QA`, `test`, `testing`, `тест`, `тестирование`, `тестировщик`.
- Use `TBD_A`, `TBD_B`, `TBD_F`, `TBD_Q` for role-known but unassigned resources.
- The actual-progress generator normalizes known aliases on render; prefer canonical names in markdown to avoid review noise.

## Actual-progress mapping

Store story/task links in markdown, not as visual PlantUML dependencies.

- Use `features/<feature>/planning/actualization.md` for story-to-task mapping.
- Use confirmed decomposition snapshots under `features/<feature>/handoffs/*/revisions/*/returns/decomposition-snapshots/` as developer-owned task information. The analyst may materialize selected cards into `features/<feature>/slices/*/execution/task-candidates.md` or `slices/*/execution/tasks.md` for actual-progress.
- Many-to-many mapping is valid: one task may replace multiple stories, and one story may be replaced by multiple tasks.
- If the user says "replace story X by tasks A/B", update `actualization.md` and the tasks' `Related Stories`.
- If mapping is obvious from semantics, role and naming, use `mapping_mode = inferred`; if the user stated it explicitly, use `explicit`.
- Story progress is calculated from linked execution tasks, weighted by estimate.
- Story finish is the latest finish of linked replacement tasks; if a story has no replacement tasks, keep its commander baseline start unless `Depends On` says otherwise.
- Render a real task once even when it maps to multiple stories.

## Requirements rules

- Requirements are living markdown artifacts until release fixation.
- `core/requirements-profile.md` is the shared root-document contract based on ISO/IEC/IEEE 29148:2018. It adapts the standard and does not claim full conformity.
- Write requirements by the harness template in `templates/requirements/`, not freeform.
- Start from `features/<feature>/requirements.md` as the primary feature-level requirement page and only place where feature requirements are authored from scratch.
- Build that page by the selected project-local requirements format:
  - new readable format: `templates/requirements/feature-requirements.readable.template.md` plus `*.readable.template.md` slice/FE/BE packs;
  - old detailed format: `templates/requirements/feature-requirements.template.md` plus the original slice/FE/BE templates.
- If the user names the format, obey it. If the feature already exists and the user does not name a format, preserve the current feature format. For a new feature without an explicit choice, use the new readable format.
- Do not mix new and old requirement formats inside one feature unless the user explicitly asks for a migration or comparison.
- Requirement diagrams must be PlantUML; do not introduce Mermaid blocks.
- During ordinary requirement work, edit only the root `requirements.md`. Do not create or refresh slice cards, FE/BE detail packs, task candidates, or package revisions after each change.
- After changing the root document, record the change origin in `features/<feature>/requirements-state.json` through `scripts/requirementsctl.py record-change`.
- Use `origin=developer-receipt` only for a change accepted from a registered developer receipt. Such a change never triggers slice regeneration, a package revision, or an offer to create one.
- Use `origin=analyst` for an analyst-initiated change. If a package was already published, offer a new revision once. Call `mark-offered` before asking. If the analyst declines, call `decline-revision` and do not offer again until an explicit preparation command.
- Derive slice cards and FE/BE detail packs only after `сформируй пакет для разработки`, an accepted one-time offer, or another explicit preparation synonym. Begin that pass with `requirementsctl.py begin-preparation`.
- `сформируй пакет для разработки` is an end-to-end publication command. Before creating a revision, validate completeness, consistency, verifiability, impacts, traceability and Russian language in the root document; then derive current slices and validate them against that root. Apply only meaning-preserving corrections automatically. For every semantic ambiguity ask the analyst exactly one question and wait; do not create or publish a package while any blocking question remains. On success publish the revision directly as `sent`, record it with `requirementsctl.py mark-published`, and require `next_sdd_action.action = process`; there is no analyst-facing `ready` state. Do not create a ZIP unless the analyst explicitly requests it; create requested transport archives only in `~/Downloads`, never inside a repository.
- Developer SDD creates self-contained cards in the package return area after targeted code research. Cards must be role-specific, linked to full requirements, checks and related slices, and detailed enough to be the primary implementation input.
- When transmitted requirements change, do not rewrite a confirmed developer decomposition or any immutable input revision. A new input revision is created only after the analyst explicitly requests or accepts its preparation.
- Receiving a decomposition snapshot does not change planning stories or approved plans. The analyst decides separately which cards to materialize into actual-progress.
- If a slice artifact exposes a missing rule or contradiction during package preparation, update `features/<feature>/requirements.md` first and only then re-derive the slice artifact in the same authorized pass.
- Requirement prose must be written in Russian. Avoid English words and transliterated anglicisms when a clear Russian formulation exists.
- English is allowed only for exact code, file paths, API/database identifiers, enum values, and fixed external-system names.
- Run the project language validator for changed requirement files before presenting the work as complete.
- Run `scripts/validate-requirements-profile.py` for changed profiled root documents. Do not force legacy documents into the profile during an unrelated edit.
- Keep business requirements, system requirements, acceptance criteria, API contracts and examples traceable to source materials.
- When current implementation facts affect requirements, inspect the registered local repository assigned role `code` automatically under `core/code-inspection.md`. Record the exact commit and relative evidence paths; do not infer business intent from code alone.
- Analyst code inspection improves the requirement input but does not replace the receiving SDD's comparison against its current code before decomposition and implementation.
- Only the user-owner may mark requirements as approved. Record the approver and date, and create a new revision for later semantic changes.

## Fast consistency sweep for requirement edits

When a requirement change replaces one variant with another, remove stale tails in the same turn instead of leaving conflicting old wording behind.

Use a two-speed approach:

- `quick local sweep` is the default for minor edits confined to one feature or one slice;
- `full sweep` is required only when the change is clearly `cross-feature` or `domain-wide`.

Quick local sweep order:

1. update `features/<feature>/requirements.md`;
2. do not touch existing slice cards or FE/BE detail packs outside an explicitly authorized package-preparation pass; they remain a snapshot of the last transmission and may be marked `stale` in `requirements-state.json`;
3. record the complete impact in the root document; do not refresh `domain-impact.md` or derived requirement artifacts during ordinary authoring;
4. run a targeted text search across the current root requirements and other authored sources, excluding immutable packages and intentionally stale slice snapshots;
   if available, use `scripts/find-stale-terms.py` as the fast default helper;
5. specifically check for superseded:
   - old endpoint names;
   - old field names;
   - old role names;
   - old status values;
   - old UX labels or option names;
   - old Decision IDs or replaced contract filenames.

Do not answer a minor local edit with a whole-repo reread or a broad manual audit unless the user asked for it or the evidence shows wider drift.

## Prototype rules

- Default prototype stack: single-file `prototype.html`, React + MUI via CDN, no build step.
- Use only MUI components unless a project override says otherwise.
- Do not generate a prototype immediately after entering prototype mode; inspect existing prototypes and visual references first.
- Clarify with the user which prototype, screenshot, page or other artifact is the visual base when the basis is not already explicit.
- First work on one common root prototype in `features/<feature>/prototype.html` and `features/<feature>/prototype-notes.md`.
- The common root prototype must be a user-facing clickable prototype for the whole feature as the user will see it; do not put frontend handoff comments, API notes or developer explanations inside that HTML.
- Before touching any `features/<feature>/slices/*/delivery-prototype/*`, verify in `features/<feature>/prototype-notes.md` that both status lines are explicitly set to `да` for user confirmation and permission to proceed.
- If those confirmations are missing, stop and report that slice prototype generation is blocked until the root prototype is approved.
- Delivery prototypes are the only place for schematic frontend-facing explanations and must be derived from the confirmed root prototype plus current root requirements.
- Never fall back to editing an existing slice prototype just because `delivery-prototype` mode is active.


## Consistency propagation rules

When changing requirements, domain rules, lifecycle states, roles, API semantics, data model, or shared UI behavior, always perform impact detection in the same turn.

During ordinary requirements authoring, put the decision, classification, affected requirements, baseline artifacts, prototypes and required neighboring work into the root `requirements.md`. Do not refresh `domain-impact.md`, slices or detailed packs. During an authorized package-preparation pass, propagate this accepted information into `domain-impact.md` and the derived package artifacts. A separate explicit domain-decision command may also update `domain-impact.md`; use `PROJECT_ROOT/planning/consistency-backlog.md` only for concrete work outside those derived artifacts that is deliberately deferred.

The agent that edits local requirements performs first-pass impact detection. The main agent confirms and normalizes impact during consistency sweep. Release-finalization performs the final consistency gate before baseline promotion.

Shared requirements and canonical baseline updates must be integrated by the main agent, not blindly by parallel subagents.

## Prototype consistency rules

Prototype updates are optional unless the prototype is an active scope-demo or delivery-handoff artifact. Still, affected prototypes must be listed in `domain-impact.md` and/or `PROJECT_ROOT/planning/consistency-backlog.md` so the user can later say "актуализируй прототипы" and the agent has a concrete target list.

Use prototype sync statuses:
- `must-update-now`;
- `defer-ok`;
- `no-update-needed`;
- `obsolete`.

## Rollback consistency rules

Rollback before release:
- mark the decision as `reverted-before-release`;
- mark related consistency items as `cancelled`;
- revert already-propagated living requirements if needed;
- do not change baseline unless the decision was already promoted.

Rollback after release:
- do not silently edit history;
- create a new rollback/change feature or release item;
- reference the original `Decision ID`;
- promote the rollback through `releases/` into a new `baseline/current` version.

Partial rollback:
- keep consistency backlog items open as `rollback-propagation-required` until affected requirements, baseline files and prototypes are reconciled.

## Safety and validation

- Never modify copied legacy/original source folders unless the user explicitly asks. For `changesWork`, read or copy only.
- Preserve user edits; do not revert unrelated changes.
- Run all workflow tools from `HARNESS_ROOT/scripts/` and pass the resolved `PROJECT_ROOT` as the project root.
- Before the final response after file edits, and always before a commit, review the current-turn diff for necessity, correctness and conciseness. Fix in-scope issues; report or ask about issues that are out of mode, touch unrelated user changes, or require a business decision.
- After planning/execution gantt edits, run `scripts/sync-quarter-gantt.py <project>/planning/<quarter>/gantt` when available.
- After structural edits, run `scripts/validate-structure.py <project>` and `scripts/validate-links.py <project>` when available.
- If validation fails, fix the cause or report the exact residual issue.

## Baseline and release rules

- Keep the domain backbone in `baseline/current/domain/`, not only in raw source folders.
- Treat `context/source-materials/` and imported legacy folders as raw evidence, not as the canonical deployed state.
- Feature work describes deltas against `baseline/current/`; use `features/<feature>/domain-impact.md` for DDD impact.
- When a change is deployed, collect final requirements under `releases/<quarter>/<release-id>/` before promoting them.
- Promotion means:
  - update `baseline/current/`;
  - copy the previous baseline into `baseline/versions/<version>/`;
  - record the promoted version in `baseline/current/VERSION.md`;
  - record the source release in `releases/<quarter>/<release-id>/promoted-baseline-version.md`.

## Delegation rules

- Treat delegation as optional acceleration, not as a required capability.
- If subagents exist, use them only for bounded, non-overlapping tasks.
- The main agent remains responsible for semantic consistency of baseline, releases, and plan-vs-fact mapping.
- Never delegate final promotion decisions blindly.
- When delegating edits, assign explicit file ownership and require a returned changed-file list.

## Skills rules

- Skills are optional reusable behaviors, not a substitute for the project contract.
- Use a skill only if it clearly matches the current mode and improves repeatability.
- A skill must not bypass mode boundaries or mutate canonical baseline files outside release-finalization.
- When a platform has no native skills, follow the same rules through prompts/templates instead.

## Tool discipline

- Prefer markdown source-of-truth files over generated representations.
- Use PlantUML as a rendering target, not as the semantic store for mapping.
- Keep raw evidence in `context/source-materials/`.
- Prefer small, reviewable edits over broad rewrites.
