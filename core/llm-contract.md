# LLM Contract

This contract is CLI-neutral. It applies to Codex CLI, Claude Code, Qwen CLI, VSCodium agents, and other LLM assistants working in a project scaffolded with this harness.

## Analyst communication

Communicate with the analyst in Russian. This includes intermediate updates, questions, choices, status summaries and final answers. Preserve English only where it is part of exact code, a path, an identifier, a fixed product name or a necessary special term. Switch the conversation language only when the analyst explicitly requests it. A generic editor language rule does not represent a project decision to use English.

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
9. `core/collaboration.md` when multi-user mode is configured or feature work is requested
10. `.workspace-state/run-state/session-brief.md` when present
11. `.workspace-state/active-mode.md`
12. `modes/<active-mode>.md`
13. `PROJECT_ROOT/README.md`
14. `PROJECT_ROOT/planning/team.md` before planning resources or regenerating actual-progress
15. relevant `PROJECT_ROOT/context/project-rules/*.md`
16. relevant templates for the current action
17. `PROJECT_ROOT/baseline/current/` for the canonical deployed state when it exists
18. relevant feature source artifacts under `PROJECT_ROOT`
19. relevant `PROJECT_ROOT/releases/` artifacts when finalizing a delivered change

If the user points to a folder with current-system docs/screenshots/change requests, inspect that folder first and keep source references in the produced artifacts.

`HARNESS_ROOT` is the `coda-analyst-harness` repository. Resolve `PROJECT_ROOT` only through `python3 HARNESS_ROOT/scripts/workspace.py --root HARNESS_ROOT project-root`. Unless a path explicitly starts with a harness directory such as `core/`, `modes/`, `scripts/`, `skills/`, `templates/` or `.workspace-state/`, resolve project paths such as `baseline/`, `context/`, `planning/`, `features/` and `releases/` under `PROJECT_ROOT`, regardless of the directory from which the LLM was launched.

Repositories assigned roles `code` and `source` are optional after the first successful bootstrap. Their absence is a supported reduced-workspace state: do not recreate them without an explicit analyst request, do not attempt code inspection when `code` is absent, and do not claim reverse-patch verification when `source` is absent. Role `analytics` is mandatory.


## Small-context operating rules

Treat context management as an internal harness responsibility. Users should not have to request context summaries, checkpoints, research, completeness sweeps, or prototype alignment checks.

When work is broad, long-running, or likely to exceed a small context window:
- read existing `context-summary.md`, `artifact-map.md`, planning context and run-state files before broad source artifacts;
- create or refresh feature/planning/execution context summaries when their source-of-truth artifacts change substantially;
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

For a command that starts work on an existing feature, absence of `.workspace-state/collaboration.json` means one-time migration is required. Run bootstrap and collaboration status, request the analyst id, migrate safely, and create the feature branch before reading or editing its requirements. Do not reinterpret the missing file as single-user mode.

`collaboration.py submit` pushes a feature branch but does not create or accept a merge request. Start the user-facing result with its exact `message`. A returned `merge_request_create_url` is a link to the creation form, not evidence that a request exists. After the user reports that the request was accepted, run `collaboration.py finish` and prove that the submitted commit is contained in `origin/main`.

Git commit messages are a strict privacy boundary. Never put a task number or tracker identifier in the subject or body, including generated, merge and squash messages and identifiers embedded in branch names or URLs. A semantic description is required instead. This rule does not remove identifiers from tracker evidence, branch names or project artifacts where they are operationally necessary. Validate the complete proposed message before every programmatic commit and keep the bootstrap-installed `commit-msg` hook active; if an existing hook configuration prevents enforcement, stop instead of bypassing or weakening the rule.

For the exact full-exchange command `синкани репы`, use `workspace.py sync` even when an `awaiting-merge` feature branch is still checked out. The program itself attempts guarded finish: it continues only when `origin/main` contains the submitted commit and otherwise stops before code and source updates. Do not decide from the stale local collaboration status that a remotely accepted request is still unmerged.

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
- return proposed feature slug, process boundaries, affected baseline artifacts, affected existing features, quarter scope draft, and workflow gaps before any scaffold step.

Only create the feature structure after the intake result is accepted or the user explicitly asks to proceed.

## Modes are guardrails

Treat the active mode as a write boundary.

