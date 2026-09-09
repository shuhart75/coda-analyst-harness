# Workflow Model

## Layers

1. Baseline
2. Planning
3. Requirements
4. Scope prototyping
5. Delivery prototyping
6. Execution updates
7. Release finalization

## Main containers

- `baseline`
- `quarter`
- `feature`
- `business delivery stage` (a sequential registry entity, not a new document directory)
- `exchange revision`
- `release`

## Main artifact types

- `baseline snapshot`
- `planning story`
- `feature requirements document`
- `scope prototype`
- `delivery prototype`
- `exchange manifest`
- `agreed developer task list`
- `developer task result`
- `developer summary`
- `implementation task`
- `release package`
- `domain impact note`

## Baseline model

- `baseline/current` is the canonical description of the deployed system
- `baseline/versions` stores previous deployed baselines
- `features/*` are working deltas against `baseline/current`
- `releases/*` are immutable release packages used to promote a new baseline

## Sequential delivery model

Follow `core/delivery-stages.md`: accepted feature -> business delivery stage ->
revision. Each stage targets a complete useful outcome approved by the analyst;
BE/FE tasks and technical slices are not business stages. Parallel stages are
unsupported. The requirements mode owns the single authored current-stage root
and the stage registry in `requirements-state.json`, without text copies.
Future scope is tentative backlog or archive, not another normative target.

Authoring review is allowed in a feature branch. Delivery audit requires accepted
current main and its collaboration gate; publication then requires all three audit
levels, explicit confirmation and human PR/MR acceptance for code delivery.
A pending branch is not delivery. Revision numbers remain global; `stage_id` and
`stage_revision` add stage identity without replacing `return_id`.

Closure requires the current detailed reviewed summary covering every input REQ
and explicit residual disposition; `unknown` and `investigate` block it. Acceptance,
deployment, report completion and closure are distinct. A next stage needs previous
closure and explicit analyst scope choice; residual scope is never automatic.
Dependencies use facts, and omitted REQs do not repeal deployed behavior. Changes
or removal must be explicit. Historical inputs/returns and legacy bindings are
not rewritten automatically. Release and baseline writes retain their existing mode.

## Planning views

- `quarter plan` uses planning stories
- `commander plan` uses planning stories with extra buffer
- `actual progress` overlays commander baseline planning stories with current execution items

All three views should keep the same feature lanes where possible.

## Actual-progress semantics

- baseline for schedule comparison is `commander-plan`
- baseline stories stay visible even after execution is materialized into real Jira tasks
- current execution layer may contain:
  - `virtual` execution items
  - `real` implementation tasks
  - a `mixed` combination of both
- when a planning story is replaced by real tasks, old virtual execution items should be marked as superseded or moved into residual scope explicitly
