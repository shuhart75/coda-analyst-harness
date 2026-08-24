# Run Loop Contract

Workflow modes define which source-of-truth artifacts may be changed. Run kinds define how one bounded unit of work is executed and verified.

## Shared Loop

Every run follows this control cycle:

1. orient from a small work packet;
2. plan the next bounded action;
3. execute one action;
4. run deterministic verification;
5. review the result independently from generation;
6. checkpoint evidence and unresolved questions;
7. continue, complete, or escalate.

A failed check does not advance the run. Repeated failure reaches the configured iteration limit and changes the run to `escalated`.

## Run Kinds

- `planning`: intake, delta, role stories, estimates, dependencies, capacity schedule, review, approval.
- `requirements`: root requirements, cross-feature impact, tail cleanup and explicit exchange revisions.
- `implementation`: code research, implementation plan, one small change, deterministic checks, review.
- `qa`: coverage, test design, execution, failure classification, routing gaps to their owner.

## Planning Invariants

- A feature is the quarter-level user or system outcome.
- A feature has at most one planning story per role: `AN`, `BE`, `FE`, `QA`.
- Missing role work means the corresponding story is absent.
- Approved quarter and commander plans are immutable baselines.
- Later scope is represented by task candidates and actual tasks in actual-progress, never by rewriting the approved plan.
- Default efficiency factors are `AN=0.80`, `BE=0.70`, `FE=0.65`, `QA=0.80`.
- FE starts no earlier than three open days after BE starts. If BE is absent, FE starts after AN or at the first available planning window when AN is also absent.
- Risk buffer is at least 20 percent. It changes commander-plan dates without being rendered as a separate management-facing bar.
- Priority is top-to-bottom. Idle roles may pipeline into the next feature, but lower-priority work must not delay newly available higher-priority work.
- Planning maximizes resource use without exceeding 100 percent.

## Requirement Impact Invariants

- Cross-feature work caused by the current feature is part of the initiating feature scope and HLE.
- Current requirements contain a dedicated `Доработки затронутых функциональностей` section.
- Every impact row is covered by root requirements and acceptance examples, or is marked `not applicable` with a reason.
- Local stale tails block completion. Cross-mode propagation may be deferred only through a concrete consistency backlog record.
- When requirements depend on current implementation, analyst research is bounded to one registered `coda` contour, records the exact commit, and verifies that the code worktree is unchanged.
- Analyst code evidence improves the input but never replaces developer-side reconciliation against the implementation branch used for delivery.

## Requirement Preparation Invariants

- Ordinary requirement authoring changes only `features/<feature>/requirements.md` as the requirements artifact. The state file and bounded code-research evidence are control and auxiliary records, not a decomposition of the requirements.
- Slices, contour packs and preliminary task candidates are never created by the requirements process. An exchange revision is created only after the analyst explicitly requests or accepts transfer.
- Every root change records `analyst` or `developer-result` origin in `requirements-state.json`.
- A `developer-result` change never triggers or offers a revision.
- After an analyst change to previously transmitted requirements, the LLM offers a new revision at most once. A refusal suppresses further offers until an explicit preparation command.
- Explicit preparation validates the root, copies only `requirements.md`, updates `manifest.json` and records the actual destination.

## Technical Decomposition Invariants

- Analysts transmit one immutable feature requirements document plus a manifest; developers define the future Jira decomposition in their own process.
- `returns/tasks.md` appears only after developers have agreed the decomposition. The analytical workflow has no proposal or confirmation state for it.
- Each task has one independently implementable technical result and direct links to `REQ-*`.
- Estimate and Jira key are optional developer metadata.
- Target size is 1-3 days for one executor. Maximum size is 5 days for backend and 10 days for frontend; an explicitly estimated excess requires a reason.
- Publication of `tasks.md` is a background result for the analyst and never gates development.
- The analyst decides separately how to use returned tasks in actual-progress. Approved planning baselines remain immutable.

## Developer Handoff Invariants

These invariants apply to the receiving developer SDD in its own code workspace. The analyst harness may use only the protected `requirements-exchange/**` transfer operation defined in `core/developer-handoff.md`; it may never change product code.

- Reconcile every requirement and scenario independently against one recorded code revision.
- Existing, differently named, or partially implemented behavior is evidence, not a package-level failure.
- Treat the input package as an immutable comparison point; never rewrite it to match the implementation.
- Let the developer-side workflow choose the technical approach, order, design artifacts and commit split.
- Continue all independent work and complete implementation, verification and commits that are feasible in the code repository.
- Report every input item, remaining work and any additional delivery in the per-task result and final summary without hiding scope differences.
- Return baseline and requirement feedback in the same revision's `returns/`; analyst review may happen later and never gates development or testing.
- The canonical return contract is defined in `core/developer-handoff.md`.
