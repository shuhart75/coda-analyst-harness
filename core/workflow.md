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
