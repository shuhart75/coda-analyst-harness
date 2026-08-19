# Code Inspection Policy

This policy governs read-only use of a locally cloned code repository by analysts and their LLM tools.

## Repository layout

The `coda-analyst-harness` clone is `HARNESS_ROOT` and contains three independent product repositories, only roles `analytics` and `code` having working trees:

```text
<workspace>/
├── AGENTS.md
├── coda-analyst.code-workspace
├── documents/                    # default analytics, PROJECT_ROOT
├── .workspace-state/
│   └── repositories/
│       └── changeswork-copy.git/  # hidden bare mirror, no worktree
└── coda/                         # default code, read-only
    ├── backend/
    └── frontend/
```

Role `analytics` remains the requirements and planning repository. Role `code` remains an independent code repository. The hidden bare mirror assigned role `source` participates only in the exchange process defined by `core/repository-exchange.md`; it is not a code-inspection target and must not be accessed directly. Do not create submodules or symlinks between these repositories.

The common workspace gives the LLM filesystem access to the registered repositories. It does not authorize whole-repository reading and does not place all code into model context.

## Resolution and setup

Repository identity, accepted remotes, relative location and contours are stored in `.workspace-state/code-repos.json`. The committed template contains only the default role mapping.

Resolution order:

1. environment variable declared by the repository entry, for example `CODA_REPO`;
2. path relative to `PROJECT_ROOT`, normally `../coda` for the default role mapping.

The harness must not store analyst-machine absolute paths in committed project files.

`python3 scripts/workspace.py bootstrap` creates the repositories and `coda-analyst.code-workspace`. Do not use `code-inspect.py setup` to replace the root `AGENTS.md`; the root contract belongs to this harness.

## Read-only contract

During analyst planning and requirements work, `coda` is read-only.

Before inspection:

- resolve and validate the registered repository;
- record branch, full commit, origin, contour and worktree state;
- require a clean worktree for evidence used in requirements;
- read the selected contour's own agent or SDD instructions.

After inspection, compare branch, commit and worktree entries with the initial snapshot. Any change blocks completion until it is understood and removed by the repository owner.

Do not fetch, pull, switch branches, build, generate, format, install dependencies, run migrations, edit code or execute commands that may create files unless the user explicitly requests a separate code operation.

This is a workflow guard, not an operating-system sandbox. Use a client-provided read-only mount for the `coda` root when available, but still perform the before/after verification.

## Bounded discovery

Code inspection is a targeted research action, not a repository audit.

1. Start from the feature question and exact identifiers already present in requirements or baseline.
2. Select one contour, `backend` or `frontend`.
3. Read local instructions for that contour.
4. Use bounded filename/content search to locate exact routes, fields, statuses, tables, classes or components.
5. Open only matched modules, adjacent tests and necessary contracts or migrations.
6. Inspect the second contour in a separate pass only when a concrete dependency is found.
7. Stop when the question has sufficient evidence; do not broaden the search without a new question.

The public user does not need to provide paths. `code-inspect.py locate` returns a capped list of repository-relative matches and the exact inspected commit.

## Automatic triggers

The LLM may inspect code without an extra confirmation when the action is read-only and current implementation is needed to:

- answer an explicit request such as `сходи в код` or `проверь по коду`;
- establish current API, data, status, role or validation behavior;
- check whether an expected capability already exists;
- identify affected neighboring code before writing requirements;
- resolve a factual mismatch between `baseline/current/`, requirements and implementation;
- refresh previously recorded code evidence after the local `coda` commit changes.

Do not inspect code merely to derive a business decision that the code does not own. Ask the analyst when evidence leaves a semantic choice.

## Evidence and authority

For an ad hoc answer, report the inspected commit and relevant repository-relative paths without creating project files.

When code findings affect requirements, use `features/<feature>/.research/code-evidence.yaml`. Record:

- repository, branch, full commit and clean worktree state;
- one contour and one bounded question;
- facts, inferences, assumptions and open questions separately;
- relative paths, symbols and short observations without copying source code;
- related requirement identifiers and transfer destination.

Code evidence is auxiliary and commit-bound. Accepted findings must be transferred into root requirements, derived slice packs, `domain-impact.md` or the consistency backlog. Do not update `baseline/current/` from code research outside the existing release-finalization rules.

## Two-stage reconciliation

Analyst-side inspection improves requirements against a recorded local code revision. It does not replace developer-side reconciliation.

Before implementation, the receiving SDD repeats a targeted comparison against its current branch because the code may be newer, differently configured or already changed. Developer findings and actual delivery are returned through the existing decomposition and receipt lifecycle.
