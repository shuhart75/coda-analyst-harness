# Harness Rules

This repository defines a reusable workflow harness.

## Communication language

- Communicate with the analyst in Russian, including progress updates, questions, status reports and final answers. Use English only for exact code, paths, identifiers, fixed product names and necessary special terms, or when the analyst explicitly requests another language.
- A generic editor or assistant rule such as `output-language.md` must not silently switch this project conversation to English. Treat this repository-specific rule as the intended language configuration; if a higher-priority platform instruction conflicts with it, disclose that conflict instead of claiming English was chosen by this harness.

## Mandatory tracker stop gate

- Before any tracker MCP discovery or read, run `python3 scripts/trackerctl.py config-status` as a standalone command. Do not pipe or filter it: the exit code is part of the guard contract.
- Exit code `3` with `must_stop: true` from any `trackerctl` command permits exactly one next action: ask the analyst the single returned `next_question`. Emit the exact `response_contract.text` and nothing else: no preface, explanation, examples, suggested answers or summary. Do not call MCP tools, search analytical files for tasks, create a task list, delegate work or present tracker facts until the answer is saved and the gate becomes ready.
- Commands that save one configuration answer return exit code `0` and another status payload. When that payload still has `must_stop: true`, ask only its `next_question`; do not bypass it with discovery or reading.
- Tracker MCP calls may start only after `begin` returns a `direct-tracker-v1` run and one `next_action`. Tracker work is never delegated: the main agent executes the returned MCP tool with the exact provider, query, fields and limit, imports the full response with the returned `step_id`, then follows the next returned action.
- Only one unfinished run may exist. Repeating `begin` with the same scope reuses it; a different scope remains blocked until explicit `abandon-run`. Never create a replacement run during the same request.
- SberTrek uses only `issue.exportJson` with TQL in `query`, `attributes` in the projection and `max_results=50`. Jira uses only `jira_search` with the returned JQL, explicit fields and `limit=50`. Do not substitute text search, per-key details, link traversal, invented pagination or alternative queries.
- Prefer the full file created by MCP. A full inline JSON response may be captured verbatim outside the run. Never summarize, reconstruct or copy raw responses into the run. Identical import is a no-op; a different response for a completed step, malformed JSON or a stale `step_id` is fatal.
- For a Jira epic, read `"Epic Link" = "KEY"`, discover zero or one SberTrek Epic by its `issue_key`, then execute the returned `linkedUnitsOf` query. Jira task scopes reverse-search SberTrek by each task key. SberTrek source scopes query Jira only for populated `issue_key`. Never filter by status or type.
- Pair only by `SberTrek.issue_key == Jira.key`. Filled SberTrek values win separately for epic, assignee, general estimate and each `AN / BE / FE / QA`; Jira fills gaps and conflicts stay visible. A confirmed nonexistent Jira counterpart excludes only the linked SberTrek task; a SberTrek task without `issue_key` remains.
- Exactly 50 cards and Jira metadata proving a larger result remain visible limitations. History is outside this protocol; facts that require it remain `unknown` with `history-not-collected`.
- `reconcile` only saves the result and returns `next_action.type=result-status`; it never authorizes a user-facing summary. Run the returned command, and only when `result-status` returns `status: tracker-read-reconciled`, `workflow_complete: true` and `final_response_allowed: true`, emit its checksum-validated `response_contract.text` verbatim with nothing added.

## First launch and workspace ownership

