# Prototype Templates

This directory contains the canonical prototype templates used by the harness.

## What lives here

- `prototype.html.template` — single-file React + MUI prototype scaffold
- `feature-prototype-notes.template.md` — notes for the common root feature prototype
- `scope-prototype-notes.template.md` — optional notes for planning-specific scope mirror
- `delivery-prototype-notes.template.md` — notes for slice-level handoff prototypes

## Workflow order

1. Inspect existing prototypes of the same feature first.
2. Inspect screenshots, current-system pages and other visual references.
3. Clarify with the user which artifact is the base for style, colors and layout when that choice is not explicit.
4. Create or update the common feature-level prototype in `features/<feature>/prototype.html` and `features/<feature>/prototype-notes.md`.
5. Use that root prototype as a user-facing clickable prototype for the whole feature, without frontend handoff comments inside the HTML.
6. Iterate only on that common prototype until the user explicitly confirms it is finished.
7. Only after that derive slice-level `delivery-prototype/*` artifacts.

## Rules

- Default stack: single-file `prototype.html`, React + MUI via CDN, no build step.
- Use only MUI components unless a project override says otherwise.
- The common feature prototype is the visual source for slice-level handoff prototypes.
- Slice-level delivery prototypes are the only place where frontend-facing explanatory comments and implementation hints belong.
- Delivery prototypes stay schematic and explanatory for frontend implementation.
- If a prototype reveals a requirement gap, update `features/<feature>/requirements.md` first and then re-derive the affected prototype.
