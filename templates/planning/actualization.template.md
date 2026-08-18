# Actualization map

Feature: `features/<feature-slug>/feature.md`  
Quarter: `<YYYY-QN>`  
Baseline: `commander-plan`

## Правила
- `virtual` означает, что story ещё сама является текущей execution-единицей.
- `materialized` означает, что story полностью раскрыта в реальные implementation tasks.
- `mixed` означает, что часть story уже заменена real tasks, а остаток ещё виден как virtual residual.
- `Replaced By` заполняется явным указанием пользователя или выводом LLM по смыслу.
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