- The `coda-analyst-harness` repository is `HARNESS_ROOT`. The repository assigned role `analytics` is `PROJECT_ROOT`.
- In a deployed analyst workspace, `HARNESS_ROOT` is a read-only installed component. Update it only with `git pull --ff-only`; never edit, commit or push its tracked files unless the user explicitly asked to develop the harness itself. Runtime state and reverse patches may be written only to Git-ignored paths registered below and must not make the harness worktree dirty.
- Resolve `PROJECT_ROOT` with `python3 scripts/workspace.py project-root`; never infer it from the current directory or a repository name.
- Resolve every relative harness path and command in this file against `HARNESS_ROOT`. When launched from `PROJECT_ROOT`, use the absolute `HARNESS_ROOT` written in the local entry point; never create replacement `scripts/`, `core/`, `modes/` or `templates/` under `PROJECT_ROOT`.
- The default role mapping is fixed: `analytics=documents`, `code=coda`, `source=changeswork-copy`. Do not ask about roles or propose changing them during normal bootstrap. Use `configure-roles` only after an explicit analyst instruction to reassign roles.
- If workspace state, a default-role repository, or the workspace file is absent, run `python3 scripts/workspace.py bootstrap` before the user's substantive request. Do not ask for repository URLs; the product URLs are fixed by this harness. On the first launch this prepares all three roles. After a successful launch, a manually removed `code` or `source` repository is an allowed reduced-workspace state and `bootstrap` must not recreate it. A removed `analytics` repository remains a blocking error because it may contain unpushed work.
- Work on requirements, plans and factual progress only under `PROJECT_ROOT`. The harness contract stays in `HARNESS_ROOT`; the tracked analytics tree must not contain an embedded `.workflow`, `.vscode`, or harness copy of `AGENTS.md`. A generated local `AGENTS.md` with marker `analyst-harness-local-entrypoint:v1` is allowed, ignored by Git, and must not be committed.
- Treat the repository assigned role `code` as strictly read-only except for registered operations: initial clone setup, `git pull --ff-only` through `workspace.py update-code`, and transfer through `requirements-exchange.py prepare`. Transfer must use an isolated temporary clone, may commit and push only an existing root `requirements-exchange/**`, and must leave the ordinary code clone unchanged. It falls back to `PROJECT_ROOT/requirements-exchange/` when the code repository, root catalog, read access or push access is unavailable. Do not otherwise change files, index, branch, `HEAD`, remotes, configuration or generated artifacts. A user prompt alone cannot authorize another exception.
- The repository assigned role `source` exists only as a hidden bare mirror under `.workspace-state/repositories/`. It has no working tree and must never be opened, edited, checked out, committed to, used as a command working directory, or added to the editor workspace. Only `scripts/workspace.py` and `scripts/repository-exchange.py` may access it.
- A legacy root `changeswork-copy/`, when found by `bootstrap`, is retired under `.workspace-state/retired-repositories/` and is never an exchange input. Never restore, inspect, edit or use a retired checkout unless the user explicitly requests recovery of its files.
- Ordinary workspace and collaboration operations may push only the repository assigned role `analytics`. The sole `code` exception is `requirements-exchange.py prepare`, which pushes an isolated temporary clone and only under an existing root `requirements-exchange/**`. Role `source` is fetch-only on this machine; applying its reverse patch belongs to a separate machine where that repository is writable.
- In role `analytics`, never use `git add -A`, `git add .`, or another broad staging command. Stage, restore, remove and commit only exact reviewed paths. This rule also applies while resolving one merge conflict or repairing one Unicode path.

## Repository exchange commands

