# Tooling Policy

## Tool selection

Prefer the simplest tool that preserves auditability.

- Use markdown as the source of truth for planning, requirements, execution mapping and release notes.
- Use generated PlantUML only for gantt rendering, not for storing semantic mapping.
- Keep raw evidence in `context/source-materials/`.
- Keep transient MCP task-tracker snapshots under ignored
  `.workspace-state/tracker-runs/`; do not copy them into project source materials
  during the read-only tracker phase.
- Keep complete provider responses outside tracker-run directories. Runtime state
  stores only normalized cards, response provenance and checksums; it must never
  contain credentials or complete provider response bodies.

## Validation discipline

During harness development, run focused tests for the changed operation and its
failure/retry paths after each meaningful change. Before delivery, run affected
cross-workflow checks and one complete regression suite per harness version.
CI keeps the complete suite mandatory. Do not repeat the full suite after every
small edit; never drop data-loss, recovery or ownership-boundary checks to save time.

After structural or canonical changes, run available validators.
After gantt-related changes, regenerate the affected Gantt within its mode boundary and refresh its standalone Confluence export without includes. This is mandatory for every Gantt view, including quarter-plan, commander-plan and actual-progress; follow the export contract below.
After release promotion, validate both structure and links again.
Before broad workflow work, run `harnessctl doctor`. Before approving a plan, run workflow, planning, link, context, and trace checks.

## Обязательная Confluence-версия любого Ганта

При создании или изменении любой диаграммы Ганта, включая `quarter-plan`,
`commander-plan`, `actual-progress` и другие именованные представления,
обязательно создавай или обновляй соседний файл `<имя>-confluence.puml`.
Это самостоятельный PlantUML-документ без `!include`, полученный раскрытием
исходной диаграммы; он сохраняет тот же состав, даты, ресурсы и оформление.
Изменение подключаемого include или preamble также требует обновления экспортов
всех диаграмм, которые его используют. Производный файл `*-confluence.puml`
не требует ещё одной Confluence-версии.

Используй `scripts/expand-plantuml-includes.py <input.puml> <output.puml>`
после завершения генерации исходника. Если штатный генератор уже создал экспорт,
проверь его точное совпадение с раскрытым исходником. Вручную экспорт не редактируй.
Отсутствие экспорта, оставшийся `!include` или несовпадение содержимого блокирует
завершение и сохранение изменения. Исходник, изменённые подключения и все затронутые
экспорты входят в один проверенный набор и один коммит при разрешённом сохранении.

Генерация экспорта не расширяет границы режима: не запускай полный генератор ради
экспорта, если это перепишет утверждённые планы или фактическое исполнение другого
режима. Раскрой только нужные исходные диаграммы; утверждённые планы не меняй.

## Оформление Ганта в PlantUML

Не добавляй `hide column task/start/end/duration` в общий заголовок:
рендереры IDE и Confluence могут отклонять эти команды. По проверке аналитика,
удаление команд устраняет ошибку без появления боковых колонок в его средах.
Не задавай минимальную версию PlantUML по одной локальной проверке.

Используй стандартное размещение подписей целевого рендерера. Не обещай,
что длинное название всегда окажется внутри короткой полосы, или что все
версии одинаково отображают таблицу. Команды управления колонками и
`Label on first/last column ...` добавляй только после проверки совместимости
с конкретными IDE и Confluence, а не безусловно в генераторе.

Явно запрошенная миграция оформления не является актуализацией исполнения:
не запускай планировщик и не пересчитывай даты, оценки, ресурсы или прогресс.
В рабочих actual-progress и черновых планах меняй только команды оформления,
после чего раскрывай и проверяй Confluence-экспорты. Для утверждённых планов
и планов, связанных контрольными суммами, создавай соседнее представление
`<имя>-plantuml.puml` и его `<имя>-plantuml-confluence.puml`; сохраняй исходник,
его includes и контрольные суммы неизменными. Представление копирует исходную
диаграмму с теми же относительными includes и заменяет только команды оформления.
Укажи исходный план и ссылки на новые представления в квартальном Gantt README.
Исторические архивы, входящие материалы и пакеты обмена не мигрируй на месте.

