# Mode: execution-update

## Goal

Track real work after planning.

## Main artifacts

- `features/*/slices/*/execution/tasks.md`
- `features/*/slices/*/execution/tasks/*.md`
- `planning/*/gantt/actual-progress.puml`

## Allowed changes

- task status
- executor
- planned dates
- actual dates
- description and notes
- milestones and factual notes in actual-progress gantt

## Forbidden without mode switch

- changing agreed planning estimates
- changing quarter or commander baseline gantt silently
