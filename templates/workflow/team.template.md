# Team Resources

This file is the project-local source of truth for planning resource lanes.

## Rules

- Keep canonical resource names stable: `A<N>`, `B<N>`, `F<N>`, `Q<N>`.
- The actual-progress generator reads this roster when laying out not-started execution tasks.
- If `Executor` is empty, `TBD_*`, or points to a non-roster lane, the generator may auto-assign a free resource from the matching role.
- Explicit roster lanes such as `B2` or `F1` are preserved, but not-started tasks are shifted if needed to avoid resource overload.
- Capacity is one full-time task per resource per open workday.
- Planning efficiency defaults are `AN=0.80`, `BE=0.70`, `FE=0.65`, `QA=0.80`.
- A personal coefficient is optional and overrides the role default only for planning.
- Closed intervals represent vacations or other periods when the resource cannot be scheduled.

## Roster

| Role | Resources | Canonical prefix | Accepted aliases |
|---|---|---|---|
| AN | A1, A2, A3 | A | A, AN, analyst, аналитик |
| BE | B1, B2, B3 | B | B, BE, back, backend, api, бэк, бек, бэкенд |
| FE | F1, F2 | F | F, FE, front, frontend, фронт, фронтенд, фронтендер |
| QA | Q1, Q2, Q3 | Q | Q, QA, test, testing, тест, тестирование, тестировщик |

## Personal Planning Parameters

| Resource | Personal coefficient | Closed intervals | Notes |
|---|---:|---|---|
| A1 |  |  | Use `YYYY-MM-DD..YYYY-MM-DD`, comma-separated |
