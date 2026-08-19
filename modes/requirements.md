# Mode: requirements

## Goal

Produce or update living requirement packs from canonical baseline, source materials and change requests.

## Main inputs

- `templates/requirements/`
- `baseline/current/`
- `context/source-materials/current-system/requirements/`
- `context/source-materials/current-system/screenshots/`
- `context/source-materials/current-system/prototypes/`
- `context/source-materials/current-system/diagrams/`
- `context/source-materials/change-requests/`

Use `templates/requirements/` as the active project-local template source. Do not write requirement packs freeform when these templates exist.

The common quality and structure contract is `core/requirements-profile.md`. It is based on ISO/IEC/IEEE 29148:2018 but does not claim full standards conformance. Both root formats implement the same profile; they differ only in presentation density and the amount of technical detail.

## Requirement format selection

Before generating or substantially rewriting requirements, choose one format:

- `new readable` / `новый лёгкий формат`: use `feature-requirements.readable.template.md`, `slice.readable.template.md`, `frontend.readable.template.md`, `backend.readable.template.md`.
- `old detailed` / `старый подробный формат`: use `feature-requirements.template.md`, `slice.template.md`, `frontend.template.md`, `backend.template.md`.

Selection rules:

- If the user explicitly says `новый формат`, `лёгкий формат`, `как deployments`, `краткие срезы`, use the new readable templates.
- If the user explicitly says `старый формат`, `подробный формат`, `как раньше`, use the old detailed templates.
- If the user does not specify a format and the feature already has requirements, preserve the feature's current format.
- If the user does not specify a format and this is a new feature, use the new readable format by default.
- Do not mix formats inside one feature unless the user explicitly asks for a migration or a partial experiment.
- Diagrams in either format must be PlantUML, not Mermaid.

## Main outputs

During ordinary analytical work:

- `features/*/requirements.md` as the only authored requirements document;
- `features/*/requirements-state.json` as machine-readable preparation state;
- optional bounded research evidence when a current implementation fact is required; cross-feature impact itself remains in the root document.

Only during an explicitly authorized package-preparation pass:

- `features/*/slices/*/slice.md`;
- `features/*/slices/*/requirements/frontend.md`;
- `features/*/slices/*/requirements/backend.md`;
- `features/*/handoffs/*/handoff.json`;
- `features/*/handoffs/*/revisions/*/package/*`.

## Source-of-truth rule

- `features/<feature>/requirements.md` is the primary and authoritative requirements document for the feature.
- Slice cards and FE/BE packs are derived transmission artifacts. Outside package preparation they may intentionally describe the last sent revision and must not be refreshed after every root change.
- `features/<feature>/requirements-state.json` records the change origin, whether slices are current or stale, whether one revision offer is pending, and whether explicit preparation is authorized.
- If a slice pack reveals a missing rule, contradiction, or new requirement during preparation, update the root feature document first and only then re-derive the slice artifacts.
- Context summaries, artifact maps and `.research/` files are auxiliary. During ordinary authoring, transfer accepted requirement findings into the root feature document. Transfer them into slice packs and `domain-impact.md` only during authorized package preparation; use `documents/planning/consistency-backlog.md` for concrete deferred work outside that derivation.

## Writing order

1. Run `scripts/requirementsctl.py status` before editing an existing feature. If it finds an unrecorded difference from the last published revision, ask the analyst for its origin and record it before making a new change.
2. Create or update only `features/<feature>/requirements.md`.
3. Keep possible semantic slice boundaries and order in that root document without creating slice files.
4. Record the origin with `scripts/requirementsctl.py record-change`.
5. Stop without touching slices, detailed packs, task candidates or handoff revisions unless package preparation was explicitly requested or accepted.
6. During an authorized preparation pass, run `begin-preparation`, derive slice cards and detailed annexes from the final root document, publish the package, then run `mark-published`.
7. Do not invent slice scope that is absent from the root feature document without editing the root feature document first.

The root feature document must follow the selected root template. The old detailed root template is `templates/requirements/feature-requirements.template.md`; the new readable root template is `templates/requirements/feature-requirements.readable.template.md`.

For every new root document, keep the profile marker `Профиль требований: **АС КОДА / ISO/IEC/IEEE 29148:2018**`. Existing legacy documents remain valid until a substantial rewrite or explicit migration. A profiled document must contain the mandatory sections, atomic normative `REQ-*`, explicit `SCN-*`, verification methods, cross-feature impacts, dependencies, completion criteria and traceability defined by `core/requirements-profile.md`.

