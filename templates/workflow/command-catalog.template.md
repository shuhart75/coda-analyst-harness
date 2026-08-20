# Command Catalog

This file documents short natural-language commands that switch context or trigger recurring workflow actions.

## Interpretation rules

- Commands are intent shortcuts, not magic shell commands.
- If a command implies a mode switch, update `.workspace-state/active-mode.md` or explicitly state that the user should switch mode if the runtime cannot edit it.
- After a mode switch, read the target mode file before editing artifacts.
- If a command includes a folder/path, inspect that path before writing outputs.
- If a command causes cross-feature/domain impact, respect the active mode: ordinary requirement authoring records it in the root requirements; package preparation or a separate domain-decision command may update `domain-impact.md` and the consistency backlog.
- For small requirement edits, prefer a quick feature-local tail cleanup over a whole-repo audit.
- If a command is ambiguous, make a reasonable assumption and state it briefly; ask only when the wrong assumption would create significant rework.

## Daily core command set

Use this as the preferred short set for everyday work. VSCodium adapters and snippets remain in the harness and are not copied into the content repository.

## How to use these commands

- Commands are ordinary natural-language phrases. Write them directly to the LLM in chat, with no slash syntax.
- A command may be the whole message or the first line of a longer request.
- After the command, add concrete context: folders, feature ids, release ids, screenshots, dates, task ids, constraints.
- One message may contain one mode switch command plus the concrete work to do in that mode.
- If you switch mode implicitly, the LLM should first read the target mode rules, then update artifacts.
- In VSCodium, type a snippet prefix such as `wf-plan` or `wf-req`, press Tab/Enter, then fill placeholders and send the generated text to the LLM.

## Recommended command shape

Use a two-part pattern:

1. first line: short command from this catalog;
2. next lines: concrete task context and expected output.

Examples:

```text
новая фича
Источник: `/home/reutov/Documents/AI/simulations_AI_agent`
Квартал: `2026-Q2`
Нужно сделать intake: ничего не создавать, сначала отделить baseline от новой дельты.
```

```text
занимаемся планированием
В папке `context/source-materials/change-requests/mobile-scorecards` лежат скрины и описание.
Нужно разложить доработку на planning stories, дать HLE в человеко-днях с разрезом `AN / FE / BE / QA` и обновить quarter-plan/commander-plan.
```

```text
делаем требования
В папке `context/source-materials/change-requests/packages-filtering` лежат текущие требования и новые скрины.
Формат: новый лёгкий
Нужно подготовить только `features/<feature>/requirements.md` по выбранному шаблону, зафиксировать в нём возможные границы и порядок срезов и обновить влияния. Срезы и пакет пока не формировать.
```

```text
делаем презентационный прототип
Feature: `packages`
Материалы: `context/source-materials/change-requests/packages-filtering`
Сначала найди существующие прототипы и скриншоты этой feature, помоги выбрать базовый референс, и пока работай только с общим `features/packages/prototype.html`.
```

```text
общий прототип согласован
Feature: `packages`
Теперь возьми подтверждённый root prototype и требования из `features/packages/requirements.md`, разложи их на slice-level handoff prototypes для фронтендера.
```

```text
обновляем прогресс
RSCON-2445 завершена вчера, RSCON-2451 взял второй фронтендер позавчера.
Добавь milestone релиза на 2026-04-30, обнови actual-progress и Confluence-копию без include.
```

