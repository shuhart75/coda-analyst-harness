# Tooling Policy

## Tool selection

Prefer the simplest tool that preserves auditability.

- Use markdown as the source of truth for planning, requirements, execution mapping and release notes.
- Use generated PlantUML only for gantt rendering, not for storing semantic mapping.
- Keep raw evidence in `context/source-materials/`.

## Validation discipline

After structural or canonical changes, run available validators.
After gantt-related changes, regenerate the gantt. For actual-progress, the regeneration must also refresh the standalone Confluence export without includes.
After release promotion, validate both structure and links again.
Before broad workflow work, run `harnessctl doctor`. Before approving a plan, run workflow, planning, link, context, and trace checks.

## Editing discipline

- Prefer small explicit edits over large opaque rewrites.
- Do not regenerate unrelated files just because a tool can.
- Preserve human-readable names in markdown; keep slugs and ids stable.
- Before the final response after file edits, and always before a commit, review only the changes made in the current turn:
  - Necessity: each change must directly support the user's request or required workflow consistency.
  - Correctness: changes must match source-of-truth artifacts, active mode rules, links, generated artifacts and available validators.
  - Conciseness: simplify without losing traceability, auditability or clarity.
- If this review finds an issue inside the allowed scope, fix it before responding. If it is outside the active mode, touches unrelated user changes, or needs a business decision, do not fix silently; report it or ask.
- Do not remove traceability, source references, acceptance criteria, risk records or consistency records only because they look verbose.
- After requirement edits, use targeted text search or an equivalent local find-in-files sweep for superseded terms such as old endpoints, field names, role names, status values and UX labels.
- Keep the sweep proportional: start with the current feature and explicitly affected artifacts; expand to neighboring features or baseline only when the change is cross-feature or domain-wide.
- If the project provides `.workflow/tools/find-stale-terms.py`, use it as the fast default helper for local tail cleanup; otherwise use the platform's normal text search.
- If the user asks for a PlantUML file "without includes" or Confluence-ready code, expand `!include` directives with `.workflow/tools/expand-plantuml-includes.py` when available instead of editing generated gantt sources by hand.
- Use `.workflow/tools/validate-context.py` after adding or materially changing context, research, handoff, implementation-plan or test-plan conventions.
- Use `.workflow/tools/validate-language.py` after changing requirements. Treat avoidable anglicisms as a completion blocker, not as optional editorial cleanup.

## CLI-neutrality

Do not assume a specific terminal agent supports:
- subagents;
- skills;
- memory;
- local plugins.

When such capabilities exist, use them as optional accelerators, not as the only workflow path.

## Consistency records

- `features/*/domain-impact.md` stores per-feature impact and affected artifact lists.
- `.workflow/consistency-backlog.md` stores unresolved or deferred propagation work, including prototype drift and rollback propagation.
- Do not hide known inconsistency in chat only; record it in one of those files.

## Context and research records

- `features/*/context-summary.md` stores the small-window feature context.
- `features/*/artifact-map.md` distinguishes authored, derived and auxiliary artifacts.
- `features/*/slices/*/context-summary.md` stores the small-window slice context.
- `features/*/slices/*/.research/` stores auxiliary research, not source-of-truth requirements.
- `features/*/slices/*/implementation-handoff.md` and `execution/implementation-plan.md` are development aids and must reference requirements.
- `features/*/slices/*/testing/test-plan.md` is a QA aid and should include coverage back to requirements.
- `.workflow/run-state/current.md` may be used as a resumable checkpoint for long-running work.
- `.workflow/runs/*/run.json` stores executable loop state and verification evidence.
