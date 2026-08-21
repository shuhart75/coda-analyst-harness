# Repository Exchange Policy

This policy governs the one-way integration path from role `source` to role `analytics` and the verified reverse patch returned to the source maintainers.

## Repository roles

| Repository | Local access | Remote access | Purpose |
|---|---|---|---|
| `analytics` (`documents` by default) | read and write | pull and push | Normal analyst work, requirements, planning and factual progress |
| `source` (`changeswork-copy` by default) | no working tree; hidden bare mirror | fetch only | Upstream analytical source received from GitHub |
| `code` (`coda` by default) | read only except registered protected pull | `git pull --ff-only` through `workspace.py` only | Implementation evidence for analytical work |

The repositories are independent under the `coda-analyst-harness` root. Roles `analytics` and `code` use normal clones. Role `source` is stored only at `.workspace-state/repositories/<repository-id>.git` as a bare mirror, is excluded from the editor workspace, and has no files that an LLM can edit. They are not submodules and must not be copied into one another. Default roles are used without questions; reassignment is allowed only by an explicit analyst command.

The first bootstrap prepares all configured roles. After that, the analyst may remove the local `code` repository, the hidden `source` mirror, or both. `bootstrap` records these optional roles as `absent` and does not recreate them. Role `analytics` is mandatory and is never automatically recloned after removal.

## Synchronization transaction

The command `repository-exchange.py sync` performs one guarded transaction:

1. Acquire the workspace lock so a second exchange cannot run concurrently.
2. Require a valid `source` bare mirror and fetch `source/main` directly into it.
3. Detect an already active analytics merge and stop without changing it. Otherwise require a clean `analytics/main` worktree. The only exception is a verified filesystem-normalized alias of a non-NFC tracked path that is removed by the incoming source commit and whose bytes equal the indexed blob.
4. Fetch `analytics/origin/main`. Fast-forward when the local branch is behind, preserve it when it is ahead, or create a normal merge commit when both sides have conflict-free commits.
5. If local and remote analytics changes conflict, abort only the merge started by the harness, return `analytics-origin-merge-conflict`, and offer `inspect-analytics-origin-conflict`. If a merge was already active before the command, return `analytics-origin-merge-in-progress` without aborting or changing it.
6. Reject any non-NFC path remaining in `source` or in the merged `analytics` tree.
7. Reject local tool settings, files outside the registered analytical roots, direct files under `features/`, and any deletion of a path inherited from `source` that has not been explicitly approved by the analyst. Paths under `context/source-materials/` are opaque evidence and are not classified by filename as local tool settings.
8. Fetch the local `source/main` commit into `analytics` through a dedicated local remote and merge it normally.
9. Abort and stop on any source conflict. Never overwrite the conflict with copied files.
10. Repeat the content policy after merge and require the merged analytics tree to contain no tracked embedded harness. A clean legacy embedded harness is removed only through the incoming Git commit; it is never deleted by file-copy logic.
11. Create a local ignored `AGENTS.md` entry point in role `analytics` and prove that the worktree remains clean. The same local exclude file covers `.codex`, `.gigacode`, `.gigaide`, `.idea`, `GIGACODE.md`, `*.iml` and `*.orig`.
12. Compare the exact Git trees, create a binary-capable reverse patch, and prove in a temporary Git index that applying it to the recorded source commit reproduces the exact analytics tree.
13. Recheck the exact commit, clean worktree and content policy immediately before push.
14. Push only `analytics/main`, unless `--no-push` was explicitly requested. The verified reverse patch is not applied to `source` on this machine.

A failed fetch, merge, verification or push leaves a visible error. The command must not suppress it or silently choose a side. A conflict with `analytics/origin/main` continues through `inspect-analytics-origin-conflict`; a conflict with `source` continues through `inspect-source-analytics-conflict`. In either case the LLM requests one concrete resolution at a time. Updating `code`, rebasing or resetting analytics, force pushing, skipping the source merge and overwriting analytics from source are not migration alternatives.

## Protective analytics snapshots

Before any fast-forward or merge that can move `analytics/main`, the exchange creates local refs under `refs/coda-analyst-harness/analytics-snapshots/<id>/` and a description under `.workspace-state/analytics-snapshots/<id>/snapshot.json`. When a conflict occurs, the base, local and incoming versions of every conflicting file are copied into that snapshot before a harness-started merge is aborted.

After an automatic merge, both original commits must be ancestors of the result. A failed ancestry check stops synchronization. Snapshot refs are local recovery state: push always names only `analytics/main`, so these refs are never published. The state directory is ignored by the harness repository.

Recovery is explicit and path-scoped:

```bash
python3 scripts/workspace.py list-analytics-snapshots
python3 scripts/workspace.py inspect-analytics-snapshot --snapshot <id>
python3 scripts/workspace.py restore-analytics-snapshot-file \
  --snapshot <id> \
  --side <base|local|incoming> \
  --path <exact-relative-path>
```

The restore command requires a clean `analytics/main`, accepts one exact file path that existed in at least one snapshot side, and never stages or commits the result. It deletes that one worktree file only when the explicitly selected snapshot side did not contain it. Automatic restoration, implicit side selection and directory restoration are prohibited.

## Reduced workspace

The natural-language synchronization command always runs `workspace.py bootstrap` before `workspace.py sync`, so local instructions, the code registry and the editor workspace reflect the repositories that actually exist.