- `planning`: owns feature scope, planning stories, estimates, scope prototype, quarter-plan and commander-plan.
- `requirements`: owns the root requirements, their state and explicit exchange revisions.
- `scope-prototype`: owns planning-stage live prototypes for customer scope alignment.
- `delivery-prototype`: owns feature-level React + MUI prototypes for frontend handoff.
- `execution-update`: owns implementation tasks, actualization mapping, and actual-progress gantt.
- `release-finalization`: owns release packages, final requirements, baseline promotion, and canonical baseline updates.

If the user asks for work outside the active mode, either switch mode explicitly or state the cross-mode change before editing.

## Canonical entities

- `baseline/current` is the canonical deployed-system description.
- `planning story` is a planning/HLE unit. It has Summary, Description, estimates split by `AN / FE / BE / QA`, and may not match implementation tasks 1:1.
- `implementation task` is an execution tracking unit. It should match Jira naming where possible and includes estimate, dates, executor, status and progress.
- The sole authored requirement source is `features/<feature>/requirements.md`.
- An exchange revision lives under `requirements-exchange/<feature>/revisions/<NNN>/`. Its input contains only immutable `requirements.md`; `manifest.json` at feature level selects the active revision and requests returns. Developer acceptance is recorded by an immutable `returns/receipt.json` bound to that revision and checksum, never by editing the analyst-owned manifest.
- In configured multi-user mode, ordinary analytical edits live in `feature/<feature>/<analyst>`; later cycles use the next free `-2`, `-3` suffix instead of deleting or overwriting historical branches. Packages may be created only from an up-to-date `analytics/main` after the feature branch has been accepted by a human merge request. A feature branch never participates directly in source exchange or reverse-patch generation.
- `returns/tasks.md` is the already agreed developer decomposition. The analytical workflow does not own or confirm the agreement process.
- Receiver-side context is disclosed progressively: feature `manifest.json`, active `requirements.md`, the selected contour's local SDD, matched code, then nearby tests. Do not load all of `coda` or both contours without a recorded dependency.
- Analyst-side code inspection is progressive and strictly read-only. Resolve role `code` through `.workspace-state/code-repos.json`, snapshot it with `code-inspect.py begin`, inspect one contour and bounded matches, then prove that files, index, branch, `HEAD` and repository configuration are unchanged with `code-inspect.py verify`. The only write-path exception is the isolated `requirements-exchange.py prepare` publication described below; no user wording extends it.
- New exchange revisions require direct `REQ-*` traceability. Migrate an older document consciously before its next transfer; never manufacture product meaning.
- `features/<feature>/requirements.md` is the primary control page and the only authored requirement source.
- The harness does not create slices, contour-specific requirement packs or preliminary development tasks.
- `common feature prototype` lives in `features/<feature>/prototype.html`; the user iterates on it first as the visual source of truth.
- `delivery prototype` is a feature-level schematic handoff artifact derived from the confirmed common feature prototype and root requirements.
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
- Use the agreed task list and task results under `requirements-exchange/<feature>/revisions/<NNN>/returns/` as developer-owned task information. The analyst may materialize selected tasks into the existing feature execution artifacts used by actual-progress.
- Many-to-many mapping is valid: one task may replace multiple stories, and one story may be replaced by multiple tasks.
- If the user says "replace story X by tasks A/B", update `actualization.md` and the tasks' `Related Stories`.
- If mapping is obvious from semantics, role and naming, use `mapping_mode = inferred`; if the user stated it explicitly, use `explicit`.
- Story progress is calculated from linked execution tasks, weighted by estimate.
- Story finish is the latest finish of linked replacement tasks; if a story has no replacement tasks, keep its commander baseline start unless `Depends On` says otherwise.
- Render a real task once even when it maps to multiple stories.

## Requirements rules

