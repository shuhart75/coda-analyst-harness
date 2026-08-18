# Repository Exchange Policy

This policy governs the one-way integration path from `changeswork-copy` to `documents` and the verified reverse patch returned to the source maintainers.

## Repository roles

| Repository | Local access | Remote access | Purpose |
|---|---|---|---|
| `documents` | read and write | pull and push | Normal analyst work, requirements, planning and factual progress |
| `changeswork-copy` | read only | pull only | Upstream analytical source received from GitHub |
| `coda` | read only | pull only | Implementation evidence for analytical work |

The repositories are independent clones under the `coda-analyst-harness` root. They are not submodules and must not be copied into one another.

## Synchronization transaction

The command `repository-exchange.py sync` performs one guarded transaction:

1. Acquire the workspace lock so a second exchange cannot run concurrently.
2. Require clean `main` worktrees in `changeswork-copy` and `documents`.
3. Fetch and fast-forward both local clones from their own `origin/main`.
4. Fetch the local `changeswork-copy/main` commit into `documents` through a dedicated local remote.
5. Merge it into `documents/main` with a normal non-fast-forward Git merge when needed.
6. Abort and stop on any conflict. Never overwrite the conflict with copied files.
7. Compare the exact Git trees of the source commit and resulting `documents` commit.
8. Create a binary-capable reverse patch and prove in a temporary Git index that applying it to the recorded source commit reproduces the exact `documents` tree.
9. Push only `documents/main`, unless `--no-push` was explicitly requested.

A failed fetch, non-fast-forward local update, merge, verification or push leaves a visible error. The command must not suppress it or silently choose a side.

## Reverse patch

`repository-exchange.py reverse-diff` does not update or merge repositories. It compares their current clean commits and writes:

- `reverse-diffs/reverse-diff-YYYYMMDD-HHMMSS.patch`;
- `reverse-diffs/reverse-diff-latest.patch`;
- `reverse-diffs/reverse-diff-latest.json` with source and target commits, trees and verification state.

The patch is intended for the maintainers of `changeswork-copy`. Normal analyst work does not apply, commit or push it. When both trees are identical, stale `reverse-diff-latest.patch` is removed and the metadata records that no patch is required.

## Prohibited shortcuts

- no `rsync` or recursive copy between repositories;
- no `git push` from `changeswork-copy` or `coda`;
- no `reset --hard`, `clean`, force push or automatic branch switching;
- no ignored `pull`, merge or push failures;
- no automatic conflict resolution;
- no reverse patch created from dirty working trees;
- no claim of equality without exact tree verification.
