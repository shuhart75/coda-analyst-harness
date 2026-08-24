# QA Loop

Run kind: `qa`

Inputs: active feature requirements, acceptance criteria, agreed developer tasks, per-task results, prototype and tested code revisions.

Writes: test plan, coverage matrix, executable/manual checks and classified gaps in the developer-owned process; factual coverage is reflected in the active revision's `returns/summary.md`.

Rules: every check traces directly to `REQ-*`; returned tasks and results are supporting context. Failures are classified as requirement gap, implementation defect, test defect, data/environment issue, or accepted limitation. Testing never rewrites immutable input requirements.