- `синкани репы`, `синхронизируй репозитории`, `обнови репы`, `обнови репозитории`, `обнови documents из changeswork-copy`: run `python3 scripts/workspace.py bootstrap`, then `python3 scripts/workspace.py sync`. If collaboration migration is not configured and `analytics/main` contains local work, migrate that work into a feature branch before sync; never commit it directly in `main`. The command performs the protected `code` pull when that role exists. It then fetches `analytics/origin/main`, fast-forwards when possible, preserves local-ahead history, and creates a normal merge commit for conflict-free divergence. With `source` present, it next merges `source` into `analytics`, verifies and archives the reverse patch, and pushes only `analytics/main`. Without `source`, it records reverse-patch verification as unavailable and still updates and pushes `analytics`. Without `code`, it skips all code access. Neither missing optional role is an error.
- Full repository exchange runs only from `analytics/main`. If the checked-out feature branch has collaboration status `awaiting-merge`, `workspace.py sync` first runs the guarded `collaboration.py finish`. When `origin/main` contains the submitted commit, finish switches to and updates `main`, closes the local feature session, and the same sync command continues. If containment is not proven, or the feature work has another status, sync stops before updating `code` or `source`; it is never a synonym for updating that branch.
- Report `source` and `analytics` as identical only when the newly fetched source tree already equals the analytics tree. A non-empty verified patch means `source_analytics_state=reverse-diff-pending` even after `analytics` was pushed; do not present this as complete synchronization.
- After `workspace.py sync`, start the user-facing result with the exact value of `report_message`. Do not prepend or append a broader success claim. The phrase `all repositories synchronized` and its Russian equivalents are allowed only when `all_repositories_synchronized=true`; when it is false, explicitly retain the limitation from `report_message`.
- `синкани без отправки`, `обнови локально без push`: run `python3 scripts/workspace.py bootstrap`, then `python3 scripts/workspace.py sync --no-push`; available repositories are updated under the same reduced-workspace rules, while analytics is not pushed.
- `обнови код`, `обнови coda`, `обнови репу с кодом`, `обнови кодовый репозиторий`: run only `python3 scripts/workspace.py update-code`. This authorizes exactly one guarded `git pull --ff-only`, not arbitrary code changes.
- After `source-analytics-merge-conflict`, run `python3 scripts/workspace.py inspect-source-analytics-conflict`. It reproduces the merge only in a temporary clone and must leave all real repositories unchanged. Explain each path and request one analyst decision at a time unless the report identifies deletion of a forbidden legacy harness path. Never invent another workspace subcommand.
- After `analytics-origin-merge-conflict`, run `python3 scripts/workspace.py inspect-analytics-origin-conflict`. It reproduces the conflict in a temporary clone, leaves the real repository clean and reports local and remote blobs separately. Request one analyst decision at a time; after all decisions, perform a normal merge of `origin/main`, stage only exact resolved paths, run checks, commit the merge and rerun sync.
- If `analytics-origin-merge-in-progress` is reported, do not abort, reset or restart the user's existing merge. Run the same inspection command against the active stages, resolve one path at a time, stage it with `git add -- <exact-path>`, run checks, commit the merge and rerun sync. Never use rebase or force push to repair analytics divergence.
- Before every fast-forward or merge that can move `analytics/main`, the harness creates a local protective snapshot under `.workspace-state/analytics-snapshots/` and Git refs under `refs/coda-analyst-harness/analytics-snapshots/`. These refs are never pushed. On conflict, base, local and incoming file versions are archived before the harness aborts its own merge. Do not delete, rewrite or publish these snapshots.
- If conflict resolution lost or replaced content, run `python3 scripts/workspace.py list-analytics-snapshots`, inspect the selected snapshot with `inspect-analytics-snapshot --snapshot <id>`, then restore only an explicitly selected file with `restore-analytics-snapshot-file --snapshot <id> --side <base|local|incoming> --path <exact-path>`. Restoration is never automatic, staged or committed by the command. Review the resulting one-file worktree change before any commit.
- Never recover a conflict with a broad checkout, directory copying, reset, rebase, force push, `git add -A` or `git add .`. A side choice applies to one exact path only; ask the analyst when the intended content is uncertain.
- `сделай обратный дифф`, `собери обратную заплату`, `подготовь изменения для changeswork-copy`: run `python3 scripts/repository-exchange.py reverse-diff`. It only creates and verifies the patch; it never applies or pushes it. If `source` is absent, explain that verification is unavailable; do not recreate it or present an older patch as current.
- Reverse-patch verification must run `git diff --check` between the exact source and analytics commits before writing any patch or metadata. Any whitespace error blocks creation and must be corrected in `analytics`; never repair an immutable transport pair after generation.
- Reverse-patch artifacts live under ignored `HARNESS_ROOT/reverse-diffs/`. Never stage or commit that directory. Every timestamped `patch + json` pair is immutable local exchange history; `reverse-diff-latest.*` is only a replaceable convenience pointer. A harness `git pull --ff-only` must leave both the history and current local artifacts untouched.
- Never hide a failed fetch, merge, patch verification, or push. A merge conflict stops the operation; no file-copy fallback is permitted. The harness aborts only a merge it started itself and leaves a pre-existing user merge active. Continue through the matching inspection command and conscious path-level resolution. Never offer a code-repository update, rebase, reset, force push, skipping the source merge or overwriting either analytics history as a migration option.
- A non-NFC path in `source` blocks synchronization. The one safe migration exception for `analytics` is a filesystem-normalized alias with identical bytes whose old tracked path is removed by the incoming `source` commit.
- An `analytics-content-policy` failure blocks merge, reverse-patch creation and push. Remove only the exact reported local/tool artifacts. For each reported source deletion, ask the analyst whether to restore it or approve that exact deletion; run `repository-exchange.py approve-deletion --path <path>` only after an explicit approval. Never treat `verified=true` as content approval: schema 2 requires both `tree_verified=true` and `content_policy_verified=true`.

## Always read first

When working in this workspace, read in this order:

