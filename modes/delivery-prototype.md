# Mode: delivery-prototype

## Goal

Build precise slice-level handoff prototypes derived from the confirmed common feature prototype and the root feature requirements.

## Main inputs

- `features/*/prototype.html`
- `features/*/prototype-notes.md`
- `features/*/requirements.md`
- `features/*/slices/*/slice.md`
- `features/*/slices/*/requirements/frontend.md`
- `features/*/slices/*/requirements/backend.md`

## Main outputs

- `features/*/slices/*/delivery-prototype/prototype.html`
- `features/*/slices/*/delivery-prototype/notes.md`
- updated `features/*/slices/*/context-summary.md` when slice prototype state changes

## Hard gate before generation

Before touching any `slices/*/delivery-prototype/*`, the agent must verify all of the following:

1. `features/<feature>/prototype.html` exists.
2. `features/<feature>/prototype-notes.md` exists.
3. `features/<feature>/prototype-notes.md` explicitly says:
   - `Пользователь подтвердил, что общий prototype завершён: да`
   - `Можно переходить к slice delivery prototypes: да`

If any of these checks fails, stop immediately. Do not edit any slice prototype. Tell the user that the workflow is blocked until the common root prototype is approved.

## Rules

- Never use the active `delivery-prototype` mode as a reason to start from a slice prototype.
- Derive slice prototypes from the root feature prototype and root feature requirements, not from scratch.
- Treat the root prototype as the visual source of truth.
- Keep the current handoff format: schematic, explanatory, and convenient for frontend implementation.
- Frontend comments, implementation notes and API reminders belong only in slice-level delivery prototypes and their `notes.md` files.
- Align with the chosen project visual language and MUI usage.
- Show real states, realistic component usage, and explicit notes for the frontend developer.
- If a slice prototype exposes a missing or conflicting requirement, update `features/<feature>/requirements.md` first, then re-derive the slice prototype.
- Automatically gather slice context from root requirements, slice card, FE/BE packs, root prototype and prototype notes. The user should not need a separate `собери контекст среза` command.
- Before presenting the prototype as ready, automatically check it against the slice requirements and confirmed root prototype.
- After creating or materially changing a slice prototype, refresh the slice context summary when present or when the slice is large enough to need small-window continuation.

## Forbidden without mode switch

- editing or replacing the common root prototype instead of the requested slice prototype
- changing planning estimates
- reframing MVP scope silently
- inventing slice-level UI decisions that contradict the confirmed root prototype