| Core command | Mode | Primary intent |
|---|---|---|
| `новая фича` | `planning` | Run feature intake/preflight before any scaffold step. |
| `занимаемся планированием` | `planning` | Switch into quarter planning and HLE mode. |
| `делаем требования` | `requirements` | Switch into living requirements mode and use the requested requirements format. |
| `сходи в код` | текущий аналитический режим | Выполнить ограниченное исследование одного контура локального `coda` без изменения кода и привязать выводы к коммиту. |
| `делаем презентационный прототип` | `scope-prototype` | Switch into common feature prototype mode and choose the visual base before generating. |
| `делаем прототип для разработки` | `delivery-prototype` | Switch into slice handoff mode, but block any slice edits until the root feature prototype is explicitly approved. |
| `обновляем прогресс` | `execution-update` | Switch into implementation tracking mode. |
| `финализируем релиз` | `release-finalization` | Switch into release/baseline promotion mode. |
| `актуализируй требования` | `requirements` | Обновить только корневые требования и зафиксировать источник изменения, не пересобирая срезы или пакет. |
| `разложи требования на срезы` | `requirements` | Явно разрешить полный проход подготовки пакета: проверить корневой документ, построить срезы и опубликовать редакцию. |
| `подготовь детальные требования по срезам` | `requirements` | Прежняя формулировка явной подготовки пакета; отдельно от опубликованной редакции срезы не создаются. |
| `сформируй пакет для разработки` | `requirements` | Проверить и безопасно исправить требования, последовательно уточнить неоднозначности, затем сразу отправить неизменяемую редакцию в SDD без создания ZIP. |
| `обработай квитанции реализации и тестирования` | `requirements` | Принять решения по факту, обновить корневые требования и текущее состояние без новых срезов и редакций пакета. |
| `собери транспортный ZIP редакции <NNN>` | `requirements` | По явному требованию создать архив опубликованной редакции в `~/Downloads`, не меняя пакет и не сохраняя ZIP в репозитории. |
| `приостанови редакцию пакета` | `requirements` | Set SDD action to wait or stop-and-report without rewriting the revision. |
| `покажи состояние пакета` | `requirements` | Read `handoff.json` and report the single current SDD action. |
| `покажи подтверждённую декомпозицию` | `execution-update` | Прочитать актуальный неизменяемый снимок карточек, полученный от разработки. |
| `обнови фактический план по подтверждённой декомпозиции` | `execution-update` | По решению аналитика материализовать выбранные карточки в фактическом слое. |
| `проверь хвосты требований` | `requirements` | Run a quick feature-local sweep for stale old variants after a requirement edit. |
| `проверь консистентность требований` | `requirements` | Run a consistency sweep across affected artifacts. |
| `актуализируй прототипы` | `delivery-prototype` | Update prototypes listed in impact/backlog. |
| `общий прототип согласован` | `delivery-prototype` | Mark the root prototype as approved and only then use it as the source for slice handoff prototypes. |
| `создай прототип среза для фронта` | `delivery-prototype` | Create or update a slice-level frontend handoff prototype after root prototype approval. |
| `обнови реальный прогресс` | `execution-update` | Update tasks, actual-progress gantt and Confluence export. |
| `собери puml без инклюдов` | `execution-update` | Create a standalone PlantUML export from an include-based gantt view. |
| `подготовь декомпозицию серверной части` | `execution-update` | Исследовать серверный контур и создать или обновить предложенные `DEV-BE-*`. |
| `подготовь декомпозицию клиентской части` | `execution-update` | Исследовать клиентский контур и создать или обновить предложенные `DEV-FE-*`. |
| `проверь декомпозицию` | `execution-update` | Проверить карточки, размеры, зависимости и покрытие без содержательных решений. |
| `декомпозиция подтверждена разработкой` | `execution-update` | Создать снимок для аналитика и разрешить сразу продолжить разработку. |
| `подготовь список для Jira` | `execution-update` | Показать подтверждённые карточки; оценка и ключ Jira необязательны. |
| `свяжи DEV-* с <ключ Jira>` | `execution-update` | Записать необязательную связь карточки с Jira. |
| `возьми DEV-* в разработку` | `execution-update` | Реализовать подтверждённую карточку и вернуть отдельную квитанцию. |
| `возьми срез <id> в тестирование` | `execution-update` | Проверить срез и вернуть независимую квитанцию тестирования. |
| `подготовь проверки по срезу` | `execution-update` | Draft QA checks and coverage for a ready slice. |
| `собери негативные сценарии` | `execution-update` | Draft negative and edge scenarios tied to slice requirements. |
| `сверь проверки с требованиями` | `execution-update` | Build or update a requirement-to-check coverage matrix. |
| `проверь прототип по срезу` | `execution-update` | Compare slice prototype behavior with slice requirements and expected checks. |
| `проверь реализацию по срезу` | `execution-update` | Compare delivered implementation with slice requirements, prototype and QA checks. |
| `зафиксируй найденные пробелы` | `execution-update` | Record gaps and route them back to requirements, prototype, implementation plan or backlog. |
| `зафиксируй доменное решение` | `requirements` | Register decision and impact in `domain-impact.md`. |
| `собери релизный пакет` | `release-finalization` | Prepare release-level final artifacts. |
| `промоуть в baseline` | `release-finalization` | Promote release outputs into canonical baseline. |
| `откати решение DEC-*` | `release-finalization` | Start rollback flow for a known decision. |
| `проверь workflow` | any | Run validations and workflow checks. |
| `проверь русский язык требований` | `requirements` | Check changed requirement prose and suggest Russian replacements for avoidable anglicisms. |
| `утверди квартальный план` | `planning` | Only the project owner marks the current draft plan approved and immutable. |
| `предложи реальные задачи по срезам` | `execution-update` | Прежняя формулировка: материализовать выбранные подтверждённые карточки в фактическом слое. |

