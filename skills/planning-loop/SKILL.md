# Planning Loop

Mode: `planning`

Inputs: intake, baseline/current, feature priorities, role estimates, team roster, closed intervals, risks.

Writes: feature role stories, estimates, dependencies, plan state, quarter/commander includes, retrospective draft.

Rules: maximum one story per AN/BE/FE/QA; maximize capacity without exceeding 100%; approved plans are immutable.

Validation: `validate-workflow.py`, `validate-planning.py`, `sync-planning-gantt.py` while draft, then `sync-quarter-gantt.py`.
