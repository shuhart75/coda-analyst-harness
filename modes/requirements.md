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

- `features/*/requirements.md`
- `features/*/references.md`
- `features/*/slices/*/slice.md`
- `features/*/slices/*/requirements/frontend.md`
- `features/*/slices/*/requirements/backend.md`
- `features/*/domain-impact.md`
- `features/*/context-summary.md`
- `features/*/artifact-map.md`
- `features/*/slices/*/context-summary.md`
- optional `features/*/.research/*` and `features/*/slices/*/.research/*`
- `features/*/slices/*/execution/task-candidates.md`
- `features/*/handoffs/*/handoff.json`
- `features/*/handoffs/*/revisions/*/package/*`

## Source-of-truth rule

- `features/<feature>/requirements.md` is the primary and authoritative requirements document for the feature.
- Slice cards and FE/BE packs are derived artifacts. They are not authored as independent parallel truths.
- If a slice pack reveals a missing rule, contradiction, or new requirement, update the root feature document first and only then re-derive the slice artifacts.
- Context summaries, artifact maps and `.research/` files are auxiliary. Accepted findings must be transferred into the root feature document, slice packs, `domain-impact.md` or `documents/planning/consistency-backlog.md`.

## Writing order

1. Create or update the root feature document `features/<feature>/requirements.md`.
2. Fix semantic slice boundaries and explicit slice order there.
3. Create or update `slices/*/slice.md` as decomposition cards derived from the root document.
4. Create or update `slices/*/requirements/frontend.md` and `slices/*/requirements/backend.md` as detailed annexes derived from the corresponding root section.
5. Do not invent slice scope that is absent from the root feature document without editing the root feature document first.

The root feature document must follow the selected root template. The old detailed root template is `templates/requirements/feature-requirements.template.md`; the new readable root template is `templates/requirements/feature-requirements.readable.template.md`.

For every new root document, keep the profile marker `Профиль требований: **АС КОДА / ISO/IEC/IEEE 29148:2018**`. Existing legacy documents remain valid until a substantial rewrite or explicit migration. A profiled document must contain the mandatory sections, atomic normative `REQ-*`, explicit `SCN-*`, verification methods, cross-feature impacts, dependencies, completion criteria and traceability defined by `core/requirements-profile.md`.

Only the user-owner may change a requirements document to `утверждён`. An approved document must record the approver and approval date. A developer receipt results in a new analytical decision or document revision; it never rewrites the historical meaning of an already transmitted revision.

## Tail cleanup rule

If the task replaces one requirement variant with another, remove stale mentions of the superseded variant in the same turn inside:

- `features/<feature>/requirements.md`;
- `features/<feature>/slices/*/slice.md`;
- `features/<feature>/slices/*/requirements/frontend.md`;
- `features/<feature>/slices/*/requirements/backend.md`;
- `features/<feature>/domain-impact.md` and `documents/planning/consistency-backlog.md` when they describe the replaced variant.

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
- For domain, lifecycle, role, API-semantic, shared-UI or neighboring-feature changes, expand into a full sweep with `domain-impact.md`, `documents/planning/consistency-backlog.md`, and affected baseline artifacts.
- Do not turn every minor wording fix into a whole-repo audit.

## Impact detection requirement

Any requirement change must be checked for consistency impact.

If the change affects domain rules, lifecycle, roles, API semantics, data model, neighboring features, or shared UI behavior:
- update `features/*/domain-impact.md`;
- list affected requirements and prototypes;
- update `documents/planning/consistency-backlog.md` when propagation is deferred;
- do not silently mutate `baseline/current/` unless the active task explicitly includes baseline update.

Обязательные доработки соседних функциональностей входят в объём работ и верхнеуровневую оценку инициирующей функциональности. Фиксируй их в отдельном разделе `Доработки затронутых функциональностей` корневых требований, а не только в `domain-impact.md`.

Каждая строка влияния должна быть связана с требованиями, включена во входной пакет и связана с проверками либо явно помечена как неприменимая с указанием причины.

## Передача на техническую декомпозицию

Каноническая команда аналитика: `сформируй пакет для разработки`. Равнозначны: `передаём в разработку`, `передаем в разработку`, `отдаём требования разработчикам`, `отдаем требования разработчикам`, `подготовь требования для разработки`, `собери пакет для разработчиков` и прежняя команда `подготовь пакет функциональности для технической декомпозиции`.

Если функциональность однозначно не следует из текущего контекста, сначала спроси, какую функциональность передавать. Далее выполни единый проход готовности:

