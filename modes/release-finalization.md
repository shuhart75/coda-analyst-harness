# Mode: release-finalization

Для проекта с `delivery-index.json` применяй `core/quarter-deliveries.md`:
legacy-пути `features/<feature>/...` означают корень выбранной поставки,
`planning/<quarter>/...` — корень квартала; разрешай их через
`scripts/project_layout.py`. Корневой каталог фич содержит человекочитаемые паспорта и сводные требования
по core/feature-passports.md. Передаваемый контракт остаётся в выбранной поставке. Примеры ниже сохраняются для legacy-проектов.

## Goal

После подтверждённого внедрения разобрать результаты релиза, обновить baseline
с фактической доменной моделью, сохранить архив завершённых передач и довести
очистку рабочего обмена до подтверждённого принятия.

## Триггер и полнота baseline

После каждой актуализации задач рассматриваются полные закрытые релизы, ещё не
отражённые в baseline, по `core/quarter-deliveries.md`. Закрытие всех задач
запускает предложение аналитику, но не доказывает внедрение. Подтверждение
аналитика связывается с точной версией, средой и составом. Подготовленный кандидат,
проверка всех разделов и отметка обработки хранятся через `baseline_releases.py`.

Проверяй влияние каждого релиза на `domain`, `requirements`, `ui`, `api`, `data`
и `decisions`. Неизменившиеся разделы сохраняют содержание; версия baseline
обновляется для каждого подтверждённого релиза. Домен остаётся связным
человекочитаемым объяснением сущностей, отношений, правил, ЖЦ и процессов,
с диаграммами внутри соответствующих разделов. История решений не удаляется:
заменённые решения связаны с заменяющими. Обновление документации не является
новым бизнес-утверждением отклонений, уже попавших в ПРОМ.

## Проверка кандидата и завершение обмена

До `prepare` подготовь review с точными `scope_hash` состава релиза, `base_hash`
текущего baseline, `sections` с хешами `domain`, `requirements`, `ui`, `api`,
`data`, `decisions` и непустым `consistency_evidence`. Обязательный `domain_review`
содержит ровно шесть аспектов: `entities`, `relations`, `rules`, `lifecycles`,
`processes`, `diagrams`. Для каждого укажи `status: updated` либо `unchanged`
и непустое `evidence`: что проверено, что изменилось или почему актуализация
не нужна. Все `unchanged` при изменившемся дереве домена недопустимы.
Кандидат, его исходный baseline и review должны оставаться неизменными до
подтверждённого продвижения. Доказательство внедрения и подробный разбор
возвратов предшествуют переносу фактов в baseline.

После `promote` заверши обмен по `core/quarter-deliveries.md` и
`core/developer-handoff.md`:

1. Подготовь полный review обмена с тем же `scope_hash`: `deliveries` связывает
   `delivery_key`, `task_ids`, `disposition` и `evidence`; `other_tasks` объясняет
   каждую оставшуюся задачу через `task_id` и `reason`. Не ограничивай проверку
   только задачами BE/FE или пакетами, которые удалось прочитать.
2. Для `complete` проверь закрытие этапов, все редакции и все фактические возвраты,
   включая новые файлы и изменения старых. Зафиксируй `packet_hash` точного дерева
   исходного пакета, рассчитанный `baseline_releases.tree_hash`. Он связывает
   решение с разобранными байтами; хеш одного summary недостаточен. `partial`
   сохраняет рабочий пакет; `no-transfer` требует отсутствия опубликованной передачи.
3. Вызови `review-exchange`: он сохраняет побайтный пакет в
   `releases/<release>/exchange-archive/<delivery-key>`, корневой договор в
   `exchange-contracts/<delivery-key>` того же релиза и запись в
   `exchange-archive-index.json`. Не переименовывай редакции и `return_id`.
4. Сохрани и передай на обычное человеческое принятие baseline, архив и реестры
   в аналитике. `cleanup-exchange` должен проверить их в свежем `analytics/main`,
   включая содержимое и связь со снимком baseline. Локальное наличие архива
   не разрешает удалять рабочий обмен.
5. Для `code` очистка создаёт отдельную review-ветку только с удалением точного
   пакета. Человек принимает её без squash/rebase. Для `analytics` очистка
   готовит локальное удаление в рабочей ветке; сохрани его и дождись принятия
   в `main`. В обоих случаях повтори `cleanup-exchange` для проверки принятия;
   до этого сохраняется `cleanup-pending`, а локальное отсутствие пакета не
   доказывает завершение. Обычный кодовый checkout не изменяется.
