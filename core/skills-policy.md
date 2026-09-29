# Skills Policy

This AS KODA variant uses native GigaCode skills under `.gigacode/skills/`.
The independent generic `analyst-harness` retains its CLI-neutral contracts.

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

GigaCode discovers the native analyst skills by their `name` and `description`
frontmatter. Load the selected SKILL.md before its procedure, and read its DOCS
references only for the requested operation. Slash commands live under
`.gigacode/commands/`; Russian phrases retain the command catalog semantics.
Bootstrap projects these files into the analytics workspace as ignored local
files while preserving settings and user memory.

The older `skills/` contracts remain reference material. In particular,
`implementation-loop` and `qa-loop` describe the developer-owned process;
they are not installed as analyst skills and cannot authorize code or returns
writes. Native routing and the operation contracts define the analyst scope.

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