## Role-Oriented Command Map

These are the recommended user-facing commands by role. Internal context refresh, checkpoints, research, completeness checks, prototype alignment and coverage matrices run automatically under these commands.

| Role | Commands | User gets |
|---|---|---|
| Analyst | `новая фича`, `занимаемся планированием`, `спланируй фичу`, `делаем требования`, `сходи в код`, `сформируй пакет для разработки`, `обработай квитанции реализации и тестирования`, `обнови фактический план по подтверждённой декомпозиции` | Планы, живые корневые требования, подтверждённые кодом факты, отправленные редакции и фактическое выполнение. |
| Developer | `подготовь декомпозицию серверной части`, `подготовь декомпозицию клиентской части`, `проверь декомпозицию`, `декомпозиция подтверждена разработкой`, `подготовь список для Jira`, `возьми DEV-* в разработку` | Карточки будущих задач Jira, снимок декомпозиции и квитанции реализации. |
| Tester | `возьми срез <id> в тестирование`, `подготовь проверки по срезу`, `собери негативные сценарии`, `сверь проверки с требованиями`, `зафиксируй найденные пробелы` | Проверки среза, покрытие и независимая квитанция тестирования. |

Ask the user only when the workflow finds ambiguity, contradiction, cross-slice or cross-feature impact, an untestable requirement, unexplained failing checks, or a required change to the source of truth.

## Accepted synonyms

Treat these as equivalent user phrasings.