- Requirements are living markdown artifacts until release fixation.
- `core/requirements-profile.md` is the compact business-specification contract. It uses stable `REQ-*` headings and nested Russian scenarios without the repeated passports and mandatory empty sections of the former profile.
- `core/requirements-wording.md` is the controlled-Russian contract. Apply its isolated-reader test while writing, not only at delivery: quantities, referents, events and observable outcomes must be explicit.
- `core/requirements-audit.md` is the mandatory three-level semantic audit: individual rules, cross-requirement system reasoning, and delivery readiness. A format validator never substitutes for it.
- Write requirements by the harness template in `templates/requirements/`, not freeform.
- Start from `features/<feature>/requirements.md` as the primary feature-level requirement page and only place where feature requirements are authored from scratch.
- Use `templates/requirements/feature-requirements.template.md` for new or consciously migrated documents. Existing older documents remain readable history, but the live root must use the compact profile before its next exchange revision.
- Do not add a `Статус` field to compact `requirements.md`. Read authoring and audit state from `requirements-state.json`, and the immutable delivery revision state from its exchange `manifest.json`.
- Requirement diagrams must be PlantUML; do not introduce Mermaid blocks.
- During ordinary requirement work, edit only the root `requirements.md`. Do not create exchange revisions after each change.
- After changing the root document, record the change origin in `features/<feature>/requirements-state.json` through `scripts/requirementsctl.py record-change`.
- Use `origin=developer-result` only for a change accepted from a registered return and pass its stable `return_id`. Such a change never triggers or offers an exchange revision.
- Use `origin=analyst` for an analyst-initiated change. If a package was already published, offer a new revision once. Call `mark-offered` before asking. If the analyst declines, call `decline-revision` and do not offer again until an explicit preparation command.
- Begin explicit transfer with `requirementsctl.py begin-preparation`. This starts the mandatory audit; it does not authorize publication.
- Audit by `core/requirements-audit.md`. Build applicable role/action, state/transition, data, scenario, dependency, impact and internal/external-view models; reason across the full requirement set, not only one paragraph at a time. Apply only meaning-preserving corrections automatically. For every semantic ambiguity ask the analyst exactly one question and wait, then recheck affected relations. After all corrections rerun all three levels over the complete document.
- Reject placeholder prose while authoring. `Когда` names a concrete state and event; `Тогда` states an observable outcome without repeating `система должна`. Never choose the intended meaning of vague quantifiers or references on the analyst's behalf.
- When blockers are resolved, record the result with `requirementsctl.py record-audit`, show the full audit report in chat, and ask the exact confirmation question from the profile. Do not infer confirmation from the original transfer command or from approval of the document.
- Only after an explicit positive answer run `requirementsctl.py confirm-audit`. Then run `requirements-exchange.py prepare` for the unchanged audited document, record the returned manifest and destination with `requirementsctl.py mark-published`, and report the actual location. Any intervening root change requires a new audit. There is no analyst-facing package state `ready`.
- Developer SDD treats the transmitted file as a business contract, derives technical deltas against current code in each contour's local SDD, and preserves `REQ-*` links. Before any other return it writes an immutable `returns/receipt.json` for the exact active revision and checksum. It then writes its already agreed single-contour decomposition to `returns/tasks.md`, per-task actual results and local SDD references to `returns/tasks/<task-id>.md`, and final coverage to `returns/summary.md`. Backend and frontend work must not be mixed in one task.
- When transmitted requirements change, do not rewrite any immutable input revision or its returns. A new input revision is created only after the analyst explicitly requests or accepts its preparation.
- Receiving `tasks.md` does not change planning stories or approved plans. The analyst separately uses it to update actual planning.
- Requirement prose must be written in Russian. Avoid English words and transliterated anglicisms when a clear Russian formulation exists.
- English is allowed only for exact code, file paths, API/database identifiers, enum values, and fixed external-system names.
- Run the project language validator for changed requirement files before presenting the work as complete.
- Run `scripts/validate-requirements-profile.py` for changed profiled root documents. Do not force legacy documents into the profile during an unrelated edit.
- Keep business requirements, system requirements, acceptance criteria, API contracts and examples traceable to source materials.
- When current implementation facts affect requirements, inspect the registered local repository assigned role `code` automatically under `core/code-inspection.md`. Record the exact commit and relative evidence paths; do not infer business intent from code alone.
- Analyst code inspection improves the requirement input but does not replace the receiving SDD's comparison against its current code before decomposition and implementation.
- Only the user-owner may mark requirements as approved. Record the approver and date, and create a new revision for later semantic changes.

## Read-only task tracker rules

