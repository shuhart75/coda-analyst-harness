# Actualization map

Feature: `features/<feature-slug>/feature.md`  
Quarter: `<YYYY-QN>`  
Baseline: `commander-plan`

## Правила
- `virtual` означает, что story ещё сама является текущей execution-единицей.
- `materialized` означает, что story полностью раскрыта в реальные implementation tasks.
- `mixed` означает, что часть story уже заменена real tasks, а остаток ещё виден как virtual residual.
- `Replaced By` заполняется по подтверждённому источнику связи story/task. Семантическая догадка LLM является предложением, а не подтверждённым составом истории; отсутствие источника требует одного вопроса аналитику.
- Ссылки содержат внутренние `Task ID`; реальный базовый ключ Jira раскрывается во все ролевые задачи этого ключа. Не используй один идентификатор одновременно как локальный ID и ключ другой задачи.
- Действующая формула генератора: `round(sum(max(estimate, 1.0) * progress) / sum(max(estimate, 1.0)))` по связанным задачам без `superseded`. Это взвешенное среднее, а не среднее процентов; Python `round` округляет точную половину к ближайшему чётному целому.
- Для `mixed` учитывается также `Residual Virtual Tasks`; для `virtual` без прямых ссылок используются `Related Stories` из реестра. Без связанных задач прогресс равен 0. Формула не доказывает состав конкретной истории.
- `Residual Virtual Tasks` заполняется только для `mixed`.
- `Depends On` используем, если story без замещения должна в actual-progress стартовать после завершения другой story по логике commander-plan.
- Planning stories являются ролевыми: не более одной `AN`, `BE`, `FE`, `QA` на фичу.
- После утверждения baseline start/duration неизменяемы; менять можно только mapping на task candidates и actual tasks.
- Task candidates, обнаруженные после утверждения плана, показывают новый scope на actual-progress, не переписывая quarter/commander plan.
- Не используем визуальные PlantUML-зависимости как source of truth для actual-progress; связи story/task фиксируются в этой таблице и в `tasks.md`.
- Сдвиг не начатых execution tasks относительно текущей даты выполняет генератор actual-progress. Плановая baseline-дата story остаётся видимой как слой `PLAN ...`.

## Mapping

| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |
|---|---|---|---:|---|---|---|---|---|
| STORY-<FEATURE>-BE | <backend role outcome> | <YYYY-MM-DD> | <N> | virtual | explicit |  |  | STORY-<FEATURE>-AN |
