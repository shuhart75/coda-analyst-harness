# Implementation tasks

Feature: `../../feature.md`  
Slice: `../slice.md`  
Дата обновления: `<YYYY-MM-DD>`

## Правила
- Summary по возможности совпадает с Jira summary.
- Этот файл является source of truth для execution-данных по данному slice.
- Фактические даты и исполнитель обновляются по мере работы.
- `Kind` показывает, является ли execution item реальной Jira-задачей (`real`) или временной виртуальной задачей (`virtual`).
- `Progress %` нужен для информативного actual-progress gantt.
- `Role` хранит семантическую роль: `AN`, `BE`, `FE`, `QA`.
- `Executor` хранит ресурсную дорожку из `.workflow/team.md`. По умолчанию: `A1-A3`, `B1-B3`, `F1-F2`, `Q1-Q3`.
- Если роль известна, а конкретный ресурс нет, используем `TBD_A`, `TBD_B`, `TBD_F`, `TBD_Q`.
- Допустимые алиасы для ввода нормализуются при генерации: `AN/A/analyst/аналитик`, `BE/B/back/backend/api/бэк/бек/бэкенд`, `FE/F/front/frontend/фронт/фронтенд/фронтендер`, `QA/Q/test/testing/тест/тестирование/тестировщик`.
- Задачи без фактических дат и с `Progress % = 0` считаются не начатыми: при каждой генерации actual-progress они не рисуются в прошлом, даже если `Planned Start` устарел.
- Для будущей работы внутри одной feature сначала ставим backend/API, frontend планируем не раньше чем через 3 рабочих дня после старта backend/API.
- Если `Executor` пустой, `TBD_*` или указывает на ресурс вне roster, actual-progress generator назначит свободный ресурс по `Role`, префиксу задачи или summary.
- При раскладке не начатых задач генератор не допускает загрузку ресурса выше 100% в один рабочий день и старается использовать доступные ресурсы до сдвига сроков.

## Реестр задач

| Jira | Summary | Kind | Role | Estimate (дн) | Executor | Planned Start | Planned Finish | Actual Start | Actual Finish | Status | Progress % | Related Stories | Details |
|---|---|---|---|---:|---|---|---|---|---|---|---:|---|---|
| BE-XXX-001 | <backend/api task> | virtual | BE | 3 | B1 | <YYYY-MM-DD> | <YYYY-MM-DD> |  |  | planned | 0 | STORY-XXX-001 |  |
| FE-XXX-001 | <frontend task> | virtual | FE | 3 | F1 | <YYYY-MM-DD> | <YYYY-MM-DD> |  |  | planned | 0 | STORY-XXX-001 |  |

## Notes