| Canonical command | Accepted synonyms |
|---|---|
| `новая фича` | `разбери новую фичу`, `сделай feature intake`, `сделай preflight по фиче`, `появилась новая фича`, `это новая фича` |
| `занимаемся планированием` | `переходим в планирование`, `давай планировать`, `давай займемся планированием`, `включаем planning`, `пора планировать` |
| `делаем требования` | `переходим к требованиям`, `давай требования`, `давай сделаем требования`, `пишем требования`, `соберем требования`, `включаем requirements`, `делаем требования в новом формате`, `делаем требования в старом формате` |
| `делаем презентационный прототип` | `делаем scope prototype`, `собери демо-прототип`, `делаем демо для заказчика`, `делаем макет для согласования`, `собери кликабельный макет для заказчика`, `нужен демо-макет`, `давай общий прототип` |
| `делаем прототип для разработки` | `делаем handoff prototype`, `делаем delivery prototype`, `собери прототип для фронта`, `делаем макет для фронта`, `собери handoff-макет`, `нужен макет для фронтендера` |
| `обновляем прогресс` | `переходим к прогрессу`, `фиксируем прогресс`, `давай обновим прогресс`, `обновим статус задач`, `зафиксируем факт`, `включаем execution update` |
| `финализируем релиз` | `переходим к релизу`, `собираем релиз`, `давай финализировать релиз`, `закрываем релизный цикл`, `готовим релизный пакет`, `включаем release finalization` |
| `актуализируй требования` | `обнови требования`, `синхронизируй требования`, `подтяни требования`, `приведи требования в актуальное состояние` |
| `сформируй пакет для разработки` | `передаём в разработку`, `передаем в разработку`, `отдаём требования разработчикам`, `отдаем требования разработчикам`, `подготовь требования для разработки`, `собери пакет для разработчиков`, `подготовь пакет функциональности для технической декомпозиции`, `собери пакет функциональности`, `передай функциональность на декомпозицию`, `разложи требования на срезы`, `разложи по срезам`, `нарежь требования на срезы`, `подготовь детальные требования по срезам`, `детализируй срезы`, `подготовь FE/BE требования по срезам` |
| `проверь хвосты требований` | `дочисти хвосты`, `убери хвосты в требованиях`, `проверь старые упоминания`, `проверь что старый вариант нигде не остался`, `сделай локальную дочистку требований` |
| `проверь консистентность требований` | `сделай consistency sweep`, `проверь консистентность`, `сверь требования`, `проверь что ничего не разъехалось`, `сделай сверку требований` |
| `актуализируй прототипы` | `обнови прототипы`, `синхронизируй прототипы`, `подтяни прототипы`, `приведи макеты в актуальное состояние` |
| `создай прототип среза для фронта` | `собери прототип среза для фронтенда`, `подготовь handoff-прототип среза`, `сделай макет среза для фронта`, `создай slice prototype` |
| `обнови реальный прогресс` | `обнови actual progress`, `обнови actual-progress`, `зафиксируй прогресс`, `синхронизируй прогресс`, `обнови фактический прогресс`, `обнови план-факт` |
| `собери puml без инклюдов` | `собери puml для Confluence`, `собери PlantUML без include`, `разверни include в puml`, `дай standalone puml`, `собери гант для конфлюенса` |
| `возьми DEV-* в разработку` | `начни реализацию DEV-*`, `реализуй DEV-*` |
| `возьми срез <id> в тестирование` | `начни проверку среза <id>`, `проверь срез <id>` |
| `разбери срез по коду` | `найди где реализовывать срез`, `сопоставь срез с кодом`, `разбери код под срез` |
| `предложи план реализации` | `собери план реализации среза`, `распиши реализацию среза`, `разложи реализацию по шагам` |
| `начни реализацию` | `начинай реализацию`, `приступай к реализации`, `выполни первую задачу реализации`, `стартуй реализацию среза` |
| `продолжи реализацию` | `продолжай реализацию`, `продолжи по плану`, `возобнови реализацию`, `продолжи с последнего чекпойнта` |
| `проверь реализацию среза` | `сверь реализацию со срезом`, `проверь код по срезу`, `проверь что срез реализован` |
| `подготовь к ревью` | `собери summary к ревью`, `подготовь review notes`, `подготовь MR summary`, `подготовь PR summary` |
| `подготовь проверки по срезу` | `собери тесты по срезу`, `подготовь тест-дизайн по срезу`, `собери QA-проверки` |
| `собери негативные сценарии` | `собери негативные кейсы`, `собери граничные сценарии`, `усиль тест-дизайн негативными сценариями` |
| `сверь проверки с требованиями` | `построй покрытие требований проверками`, `проверь покрытие тестами`, `сверь тест-дизайн с требованиями` |
| `проверь прототип по срезу` | `сверь прототип со срезом`, `проверь макет по требованиям среза`, `проверь slice prototype` |
| `проверь реализацию по срезу` | `сверь реализацию с требованиями среза`, `проверь реализацию по требованиям`, `проверь готовый срез` |
| `зафиксируй найденные пробелы` | `запиши пробелы`, `зафиксируй gaps`, `верни пробелы в требования`, `оформи найденные вопросы` |
| `зафиксируй доменное решение` | `зарегистрируй решение`, `зафиксируй decision`, `добавь domain decision`, `запиши доменное решение`, `оформи доменное решение`, `зарегистрируй DEC` |
| `собери релизный пакет` | `подготовь release package`, `собери release`, `зафиксируй релиз`, `подготовь пакет релиза`, `собери пакет релиза`, `подготовь релизный комплект` |
| `промоуть в baseline` | `обнови baseline`, `сделай baseline promotion`, `перенеси в baseline`, `зафиксируй новый baseline`, `обнови текущее состояние системы` |
| `откати решение DEC-*` | `rollback DEC-*`, `отмени решение DEC-*`, `сделай rollback по DEC-*`, `откати DEC-*`, `верни решение DEC-*` |
| `проверь workflow` | `проверь всё`, `прогони проверки`, `workflow check`, `проверь harness`, `сделай полный check`, `проверь структуру и ссылки` |

