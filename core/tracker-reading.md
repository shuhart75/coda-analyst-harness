# Чтение, сверка и применение данных трекеров

## Назначение

`targeted-tracker-v2` разделяет работу на независимые контексты:

1. координатор определяет область и создаёт run;
2. изолированный сборщик читает один провайдер;
3. отдельные history-сборщики читают ограниченные пакеты ключей;
4. `trackerctl.py` детерминированно склеивает результаты;
5. основной агент читает только `reconciled.json` и применяет его к планам лишь
   при явном intent `update-planning`.

Команда «прочитай» всегда имеет intent `read-only` и не разрешает менять задачи,
требования, планы, Gantt или `actual-progress`. Команда «актуализируй задачи/Гант»
имеет intent `update-planning`, но применение разрешено только после успешной
сверки и только в артефактах режима исполнения.

## Стоп-барьер

До MCP и делегирования координатор выполняет отдельно:

```bash
python3 scripts/trackerctl.py config-status
```

Команду нельзя передавать в `head`, `tail`, `grep`, `jq` или иной pipeline: код
выхода является частью fail-closed договора.

Код `3` и `must_stop: true` разрешают только дословно задать
`response_contract.text`. Не выполнять discovery, поиск, bootstrap, делегирование
или предварительную сводку.

## Область

Run получает один эпик либо явный набор ключей и явно известный исходный трекер.
Jira и SberTrek используют одинаковую форму ключа, поэтому провайдер не выводится
из `PROJECT-123`. При неоднозначности задать один вопрос.

Примеры:

```bash
python3 scripts/trackerctl.py begin \
  --scope-kind epic --scope-provider sbertrek \
  --scope-id RSCON-6607 --label 'Когорты' \
  --scope-source 'Ответ аналитика' --intent read-only

python3 scripts/trackerctl.py begin \
  --scope-kind tasks --scope-provider jira \
  --scope-id RSCON-2905 --scope-id RSCON-2906 \
  --label 'Когорты' --scope-source 'features/cohorts/actual-progress.md' \
  --intent update-planning
```

`begin` создаёт `run_id` и первый job. Координатор не выполняет напечатанный
TQL/JQL сам. Он получает готовый prompt:

```bash
python3 scripts/trackerctl.py collector-brief --run-id <run-id>
```

и создаёт отдельного субагента с этим prompt. Если runtime не поддерживает
субагентов, операция блокируется: нельзя возвращаться к чтению в основном
контексте.

## Collection jobs

Job хранится в `jobs/collection-<provider>.json` и содержит точный запрос и его
SHA-256. `collector-brief` передаёт субагенту сам точный запрос в первой короткой
команде без названия фичи, смысла эпика, исходного пользовательского текста и
иного аналитического контекста. Сборщик следует только этой команде,
`core/tracker-collector.md` и job-файлу. Он не читает общие правила проекта или
документацию MCP.

Обвязка строит только следующие запросы:

```text
SberTrek epic:
unit IN linkedUnitsOf("unit = 'RSCON-6607'", "Состоит из")

SberTrek tasks:
unit = "RSCON-6848" or unit = "RSCON-6849"

SberTrek by Jira counterparts:
issue_key = "RSCON-2905" or issue_key = "RSCON-2906"

Jira tasks:
key IN ("RSCON-2905", "RSCON-2906")

Jira epic:
parent = "RSCON-2911"
```

Для Jira-эпика fallback `"Epic Link" = "..."` разрешён только после
зарегистрированной ошибки исходного `parent`-запроса.

Поисковый ответ нормализуется без detail-вызовов каждой задачи. Для SberTrek
сборщик использует только `issue.exportJson` либо эквивалентную bulk JSON export
операцию: точный TQL передаётся в параметре `query`, а имя MCP-сервера может
отличаться. `issue.search.text`, `issue.getByKey` и `link.list` запрещены и не
являются проверкой поддержки TQL. Сборщик не читает отображаемый preview: большой
ответ может быть показан только частично, хотя MCP вернул все элементы. Если
инструмент поддерживает проекцию, он запрашивает только поля из job и никогда не
использует `fields=null`. Полный JSON-файл ответа передаётся в
`trackerctl.py ingest-query-response`; программа
структурно находит массив задач, вычисляет SHA-256, считает карточки и атомарно
регистрирует evidence, страницу и компактные записи. Ручные `mcp-log`,
`query-page` и `record-issue` не могут завершить SberTrek collection-job.

