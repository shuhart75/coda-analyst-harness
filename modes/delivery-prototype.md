# Mode: delivery-prototype

## Goal

Build a precise feature-level handoff prototype derived from the confirmed common feature prototype and the root feature requirements.

## Main inputs

- `features/*/prototype.html`
- `features/*/prototype-notes.md`
- `features/*/requirements.md`

## Main outputs

- `features/*/delivery-prototype/prototype.html`
- `features/*/delivery-prototype/notes.md`
- updated `features/*/context-summary.md` when prototype state changes

## Hard gate before generation

Before touching any `delivery-prototype/*`, the agent must verify all of the following:

1. `features/<feature>/prototype.html` exists.
2. `features/<feature>/prototype-notes.md` exists.
3. `features/<feature>/prototype-notes.md` explicitly says:
   - `Пользователь подтвердил, что общий prototype завершён: да`
   - `Можно переходить к прототипу для разработки: да`

If any of these checks fails, stop immediately. Do not edit the delivery prototype. Tell the user that the workflow is blocked until the common root prototype is approved.

## Rules

- Derive the delivery prototype from the root feature prototype and root feature requirements, not from scratch.
- Treat the root prototype as the visual source of truth.
- Keep the current handoff format: schematic, explanatory, and convenient for frontend implementation.
- Frontend comments, implementation notes and API reminders belong only in the delivery prototype and its `notes.md` file.
- Align with the chosen project visual language and MUI usage.
- Show real states, realistic component usage, and explicit notes for the frontend developer.
- If the delivery prototype exposes a missing or conflicting requirement, update `features/<feature>/requirements.md` first, then re-derive the prototype.
- Automatically gather context from root requirements, root prototype and prototype notes.
- Before presenting the prototype as ready, automatically check it against the root requirements and confirmed root prototype.
- After creating or materially changing the delivery prototype, refresh the feature context summary when present or needed for continuation.

## Forbidden without mode switch

- editing or replacing the common root prototype instead of the requested delivery prototype
- changing planning estimates
- reframing MVP scope silently
- inventing UI decisions that contradict the confirmed root prototype