## Modes

- `planning`
- `requirements`
- `scope-prototype`
- `delivery-prototype`
- `execution-update`
- `release-finalization`

## Context Switch Commands

| User command | Target mode | Required first action |
|---|---|---|
| `синкани репы` | текущий аналитический режим | Выполнить `workspace.py bootstrap`, проверить и точечно зафиксировать понятные изменения `analytics`, затем выполнить `workspace.py sync`: обновить доступный `code`; при наличии `source` объединить его с `analytics` и проверить обратную заплату, без `source` обновить и отправить только `analytics`. Отсутствующие `code` и `source` не восстанавливать. |
| `обнови код` | текущий аналитический режим | Выполнить только `workspace.py update-code`, то есть защищённый `git pull --ff-only`; другие изменения кода запрещены. |
| `новая фича` | `planning` | Switch mode, inspect source folder, run intake, do not scaffold yet. |
| `занимаемся планированием` | `planning` | Switch mode, read baseline/current and current quarter planning. |
| `делаем требования` | `requirements` | Switch mode, select the format, read baseline/current and author only the root feature requirements. |
| `сходи в код` | текущий аналитический режим | Если роль `code` доступна, разрешить путь из реестра, зафиксировать состояние `coda`, исследовать один контур, сообщить коммит и доказательства, затем проверить неизменность кодового репозитория. Если роль отсутствует, сообщить, что сверка с кодом недоступна, и не искать другой клон. |
| `разложи требования на срезы` | `requirements` | Считать явной подготовкой пакета: сначала проверить корневые требования, затем построить срезы и опубликовать редакцию. |
| `подготовь детальные требования по срезам` | `requirements` | Считать прежней формулировкой той же полной подготовки и публикации пакета. |
| `сформируй пакет для разработки` | `requirements` | Переключить режим; проверить полноту, непротиворечивость, проверяемость, влияния, трассировку и язык корневого документа; автоматически исправить только однозначное; при сомнениях задавать по одному вопросу; затем построить срезы и опубликовать редакцию как `sent`. |
| `приостанови редакцию пакета` | `requirements` | Change only the root lifecycle manifest; use stop-and-report when an already claimed revision must return partial fact. |
| `покажи подтверждённую декомпозицию` | `execution-update` | Найти актуальный снимок декомпозиции и показать карточки, оценки и необязательные связи Jira. |
| `обнови фактический план по подтверждённой декомпозиции` | `execution-update` | Материализовать выбранные аналитиком карточки, не меняя утверждённый план. |
| `делаем презентационный прототип` | `scope-prototype` | Switch mode, inspect existing prototypes/references and choose the common feature prototype base before writing. |
| `делаем прототип для разработки` | `delivery-prototype` | Switch mode, verify the root prototype exists and is explicitly approved in `prototype-notes.md`, otherwise stop without editing slice prototypes. |
| `создай прототип среза для фронта` | `delivery-prototype` | Switch mode, verify root prototype approval, then derive the slice handoff prototype from root prototype and requirements. |
| `обновляем прогресс` | `execution-update` | Switch mode, read planning actualization, execution tasks and team roster. |
| `подготовь декомпозицию серверной части` | `execution-update` | Прочитать `handoff.json`, исследовать серверный контур и подготовить `DEV-BE-*`; пути пользователь не указывает. |
| `подготовь декомпозицию клиентской части` | `execution-update` | Прочитать `handoff.json`, исследовать клиентский контур и подготовить `DEV-FE-*`; пути пользователь не указывает. |
| `проверь декомпозицию` | `execution-update` | Проверить структуру, покрытие, размеры и зависимости текущих карточек. |
| `декомпозиция подтверждена разработкой` | `execution-update` | Подтвердить действующие карточки, создать снимок для аналитика и сохранить `next_sdd_action = continue`. |
| `возьми DEV-* в разработку` | `execution-update` | Найти подтверждённую карточку, сверить её с текущим кодом, выполнить доступную работу и зарегистрировать квитанцию. |
| `возьми срез <id> в тестирование` | `execution-update` | Найти срез активной редакции, связанные карточки и квитанции реализации, выполнить проверку и зарегистрировать квитанцию среза. |
| `подготовь проверки по срезу` | `execution-update` | Switch mode, read slice requirements/prototypes and draft QA coverage. |
| `финализируем релиз` | `release-finalization` | Switch mode, read releases, domain-impact files, consistency backlog. |