Provider-файл хранит только ключи, название, тип, статус, исполнителя, оценку,
эпик, релизы, даты создания/обновления, состояния `value/absent/not-returned`,
evidence, число элементов и SHA-256 исходной страницы. При этом описания, комментарии, вложения
и полный MCP-ответ в tracker-run не сохраняются. Отсутствие полного
JSON-файла, неподдержанная схема, пустые `pages`, несовпадение машинного числа
карточек или карточка без evidence блокируют `collector-complete` и `reconcile`.
Для SberTrek наличие `issue_key` фиксируется отдельно: `absent` является
нормальным отсутствием связи, а `not-returned` означает неполное чтение и
блокирует переход к Jira и reconciliation.

После `collector-complete` субагент обязан остановиться. Команда сама создаёт
counterpart-job либо history-jobs; запускать их должен координатор в новых
контекстах.

Если collection-сборщик вернул ошибку или не нашёл требуемую export-операцию,
координатор может прочитать только `run-status`, после чего обязан остановиться и
сообщить блокировку. Он не вызывает MCP сам, не проверяет `getByKey`/`link.list` и
не ищет альтернативный способ собрать карточки.

## History jobs

История разбивается на job не более восьми ключей. Каждый job относится к одному
провайдеру. Сборщик делает ровно один history-вызов на ключ и сохраняет только
события `assignee` и `status`.

По истории вычисляются:

- `assigned_at` — первое возвращённое назначение на исполнителя;
- `work_started_at` — первое возвращённое назначение участнику с ролью developer;
- завершение по передаче — последнее доказанное назначение developer → известная
  не-developer роль, если явный статус не означает завершение или исключение.

Отсутствующая или недоступная история даёт `null` и явное limitation; дата не
угадывается по `updated_at`.

## Сопоставление и склейка

Пары образуются только так:

```text
SberTrek.issue_key == Jira.key
```

Для каждого поля заполненный SberTrek имеет приоритет. Jira заполняет пропуск.
Два разных заполненных значения сохраняют SberTrek и создают discrepancy.
Эпик и релиз остаются независимыми группировками. `1 SP = 1 человекодень`, другие
единицы молча не преобразуются.

Неизвестные account id запрашиваются по одному во время `reconcile`. Аналитик
указывает `AN1`, `BE2`, `FE1`, `QA1`; алиасы `A/B/F/Q` нормализуются.

## Координатор

Последовательность основного агента:

```text
config-status
-> begin
-> collector-brief -> isolated collection agent -> run-status
-> collector-brief -> isolated counterpart agent -> run-status
-> collector-brief -> isolated history agent(s) -> run-status
-> reconcile
-> result-status
```

Координатор не вызывает tracker MCP, не читает provider-файлы и не пересказывает
ответы субагентов. Между job он читает только `run-status`. Итог разрешён только
при одновременном выполнении:

```json
{
  "status": "tracker-read-reconciled",
  "workflow_complete": true,
  "final_response_allowed": true
}
```

Для изменения Ганта дополнительно требуется:

```json
{"planning_application_allowed": true}
```

При `false` допускается только отчёт без изменений. Даже при `true` основной
агент сначала читает компактный `reconciled.json`, проверяет предлагаемые новые
эпики/релизы и соблюдает режим, ветку и правила сохранения аналитического проекта.

## Артефакты

Успешный run создаёт только в игнорируемом каталоге:

```text
.workspace-state/tracker-runs/<run-id>/
├── scope.json
├── tracker-session-log.md
├── run-status.json
├── jobs/
├── providers/
│   ├── sbertrek.json
│   └── jira.json
├── reconciled.json
├── report.md
└── completion-status.json
```

Старые незавершённые run `targeted-tracker-v1` не продолжаются. Начать новый run.
