# Repository Exchange Policy

This policy governs the one-way integration path from role `source` to role `analytics` and the verified reverse patch returned to the source maintainers.

## Repository roles

| Repository | Local access | Remote access | Purpose |
|---|---|---|---|
| `analytics` (`documents` by default) | read and write | pull and push | Normal analyst work, requirements, planning and factual progress |
| `source` (`changeswork-copy` by default) | no working tree; hidden bare mirror | fetch only | Upstream analytical source received from GitHub |
| `code` (`coda` by default) | ordinary clone is read-only except registered protected pull | protected pull and isolated requirements review-branch publication only | Implementation evidence and reviewed requirements delivery |

The repositories are independent under the `coda-analyst-harness` root. Roles `analytics` and `code` use normal clones. Role `source` is stored only at `.workspace-state/repositories/<repository-id>.git` as a bare mirror, is excluded from the editor workspace, and has no files that an LLM can edit. They are not submodules and must not be copied into one another. Default roles are used without questions; reassignment is allowed only by an explicit analyst command.

The first bootstrap prepares all configured roles. After that, the analyst may remove the local `code` repository, the hidden `source` mirror, or both. `bootstrap` records these optional roles as `absent` and does not recreate them. Role `analytics` is mandatory and is never automatically recloned after removal.

## Synchronization transaction

The command `repository-exchange.py sync` performs one guarded transaction:

1. Acquire the workspace lock so a second exchange cannot run concurrently.
2. Require a valid `source` bare mirror and fetch `source/main` directly into it.
3. Detect an already active analytics merge and stop without changing it. Otherwise require a clean `analytics/main` worktree. The only exception is a verified filesystem-normalized alias of a non-NFC tracked path that is removed by the incoming source commit and whose bytes equal the indexed blob.
4. Fetch `analytics/origin/main` and update local main only by fast-forward from that accepted history. Local-ahead and diverged histories both stop with `analytics-unaccepted-history` and a protective snapshot in state `prepared`. Even conflict-free divergence requires a separate feature branch and human PR/MR acceptance; sync never integrates or publishes those local commits into main.
5. Read incoming source history into Git objects without merging it into the ordinary documents checkout. If the incoming commit is already contained in accepted main and no import is pending, continue to reverse-patch verification.
6. Otherwise prepare `codex/source-import/<full-source-commit>` from accepted remote documents main in an isolated clone. Keep that clone at `.workspace-state/source-imports/<source-commit>/repository` and its pending state at `.workspace-state/source-import.json` for retry and conflict resolution.
7. Merge source only in that isolated review branch, preserving shared history with a merge commit. Reject non-NFC paths, local tool settings, files outside registered analytical roots, direct files under `features/`, unapproved deletions relative to source, embedded harness content and whitespace errors. Paths under `context/source-materials/` remain opaque evidence. Legacy harness removal is reviewed in the import branch.
8. Save the candidate commit and review report before sending the branch. The report lists changed and deleted files, removed `REQ-*` headings and scenarios, and changes to protected requirements, baseline, planning and release artifacts. Content-policy verification does not approve those changes; human review decides whether useful content may be removed or replaced.
9. Push only the review branch for the incoming import, unless `--no-push` was requested. Return `merge_request_created=false`, branch information and, when available, a creation URL. The analyst creates and accepts the PR/MR; no hosting API credentials are required by this workflow.
10. While review or sending is pending, return `source_analytics_state=source-import-pending` and `all_repositories_synchronized=false`. Do not generate a reverse patch against the unaccepted source. Invalidate the latest patch pointer and mark its metadata unavailable; preserve immutable historical pairs.
11. Repeat sync after human acceptance. Prove that the exact import commit is contained in accepted `documents/origin/main`, fast-forward local main with a protective snapshot, and only then use the accepted result for ordinary work and reverse-patch generation.
    Save the acceptance receipt in `.workspace-state/source-imports/<source-commit>/merged.json` before clearing the active import state.
12. Refresh the ignored local `AGENTS.md` entry point after a completed import. Prove the documents worktree remains clean, compare the exact source and analytics trees, and verify the reverse patch in a temporary index. The patch is never applied to source on this machine.