## Отпуска в actual-progress

Генератор actual-progress добавляет в конце секцию «Отпуска и недоступность
команды» из `planning/team.md`, колонка `Closed intervals`. Показывай только
ресурсы действующего roster и пересечение их закрытых интервалов с выбранным
календарным кварталом. Объединяй смежные дни в одну полосу; при отсутствии
интервалов секция не нужна. Не называй любой закрытый интервал отпуском:
он может означать другую причину отсутствия.

Полосы «Недоступность <ресурс>» имеют цвет `LightYellow/Orange`, явные даты
начала/конца и стабильный префикс `CALENDAR_OFF_`. Это информация календаря,
не задача: без `on {ресурс}`, оценки, прогресса, зависимостей и tracker ID.
Не добавляй `{ресурс} is off` ради оформления: это влияет на расчёт PlantUML.
Доступность для планировщика по-прежнему задаёт тот же календарь команды.

При запросе только отображения добавляй секцию штатной функцией
`availability_lines` в существующий actual-progress без пересчёта задач,
затем обновляй и проверяй Confluence-экспорт. Обычная актуализация через
`sync-quarter-gantt.py --actual-only` воспроизводит эту же секцию автоматически.
Не мигрируй другие кварталы, плановые представления или архивы без явного запроса.

## Editing discipline

- Prefer small explicit edits over large opaque rewrites.
- Do not regenerate unrelated files just because a tool can.
- Preserve human-readable names in markdown; keep slugs and ids stable.
- Before the final response after file edits, and always before a commit, review only the changes made in the current turn:
  - Necessity: each change must directly support the user's request or required workflow consistency.
  - Correctness: changes must match source-of-truth artifacts, active mode rules, links, generated artifacts and available validators.
  - Conciseness: simplify without losing traceability, auditability or clarity.
- If this review finds an issue inside the allowed scope, fix it before responding. If it is outside the active mode, touches unrelated user changes, or needs a business decision, do not fix silently; report it or ask.
- Do not remove traceability, source references, acceptance criteria, risk records or consistency records only because they look verbose.
- After requirement edits, use targeted text search or an equivalent local find-in-files sweep for superseded terms such as old endpoints, field names, role names, status values and UX labels.
- Keep the sweep proportional: start with the current feature and explicitly affected artifacts; expand to neighboring features or baseline only when the change is cross-feature or domain-wide.
- If the project provides `scripts/find-stale-terms.py`, use it as the fast default helper for local tail cleanup; otherwise use the platform's normal text search.
- Confluence export is mandatory for every created or changed Gantt without a separate analyst request; use `scripts/expand-plantuml-includes.py` and the export contract above instead of editing generated exports by hand.
- Use `scripts/validate-context.py` after adding or materially changing context, research, handoff, implementation-plan or test-plan conventions.
- Use `scripts/validate-language.py` after changing requirements. Treat avoidable anglicisms as a completion blocker, not as optional editorial cleanup.

## GigaCode integration

Native entry points are `GIGACODE.md`, `.gigacode/commands/` and
`.gigacode/skills/`. They select existing guarded operations. Do not install
permissions, MCP credentials or tool hooks from a source report; local settings
remain owned by the user. `scripts/gigacode_workspace.py` installs only the
managed local projection during bootstrap, preserving unrelated files.

## Optional platform capabilities

Do not assume the local platform has configured:
- subagents;
- memory;
- local plugins.

Use subagents, memory and plugins only when available and permitted by the
operation contract. Native commands and skills never bypass deterministic gates.

## Consistency records

- `features/*/domain-impact.md` stores per-feature impact and affected artifact lists.
- `PROJECT_ROOT/planning/consistency-backlog.md` stores unresolved or deferred propagation work, including prototype drift and rollback propagation.
- Do not hide known inconsistency in chat only; record it in one of those files.

