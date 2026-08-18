# Оценки planning stories

Feature: `features/<feature-slug>/feature.md`  
Квартал: `<YYYY-QN>`

На одну фичу допускается не более одной planning story каждой роли: `AN`, `BE`, `FE`, `QA`.
Отсутствующая роль не создаёт пустую story.

| Story ID | Role | Summary | Analyst anchor effort, дн | Team effort, дн | Agreed effort, дн | Max parallelism | Efficiency | Depends On | Not before | Notes |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|
| STORY-<FEATURE>-AN | AN | Аналитическая проработка фичи |  |  |  | 1 | 0.80 |  |  |  |
| STORY-<FEATURE>-BE | BE | Backend-реализация фичи |  |  |  | 1 | 0.70 | STORY-<FEATURE>-AN |  |  |
| STORY-<FEATURE>-FE | FE | Frontend-реализация фичи |  |  |  | 1 | 0.65 | STORY-<FEATURE>-AN, STORY-<FEATURE>-BE + 3 open days |  |  |
| STORY-<FEATURE>-QA | QA | Проверка фичи |  |  |  | 1 | 0.80 | STORY-<FEATURE>-BE, STORY-<FEATURE>-FE |  |  |

## Duration Formula

`ceil(agreed effort / min(available resources, max parallelism) / efficiency)`

The final agreed effort is explicitly approved. It is never calculated by averaging analyst and team estimates.
