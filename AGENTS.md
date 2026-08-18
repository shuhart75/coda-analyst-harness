# Harness Rules

This repository defines a reusable workflow harness.

## Always read first

When working inside a project that uses this harness, read in this order:

1. `AGENTS.md`
2. `.workflow/llm-contract.md`
3. `.workflow/requirements-profile.md` before authoring or substantially rewriting requirements
4. `.workflow/agent-delegation.md`
5. `.workflow/skills-policy.md`
6. `.workflow/tooling-policy.md`
7. `.workflow/context-policy.md`
8. `.workflow/research-policy.md`
9. `.workflow/code-inspection.md`
10. `.workflow/run-loop.md`
11. `.workflow/harness.json`
12. `.workflow/run-state/session-brief.md` when present
13. `.workflow/active-mode.md`
14. `.workflow/modes/<active-mode>.md`
15. `.workflow/team.md` before planning resources or regenerating actual-progress
16. relevant files under `.workflow/overrides/`

## Primary workflow rule

Treat workflow mode as a hard guardrail.

- Do not change artifacts outside the active mode unless the user explicitly asks for a mode switch.
- If the requested change belongs to another mode, switch mode first or ask the user to confirm the switch.

## Canonical distinctions

Project-local intake templates live in `.workflow/templates/intake/`. Use them before scaffolding a new feature from an external folder or an unstructured initiative.

Project-local requirement templates live in `.workflow/templates/requirements/`. Use them as the active template source when writing or updating requirement packs.

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

The project-local `.workflow/llm-contract.md` is the canonical CLI-neutral contract for Codex, Claude, Qwen, VSCodium agents, and similar assistants. Follow it before applying mode-specific rules.

## Companion policies

Project-local files `.workflow/agent-delegation.md`, `.workflow/skills-policy.md` and `.workflow/tooling-policy.md` define how an LLM should use delegation, reusable skills, and tools within this workflow.

## Consistency backlog

When a local change affects neighboring requirements, baseline artifacts, or prototypes and cannot be fully propagated immediately, record it in `.workflow/consistency-backlog.md`.

## Command catalog

Use `.workflow/command-catalog.md` to interpret short workflow commands like `делаем требования`, `обнови реальный прогресс`, `актуализируй прототипы`, or `промоуть в baseline`.

Use `.workflow/command-cheatsheet.md` as the preferred quick-reference list of ready-to-send Russian prompt phrasings.

## Context and research

Context summaries, checkpoints and research files are internal harness operations, not extra commands the user must remember.

- Use `.workflow/context-policy.md` to decide when to create or refresh context summaries and checkpoints.
- Use `.workflow/research-policy.md` to run role-based research for large features, slices, prototypes, development handoff, implementation planning and QA checks.
- Treat `.research/`, context summaries and external memory as auxiliary. Accepted findings must be transferred into the authoritative planning, requirements, prototype, execution, release or baseline artifacts.

## Analyst code inspection

- Use `.workflow/code-inspection.md` when the analyst asks to inspect code or when current implementation facts are needed for planning or requirements.
- Resolve `coda` through `.workflow/code-repos.json`; never require the user to provide a path in each prompt.
- Treat `coda` as read-only in analyst work. Record its branch, commit and worktree state before inspection and verify that they are unchanged afterward.
- Inspect one contour at a time. Read that contour's local instructions, locate exact identifiers, then open only matched modules and nearby tests, contracts or migrations.
- Code observations are commit-bound auxiliary evidence, not automatic business requirements or baseline updates.

## Executable harness

- Run `.workflow/tools/harnessctl.py doctor <project>` before broad workflow changes.
- Use `.workflow/tools/harnessctl.py session-brief <project>` for progressive context disclosure.
- Approved quarter and commander plans are immutable planning baselines.
- Route later scope into task candidates and actual-progress instead of rewriting an approved plan.

## Requirements language

- Write requirement prose in Russian.
- Keep English only for exact code, paths, API/database identifiers, enum values, and fixed external-system names.
- Prefer a Russian explanation before an unavoidable special term.
- Run `.workflow/tools/validate-language.py` for changed requirements before completion.
- Run `.workflow/tools/validate-requirements-profile.py` for changed root documents that use the profile marker.
