# Harness Rules

This repository defines a reusable workflow harness.

## Always read first

When working inside a project that uses this harness, read in this order:

1. `AGENTS.md`
2. `.workflow/active-mode.md`
3. `.workflow/modes/<active-mode>.md`
4. relevant files under `.workflow/overrides/`

## Primary workflow rule

Treat workflow mode as a hard guardrail.

- Do not change artifacts outside the active mode unless the user explicitly asks for a mode switch.
- If the requested change belongs to another mode, switch mode first or ask the user to confirm the switch.

## Canonical distinctions

- `planning story` is a planning and estimation unit only.
- `implementation task` is an execution tracking unit only.
- They are related, but they are not the same artifact.

## Feature-centered structure

Work should be grouped by:

- `feature`
- then `slice`
- then FE/BE requirement packs and execution artifacts

## Prototype stack

Use React + MUI without a build step unless a project override explicitly says otherwise.