The code update performed by `workspace.py sync` remains the separate guarded pull. `--no-push` prepares and retains the same review candidate locally; it never applies incoming source changes to documents/main.

## Pending imports and conflicts

Retry uses the saved clone, candidate and branch. A failed push retains `prepared-not-pushed`; repeat sync to check the remote outcome and send the same candidate. Existing remote import branches are discovered and reused, including after loss of local state. Multiple unfinished imports or incompatible local and remote branch histories block automatic progress. Never overwrite or force-push an open request.

New source commits do not silently replace an open import: `source_has_newer_commit` exposes the difference, and the current request remains bound to its original source commit. New documents/main commits may fast-forward the ordinary checkout while review is pending; they do not authorize accepting the import. Acceptance must retain the import commit as an ancestor. **Source imports require merge-commit acceptance, not squash or rebase.** Tree equality alone does not prove acceptance or preserve shared source history.

A source conflict returns `source-analytics-merge-conflict`, `import_checkout` and a protective snapshot. The conflict remains active only in the isolated checkout; ordinary documents/main retains its accepted content. `inspect-source-analytics-conflict` reports the exact pending base/source pair and saved file versions. Request one path-level analyst decision at a time, resolve only the selected paths in `import_checkout`, create a semantic merge commit there, then repeat sync and complete human PR/MR review. Never resolve the import by editing main.

`inspect-analytics-origin-conflict` remains available to inspect diverged documents histories and archive file versions without modifying the ordinary checkout. A pre-existing user merge returns `analytics-origin-merge-in-progress`; the harness neither aborts nor restarts it. Any local resolution still requires the separate branch and PR/MR route before sync can accept it into main.

If the analyst explicitly rejects or defers the import, the human first closes the PR/MR and removes its remote review branch. Only after that explicit analyst decision may the agent run:

```bash
python3 scripts/repository-exchange.py defer-source-import --analyst-confirmed
```

The command checks that the remote branch is absent, retains the isolated clone and writes `.workspace-state/source-imports/<source-commit>/deferred.json`. The same source commit stays `deferred`, continues to block reverse-diff generation and is not automatically republished. A newer source commit starts a new review branch. Never use this command to bypass a failed push or a conflict.

## Protective analytics snapshots

Before any fast-forward that can move `analytics/main`, the exchange creates local refs under `refs/coda-analyst-harness/analytics-snapshots/<id>/` and a description under `.workspace-state/analytics-snapshots/<id>/snapshot.json`. Rejected local-ahead or diverged main histories retain a `prepared` snapshot without a result commit. Source-conflict snapshots archive base, local and incoming file versions from the isolated import; the pending merge remains there for explicit resolution.

After a completed fast-forward, both original commits must be ancestors of the result. A failed ancestry check stops synchronization. Snapshot refs are local recovery state and are never published. The state directory is ignored by the harness repository.

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

When `source` is absent, `workspace.py sync` selects `sync-analytics-only`. It requires a clean `analytics/main`, performs the same fast-forward-only origin update, applies the Unicode, structure and content-policy checks and refreshes the local entry point. Local commits must first be accepted through human PR/MR; sync does not publish unaccepted main history. It removes the stale `reverse-diff-latest.patch` and writes `reverse-diff-latest.json` with `status=unavailable`, `reason=source-role-absent` and `verified=false`. Historical timestamped patches remain historical artifacts and must not be presented as current.

When `code` is absent, the protected update returns `status=skipped` and no code command is run. The generated code registry has no repository entries, the editor workspace omits code and the local `AGENTS.md` explicitly prohibits code access. Full `source` to `analytics` exchange continues when `source` is present.

Scripts never invent a semantic commit from a dirty `analytics` tree. The LLM reviews intentional changes, runs applicable checks, stages only exact paths in a feature branch and completes human PR/MR acceptance before retrying synchronization. Ambiguous changes require one analyst decision at a time. Broad staging remains prohibited.

## Feature branches

When multi-user work is configured, full repository synchronization itself runs only with `analytics/main` checked out. `workspace.py sync` may first finish a checked-out `awaiting-merge` feature branch: `collaboration.py finish` fetches `origin/main`, proves that it contains the submitted commit, switches to `main` and fast-forwards it. Only then may the same command start code and source updates. If containment is not proven, or feature work has another status, the operation stops before the protected code pull or any source update. Full synchronization must never merge `source` into a feature branch.