Only the user-owner may change a requirements document to `утверждён`. An approved document must record the approver and approval date. A developer receipt results in a new analytical decision or document revision; it never rewrites the historical meaning of an already transmitted revision.

## Tail cleanup rule

If the task replaces one requirement variant with another during ordinary authoring, remove stale mentions of the superseded variant in `features/<feature>/requirements.md` in the same turn. Do not treat deliberately stale slices, `domain-impact.md` or immutable package revisions as tails at this stage. Derived requirement artifacts are cleaned and regenerated only in an authorized preparation pass; immutable revisions are never rewritten. Record a concrete consistency-backlog item only when the change also requires a separate update outside the requirements derivation.

Examples of stale tails to search for:

- old endpoint paths;
- old request or response field names;
- old role names;
- old status names;
- old UX control names or option labels;
- old contract filenames or Decision IDs.

## Two-speed consistency sweep

Keep consistency work proportional to the size of the change.

- For a small local edit, do a quick feature-local sweep with targeted text search or equivalent local find-in-files and stop when the changed feature is clean.
- For domain, lifecycle, role, API-semantic, shared-UI or neighboring-feature changes, expand the analysis across related sources, but record the requirement result in the root document. Refresh derived impact and slice artifacts only during authorized package preparation; never silently change baseline artifacts.
- Do not turn every minor wording fix into a whole-repo audit.

## Impact detection requirement

Any requirement change must be checked for consistency impact.

If the change affects domain rules, lifecycle, roles, API semantics, data model, neighboring features, or shared UI behavior, record the full impact, required neighboring work and completion criteria in the initiating root `requirements.md`. During ordinary authoring, do not refresh slices or `domain-impact.md`. During authorized package preparation, propagate the accepted impact into `domain-impact.md`, derived slices and the package; use `documents/planning/consistency-backlog.md` only for a concrete update outside that derivation. Do not silently mutate `baseline/current/` unless the active task explicitly includes baseline update.

Обязательные доработки соседних функциональностей входят в объём работ и верхнеуровневую оценку инициирующей функциональности. Фиксируй их в отдельном разделе `Доработки затронутых функциональностей` корневых требований, а не только в `domain-impact.md`.

Каждая строка влияния должна быть связана с корневыми требованиями. При подготовке пакета она также должна быть включена во входную редакцию и связана со срезами и проверками либо явно помечена как неприменимая с указанием причины.

## Передача на техническую декомпозицию

Каноническая команда аналитика: `сформируй пакет для разработки`. Равнозначны: `передаём в разработку`, `передаем в разработку`, `отдаём требования разработчикам`, `отдаем требования разработчикам`, `подготовь требования для разработки`, `собери пакет для разработчиков` и прежняя команда `подготовь пакет функциональности для технической декомпозиции`.

Если функциональность однозначно не следует из текущего контекста, сначала спроси, какую функциональность передавать. Далее выполни единый проход готовности:

1. Проверь корневые требования по `core/requirements-profile.md`: цель, границы, исключения, участники, предпосылки, правила, состояния и переходы, данные, права, интеграции, ошибки, отрицательные и граничные случаи, наблюдаемые критерии приёмки.
2. Проверь внутреннюю непротиворечивость и соответствие относящемуся к функциональности `baseline/current/`. Если требование зависит от фактического API, данных, статусов, ролей, проверок или уже реализованного поведения, выполни точечное исследование локального `coda` по `core/code-inspection.md`. Не сверяй весь код. Зафиксируй коммит и доказательства; принимающая SDD всё равно повторно сверяет требования со своей актуальной веткой перед реализацией.
3. Проверь раздел влияний. Каждая обязательная доработка соседней функциональности должна входить в объём и иметь критерий завершения в корневых требованиях; после этого отрази её в производных срезах текущего прохода.
4. После завершения проверки корневого документа построй заново срезы и подробные требования контуров. Они должны покрывать текущие требования, сценарии, влияния и проверки и не должны наследовать устаревшее содержимое прошлой передачи.
5. Проверь устойчивые идентификаторы и трассировку, зависимости, открытые вопросы, устаревшие хвосты, ссылки и русский язык.
6. Исправь автоматически только однозначные проблемы без изменения смысла: структуру, язык, ссылки, трассировку и явно устаревшие остатки уже принятого решения. Производные материалы создавай только внутри этого прохода.
7. Если для исправления требуется выбрать поведение, границу, источник данных, приоритет правила, исключение или иной предметный смысл, ничего не придумывай. Задай аналитику ровно один вопрос, дождись ответа, внеси решение и только затем переходи к следующему вопросу.
8. Пока остаются противоречия, непроверяемые правила, блокирующие открытые вопросы или неразобранные влияния, пакет не создавай.
9. После исправлений повторно запусти профильную, языковую, ссылочную и трассировочную проверки для затронутой функциональности.
10. До изменения срезов выполни `requirementsctl.py begin-preparation`. Создай общий пакет или следующую редакцию существующего пакета, выполни `handoffctl publish`, затем `requirementsctl.py mark-published`; проверь состояние `sent` и `next_sdd_action.action = process`.

