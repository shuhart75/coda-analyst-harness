# Research Policy

This policy adapts useful ideas from `openspec-skills` without adopting OpenSpec as the source-of-truth model.

## Purpose

Research helps an assistant inspect large requirement packs, prototypes, source materials or code without loading everything into the main context.

Research results are auxiliary. Accepted findings must be transferred into the relevant requirements, planning, prototype, execution, testing, domain-impact, consistency backlog, release or baseline artifacts.

## When Research Runs Automatically

Run research internally when:

- decomposing a large feature into slices;
- preparing detailed requirements for slices;
- checking slice completeness;
- preparing a slice for development;
- building an implementation plan;
- preparing QA checks and negative scenarios;
- updating requirements from source materials that touch several areas;
- establishing current implementation facts needed for planning or requirements;
- validating that prototypes still match requirements.

The user should not need to call `исследуй срез` or similar internal commands.

## Research Output

For feature-level research, use:

```text
features/<feature>/.research/
```

For slice-level research, use:

```text
features/<feature>/slices/<slice>/.research/
```

Expected files:

- `frontend.yaml`
- `backend.yaml`
- `data.yaml`
- `integrations.yaml`
- `errors-validation.yaml`
- `roles-access.yaml`
- `observability-config.yaml`
- `code-evidence.yaml` when findings come from a local code clone
- `summary.md`

Use only the files needed for the current action. Do not create empty research files just to satisfy a template.

## Internal Research Roles

| Role | Focus |
|---|---|
| `frontend` | Screens, states, user actions, visibility, UX errors, prototype alignment |
| `backend` | API, handlers, business rules, server-side constraints |
| `data` | Entities, fields, tables, migrations, dictionaries |
| `integrations` | External systems, events, queues, files, documents, exchange statuses |
| `errors-validation` | Validation, errors, retries, manual resolution, edge cases |
| `roles-access` | Roles, permissions, screen/action availability |
| `observability-config` | Logs, metrics, audit, configuration, operational signals |
| `code-evidence` | Commit-bound implementation facts with relative paths and symbols |

## Completeness Checklist

For requirements and slice readiness, check:

- purpose and user outcome;
- scope boundaries and out-of-scope items;
- roles and access;
- UI states and prototype alignment;
- API and backend behavior;
- data model and migrations;
- integrations and external ownership;
- validation and errors;
- statuses and lifecycle transitions;
- logging, metrics, audit and configuration;
- acceptance checks and negative scenarios;
- affected neighboring features, baseline and prototypes.

If a checklist item is not affected, record `not affected` in the relevant summary or handoff. If it is unclear, record an open question.

## Aggregation Rules

After research:

1. Store short role outputs before aggregating.
2. Build `summary.md` with facts, inferences, assumptions and open questions.
3. Deduplicate by stable keys: endpoint, field, role, status, screen, integration, config parameter, metric.
4. Transfer accepted findings into source-of-truth files.
5. Record unresolved propagation in `documents/planning/consistency-backlog.md`.

## Guardrails

- Do not create `change.md` as a parallel source of truth.
- Do not merge research directly into `baseline/current/` outside release finalization.
- Do not let `.research/` replace feature requirements or slice packs.
- Do not continue silently if research finds a business decision, contradiction or prototype mismatch.
- Do not treat the current implementation as the owner of business intent. Separate code facts from inferences and ask the analyst when a semantic choice remains.
- Do not copy source fragments into the documents repository. Record only the commit, relative path, symbol and short observation.