`repository-exchange.py update-feature-branch` is a separate operation. It fetches only `analytics/origin/main`, fast-forwards or merges that commit into the current `feature/<feature>/<analyst>` branch, creates protective snapshots and leaves a clean worktree after success or a harness-aborted conflict. It does not access `source`, update `code`, create a reverse patch or push; `collaboration.py update` pushes only the feature branch after this operation succeeds.

`repository-exchange.py fast-forward-analytics-main` updates only a clean local `main` that is an ancestor of `origin/main`. It creates a protective snapshot and refuses divergence. Migration and completion use this narrow operation instead of rewriting `main`.

Preparing or sending a source review branch is pending work, not completed synchronization. Once the import is accepted, a non-empty reverse patch leaves `source_analytics_state=reverse-diff-pending` because role `source` is fetch-only on this machine. The result uses `source_analytics_state=identical` only when the accepted source tree equals the analytics tree. The verified patch is transferred separately and applied on a machine where `source` is a writable working repository.

## Reverse patch

`repository-exchange.py reverse-diff` does not merge, apply or push repositories. It requires the current source commit to be contained in accepted documents history; pending or deferred imports cannot be reverse-patch inputs. It compares the bare source commit with the clean and policy-compliant `documents` commit and writes to the Git-ignored local `reverse-diffs/` directory:

- `reverse-diffs/reverse-diff-<artifact-id>.patch`;
- `reverse-diffs/reverse-diff-<artifact-id>.json`;
- `reverse-diffs/reverse-diff-latest.patch`;
- `reverse-diffs/reverse-diff-latest.json` with source and target branches, commits and trees, patch checksum, the complete changed-path list, included analytics commits and features, explicitly approved source deletions and verification state.

The timestamped patch and JSON are an immutable pair. Later runs replace only the `latest` convenience copies and create another timestamped pair. Because the whole directory is ignored, these artifacts neither dirty the harness worktree nor block or get overwritten by a normal harness `git pull --ff-only`.

The patch is intended for the maintainers of `changeswork-copy`. Normal analyst work does not apply, commit or push it. Before any transport files are written, generation runs `git diff --check` between the exact source and analytics commits and rejects trailing whitespace and other patch-format errors. Metadata schema 2 sets `verified=true` only when whitespace validation (`diff_check_verified`), exact-tree reproduction (`tree_verified`) and repository-content policy (`content_policy_verified`) pass. Fields `included_analytics_commits` and `included_features` preserve provenance even though applying a patch creates one integration commit in the receiving repository. This does not mean that draft requirements are approved. When both trees are identical, stale `reverse-diff-latest.patch` is removed and the metadata records that no patch is required.

Transfer the immutable timestamped JSON and patch together through an approved external channel; do not commit them to this harness. On the machine where `changeswork-copy` is writable, the receiving analyst harness verifies the pair again, requires the exact source commit and tree after a protected fast-forward-only pull, creates one integration commit whose tree equals `analytics_tree`, pushes it and writes a local application receipt. The next full sync fetches that new source commit and routes it through review when its history is not yet contained in documents/main, even if the trees match. Only a result with `source_analytics_state=identical`, `repositories_identical=true` and `all_repositories_synchronized=true` confirms the complete round trip. If analytics advanced meanwhile, a new reverse patch is expected after acceptance.

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
- no automatic source merge into documents/main or a feature branch;
- no squash or rebase acceptance of a source-import branch;
- no silent replacement of a pending import with newer source content;
- no reverse diff against an unaccepted or deferred incoming source commit;
- no deletion, rewriting or publication of local analytics protective snapshots;
- no rebase, reset, force push or silent selection of one analytics history when local `analytics/main` and `origin/main` diverge;
- no reverse patch created from a dirty `documents` worktree;
- no `git add -A`, `git add .` or broad staging while repairing exchange state;
- no tracked local IDE/LLM settings or test artifacts outside the analytical structure;
- no source-file deletion in a reverse patch without an explicit path-level analyst approval;
- no claim of equality without exact tree verification.