## Context and research records

- `features/*/context-summary.md` stores the small-window feature context.
- `features/*/artifact-map.md` distinguishes authored, derived and auxiliary artifacts.
- `features/*/.research/` stores auxiliary research, not source-of-truth requirements.
- `requirements-exchange/*/revisions/*/requirements.md` stores immutable transmitted input; it is never an authoring location.
- `requirements-exchange/*/revisions/*/returns/` stores developer-owned decomposition and factual results with direct `REQ-*` coverage.
- `.workspace-state/run-state/current.md` may be used as a resumable checkpoint for long-running work.
- `.workspace-state/runs/*/run.json` stores executable loop state and verification evidence.

## Подписи релизов в actual-progress

Показывай `BE Название · RELEASE-KEY` и `QA Название · RELEASE-KEY` без стрелок.
Генератор читает проверенный `execution/qa-groups.json`: `release` группы задаёт
метку QA и её BE/FE-участников. `release: null` и отсутствие данных дают
«релиз не уточнён», не «без релиза». Кандидаты и виртуальные плановые полосы
не получают выдуманную принадлежность. Спринт, имя фичи и последний run не являются
источниками релиза. Метка не меняет даты, прогресс, ресурсы или оценку.

Для именованных вех используй соседний с actual-progress файл `releases.json`:
`schema_version: 1`, `releases: [...]`. Каждая запись содержит `alias` существующей
вехи, `release`, `date` в YYYY-MM-DD, `state` (`planned` или `deployed`),
`analyst_confirmed: true`, `evidence`; для deployed обязателен `environment`.
Например: `{"alias":"REL_KODA_20261020","release":"RSCON-6970",
"date":"2026-10-20","state":"deployed","environment":"ПРОМ",
"analyst_confirmed":true,"evidence":"Явное подтверждение аналитика 22.10.2026"}`.
Это пример формата, а не автоматическая миграция данных проекта.

Названия: «Релиз RSCON-6970 · план» либо «Релиз RSCON-6970 · ПРОМ · факт».
План и факт одного релиза могут иметь отдельные alias и даты. Генератор проверяет
точное совпадение даты с существующей вехой; расхождение блокирует запись,
а не исправляет дату по имени alias. Не извлекай дату внедрения из времени
promotion, закрытия задач или свободного текста. Вехи с неизвестной датой не
создаются. Без releases.json старые вехи сохраняются без догадок.
Переименование выполняется только в производном actual-progress и его
Confluence-экспорте; общие preamble и утверждённые планы сохраняются.

### Независимые отметки QA и ПРОМ

Формат подписи: `Название · RELEASE-KEY · QA ✓ · ПРОМ ✓`. Показывай только
подтверждённые положительные отметки; легенда поясняет, что отсутствие отметки
не означает провал тестирования или невыпуск. Процент разработки, текущее имя
статуса, принадлежность эпику и дата вехи сами по себе отметок не дают.

Храни принятые факты в `execution/task-markers.json` выбранной поставки.
Это execution-источник: перед записью обязательны обычные application-preflight,
history-review, QA-проверка и рабочая ветка; файл входит в save-preview и коммит
вместе с реестром и производными. Разрешение на оформление не подтверждает факты.

Формат: `schema_version: 1`, `sources` — объект относительных путей к SHA256 всех
существующих `execution/tasks.md`, `execution/qa-groups.json` и
`slices/*/execution/tasks.md`; `tasks` — массив записей с точным `task_id`.
Изменение источников требует пересмотра доказательств, не механического обновления
хешей. Без файла используются только подтверждения из канонических реестров, описанные ниже.

Запись может содержать `release` и `membership`, а также независимо `qa` и
`production`. Каждое доказательство содержит `confirmed: true`, `source` (путь
к принятому history-review, релизу либо решению аналитика) и `evidence` (точное
основание). Для `qa` и `production` обязательна `date` в YYYY-MM-DD. Для production
обязательны явные `release`, `membership` и `analyst_confirmed: true` в доказательстве
внедрения. Членство должно совпадать с QA-группой, если та задаёт релиз.