## Workflow Commands

### Planning

| User command | Meaning | Main artifacts |
|---|---|---|
| `новая фича` | Run feature intake/preflight and separate baseline coverage from the new delta before scaffolding. | `planning/intake/*.md`, `baseline/current/*`, `features/*`, source folders |
| `спланируй квартал` | Build or update quarter planning structure and gantt. | `planning/<quarter>/gantt/*`, `features/*/planning/*` |
| `разложи фичу на planning stories` | Create/update planning stories with Summary, Description and role-split estimates. | `features/<feature>/planning/stories/*.md`, `estimates.md` |
| `подготовь HLE` | Create at most one role story per `AN`, `BE`, `FE`, `QA`; preserve analyst, team and explicitly agreed effort plus parallelism and efficiency. | role planning stories, `estimates.md`, scope prototype notes |
| `спланируй фичу` | Prepare feature planning context, role stories, assumptions, risks and source/delta/slice mapping. | `planning-context.md`, `assumptions.md`, `risk-register.md`, `story-map.md`, role stories |
| `собери квартальный план` | Schedule draft role stories by priority, dependencies, team capacity, closed intervals and efficiency. | quarter-plan includes and PlantUML |
| `собери командирский план` | Schedule the same draft scope with approved hidden risk buffer of at least 20 percent. | commander-plan includes and PlantUML |
| `утверди квартальный план` | After all checks, let only the project owner mark plan-state as approved. Approved quarter and commander plans are immutable. | `planning/<quarter>/plan-state.md` |
| `сравни квартальный план с фактом` | Compare immutable planning baselines with task candidates and actual tasks, then propose future calibration. | `planning/<quarter>/retrospective.md` |
| `собери командирский план` | Produce buffered management plan from quarter plan. | `commander-plan.puml`, includes |
| `сравни план и факт` | Compare quarter/commander baseline with actual-progress. | gantt files, execution tasks |

### Requirements

