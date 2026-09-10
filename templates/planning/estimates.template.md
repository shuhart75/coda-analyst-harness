# Оценки planning stories

Feature: `features/<feature-slug>/feature.md`  
Квартал: `<YYYY-QN>`

На одну фичу допускается не более одной planning story каждой роли: `AN`, `BE`, `FE`, `QA`.
Отсутствующая роль не создаёт пустую story.

| Story ID | Role | Summary | Analyst anchor effort, дн | Team effort, дн | Agreed effort, дн | Max parallelism | Efficiency | Depends On | Not before | Notes | Estimate Unit |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---|
| STORY-<FEATURE>-AN | AN | Аналитическая проработка фичи |  |  |  | 1 | 1 |  |  |  | team-days |
| STORY-<FEATURE>-BE | BE | Backend-реализация фичи |  |  |  | 1 | 1 | STORY-<FEATURE>-AN |  |  | team-days |
| STORY-<FEATURE>-FE | FE | Frontend-реализация фичи |  |  |  | 1 | 1 | STORY-<FEATURE>-AN, STORY-<FEATURE>-BE + 3 open days |  |  | team-days |
| STORY-<FEATURE>-QA | QA | Проверка фичи |  |  |  | 1 | 1 | STORY-<FEATURE>-BE, STORY-<FEATURE>-FE |  |  | team-days |

## Duration Formula

Командо-дни (`team-days`) уже учитывают распараллеливание и производительность:
`ceil(agreed effort)`. Командирский план: `ceil(agreed effort * (1 + buffer / 100))`.
`Max parallelism` задаёт подтверждённый состав команды для резервирования ресурсов,
но не делитель длительности. `Efficiency` для `team-days` не применяется повторно.

Для явных legacy-оценок `person-days` сохраняется прежняя формула
`ceil(agreed effort / effective parallel capacity)`. Старая таблица без
`Estimate Unit` не переинтерпретируется автоматически: валидатор предупреждает,
генерация нового плана требует явной единицы. Актуализация утверждённого плана
использует сохранённую длительность и не зависит от этой миграции оценок.

The final agreed effort is explicitly approved. It is never calculated by averaging analyst and team estimates.
