# Implementation Loop

Run kind: `implementation`

Inputs: confirmed developer card from the latest decomposition snapshot, related requirements and slices, local SDD, code root.

Writes: code, tests, implementation plan, verification evidence, review summary.

Rules: one bounded committable action per iteration; deterministic checks before review; preserve the input package and confirmed card as comparison points; do not invent ambiguous product semantics; continue independent work; record deviations, remaining work and additional delivery in the task implementation receipt. Analyst processing is asynchronous and never gates implementation.