| User command | Meaning | Main artifacts |
|---|---|---|
| `давай сделаем требования` | Создать или обновить только корневой документ функциональности; производные материалы не формировать. | `features/*/requirements.md`, `features/*/requirements-state.json` |
| `делаем требования в новом формате` | Use the new readable templates: business context in root, short visual slice packs, tester checklists in every slice, PlantUML only. | `templates/requirements/*.readable.template.md`, requirements |
| `делаем требования в старом формате` | Use the old detailed templates and preserve the earlier Confluence-style structure. | `templates/requirements/feature-requirements.template.md`, requirements |
| `актуализируй требования` | Обновить корневые требования, зафиксировать источник изменения и не трогать производные материалы. Уже отправленную редакцию и карточки разработчиков не переписывать. | requirements, `requirements-state.json` |
| `проверь хвосты требований` | Проверить живые корневые требования; не считать хвостами неизменяемые пакеты и производные материалы прошлой передачи. | current feature requirements |
| `проверь консистентность требований` | Run a consistency sweep across affected features and baseline. | requirements, `baseline/current/*`, `planning/consistency-backlog.md` |
| `проверь русский язык требований` | Run the language validator for changed requirements; keep English only for exact technical identifiers and fixed special terms. | changed root/slice requirement files |
| `сходи в код` | Inspect current implementation facts in one registered `coda` contour without changing it. | ad hoc answer or `features/*/.research/code-evidence.yaml`, then requirements when accepted |
| `разложи требования на срезы` | Явно разрешить полный проход подготовки и публикации пакета; срезы отдельно не оставлять. | root requirements, slices, handoff revision |
| `подготовь детальные требования по срезам` | Прежняя формулировка той же явной подготовки пакета. | root requirements, slices, handoff revision |
| `сформируй пакет для разработки` | Выполнить полный проход готовности требований; при содержательных сомнениях задавать аналитику по одному вопросу; затем создать транспорт и сразу опубликовать редакцию для SDD без промежуточного `ready`. | требования, срезы, `features/<feature>/handoffs/<package-id>/*` |
| `обработай квитанции реализации и тестирования` | Обновить требования и текущее состояние на основании квитанций без пересборки срезов, предложения или создания новой редакции. | requirements, baseline, analyst review, `requirements-state.json` |
| `приостанови редакцию пакета` | Tell SDD to wait or stop and report current fact, without mutating the input revision. | `handoff.json` |
| `покажи состояние пакета` | Show active revision, expected receipt and exact next SDD action. | `handoff.json` |
| `покажи подтверждённую декомпозицию` | Показать актуальный снимок карточек, который уже фоново доступен аналитику. | `returns/decomposition-snapshots/*` |
| `обнови фактический план по подтверждённой декомпозиции` | По решению аналитика перенести выбранные карточки в фактический слой; разработку не блокировать. | task candidates, execution tasks, actual-progress |
| `предложи реальные задачи по срезам` | Прежняя формулировка: материализовать выбранные карточки подтверждённого снимка. | task candidates, execution tasks |
| `зафиксируй доменное решение` | Add Decision ID and impact record for a domain/business rule decision. | `domain-impact.md`, consistency backlog |

### Scope Prototype

| User command | Meaning | Main artifacts |
|---|---|---|
| `сделай презентационный прототип` | First choose a visual base, then build/update the common root feature prototype with fake data as a user-facing whole-feature mockup. | `features/<feature>/prototype.html`, `features/<feature>/prototype-notes.md` |
| `покажи ideal и MVP` | Make the common feature prototype demonstrate ideal target and MVP cut. | root feature prototype, prototype notes |
| `подготовь демо для заказчика` | Polish the common feature prototype for scope alignment presentation. | root feature prototype |

### Delivery Prototype

| User command | Meaning | Main artifacts |
|---|---|---|
| `сделай прототип для фронта` | Only after root prototype confirmation, build/update slice-level MUI handoff prototypes derived from it; otherwise stop without editing them. | `features/<feature>/slices/<slice>/delivery-prototype/*` |
| `создай прототип среза для фронта` | Build or update one slice-level frontend handoff prototype after root prototype confirmation. | `features/<feature>/slices/<slice>/delivery-prototype/*` |
| `актуализируй прототипы` | Update affected prototypes listed in domain-impact/backlog, starting from the common feature prototype when relevant. | affected prototype files |
| `актуализируй прототип по требованиям` | Align one delivery prototype with current slice requirements. | delivery prototype and notes |
| `общий прототип согласован` | Mark the root prototype as approved and only then use it as the source for slice-level handoff prototypes. | `features/<feature>/prototype.html`, `features/<feature>/prototype-notes.md`, `features/<feature>/slices/*/delivery-prototype/*` |

### Execution Update