1. `AGENTS.md`
2. `core/llm-contract.md`
3. `core/requirements-profile.md` before authoring or substantially rewriting requirements
4. `core/requirements-wording.md` before writing or checking requirement prose
5. `core/requirements-audit.md` before checking or delivering requirements
6. `core/agent-delegation.md`
7. `core/skills-policy.md`
8. `core/tooling-policy.md`
9. `core/context-policy.md`
10. `core/research-policy.md`
11. `core/code-inspection.md`
12. `core/tracker-reading.md` when the user asks to read or compare task trackers
13. `core/run-loop.md`
14. `core/collaboration.md` when `.workspace-state/collaboration.json` exists or the user asks to start, save, update, submit or migrate feature work
15. `.workspace-state/run-state/session-brief.md` when present
16. `.workspace-state/active-mode.md`
17. `modes/<active-mode>.md`
18. `PROJECT_ROOT/README.md`
19. `PROJECT_ROOT/planning/team.md` before planning resources or regenerating actual-progress
20. relevant files under `PROJECT_ROOT/context/project-rules/`

## Multi-user feature work

- Commit subjects and bodies must never contain task numbers or tracker identifiers, in any case or spelling. This prohibition also applies to generated, merge and squash commit messages. Use only a semantic description of the change. Task identifiers may remain in tracker data, branches and project artifacts where required, but never in Git commit messages. Before any commit, validate the complete subject and body; never bypass the managed `commit-msg` hook. Bootstrap must fail closed if that hook cannot be installed without replacing an existing custom hook.
- Multi-user branches are the required mode for feature requirements in a deployed `coda-analyst-harness` workspace. A missing `.workspace-state/collaboration.json` means that one-time local migration is still required; it never authorizes work directly in `analytics/main`.
- The canonical start command is `начинаю работу над фичей <feature>`. Treat `пишем требования по фиче <feature>`, `работаем с фичой <feature>`, `беру фичу <feature> в работу`, `берём фичу <feature> в работу`, `начинаем работу над требованиями по <feature>`, `начинаем работы над требованиями по <feature>` and close grammatical variants as exact synonyms. Resolve an existing feature slug with a bounded lookup; ask one question only if the feature is ambiguous.
- On any start command, run `workspace.py bootstrap` and `collaboration.py status` before reading the feature requirements. If status is `migration-required`, ask once for the analyst id, run `collaboration.py migrate --analyst <id>`, and then run `collaboration.py start --feature <slug>`. If migration reports existing local work, ask whether that work belongs to the requested feature before passing `--feature`; never infer ownership. Do not inspect or edit requirements or exchange revisions until the feature branch is active.
- While feature work is active, `сохрани`, `сохрани работу`, `зафиксируй`, `зафиксируй изменения`, `закоммить`, `закоммить изменения` and `сделай коммит` mean: inspect all changes, run applicable checks, ask about any uncertain path, then call `collaboration.py save` with every exact changed path and a semantic commit message. The command pushes only the feature branch. Never invent a commit from ambiguous changes.
- While feature work is active, bare `обнови`, `синкани`, `подтяни изменения`, `обнови мою ветку`, `синкани мою ветку` and `обнови рабочую ветку` mean `collaboration.py update`: merge current `origin/main` into the active feature branch with protective snapshots and push that branch. Exact phrases containing `репы`, `репозитории`, `code` or `coda` retain their registered repository meanings and must not be reinterpreted.
- `требования готовы к объединению`, `отправь требования на проверку`, `подготовь требования к слиянию`, `отправь ветку на слияние`, `вливаем в основную ветку` and `влей в основную ветку` mean: complete requirement checks, save exact changes, update the branch, repeat checks, then run `collaboration.py submit`. This pushes only the feature branch. It does not create a merge request and never creates a developer package. Start the result with the exact `message` returned by the command. If `merge_request_create_url` is present, call it only a link to the merge-request creation form, never a link to an existing or created request.
- `запрос на слияние принят`, `MR принят`, `PR принят`, `ветка принята в main`, `ветка влита в main` and `слияние выполнено` mean: run `collaboration.py finish`, which must first prove that the submitted commit is contained in `origin/main`, then report the resulting collaboration status. These phrases never mean accepting or creating a merge request.
- `сформируй пакет для разработки`, `отправь требования в разработку`, `передай требования разработчикам`, `передай разрабам`, `отдай требования разрабам`, `отправь разрабам`, `отдаём в разработку` and `передаём в разработку` are exact delivery synonyms. First finish a remotely accepted branch with `collaboration.py finish`, then require `collaboration.py require-main-for-delivery --feature <slug>`. Missing collaboration state is a migration blocker, not single-user permission. If the branch is not yet in `origin/main`, stop without creating an exchange revision. After the guard passes, run all three levels from `core/requirements-audit.md`, repair only meaning-preserving issues, ask one semantic question at a time and rerun the complete audit after corrections. Record the audit, show its final report to the analyst, and request explicit confirmation. Only a reply that confirms both the shown audit and transfer authorizes `requirementsctl.py confirm-audit` and `requirements-exchange.py prepare`. Publish the unchanged audited file directly to `sent`; any intervening change requires a new audit. There is no package state `ready`.
- `включи совместную работу`, `включи многопользовательский режим`, `мигрируй на веточную работу` and `переведи documents на работу через ветки` mean the migration procedure in `core/collaboration.md`. Ask for a short analyst id once. If local work exists, ask for exactly one feature before calling `collaboration.py migrate`; never guess its ownership.

