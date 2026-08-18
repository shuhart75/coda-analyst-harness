# Context Policy

This policy defines how assistants keep large planning, requirements, prototype, execution and release work usable with small context windows.

## Principle

Users work in the terms of their role. They should not need to ask for context summaries, checkpoints, research files, or completeness sweeps.

The harness performs context operations automatically when a workflow action would otherwise require broad rereading or long memory.

## Source Of Truth

Repository markdown artifacts remain the source of truth:

- `baseline/current/`
- `planning/intake/`
- `features/<feature>/requirements.md`
- `features/<feature>/slices/*`
- `features/<feature>/planning/*`
- `features/<feature>/domain-impact.md`
- `features/<feature>/prototype.html` and prototype notes
- `releases/*`

Context summaries, checkpoints, `.research/` files and external memory tools are accelerators. They are not authoritative unless a decision is promoted back into the source-of-truth artifacts.

## Context Artifacts

Use these lightweight artifacts when a feature, slice, planning track, prototype or execution update becomes too large for one pass:

- `features/<feature>/context-summary.md`
- `features/<feature>/artifact-map.md`
- `features/<feature>/planning/planning-context.md`
- `features/<feature>/planning/assumptions.md`
- `features/<feature>/planning/risk-register.md`
- `features/<feature>/planning/story-map.md`
- `features/<feature>/execution-context.md`
- `features/<feature>/slices/<slice>/context-summary.md`
- `.workspace-state/run-state/current.md`
- local code-inspection state under the user state directory, created by `scripts/code-inspect.py`

Keep each context summary short enough to read at the start of a small-window session. Prefer links to source files over copying full requirement text.

## Automatic Context Triggers

The assistant should automatically build or refresh context when:

- starting a new feature intake;
- creating or substantially updating feature requirements;
- decomposing root requirements into slices;
- creating or updating detailed slice requirements;
- resolving current implementation facts needed by planning or requirements;
- creating or updating root or slice prototypes;
- preparing a slice for development;
- preparing implementation plans or QA checks;
- updating actual progress;
- finalizing a release or promoting baseline;
- resuming after compaction or a long interruption.

Do not expose these internal operations as user-facing commands unless the user explicitly asks to inspect or repair context artifacts.

## Checkpoints

Before and after long-running work, update `.workspace-state/run-state/current.md` or a mode-specific checkpoint with:

- active mode;
- objective;
- feature and slice, if any;
- source materials already inspected;
- current source-of-truth files;
- decisions made;
- assumptions;
- open questions;
- next step;
- files that must not be touched.

If a session resumes after compaction, read the checkpoint first, then only the linked context summaries and source files needed for the next step.

## Facts, Inferences, Assumptions

Separate these explicitly:

- `Fact` - directly supported by a source artifact.
- `Inference` - reasoned from facts, but not explicitly stated.
- `Assumption` - useful working premise that needs confirmation.
- `Open question` - requires user or stakeholder decision.

Do not silently promote an inference or assumption into requirements, planning, prototype, execution, or baseline artifacts.

## External Memory

External memory systems, including MCP-based memory, may be used only as optional accelerators.

Rules:

- Treat external memory as stale until verified against repository files.
- Do not store secrets or sensitive business content in external memory.
- Do not make external memory a required dependency of the harness.
- If memory-derived content affects source-of-truth artifacts, verify and write the decision into repository markdown.

## Context Hygiene

- Prefer targeted reads over whole-repo rereads.
- Prefer artifact maps and context summaries over copying large documents.
- Keep research outputs under `.research/` and mark them as auxiliary.
- Keep code references relative to the registered repository and bind them to a full commit identifier; never store analyst-machine absolute code paths in requirements.
- Delete or archive temporary research only after its accepted decisions are transferred to source-of-truth artifacts.
- If context and source disagree, source wins and context must be updated.