| User command | Meaning | Main artifacts |
|---|---|---|
| `обнови реальный прогресс` | Update implementation tasks and regenerate actual-progress gantt plus standalone Confluence export. | `execution/tasks.md`, actualization, `planning/team.md`, gantt |
| `задача X завершена` | Mark implementation task done and adjust actual dates/progress. | task registry, actual-progress |
| `задачу X взял Y` | Set executor and actual/planned start for a task. | task registry, actual-progress |
| `добавь реальные задачи вместо story X` | Materialize planning story with implementation tasks. | task registry, `actualization.md` |
| `добавь milestone релиза` | Add release milestone to actual-progress/related gantt. | gantt preamble/include |
| `собери puml без инклюдов` | Expand an include-based PlantUML view into a standalone export file for Confluence or external sharing. | generated gantt view, standalone export puml |
| `подготовь декомпозицию серверной части` | Исследовать серверную SDD и код, затем создать или обновить `DEV-BE-*`. | `returns/development-tasks/*` |
| `подготовь декомпозицию клиентской части` | Исследовать клиентскую SDD и код, затем создать или обновить `DEV-FE-*`. | `returns/development-tasks/*` |
| `проверь декомпозицию` | Проверить карточки, зависимости, размеры и полное распределение `REQ-*`, `SCN-*`, `IMP-*`. | `returns/development-tasks/index.md`, decomposition receipt |
| `декомпозиция подтверждена разработкой` | Создать неизменяемый снимок для аналитика без ожидания ответа. | `returns/decomposition-snapshots/*`, `handoff.json` |
| `подготовь список для Jira` | Показать подтверждённые карточки; оценки и ключи Jira необязательны. | confirmed decomposition snapshot |
| `свяжи DEV-* с <ключ Jira>` | Добавить необязательную внешнюю связь, не меняя технический объём карточки. | development task card, decomposition receipt |
| `возьми DEV-* в разработку` | Реализовать подтверждённую карточку и зафиксировать фактический результат независимо от других карточек. | code, tests, `returns/implementation-results/*` |
| `возьми срез <id> в тестирование` | Проверить срез, используя карточки и квитанции реализации как контекст. | `returns/test-results/*` |
| `подготовь проверки по срезу` | Start a `qa` run and draft QA checks and coverage for a ready slice. | `testing/test-plan.md`, run state |
| `собери негативные сценарии` | Add negative and edge scenarios tied to requirements. | `testing/test-plan.md` |
| `сверь проверки с требованиями` | Ensure every accepted check traces to requirements and assumptions are marked. | coverage matrix |
| `проверь прототип по срезу` | Compare slice prototype with requirements and checks; route gaps back to source artifacts. | prototype review notes, gaps |
| `проверь реализацию по срезу` | Compare delivered behavior with requirements, prototype and QA checks. | implementation review notes, verification gaps |
| `зафиксируй найденные пробелы` | Record gaps found during development/testing and route them to requirements, prototype, implementation plan or backlog. | updated gap list or source artifacts |

### Release Finalization

| User command | Meaning | Main artifacts |
|---|---|---|
| `собери релизный пакет` | Create/update release package from delivered feature artifacts. | `releases/<quarter>/<release-id>/*` |
| `зафиксируй итоговые требования` | Copy/normalize final delivered requirements into release package. | release final requirements |
| `промоуть в baseline` | Promote release outputs into `baseline/current`. | baseline/current, baseline/versions, release notes |
| `откати решение DEC-*` | Handle rollback according to pre/post-release status. | domain-impact, backlog, release/baseline as needed |
| `закрой consistency backlog` | Propagate or explicitly defer open consistency items for release. | `planning/consistency-backlog.md`, impacted artifacts |

## Validation Commands

| User command | Meaning |
|---|---|
| `проверь структуру` | Run structure validation if available. |
| `проверь ссылки` | Run markdown link validation if available. |
| `пересобери гант` | Run gantt sync script for the active quarter. |
| `проверь workflow` | Run structure, links and relevant generated-artifact checks. |
| `проверь harness doctor` | Run executable workflow, planning, context, trace and managed-file checks. |
| `проверь контекст` | Run context/research/handoff/test-plan validation if available. |
| `проверь профиль требований` | Проверить обязательную структуру, нормативные `REQ-*`, сценарии и трассировку нового профиля требований. |