6. Проверь `scan`. Недоступность, новая редакция или новый возврат, ошибка push
   и непринятый merge оставляют очистку незавершённой. Продвинутый baseline
   не откатывается и повторно не продвигается ради восстановления очистки.

Внутренние вызовы обвязки; значения берутся из проверенного проекта и review,
аналитику не требуется вводить команды или заполнять JSON:

```text
python3 scripts/baselinectl.py --project <PROJECT_ROOT> prepare --release <release> --candidate <candidate-dir> --review <baseline-review.json>
python3 scripts/baselinectl.py --project <PROJECT_ROOT> promote --release <release> --scope-hash <scope-hash> --deployment <deployment.json> --analyst-confirmed
python3 scripts/baselinectl.py --project <PROJECT_ROOT> review-exchange --release <release> --review <exchange-review.json>
python3 scripts/baselinectl.py --project <PROJECT_ROOT> cleanup-exchange --release <release> --delivery <delivery-key>
python3 scripts/baselinectl.py --project <PROJECT_ROOT> scan
```

`--analyst-confirmed` используется только для фактического явного подтверждения.
`scripts/release_exchange.py` проверяет архив и его принятие;
`scripts/exchange_cleanup.py` публикует ограниченное удаление в code. Ручная
правка их состояния, удаление через shell и обход человеческого merge запрещены.

## Main artifacts

- `releases/*/README.md`
- `releases/*/*/release.md`
- `releases/*/*/final-requirements/*`
- `releases/*/*/final-domain-delta.md`
- `releases/*/*/final-ui-delta.md`
- `releases/*/*/final-api-delta.md`
- `releases/*/*/promotion-checklist.md`
- `releases/*/*/promoted-baseline-version.md`
- `releases/baseline-status.json`
- `releases/*/exchange-archive/**`
- `releases/*/exchange-contracts/**`
- `releases/*/exchange-archive-index.json`
- `baseline/current/**`
- `baseline/versions/**`
- `features/*/domain-impact.md`
- `features/*/context-summary.md` and `features/*/artifact-map.md` when needed for release traceability

## Allowed changes

- release package contents
- final requirements after delivery
- baseline promotion notes and version metadata
- canonical requirements, human-readable domain model, lifecycles, processes, diagrams, API, UI, data-model and decision baseline files
- baseline snapshots under `baseline/versions/`
- feature deployment status notes tied to promotion
- архивы обмена с привязкой к релизу, их реестры и состояние очистки
- удаление точного архивированного пакета analytics после принятия baseline и архива;
  очистка code только зарегистрированной операцией через изолированную review-ветку


## Consistency gate

`baseline/current/` describes the deployed system. A development report alone
does not prove deployment. Before promotion, identify the deployed version,
environment and evidence, record the release and review the analyst's decisions
from `features/<feature>/development-results-state.json`.

Promote only the deployed behavior. Preserve immutable input requirements and
returns. If deployed behavior differs from the requested behavior, describe that
fact and the known limitation without implying business acceptance; link the
analyst's acceptance or rejection and any follow-up. Undeployed code remains in
release preparation. A rejected deployed change may require rollback or repair,
but must not be hidden from the description of the current system.

Before promoting a release into `baseline/current/`:
- review every included feature's `domain-impact.md`;
- review `documents/planning/consistency-backlog.md`;
- block promotion if there are unresolved `domain-wide` items that affect released scope;
- either propagate or explicitly defer cross-feature items;
- write rollback notes for decisions that supersede or revert previous released decisions.
- check auxiliary `.research/`, exchange returns, implementation-plan and test-plan artifacts for findings that were accepted but not transferred into final requirements, release notes or baseline;
- update context summaries or mark them obsolete after baseline promotion when they would otherwise point at pre-release state.

## Forbidden without mode switch

- changing quarter or commander planning baselines silently
- changing historical execution facts without explicit instruction
- rewriting raw source materials under `context/source-materials/` or imported legacy folders

## Паспорт долгоживущей фичи

Следуй `core/feature-passports.md`. После изменения поставки или подтверждённых
фактов обновляй паспорт соответствующей фичи с датой, содержанием и основанием.
При актуализации исполнения меняй только сводку состояния README и ссылки поставок;
требования паспорта и контракт поставки требуют своего режима работы.