## Primary workflow rule

Treat workflow mode as a hard guardrail.

- Do not change artifacts outside the active mode unless the user explicitly asks for a mode switch.
- If the requested change belongs to another mode, switch mode first or ask the user to confirm the switch.

## Canonical distinctions

Intake templates live in `templates/intake/`. Use them before scaffolding a new feature from an external folder or an unstructured initiative.

Requirement templates live in `templates/requirements/`. Use them as the active template source when writing or updating requirement packs.

- `planning story` is a planning and estimation unit only.
- `implementation task` is an execution tracking unit only.
- They are related, but they are not the same artifact.

## Requirements

- Author requirements in Russian. Keep English only for exact code, paths, API and database identifiers, enum values, formats, fixed product names, and necessary technical terms.
- Root requirements are authored. During ordinary work, change only `PROJECT_ROOT/features/<feature>/requirements.md`; do not create slices, contour detail packs, preliminary developer tasks or exchange revisions.
- On an explicit one-time migration from the superseded ISO-shaped document, rename the former root to `requirements_iso.md`, create the new root from the compact template, and then treat the archive as immutable history. Never send `requirements_iso.md` or use it as the authored source.
- Before editing an existing feature, run `requirementsctl.py status`. If it reports an unrecorded divergence from the last published revision, do not guess its origin: ask whether it came from analyst initiative or a registered developer result and record that answer first.
- After every root requirement change, record its origin with `scripts/requirementsctl.py record-change`: `analyst` for an analyst-initiated change or `developer-result` with the stable `return_id` for accepted developer feedback.
- A `developer-result` change never creates or offers an exchange revision. An analyst-initiated change after an existing publication may produce one offer to prepare a new revision. Record the offer before asking; if declined, persist the refusal and do not ask again until an explicit preparation command.
- New root documents follow the compact specification contract in `core/requirements-profile.md`; the ISO-shaped and former sequential profiles are no longer used for new delivery revisions.
- Compact `requirements.md` must not contain `Статус`. Use `requirements-state.json` for authoring and audit state and exchange `manifest.json` for delivery state.
- Requirement prose follows `core/requirements-wording.md`. Use explicit quantities and named referents; every scenario must identify a concrete state, event and observable result. Run `validate-requirements-wording.py` after substantive edits and before showing the document as checked.
- Do not reproduce the developer's technical `spec.md`. Write a business contract with stable `REQ-*` headings and nested Russian `Когда`/`Тогда` scenarios; the receiving SDD derives its own technical delta from it and the code.
- Detect impact on neighboring features, include required neighboring work in the current requirements, and record deferred propagation in `planning/consistency-backlog.md` inside the analytical project.
- Never invent a business rule from code. Code observations are commit-bound technical evidence.
- Only the user-owner may approve requirements or plans.

## Developer handoff

