# Repository Exchange Policy

This policy governs the one-way integration path from role `source` to role `analytics` and the verified reverse patch returned to the source maintainers.

## Repository roles

| Repository | Local access | Remote access | Purpose |
|---|---|---|---|
| `analytics` (`documents` by default) | read and write | pull and push | Normal analyst work, requirements, planning and factual progress |
| `source` (`changeswork-copy` by default) | no working tree; hidden bare mirror | fetch only | Upstream analytical source received from GitHub |
| `code` (`coda` by default) | read only | pull only | Implementation evidence for analytical work |

The repositories are independent under the `coda-analyst-harness` root. Roles `analytics` and `code` use normal clones. Role `source` is stored only at `.workspace-state/repositories/<repository-id>.git` as a bare mirror, is excluded from the editor workspace, and has no files that an LLM can edit. They are not submodules and must not be copied into one another. Default roles are used without questions; reassignment is allowed only by an explicit analyst command.

## Synchronization transaction

The command `repository-exchange.py sync` performs one guarded transaction:

1. Acquire the workspace lock so a second exchange cannot run concurrently.
2. Require a valid `source` bare mirror and fetch `source/main` directly into it.
3. Require a clean `analytics/main` worktree. The only exception is a verified filesystem-normalized alias of a non-NFC tracked path that is removed by the incoming source commit and whose bytes equal the indexed blob.
4. Fast-forward `analytics/main` from its own origin.
5. Reject any non-NFC path remaining in `source` or in the merged `analytics` tree.
6. Fetch the local `source/main` commit into `analytics` through a dedicated local remote and merge it normally.
7. Abort and stop on any conflict. Never overwrite the conflict with copied files.
8. Require the merged analytics tree to contain no tracked embedded harness. A clean legacy embedded harness is removed only through the incoming Git commit; it is never deleted by file-copy logic.
9. Create a local ignored `AGENTS.md` entry point in role `analytics` and prove that the worktree remains clean.
10. Compare the exact Git trees, create a binary-capable reverse patch, and prove in a temporary Git index that applying it to the recorded source commit reproduces the exact analytics tree.
11. Push only `analytics/main`, unless `--no-push` was explicitly requested.

A failed fetch, non-fast-forward local update, merge, verification or push leaves a visible error. The command must not suppress it or silently choose a side.

## Reverse patch

`repository-exchange.py reverse-diff` does not update or merge repositories. It compares the current bare source commit with the clean `documents` commit and writes:

- `reverse-diffs/reverse-diff-YYYYMMDD-HHMMSS.patch`;
- `reverse-diffs/reverse-diff-latest.patch`;
- `reverse-diffs/reverse-diff-latest.json` with source and target commits, trees and verification state.

The patch is intended for the maintainers of `changeswork-copy`. Normal analyst work does not apply, commit or push it. When both trees are identical, stale `reverse-diff-latest.patch` is removed and the metadata records that no patch is required.

## Prohibited shortcuts

- no `rsync` or recursive copy between repositories;
- no `git push` from `changeswork-copy` or `coda`;
- no checkout, worktree, direct edit or ordinary Git command in the `changeswork-copy` mirror;
- no `reset --hard`, `clean`, force push or automatic branch switching;
- no ignored `pull`, merge or push failures;
- no automatic conflict resolution;
- no reverse patch created from a dirty `documents` worktree;
- no claim of equality without exact tree verification.
