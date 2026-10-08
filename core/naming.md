# Naming

Для проекта с `delivery-index.json` применяй `core/quarter-deliveries.md`:
legacy-пути `features/<feature>/...` означают корень выбранной поставки,
`planning/<quarter>/...` — корень квартала; разрешай их через
`scripts/project_layout.py`. Корневой каталог фич содержит человекочитаемые паспорта и сводные требования
по core/feature-passports.md. Передаваемый контракт остаётся в выбранной поставке. Примеры ниже сохраняются для legacy-проектов.

## GigaCode files

- Native entry point: `GIGACODE.md` in HARNESS_ROOT; `AGENTS.md` is the compatibility entry point.
- Slash commands: `.gigacode/commands/<action>.md`, YAML `description` and `argument-hint`.
- Analyst skills: directories with the `coda-` prefix and a `SKILL.md`, for example `.gigacode/skills/coda-requirements/SKILL.md`; YAML `name` and `description`.
- Command arguments are request text, never executable shell fragments.
- Tracked native definitions belong to HARNESS_ROOT. Analytics receives only an ignored managed projection.
- Settings, permissions, MCP configuration and memories remain local; no native files are installed into code or source.

## Slugs

Use English slugs for paths.

Examples:
- `features/deployments/`
- `requirements-exchange/deployments/revisions/001/`
- `releases/2026-Q2/rscon-2438/`

Human-readable Russian names live inside markdown files.

## Delivery stage identifiers

Follow `core/delivery-stages.md`. Use a plain `Этап поставки: stage-1` line in
`Границы`, replacing `stage-1` with the current `stage_id`. Do not create additional stage text copies within a delivery. In migrated
projects the authored root is the registered quarter delivery directory. `stage_revision` counts
revisions inside one stage; `revisions/NNN` stays global within the feature.
For example, `001` and `002` may be revisions 1 and 2 of `stage-1`, and `003`
revision 1 of `stage-2` after explicit closure and a new analyst decision.
Keep existing `return_id` and `REQ-*` identities; omission does not repeal earlier
deployed behavior. Legacy inputs and returns are not renamed or rebound automatically.

## Requirements publication branches

Новые ветки передачи создаёт только `requirements-exchange.py prepare`:
`requirements/<key14>/<target6>-<request12>`, не более 48 байт ASCII.
`key14` — первые 14 символов полного ключа без завершающего дефиса;
`target6` — первые 6 символов SHA-256 целевой ветки; `request12` — первые
12 символов контрольной суммы объекта с полным ключом, целевой веткой и
полной контрольной суммой подтверждённого входа. Для SDD вход включает
`requirements.md` и дескриптор всего SDD-пакета.

Имя — только короткий псевдоним. `manifest.json/publication_request` хранит
`schema_version: 1`, полный `feature`, `target_branch`, `input_sha256`,
`identity_sha256` и `request_branch`. Ключи каталогов обмена, номера
редакций и полные контрольные суммы не сокращаются. Перед повторным
использованием ветки проверяется полный вход; совпадение коротких имён
не разрешает заменить чужой пакет или выполнить force push.

Повторная подготовка неизменного входа использует ту же ветку. Уже
существующие ветки прежнего длинного формата проверяются и продолжаются
без переименования и дублирования; исторические манифесты без
`publication_request` остаются допустимыми. Целевая ветка никогда
не используется для отправки: принятие выполняет человек через PR/MR.

## File conventions

- feature root: `feature.md`
- feature references: `references.md`
- feature domain delta: `domain-impact.md`
- planning stories: `STORY-<FEATURE>-NNN.md`
- feature requirements: `requirements.md`
- delivery stage registry: `requirements-state.json` at the feature root, without copies of requirement text
- exchange manifest: `manifest.json`
- derived analyst SDD: selected delivery `sdd/package.json` and `sdd/<contour>/<change-id>/` under core/sdd-input.md
- returned task list: `returns/tasks.md`
- execution registry: `tasks.md`
- detailed task note: `<JIRA>.md`
- prototypes: `prototype.html`, `notes.md`
- baseline version note: `baseline/current/VERSION.md`
- release root: `release.md`
- final release delta: `final-domain-delta.md`
