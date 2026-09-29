---
name: coda-tracker
description: Чтение, явное сравнение и актуализация задач, релизов или квартала АС КОДА из выбранного трекера, с сохранением истории и проверками применения.
---

# Трекеры и фактическое исполнение

**RULE:** Первое действие перед tracker MCP discovery/read: отдельная команда
`python3 scripts/trackerctl.py config-status`. При stop gate выдай только
точный `response_contract.text`. Не продолжай discovery, чтение или запись
до сохранённого ответа и ready gate. Работу с трекером выполняет основной агент.

**DOCS:** После ready прочитай `core/tracker-adaptive.md`, для релиза также
`core/tracker-release.md`; для применения `core/tracker-actualization.md`
и `modes/execution-update.md`. После потери контекста используй `resume`
с текущими scope и intent.

**RULE:** Подтверди ключи и провайдер. Чтение имеет intent `read-only`,
«актуализируй» означает `update-planning`. Новый run использует `begin --adaptive`.
Второй трекер подключается только по явному сравнению. Следуй next_action,
сохраняй полные ответы дословно, проверяй историю до применения дат и состояний.

**RULE:** До любой записи PROJECT_ROOT нужны рабочая ветка, подтверждённая
область, `application-preflight`, актуальные history-review и QA-проверки.
Перед сохранением выполни `save-preview`; генерация только `--actual-only`
с проверкой diff и Confluence. Read-only ничего не записывает в аналитику.

**PROHIBITED:** Не заменяй незавершённый run, не придумывай даты, роли и QA,
не меняй утверждённые планы и не генерируй после ошибки QA-проверки.