- Follow `core/tracker-reading.md` for every MCP-backed task lookup or comparison.
- Resolve SberTrek and Jira by available capabilities, not by a persisted MCP server name.
- SberTrek is primary. Jira only fills absent fields, adds non-duplicate history events and exposes conflicts.
- Merge paired issue fields independently. For `epic`, `assignee`, the general estimate and each `AN / BE / FE / QA` estimate, use a populated SberTrek value; otherwise use a populated Jira value. If both are populated and differ, keep SberTrek and expose the conflict. Normalize story points and person-days to `story-points` at the agreed 1:1 ratio; do not convert other units silently. Only when no role estimate exists, infer a general estimate as `AN`, `BE` or `FE` from one unambiguous task prefix; never infer a multi-prefix or unprefixed task, and never count the general estimate in addition to populated role estimates.
- Epic and release are independent task groupings. Neither grouping automatically defines an analytical feature.
- Development completion by reassignment requires an analyst-supplied roster `team_id` and history proving the latest developer-to-non-developer handoff. The role is derived only from the `AN/BE/FE/QA` prefix; aliases `A/B/F/Q` normalize to those prefixes. Several provider account IDs may map to one `team_id`; never invent a new team member merely because Jira or SberTrek returned another account ID for the same person. Explicit excluded or completed statuses override this inference. Apply it to configured development work-item types, which may include a tracker Story. Classify a nonterminal unit as `in-progress` only when its current assignee is a mapped developer or its history proves a developer assignment without a later handoff. Complete empty history plus no assignee means `not-started`; incomplete evidence means `unknown`.
- Resolve a lifecycle conflict only when paired mapped current assignees disagree on developer versus non-developer and the independently derived provider states differ. `reconcile` asks exactly one task at a time and must expose five choices: prefer SberTrek for this task, prefer SberTrek for this and every later conflict in the current run, prefer Jira for this task, prefer Jira for this and every later conflict in the current run, or set a custom state for this task. Persist the answer only through `set-development-decision`; an apply-to-all choice is scoped to the current run and is forbidden for a custom state.
- Reduce the request to one explicit epic or an explicit set of task keys in a known provider. Ask one question when the provider or scope is ambiguous. Use only the exact targeted TQL/JQL returned by `trackerctl`; never build a full-project inventory or use title/description search. For a Jira epic, find its zero-or-one SberTrek Epic counterpart by the Jira epic's own `issue_key`, then read the found SberTrek epic through `linkedUnitsOf`; never reverse-search SberTrek by each Jira member key. Per-task `issue_key` reverse lookup is reserved for an explicit Jira task scope. Missing releases are proposals for later actual-progress application, not read-time writes.
- Treat `trackerctl config-status`, `begin` and `reconcile` as mandatory fail-closed gates. Run `config-status` as a standalone command without `head`, `tail`, `grep`, `jq` or another pipeline that can hide its exit code.
- On exit code `3` with `must_stop: true` from any `trackerctl` command, perform only `allowed_next_action: ask-user`: emit exactly `response_contract.text`, without a preface, explanation, examples or suggested answers. Do not call tracker MCP tools, search for task facts, create a task list, delegate work or present a tracker summary.
- Configure completed and excluded statuses separately for SberTrek and Jira; never combine provider-specific answers into one list.
- After every real tracker MCP call, the isolated collector immediately records provider, operation, outcome, evidence, exact query when applicable and a bounded summary. Successful collection responses from both providers go directly through structural `ingest-query-response`; failed Jira counterpart queries and both history providers use `mcp-log`. Jira and SberTrek history use one canonical call per key. The collector must pass the exact job key to `mcp-log` and must never relabel one response as evidence for another key. SberTrek collection must use `issue.exportJson` or a semantically equivalent bulk JSON export operation: pass the exact TQL through its `query` parameter with `max_results: 50`, never through `issue.search.text`, and never substitute pagination, `issue.getByKey` or `link.list`. The projection must include the top-level `attributes` container; parse assignee, Jira counterpart, general and role estimates, and releases from it instead of requesting custom codes in place of the container. A successful provider bulk response passes the complete MCP-produced JSON file directly to `ingest-query-response --max-results 50`, which atomically logs evidence, computes SHA-256 and the item count, and records every compact card. A SberTrek result of exactly 50 cards must expose `sbertrek-export-limit-reached:50`. Never inspect a truncated preview, supply `fields=null` when projection is available, create a shortened response copy, log a planned call, MCP server names, credentials or complete response bodies. Collection jobs preserve compact cards only; history jobs preserve only `assignee` and `status` events.
- `begin` requires one explicit scope kind (`epic` or `tasks`), provider (`jira` or `sbertrek`), scope ids, label, source and intent (`read-only` or `update-planning`). Never infer the provider from key syntax. Only one unfinished run may exist; a second `begin` is blocked until completion or explicit coordinator `abandon-run`. It creates a `targeted-tracker-v3` job with the exact query and hash. The coordinator obtains `collector-brief` and delegates the job to a fresh isolated subagent. The collector reads only `core/tracker-collector.md` and that job, never runs `begin`, never changes `run_id` or job path, never edits runtime JSON directly, does not read MCP documentation and does not change the query. Jira epic discovery is the sole allowed Jira detail call: read `issuelinks`, structurally select only `PartOf + inward_issue`, then bulk-read all selected keys without type or status filters. The counterpart collector for that scope first bulk-searches SberTrek for one Epic whose `issue_key` equals the source Jira epic key; `trackerctl` validates the result and derives the standard `linkedUnitsOf` member query from the returned SberTrek key.
- The coordinator must never call tracker MCP. Between jobs it reads only `run-status`; it must not ingest provider files or collector task summaries. After a collector error it may read `run-status` once and must then stop and report the block; it must not try `issue.getByKey`, `link.list`, search or any alternative MCP route. Each collector stops after `collector-complete` or `history-job-complete` and returns status/paths only. If subagents are unavailable, stop instead of falling back to the main context. Present task facts only after deterministic `reconcile` returns `tracker-read-reconciled`, `workflow_complete: true` and `final_response_allowed: true` for the same `run_id`.
- Pair tracker issues only when the SberTrek card's returned `issue_key` (`Объект Jira`) equals `Jira.key`. Equality of own keys, text, epics, releases and manual mappings never creates a pair.
- If an exact Jira counterpart query explicitly reports that one or more requested keys do not exist, the collector records the full error and passes exactly those keys to `jira-record-absent-counterparts`; only `trackerctl` may derive the retry JQL. Exclude each SberTrek issue whose populated `issue_key` is confirmed absent in Jira from merged issues, estimates, statuses, groupings and history collection. Keep SberTrek issues whose `issue_key` is genuinely absent.
- Do not proceed with an incomplete configuration, pending collector job, unfinished exact-query pagination, a missing compact card for any returned key, incomplete history batch or unknown target-task participant. `set-participant` is valid only for the exact pending participant and `run_id`; ask all returned participant questions sequentially and accept only a roster `team_id`, never an invented role.
- Copy tracker timestamps without changing their timezone. Report deterministic `counts`, `summary` and `limitations` from trackerctl verbatim. Never manually reconstruct totals for estimates, statuses, development states, discrepancies or missing counterparts.
- An `update-planning` application creates one execution row and one Gantt bar for each populated role estimate. Rows may share the same tracker key; their unique work-item ids are `<tracker-key>/<role>`, and a base-key actualization reference expands to all role rows.
- Tracker collection writes only ignored `.workspace-state/tracker-runs/` jobs, compact provider evidence and reconciliation outputs. A `read-only` run never changes a tracker, analytical artifact, repository branch or Git index. An `update-planning` run may be applied by the main agent only after `planning_application_allowed: true`; collectors never apply it.