1. Проверь корневые требования по `core/requirements-profile.md`: цель, границы, исключения, участники, предпосылки, правила, состояния и переходы, данные, права, интеграции, ошибки, отрицательные и граничные случаи, наблюдаемые критерии приёмки.
2. Проверь внутреннюю непротиворечивость и соответствие относящемуся к функциональности `baseline/current/`. Если требование зависит от фактического API, данных, статусов, ролей, проверок или уже реализованного поведения, выполни точечное исследование локального `coda` по `core/code-inspection.md`. Не сверяй весь код. Зафиксируй коммит и доказательства; принимающая SDD всё равно повторно сверяет требования со своей актуальной веткой перед реализацией.
3. Проверь раздел влияний. Каждая обязательная доработка соседней функциональности должна входить в объём, иметь критерий завершения и быть отражена в нужных требованиях и срезах.
4. Проверь срезы и подробные требования контуров: они производны от корневого документа, не расходятся с ним и покрывают требования, сценарии, влияния и проверки.
5. Проверь устойчивые идентификаторы и трассировку, зависимости, открытые вопросы, устаревшие хвосты, ссылки и русский язык.
6. Исправь автоматически только однозначные проблемы без изменения смысла: структуру, язык, ссылки, трассировку, синхронизацию производных материалов и явно устаревшие остатки уже принятого решения.
7. Если для исправления требуется выбрать поведение, границу, источник данных, приоритет правила, исключение или иной предметный смысл, ничего не придумывай. Задай аналитику ровно один вопрос, дождись ответа, внеси решение и только затем переходи к следующему вопросу.
8. Пока остаются противоречия, непроверяемые правила, блокирующие открытые вопросы или неразобранные влияния, пакет не создавай.
9. После исправлений повторно запусти профильную, языковую, ссылочную и трассировочную проверки для затронутой функциональности.
10. Создай общий пакет или следующую редакцию существующего пакета, выполни `handoffctl publish` и проверь, что состояние редакции равно `sent`, а `next_sdd_action.action` равно `process`.

Аналитик проверяет требования, а не служебное содержимое пакета. Поэтому успешная команда всегда заканчивается опубликованной редакцией без транспортного ZIP; промежуточного пользовательского состояния `ready` нет. В ответе покажи функциональность, номер редакции, путь к общему каталогу, способ трассировки и действие SDD. ZIP создавай только по отдельному явному требованию и только в `~/Downloads`.

Аналитик не определяет окончательную разбивку будущих задач Jira. После успешного прохода создай общий пакет командой `handoffctl init-feature`, если его ещё нет, добавь новую неизменяемую редакцию командой `handoffctl add-revision` и сразу опубликуй её командой `handoffctl publish`.

Пакет автоматически включает:

- `features/<feature>/requirements.md`;
- все `features/<feature>/slices/*/slice.md`;
- полные списки требований, `SCN-*`, `IMP-*` и способ трассировки;
- контрольные суммы файлов;
- правила разработческой декомпозиции и последующего тестирования.

Подтверждённые карточки создаются разработчиками в возвратах пакета. При изменении уже переданных требований не переписывай карточки разработчиков: создай новую входную редакцию и явно измени состояние прежней. Получение декомпозиции не меняет утверждённые планы; её материализация относится к `execution-update`.

## Языковой контроль

- Требования пишутся на русском языке.
- Английская форма допускается только для точного кода, пути, API/БД-идентификатора, значения перечисления или закреплённого названия внешней системы.
- В обычном тексте используй русские формулировки и избегай англицизмов, даже если они короче.
- Перед завершением прохода запусти `scripts/validate-language.py` для изменённой функциональности.
- Запусти `scripts/validate-requirements-profile.py` для изменённой функциональности. Документы прежнего формата без маркера профиля пропускаются до их осознанного перевода.
- Языковой контроль является частью обязательного второго прохода и выполняется до фиксации результата.

## Tail cleanup gate

- First update root requirements and directly derived slice artifacts.
- Then search for superseded terms, fields, statuses, routes, rules, and prototype behavior.
- Local unexplained tails block completion.
- Cross-mode propagation may be deferred only through a concrete consistency backlog item.

## Small-context requirements rules

For `делаем требования`, `актуализируй требования`, `разложи требования на срезы` and `подготовь детальные требования по срезам`, the assistant must automatically:

- read existing feature/slice context summaries and artifact map when present;
- create or refresh `features/<feature>/context-summary.md` after substantial root requirement changes;
- create or refresh `features/<feature>/artifact-map.md` when authored, derived or auxiliary artifacts change;
- create or refresh `features/<feature>/slices/<slice>/context-summary.md` when a slice card or FE/BE pack is created or materially changed;
- run role-based research from `core/research-policy.md` when requirements are large, ambiguous, cross-cutting, or code/source-material inspection is needed;
- automatically inspect the registered local `coda` clone when a current implementation fact is necessary, without asking the user for repository paths;
- keep analyst code access read-only and complete the before/after verification from `core/code-inspection.md`;
- run the completeness checklist before presenting slice requirements as ready;
- update a checkpoint before and after long decomposition or derivation passes.

Do not expose `собери контекст`, `исследуй срез` or `проверь полноту среза` as required user commands. Ask the user only when research finds a contradiction, missing business decision, prototype mismatch, neighboring-feature impact or required root requirement change.

## Forbidden without mode switch

- changing agreed planning estimates
- changing quarter or commander baseline gantt
- changing actual execution dates
