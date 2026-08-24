# Skills Policy

This harness is CLI-neutral, so "skills" may come from Codex, Claude, Qwen, or project-local prompting conventions.

## Principle

Use a skill only when it adds repeatable domain value or enforces a stable workflow pattern.

## Recommended skill categories

- `planning-analyst` — HLE decomposition, planning stories, estimates, gantt semantics.
- `requirements-analyst` — business/system requirements from baseline and source materials.
- `scope-prototyper` — planning-stage clickable prototype with fake data.
- `delivery-prototyper` — feature-level MUI handoff prototype.
- `execution-tracker` — implementation task updates and actual-progress mapping.
- `release-promoter` — final requirements, baseline promotion, release package assembly.
- `domain-curator` — baseline/current/domain maintenance and DDD normalization.
- `context-curator` — small-window feature/planning summaries, artifact maps and checkpoints.
- `research-analyst` — bounded role-based research over requirements, prototypes, source materials or code.
- `qa-analyst` — requirement-level checks, negative scenarios and coverage matrices.

The harness ships CLI-neutral skill contracts under `skills/`. Native agent platforms may wrap these contracts, but their mode, inputs, write scope, and validation rules remain canonical.

## Skill input discipline

A skill should explicitly state:
- which mode it assumes;
- which directories are canonical inputs;
- which files it is allowed to write;
- what validation is expected after completion.

## Skill anti-patterns

Do not create skills that:
- duplicate one-off commands with no reusable logic;
- bypass mode boundaries;
- assume one vendor-specific tool unless clearly marked;
- silently mutate canonical baseline files without release-finalization context.
- expose internal context/research/checkpoint operations as mandatory user commands instead of automating them under role-oriented commands.
