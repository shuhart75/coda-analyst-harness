# Harness Rules

This repository defines a reusable workflow harness.

## First launch and workspace ownership

- The repository root is the workspace root.
- If `.workspace-state/workspace.json`, `coda-analyst.code-workspace`, or any of `documents/`, `coda/`, and `changeswork-copy/` is absent, run `python3 scripts/workspace.py bootstrap` before the user's substantive request. Do not ask for repository URLs; the product URLs are fixed by this harness.
- Work on requirements, plans and factual progress only in `documents/`. The harness contract stays in this root; `documents/` must not contain an embedded `.workflow`, `AGENTS.md`, or `.vscode` harness copy.
- Treat `coda/` as read-only during analytical work. It is available only for bounded implementation research.
- Treat `changeswork-copy/` as pull-only. Never commit or push there during normal analyst work. Transfer differences back only through a verified reverse patch in `reverse-diffs/`.
- Only `documents/` may be pushed by this harness.

## Repository exchange commands

- `синкани репы`, `синхронизируй репозитории`, `обнови documents из changeswork-copy`: run `python3 scripts/repository-exchange.py sync`. This explicitly authorizes updating both local clones, merging `changeswork-copy/main` into `documents/main`, generating a verified reverse patch, and pushing only `documents/main`.
- `синкани без отправки`, `обнови локально без push`: run `python3 scripts/repository-exchange.py sync --no-push`.
- `сделай обратный дифф`, `собери обратную заплату`, `подготовь изменения для changeswork-copy`: run `python3 scripts/repository-exchange.py reverse-diff`. Do not apply or push the patch unless the user separately asks.
- `обнови код`, `обнови coda`: run `python3 scripts/workspace.py update-code`. This is a separate read-only fast-forward update and is not part of repository exchange.
- Never hide a failed fetch, merge, patch verification, or push. A merge conflict stops the operation and is aborted; no file-copy fallback is permitted.

## Always read first

When working in this workspace, read in this order:

1. `AGENTS.md`
2. `core/llm-contract.md`
3. `core/requirements-profile.md` before authoring or substantially rewriting requirements
4. `core/agent-delegation.md`
5. `core/skills-policy.md`
6. `core/tooling-policy.md`
7. `core/context-policy.md`
8. `core/research-policy.md`
9. `core/code-inspection.md`
10. `core/run-loop.md`
11. `.workspace-state/run-state/session-brief.md` when present
12. `.workspace-state/active-mode.md`
13. `modes/<active-mode>.md`
14. `documents/README.md`
15. `documents/planning/team.md` before planning resources or regenerating actual-progress
16. relevant files under `documents/context/project-rules/`

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

## Feature-centered structure

Work should be grouped by:

- `feature`
- then `slice`
- then FE/BE requirement packs and execution artifacts

## Developer handoff

- Treat `сформируй пакет для разработки` and its documented Russian synonyms as one end-to-end analyst command: validate and safely repair requirements, ask one semantic question at a time when needed, then publish directly to `sent`. Do not leave a `ready` revision for analyst inspection.
- For new work, send one `feature-delivery` package containing root requirements and slices; do not pre-author the final Jira decomposition under `features/<feature>/tasks/`.
- In a received package, read `handoff.json`, then `request.md` and `manifest.json`, then only the requirements and slices for one selected contour. Read that contour's local SDD before opening matched code and nearby tests. Never load all of `coda` or both contours by default.
- Developers own confirmed `DEV-BE-*` and `DEV-FE-*` cards after inspecting their local SDD and code.
- Create every card from `development-task-card.template.md`, fill every section, and preserve the complete Russian `Короткие команды разработчика` block after every update.
- A confirmed decomposition snapshot is delivered to the analyst in the background and never blocks implementation.
- Keep decomposition state, implementation receipts and slice test receipts independent.
- QA works by slice; development cards and implementation receipts are supporting context.
- Preserve already sent input revisions and confirmed decomposition snapshots as immutable history.
- Treat `traceability.mode = legacy-sections` as deliberate section-level compatibility. Do not invent `REQ-*` or `SCN-*` absent from the source.

## Prototype stack

Use React + MUI without a build step unless a project override explicitly says otherwise.

## LLM contract

`core/llm-contract.md` is the canonical CLI-neutral contract for Codex, Claude, Qwen, VSCodium agents, and similar assistants. Follow it before applying mode-specific rules.

## Companion policies

Files `core/agent-delegation.md`, `core/skills-policy.md` and `core/tooling-policy.md` define how an LLM should use delegation, reusable skills, and tools within this workflow.

## Consistency backlog

When a local change affects neighboring requirements, baseline artifacts, or prototypes and cannot be fully propagated immediately, record it in `documents/planning/consistency-backlog.md`.

## Command catalog

Use `templates/workflow/command-catalog.template.md` to interpret short workflow commands like `делаем требования`, `обнови реальный прогресс`, `актуализируй прототипы`, or `промоуть в baseline`.

Use `templates/workflow/command-cheatsheet.template.md` as the preferred quick-reference list of ready-to-send Russian prompt phrasings.

## Context and research

Context summaries, checkpoints and research files are internal harness operations, not extra commands the user must remember.

- Use `core/context-policy.md` to decide when to create or refresh context summaries and checkpoints.
- Use `core/research-policy.md` to run role-based research for large features, slices, prototypes, development handoff, implementation planning and QA checks.
- Treat `.research/`, context summaries and external memory as auxiliary. Accepted findings must be transferred into the authoritative planning, requirements, prototype, execution, release or baseline artifacts.

## Analyst code inspection

- Use `core/code-inspection.md` when the analyst asks to inspect code or when current implementation facts are needed for planning or requirements.
- Resolve `coda` through `templates/workflow/code-repos.template.json`; never require the user to provide a path in each prompt.
- Treat `coda` as read-only in analyst work. Record its branch, commit and worktree state before inspection and verify that they are unchanged afterward.
- Inspect one contour at a time. Read that contour's local instructions, locate exact identifiers, then open only matched modules and nearby tests, contracts or migrations.
- Code observations are commit-bound auxiliary evidence, not automatic business requirements or baseline updates.

## Repository exchange policy

- Follow `core/repository-exchange.md` for all operations between `changeswork-copy` and `documents`.
- Do not use `rsync`, file copying, force push, automatic conflict resolution, or destructive cleanup as a substitute for Git merge.

## Executable harness

- Run `python3 scripts/harnessctl.py doctor documents` before broad workflow changes.
- Use `python3 scripts/harnessctl.py session-brief documents` for progressive context disclosure.
- Approved quarter and commander plans are immutable planning baselines.
- Route later scope into task candidates and actual-progress instead of rewriting an approved plan.

## Requirements language

- Write requirement prose in Russian.
- Keep English only for exact code, paths, API/database identifiers, enum values, and fixed external-system names.
- Prefer a Russian explanation before an unavoidable special term.
- Run `python3 scripts/validate-language.py documents` for changed requirements before completion.
- Run `python3 scripts/validate-requirements-profile.py documents` for changed root documents that use the profile marker.