- Treat every documented delivery synonym as a two-stage action. First run all three audit levels in `core/requirements-audit.md`: individual rules, cross-requirement system reasoning and delivery readiness. Repair only meaning-preserving issues, ask one semantic question at a time, recheck affected relations after each answer, then rerun all three levels over the complete document. Show the final audit report and request explicit confirmation. Do not create or publish a revision before that confirmation.
- Record the completed audit with `requirementsctl.py record-audit`. Only an analyst reply that explicitly confirms both the shown audit and transfer authorizes `requirementsctl.py confirm-audit`; silence, an earlier transfer command, or approval of the requirements document itself is not confirmation of the audit.
- After confirmation, publish the unchanged audited file directly to `sent`. If `requirements.md` changes at any point after the audit, repeat the audit and confirmation; `requirements-exchange.py prepare` enforces this checksum boundary.
- Send only one immutable root `requirements.md` plus `manifest.json`. Do not create slices, contour packs, analyst-authored developer tasks or local OpenSpec artifacts on behalf of developers.
- The receiver treats `requirements.md` as an upstream business contract, compares it with current code and creates its own local SDD artifacts separately for backend and frontend. Mixed backend/frontend tasks are forbidden; one `REQ-*` may map to multiple contour tasks.
- Developers first acknowledge the exact revision and checksum with immutable `returns/receipt.json`, then return their already agreed decomposition in `returns/tasks.md`, per-task factual results in `returns/tasks/<task-id>.md`, and final `REQ-*` coverage in `returns/summary.md`. A new revision always requires its own receipt; analyst review never gates development.
- Preserve every sent input revision and its returns as immutable history.
- Always report the actual destination: remote code branch and repository path, or the absolute reserve path in role `analytics`, plus the revision number.
- Never create a ZIP unless the user explicitly requests it. A requested transport ZIP belongs only in `~/Downloads`, never in a repository.

## Prototype stack

Use React + MUI without a build step unless a project override explicitly says otherwise.

## LLM contract

`core/llm-contract.md` is the canonical CLI-neutral contract for Codex, Claude, Qwen, VSCodium agents, and similar assistants. Follow it before applying mode-specific rules.

## Companion policies

Files `core/agent-delegation.md`, `core/skills-policy.md` and `core/tooling-policy.md` define how an LLM should use delegation, reusable skills, and tools within this workflow.

## Consistency backlog

When a local change affects neighboring requirements, baseline artifacts, or prototypes and cannot be fully propagated immediately, record it in `PROJECT_ROOT/planning/consistency-backlog.md`.

## Command catalog

Use `templates/workflow/command-catalog.template.md` to interpret short workflow commands like `делаем требования`, `обнови реальный прогресс`, `актуализируй прототипы`, or `промоуть в baseline`.

Use `templates/workflow/command-cheatsheet.template.md` as the preferred quick-reference list of ready-to-send Russian prompt phrasings.

## Context and research

Context summaries, checkpoints and research files are internal harness operations, not extra commands the user must remember.

- Use `core/context-policy.md` to decide when to create or refresh context summaries and checkpoints.
- Use `core/research-policy.md` to run role-based research for large features, prototypes, development handoff, implementation planning and QA checks.
- Treat `.research/`, context summaries and external memory as auxiliary. Accepted findings must be transferred into the authoritative planning, requirements, prototype, execution, release or baseline artifacts.

## Analyst code inspection

- Use `core/code-inspection.md` when the analyst asks to inspect code or when current implementation facts are needed for planning or requirements.
- Resolve role `code` through `.workspace-state/code-repos.json`; never require the user to provide a path in each prompt.
- Treat role `code` as strictly read-only during inspection. Record its branch, commit, configuration and worktree state before inspection and verify that they are unchanged afterward. Do not fetch, pull, switch, build, format, generate, install, edit, commit or push there; protected pull is a separate workspace operation completed before inspection.
- Inspect one contour at a time. Read that contour's local instructions, locate exact identifiers, then open only matched modules and nearby tests, contracts or migrations.
- Code observations are commit-bound auxiliary evidence, not automatic business requirements or baseline updates.

## Task tracker reading

