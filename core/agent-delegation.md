# Agent Delegation Rules

Use subagents only when the CLI/runtime supports them and only for bounded, non-overlapping work.

## When to delegate

Delegate only if all conditions hold:
- the subtask is clearly scoped and self-contained;
- the result can be integrated without redoing the work manually;
- the subtask is not the immediate blocking step on the critical path;
- file ownership can be made explicit.

## What to delegate

Good delegation targets:
- one feature or one slice worth of read-only analysis;
- one isolated code/document patch with a disjoint write scope;
- one verification or data-extraction task that can run in parallel.

Avoid delegating:
- baseline promotion decisions;
- cross-feature semantic reconciliation that requires one coherent judgment;
- the exact next step when the main agent is blocked on that answer.

## Ownership rules

When delegating implementation or document edits:
- state exact file paths or directories the subagent owns;
- state that other agents may be editing nearby files;
- instruct the subagent not to revert unrelated changes;
- require a short return note listing changed files.

## For this workflow

Preferred parallelization examples:
- one agent analyzes raw source materials while the main agent prepares target structure;
- one agent fills one feature's `domain-impact.md` while another normalizes a different feature;
- one agent validates links/structure while the main agent reviews generated baseline files.


## Consistency responsibilities

Subagents may detect and draft impact, but the main agent owns semantic consistency.

Subagents may:
- inspect neighboring features for impact;
- propose affected requirements/baseline/prototype lists;
- draft local `domain-impact.md` sections in owned files;
- propose consistency backlog items.

Subagents must not independently finalize:
- shared baseline/current updates;
- cross-feature requirement reconciliation;
- release promotion;
- rollback decisions.

For cross-feature work, use a hub-and-spoke model: subagents produce candidate impact notes, the main agent integrates and writes the final shared artifacts.

## Integration rule

The main agent remains responsible for:
- final consistency of baseline/current;
- final consistency of release packages;
- final story/task mapping semantics;
- any promotion into canonical source-of-truth files.
