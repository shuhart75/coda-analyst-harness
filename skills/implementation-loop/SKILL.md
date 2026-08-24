# Implementation Loop

Run kind: `implementation`

Inputs: one agreed task from `returns/tasks.md`, related `REQ-*` from the active immutable requirements revision, local SDD and code root.

Writes: code, tests, implementation plan, verification evidence, review summary.

Rules: one bounded committable action per iteration; deterministic checks before review; preserve the input revision as the comparison point; do not invent ambiguous product semantics; continue independent work; record deviations, remaining work and additional delivery in `returns/tasks/<task-id>.md`. Analyst processing is asynchronous and never gates implementation.