Аналитик проверяет требования, а не служебное содержимое пакета. Поэтому успешная команда всегда заканчивается опубликованной редакцией без транспортного ZIP; промежуточного пользовательского состояния `ready` нет. В ответе покажи функциональность, номер редакции, путь к общему каталогу, способ трассировки и действие SDD. ZIP создавай только по отдельному явному требованию и только в `~/Downloads`.

Аналитик не определяет окончательную разбивку будущих задач Jira. После успешного прохода создай общий пакет командой `handoffctl init-feature`, если его ещё нет, добавь новую неизменяемую редакцию командой `handoffctl add-revision` и сразу опубликуй её командой `handoffctl publish`.

Пакет автоматически включает:

- `features/<feature>/requirements.md`;
- все `features/<feature>/slices/*/slice.md`;
- полные списки требований, `SCN-*`, `IMP-*` и способ трассировки;
- контрольные суммы файлов;
- правила разработческой декомпозиции и последующего тестирования.

Подтверждённые карточки создаются разработчиками в возвратах пакета. При изменении уже переданных требований не переписывай карточки разработчиков или прежнюю редакцию. Новую входную редакцию создавай только после отдельной команды или принятого предложения аналитика. Получение декомпозиции не меняет утверждённые планы; её материализация относится к `execution-update`.

## Изменения после первой передачи

- После любого изменения корневых требований вызови `requirementsctl.py record-change` с фактическим источником изменения.
- Для `developer-receipt` обязательно передай путь к зарегистрированной квитанции. Не меняй срезы и не создавай или не предлагай редакцию пакета.
- Для `analyst`, если пакет уже существует и состояние требует предложения, вызови `mark-offered` и один раз предложи подготовить новую редакцию и пересмотреть срезы.
- При отказе вызови `decline-revision`. После этого не повторяй предложение и не трогай производные материалы, даже после следующих правок, пока аналитик явно не поручит подготовку.
- Согласие на предложение и любая явная команда подготовки равнозначны `begin-preparation` и разрешают один полный проход до опубликованной редакции.

## Языковой контроль

- Требования пишутся на русском языке.
- Английская форма допускается только для точного кода, пути, API/БД-идентификатора, значения перечисления или закреплённого названия внешней системы.
- В обычном тексте используй русские формулировки и избегай англицизмов, даже если они короче.
- Перед завершением прохода запусти `scripts/validate-language.py` для изменённой функциональности.
- Запусти `scripts/validate-requirements-profile.py` для изменённой функциональности. Документы прежнего формата без маркера профиля пропускаются до их осознанного перевода.
- Языковой контроль является частью обязательного второго прохода и выполняется до фиксации результата.

## Tail cleanup gate

- First update root requirements and record the change origin. Update slice artifacts only during authorized package preparation.
- Then search for superseded terms, fields, statuses, routes, rules, and prototype behavior.
- Local unexplained tails block completion.
- Cross-mode propagation may be deferred only through a concrete consistency backlog item.

## Small-context requirements rules

For `делаем требования` and `актуализируй требования`, the assistant must automatically:

- read existing feature/slice context summaries and artifact map when present;
- create or refresh `features/<feature>/context-summary.md` after substantial root requirement changes;
- create or refresh `features/<feature>/artifact-map.md` only when authored or auxiliary artifacts change;
- run role-based research from `core/research-policy.md` when requirements are large, ambiguous, cross-cutting, or code/source-material inspection is needed;
- automatically inspect the registered local `coda` clone when a current implementation fact is necessary, without asking the user for repository paths;
- keep analyst code access read-only and complete the before/after verification from `core/code-inspection.md`;
- run the root-document completeness checklist before presenting requirements as complete;
- update a checkpoint before and after long work on the root document.

Slice context summaries, slice completeness checks and derivation checkpoints run only under an authorized package-preparation command.

Do not expose `собери контекст`, `исследуй срез` or `проверь полноту среза` as required user commands. Ask the user only when research finds a contradiction, missing business decision, prototype mismatch, neighboring-feature impact or required root requirement change.

## Forbidden without mode switch

- changing agreed planning estimates
- changing quarter or commander baseline gantt
- changing actual execution dates