When `source` is absent, `workspace.py sync` selects `sync-analytics-only`. It requires a clean `analytics/main`, performs the same protected origin update or conflict workflow, applies the Unicode, structure and content-policy checks, refreshes the local entry point and pushes only `analytics/main` unless `--no-push` was requested. It removes the stale `reverse-diff-latest.patch` and writes `reverse-diff-latest.json` with `status=unavailable`, `reason=source-role-absent` and `verified=false`. Historical timestamped patches remain historical artifacts and must not be presented as current.

When `code` is absent, the protected update returns `status=skipped` and no code command is run. The generated code registry has no repository entries, the editor workspace omits code and the local `AGENTS.md` explicitly prohibits code access. Full `source` to `analytics` exchange continues when `source` is present.

Scripts never invent a semantic commit from a dirty `analytics` tree. The LLM reviews intentional changes, runs applicable checks, stages only exact paths and commits them before retrying synchronization. Ambiguous changes require one analyst decision at a time. Broad staging remains prohibited.

## Feature branches

When multi-user work is configured, full repository synchronization is accepted only with `analytics/main` checked out. A feature branch blocks the operation before the protected code pull or any source fetch. Full synchronization must never merge `source` into a feature branch.

`repository-exchange.py update-feature-branch` is a separate operation. It fetches only `analytics/origin/main`, fast-forwards or merges that commit into the current `feature/<feature>/<analyst>` branch, creates protective snapshots and leaves a clean worktree after success or a harness-aborted conflict. It does not access `source`, update `code`, create a reverse patch or push; `collaboration.py update` pushes only the feature branch after this operation succeeds.

`repository-exchange.py fast-forward-analytics-main` updates only a clean local `main` that is an ancestor of `origin/main`. It creates a protective snapshot and refuses divergence. Migration and completion use this narrow operation instead of rewriting `main`.

Successful local synchronization means that incoming changes were merged, the result was verified and `analytics` was pushed. A non-empty reverse patch leaves `source_analytics_state=reverse-diff-pending` because role `source` is fetch-only on this machine. The result uses `source_analytics_state=identical` only when the fetched source tree already equals the analytics tree. The verified patch is transferred separately and applied on a machine where `source` is a writable working repository.

## Reverse patch

`repository-exchange.py reverse-diff` does not update, merge, apply or push repositories. It compares the current bare source commit with the clean and policy-compliant `documents` commit and writes to the Git-ignored local `reverse-diffs/` directory:

- `reverse-diffs/reverse-diff-<artifact-id>.patch`;
- `reverse-diffs/reverse-diff-<artifact-id>.json`;
- `reverse-diffs/reverse-diff-latest.patch`;
- `reverse-diffs/reverse-diff-latest.json` with source and target branches, commits and trees, patch checksum, the complete changed-path list, included analytics commits and features, explicitly approved source deletions and verification state.

The timestamped patch and JSON are an immutable pair. Later runs replace only the `latest` convenience copies and create another timestamped pair. Because the whole directory is ignored, these artifacts neither dirty the harness worktree nor block or get overwritten by a normal harness `git pull --ff-only`.

The patch is intended for the maintainers of `changeswork-copy`. Normal analyst work does not apply, commit or push it. Metadata schema 2 sets `verified=true` only when both exact-tree reproduction (`tree_verified`) and repository-content policy (`content_policy_verified`) pass. Fields `included_analytics_commits` and `included_features` preserve provenance even though applying a patch creates one integration commit in the receiving repository. This does not mean that draft requirements are approved. When both trees are identical, stale `reverse-diff-latest.patch` is removed and the metadata records that no patch is required.

Transfer the immutable timestamped JSON and patch together through an approved external channel; do not commit them to this harness. On the machine where `changeswork-copy` is writable, the receiving analyst harness verifies the pair again, requires the exact source commit and tree after a protected fast-forward-only pull, creates one integration commit whose tree equals `analytics_tree`, pushes it and writes a local application receipt. The next full sync in this workspace fetches that source commit. Only a result with `source_analytics_state=identical`, `repositories_identical=true` and `all_repositories_synchronized=true` confirms the complete round trip. If analytics advanced meanwhile, a new reverse patch is expected.

Every deletion of a path inherited from `source` is blocked by default. After the analyst explicitly confirms one exact deletion, register it with `repository-exchange.py approve-deletion --path <path>`. The local approval is bound to the current source blob and becomes invalid if that source file changes. Never run this command merely to make synchronization pass.

If `source` is absent, a new reverse patch cannot be built or verified. `reverse-diff` reports this as unavailable and must not recreate `source` implicitly.

## Prohibited shortcuts

- no `rsync` or recursive copy between repositories;
- no `git push` from `changeswork-copy` or `coda`;
- no commit or push to `source` from this machine;
- no edit, commit or push of tracked `HARNESS_ROOT` files during normal analyst work; harness updates use `git pull --ff-only`, while runtime state and reverse patches stay in registered ignored paths;
- no checkout, switch, merge, reset, clean, commit, push, file generation or direct fetch/pull in `coda`; only the registered `workspace.py update-code` operation may execute protected `git pull --ff-only`;
- no checkout, worktree, direct edit or ordinary Git command in the `changeswork-copy` mirror;
- no `reset --hard`, `clean`, force push or automatic branch switching;
- no ignored `pull`, merge or push failures;
- no automatic conflict resolution;
- no deletion, rewriting or publication of local analytics protective snapshots;
- no rebase, reset, force push or silent selection of one analytics history when local `analytics/main` and `origin/main` diverge;
- no reverse patch created from a dirty `documents` worktree;
- no `git add -A`, `git add .` or broad staging while repairing exchange state;
- no tracked local IDE/LLM settings or test artifacts outside the analytical structure;
- no source-file deletion in a reverse patch without an explicit path-level analyst approval;
- no claim of equality without exact tree verification.
