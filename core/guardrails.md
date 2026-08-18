# Guardrails

## Global

- Do not silently rewrite planning baselines while working in execution mode.
- Do not silently rewrite actual progress while working in planning mode.
- Do not treat scope prototypes as delivery-ready prototypes.
- Do not assume requirement packs map 1:1 to planning stories.
- Do not hide commander baseline from `actual-progress`; that diagram is for plan-vs-fact comparison.
- Do not treat raw imported source materials as canonical current-state documentation when `baseline/current` exists.
- Do not promote a release into `baseline/current` without recording a release package and a baseline version note.

## Mode-sensitive edits

- Planning mode owns planning stories, estimates, and quarter/commander gantt baselines.
- Requirements mode owns slices, requirement packs, and references.
- Scope-prototype mode owns feature-level demo prototypes.
- Delivery-prototype mode owns slice-level handoff prototypes.
- Execution-update mode owns implementation tasks, story actualization mapping, and actual-progress gantt.
- Release-finalization mode owns release packages, baseline promotion, canonical baseline docs, and deployment-final deltas.
