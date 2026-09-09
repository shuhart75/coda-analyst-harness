# Actualization map

Feature: `features/<feature-slug>/feature.md`  
Quarter: `<YYYY-QN>`  
Baseline: `<commander-plan / absent / mixed>`

## Правила
- `Baseline State` независимо от `Actualization State` фиксирует наличие baseline каждой истории: `present` или `absent`. Отсутствующая колонка в старых картах означает `present`; пустая или неизвестная величина в новой колонке запрещена.
- При `present` нужны исходная дата `Baseline Start` и положительная целая длительность `Baseline Duration (дн)` в рабочих днях. Источник и подтверждение плана фиксируются в Notes; значение `present` само не утверждает план.
- При подтверждённом отсутствии baseline укажи `absent`, оставь обе baseline-ячейки пустыми и перечисли проверенные источники в Notes. Не копируй даты из actual-progress и не утверждай план задним числом ради генератора.
- Карта связей может быть создана без baseline: для `absent` обязателен подтверждённый состав задач. Состояние `materialized` описывает замещение задачами, а не завершение или утверждение плана; отсутствие baseline не требует `virtual`.
- Для `absent` генератор показывает задачи, но не полосу `PLAN` этой истории. Связи и вычисленный процент истории сохраняются в комментариях PlantUML, не превращаясь в плановые даты. Переход существующего утверждённого baseline в `absent` блокируется.
- `virtual` означает, что story ещё сама является текущей execution-единицей.
- `materialized` означает, что story полностью раскрыта в реальные implementation tasks.
- `mixed` означает, что часть story уже заменена real tasks, а остаток ещё виден как virtual residual.
- `Replaced By` заполняется по подтверждённому источнику связи story/task. Семантическая догадка LLM является предложением, а не подтверждённым составом истории; отсутствие источника требует одного вопроса аналитику.
- Ссылки содержат внутренние `Task ID`; реальный базовый ключ Jira раскрывается во все ролевые задачи этого ключа. Не используй один идентификатор одновременно как локальный ID и ключ другой задачи.
- Действующая формула генератора: `round(sum(max(estimate, 1.0) * progress) / sum(max(estimate, 1.0)))` по связанным задачам без `superseded`. Это взвешенное среднее, а не среднее процентов; Python `round` округляет точную половину к ближайшему чётному целому.
- `progress` берётся в процентных пунктах: `round((5*75 + 6*100 + 7*100)/(5+6+7)) = 93`. Не округляй долю `0.93055...` до целого перед переводом в проценты.
- QA связывается с историей только по подтверждённому составу. Если история покрывает только FE, перечисляй `<ключ>/FE`, а не базовый ключ, иначе в состав автоматически попадёт и `<ключ>/QA`.
- Для `mixed` учитывается также `Residual Virtual Tasks`; для `virtual` без прямых ссылок используются `Related Stories` из реестра. Без связанных задач прогресс равен 0. Формула не доказывает состав конкретной истории.
- `Residual Virtual Tasks` заполняется только для `mixed`.
- `Depends On` используем, если story без замещения должна в actual-progress стартовать после завершения другой story по логике commander-plan.
- Planning stories являются ролевыми: не более одной `AN`, `BE`, `FE`, `QA` на фичу.
- После утверждения baseline start/duration неизменяемы; менять можно только mapping на task candidates и actual tasks.
- Task candidates, обнаруженные после утверждения плана, показывают новый scope на actual-progress, не переписывая quarter/commander plan.
- Не используем визуальные PlantUML-зависимости как source of truth для actual-progress; связи story/task фиксируются в этой таблице и в `tasks.md`.
- Сдвиг не начатых execution tasks относительно текущей даты выполняет генератор actual-progress. Плановая baseline-дата story остаётся видимой как слой `PLAN ...`.

## Mapping

| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On | Baseline State |
|---|---|---|---:|---|---|---|---|---|---|
| STORY-<FEATURE>-BE | <backend role outcome> | <YYYY-MM-DD> | <N> | virtual | explicit |  |  | STORY-<FEATURE>-AN | present |

Для истории с реальными задачами, но без baseline, вместо строки выше:

`| STORY-<FEATURE>-FE | <подтверждённая история> | | | materialized | explicit | <ключ>/FE | | | absent |`

## Notes

Укажи точные пути и фрагменты источников состава историй, baseline либо его отсутствия.
