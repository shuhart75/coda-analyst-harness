# Naming

## Slugs

Use English slugs for paths.

Examples:
- `features/deployments/`
- `requirements-exchange/deployments/revisions/001/`
- `releases/2026-Q2/rscon-2438/`

Human-readable Russian names live inside markdown files.

## Delivery stage identifiers

Follow `core/delivery-stages.md`. Use a plain `Этап поставки: stage-1` line in
`Границы`, replacing `stage-1` with the current `stage_id`. Do not create a
stage-specific authored requirements file or directory. `stage_revision` counts
revisions inside one stage; `revisions/NNN` stays global within the feature.
For example, `001` and `002` may be revisions 1 and 2 of `stage-1`, and `003`
revision 1 of `stage-2` after explicit closure and a new analyst decision.
Keep existing `return_id` and `REQ-*` identities; omission does not repeal earlier
deployed behavior. Legacy inputs and returns are not renamed or rebound automatically.

## File conventions

- feature root: `feature.md`
- feature references: `references.md`
- feature domain delta: `domain-impact.md`
- planning stories: `STORY-<FEATURE>-NNN.md`
- feature requirements: `requirements.md`
- delivery stage registry: `requirements-state.json` at the feature root, without copies of requirement text
- exchange manifest: `manifest.json`
- returned task list: `returns/tasks.md`
- execution registry: `tasks.md`
- detailed task note: `<JIRA>.md`
- prototypes: `prototype.html`, `notes.md`
- baseline version note: `baseline/current/VERSION.md`
- release root: `release.md`
- final release delta: `final-domain-delta.md`