## Fast consistency sweep for requirement edits

When a requirement change replaces one variant with another, remove stale tails in the same turn instead of leaving conflicting old wording behind.

Use a two-speed approach:

- `quick local sweep` is the default for minor edits confined to one feature;
- `full sweep` is required only when the change is clearly `cross-feature` or `domain-wide`.

Quick local sweep order:

1. update `features/<feature>/requirements.md`;
2. do not touch immutable exchange revisions;
3. record the complete impact in the root document; do not refresh `domain-impact.md` or derived requirement artifacts during ordinary authoring;
4. run a targeted text search across the current root requirements and other authored sources, excluding immutable exchange revisions;
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
- Before touching `features/<feature>/delivery-prototype/*`, verify in `features/<feature>/prototype-notes.md` that the root prototype is confirmed and development handoff is explicitly allowed.
- If those confirmations are missing, stop and report that delivery-prototype generation is blocked until the root prototype is approved.
- Delivery prototypes are the only place for schematic frontend-facing explanations and must be derived from the confirmed root prototype plus current root requirements.
- Never treat an older slice prototype as the active target merely because `delivery-prototype` mode is active.


## Consistency propagation rules

When changing requirements, domain rules, lifecycle states, roles, API semantics, data model, or shared UI behavior, always perform impact detection in the same turn.

During ordinary requirements authoring, put the decision, classification, affected requirements, baseline artifacts, prototypes and required neighboring work into the root `requirements.md`. Do not refresh `domain-impact.md` or other derived artifacts. Explicit transfer copies only the root requirements and does not create another requirements representation. A separate explicit domain-decision command may update `domain-impact.md`; use `PROJECT_ROOT/planning/consistency-backlog.md` only for concrete propagation deliberately deferred outside the root document.

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