- `прочитай задачи из трекеров`, `проверь задачи в трекерах`, `сверь SberTrek и Jira`, `покажи текущее состояние по трекерам` and close equivalents mean the read-only workflow in `core/tracker-reading.md`.
- `актуализируй задачи`, `актуализируй Гант` and close equivalents use the same collection workflow with intent `update-planning`; no plan change is allowed before successful reconciliation returns `planning_application_allowed: true`.
- The analyst-specified provider is always read first. The main agent performs all tracker MCP calls itself and follows only the current machine-returned `next_action`; tracker calls are never delegated.
- Merge paired `epic`, `assignee`, general estimate and each `AN / BE / FE / QA` estimate independently: prefer a populated SberTrek value and use Jira only when that exact SberTrek field is missing or not returned. Preserve a populated SberTrek value on conflict and report the discrepancy. Normalize `SP`, story points and person-days to `story-points` using the agreed `1 SP = 1 person-day`; never convert other units silently. If no role estimate is populated but a general estimate exists, assign it to `AN`, `BE` or `FE` only from one unambiguous task prefix. Never guess when the prefix is missing or contains several roles, and never add a general estimate again when at least one role estimate exists.
- Treat epic and release as independent grouping dimensions. Never infer that an epic equals an analytical feature without an explicit mapping.
- Current completed and excluded statuses remain authoritative. This protocol does not read history, so any lifecycle fact requiring an assignment handoff remains `unknown` and is disclosed as `history-not-collected`.
- Reduce every request to an explicitly provided or confirmed scope: one epic or task keys in Jira or SberTrek. Never infer the provider from `PROJECT-123`. For a SberTrek epic use only `unit IN linkedUnitsOf("unit = 'KEY'", "Состоит из")`; for SberTrek task keys use only `unit = "KEY" or ...`; for Jira task keys use only `key IN ("KEY", ...)`. For a Jira epic use one direct `jira_search` with exact JQL `"Epic Link" = "KEY"`, every preferred field and `limit=50`; never call `jira_get_issue`, inspect `issuelinks` or replace it with an intermediate `key IN (...)`. A zero-member epic is proven only by structurally importing the real empty Jira search page. Its reverse SberTrek path is separate: find zero or one SberTrek Epic by the Jira epic's own `issue_key`, then read that SberTrek epic through the standard `linkedUnitsOf` query. Never search SberTrek separately by every Jira member key for an epic scope; that per-task `issue_key` search is allowed only for an explicit Jira task scope. Do not filter Jira members by issue type or status: completed issues are required evidence. Never use a global unfinished-task inventory, title/description search or semantic discovery as a substitute.
- Run the exact linear protocol `config-status -> begin -> (next_action MCPP call -> ingest)* -> reconcile -> result-status`. One user request owns one run. Never create another run, choose another query or use a subagent while it is unfinished.
- Pair only through the value returned by SberTrek in `Объект Jira`: `SberTrek.Объект Jira == Jira.key`. Never pair by equality of own keys, titles, descriptions, assignees, epics, releases, semantic similarity or manual mappings.
- Pass each full MCP JSON to `ingest` with the returned `step_id`; never reconstruct cards from previews. Only `ingest-error` may process a Jira counterpart error that explicitly names nonexistent requested keys. It derives the reduced retry and excludes the linked SberTrek issue; an SberTrek issue without `issue_key` remains.
- When applying an `update-planning` result, materialize one execution row and one Gantt bar for every populated `AN / BE / FE / QA` estimate. The tracker key may repeat across roles; the unique internal work item is `<tracker-key>/<role>`, and its visible summary starts with that role. A base tracker-key reference in actualization expands to all of its role work items.
- Strip square brackets from role prefixes before generating a work item label: `[FE] Name` becomes `FE Name`.
- Present only the exact `response_contract.text` from `result-status`; never recount from MCP responses or result files. The read command may write only ignored runtime artifacts under `.workspace-state/tracker-runs/` and must not change trackers, `PROJECT_ROOT`, requirements, plans, actual-progress or Git state.

## Repository exchange policy

- Follow `core/repository-exchange.md` for all operations between `changeswork-copy` and `documents`.
- Do not use `rsync`, file copying, force push, automatic conflict resolution, or destructive cleanup as a substitute for Git merge.

## Executable harness

- Run `python3 scripts/harnessctl.py doctor "$PROJECT_ROOT"` before broad workflow changes.
- Use `python3 scripts/harnessctl.py session-brief "$PROJECT_ROOT"` for progressive context disclosure.
- Approved quarter and commander plans are immutable planning baselines.
- Route later scope into task candidates and actual-progress instead of rewriting an approved plan.

## Requirements language

- Write requirement prose in Russian.
- Keep English only for exact code, paths, API/database identifiers, enum values, and fixed external-system names.
- Prefer a Russian explanation before an unavoidable special term.
- Run `python3 scripts/validate-language.py "$PROJECT_ROOT"` for changed requirements before completion.
- Run `python3 scripts/validate-requirements-profile.py "$PROJECT_ROOT"` for changed root documents that use the profile marker.
- Run `python3 scripts/validate-requirements-wording.py "$PROJECT_ROOT"` for changed compact requirements. A successful script result never replaces the isolated-reader review from `core/requirements-wording.md`.
