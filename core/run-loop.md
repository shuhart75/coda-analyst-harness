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
- `requirements`: root requirements, slices, detailed packs, cross-feature impact and tail cleanup.
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
- Every impact row is covered by requirements, task candidates, and checks or explicitly marked `not applicable` with a reason.
- Local stale tails block completion. Cross-mode propagation may be deferred only through a concrete consistency backlog record.
- When requirements depend on current implementation, analyst research is bounded to one registered `coda` contour, records the exact commit, and verifies that the code worktree is unchanged.
- Analyst code evidence improves the input but never replaces developer-side reconciliation against the implementation branch used for delivery.

## Technical Decomposition Invariants

- Analysts transmit one immutable feature requirements package with slices; developers define the future Jira decomposition after targeted code research.
- Each active development card has one contour and one independently implementable technical result.
- Each card is self-contained and is the primary input for the implementation plan. Links provide traceability rather than missing instructions.
- Estimate and Jira key are optional developer metadata. Their absence never blocks decomposition confirmation or implementation.
- Target size is 1-3 days for one executor. Maximum size is 5 days for backend and 10 days for frontend; an explicitly estimated excess requires a reason.
- Confirmation creates a background snapshot for the analyst and immediately permits development without analyst approval.
- The analyst decides separately whether to materialize returned cards into actual-progress. Approved planning baselines remain immutable.
- Slices remain the primary QA units and use development cards and implementation receipts as supporting context.

## Developer Handoff Invariants

- Reconcile every requirement and scenario independently against one recorded code revision.
- Existing, differently named, or partially implemented behavior is evidence, not a package-level failure.
- Treat the input package as an immutable comparison point; never rewrite it to match the implementation.
- Let the developer-side workflow choose the technical approach, order, design artifacts and commit split.
- Continue all independent work and complete implementation, verification and commits that are feasible in the code repository.
- Report every input item, remaining work and any additional delivery in the receipt without hiding scope differences.
- Return baseline and requirement feedback in receipts; analyst review may happen later and never gates development or testing.
- The canonical states and receipt contract are defined in `.workflow/developer-handoff.md`.
