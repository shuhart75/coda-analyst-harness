# Planning Loop

Mode: `planning`

Inputs: intake, baseline/current, feature priorities, role estimates, team roster, closed intervals, risks.

Writes: feature role stories, estimates, dependencies, plan state, quarter/commander includes, retrospective draft.

Rules: maximum one story per AN/BE/FE/QA; maximize capacity without exceeding 100%; approved plans are immutable.

Confluence: every created or changed Gantt must have a matching standalone `<name>-confluence.puml` without includes, generated with `scripts/expand-plantuml-includes.py` under `core/tooling-policy.md`. Refresh it after include/preamble changes and preserve mode boundaries and approved plans. Save source and export together.

Validation: `validate-workflow.py`, `validate-planning.py`, `sync-planning-gantt.py` while draft, then `sync-quarter-gantt.py`.