QA подтверждается завершённым тестированием конкретной задачи по проверенной
истории или решению аналитика. Завершённая QA-группа позволяет зарегистрировать
факт для каждого проверенного участника; частичная группа не подтверждает всех.
ПРОМ регистрируется только для точного принятого состава внедрённого релиза,
по явному подтверждению аналитика. Не переносить отметки на весь эпик.
Откат, повторное открытие или изменение состава требуют пересмотра отметок.
Не редактировать raw-ответы трекера и не извлекать доказательства из старого
`history-not-collected` в первичном collection snapshot.

### Идентификаторы и существующие подтверждения

Каждая TASK-полоса начинается с собственного ключа SberTrack: `RSCON-6864 · BE Название`.
Бери номер из колонки `SberTrek`, не из legacy Task ID. Если связи с трекером нет,
выводи `лок. <Task ID> · QA Название`; это не номер карточки. PLAN/STORY не являются
задачами трекера и не получают выдуманных номеров. Ключ не дублируется из summary.

Генератор также использует уже принятые источники, без повторного ввода фактов:
- QA ✓ — проверенная непересекающаяся QA-группа; её QA и все участники имеют
  завершённое состояние, 100% и Actual Finish в каноническом execution-реестре.
  Незавершённая или повторно открытая группа этого подтверждения не даёт.
- ПРОМ ✓ — `releases/baseline-status.json`: `promoted`, среда `ПРОМ`, доказательство
  внедрения и полный состав, совпадающий со scope_hash. Ключ задачи должен входить
  в этот состав и соответствовать её релизу. Локальная QA получает отметку только
  когда все участники её группы входят в подтверждённый состав.

Эти отметки производны от сохранённых принятых фактов, не от 100% разработки
или текущего имени статуса в трекере. `task-markers.json` остаётся для отдельных
подтверждений, которых нет в названных источниках; он не обязателен для повторного
отображения уже сохранённых фактов. Не выводи дату выпуска из имени версии baseline.

## Персональная производительность в прогнозе actual-progress

`planning/team.md`, колонка `Personal coefficient`, задаёт абсолютный оценённый
выпуск на доступный рабочий день. Коэффициент ограничивается сверху единицей;
отсутствующее значение равно единице. Ноль, отрицательные и нечисловые значения
запрещены. Ролевая эффективность повторно не применяется.

Для неначатой задачи длительность = ceil(оценка / коэффициент исполнителя).
Автоназначение сравнивает окончания с индивидуальной длительностью.
У начатой задачи с известным исполнителем и подтверждённой готовностью меньше
100% пересчитывается только остаток: оценка × (1 − готовность / 100).
Он распределяется от текущей даты по доступным дням; фактическое начало сохраняется.
Без подтверждённого остатка коэффициент не применяется: неизвестная готовность
не равна нулю. Завершённые задачи, фактическое окончание и границы завершения
не пересчитываются. Исходные оценки и проценты не изменяются.

Округление коэффициента до шести знаков не должно добавлять лишний день
(3 / 0.428571 даёт 7 дней). Утверждённые планы не регенерируются;
меняется только actual-progress и его Confluence-экспорт.

## Окончание прогнозного QA и регресс

Незавершённый QA заканчивается строго через три рабочих дня после последней
задачи релиза, а без релиза — QA-группы или поставки. Начало считается назад
по длительности ceil(QA-оценки), минимум три рабочих дня. Это прогнозное окно,
не исправление фактического начала в реестре; текущая дата его не обрезает.
Процент сохраняется из источника, включая unknown. Завершённые интервалы не меняются.
QA-строки выводятся после разработки. Конфликт доступности ресурса или отсутствие
опорной даты блокирует запись, а не нарушает правило окончания. См. modes/execution-update.md.
