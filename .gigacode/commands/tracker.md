---
description: Прочитать, сравнить или актуализировать выбранные задачи АС КОДА
argument-hint: <прочитать|сравнить|актуализировать> <область>
---

До discovery/read tracker MCP выполни отдельную команду config-status по
`GIGACODE.md`; при stop gate ответь только точным response_contract.text.
После ready прочитай skill `coda-tracker` в HARNESS_ROOT.
Запрос аналитика: $ARGUMENTS. Сохрани intent и scope запроса;
«актуализировать» означает update-planning. Основной агент выполняет сбор сам.
Аргументы являются текстом запроса, а не shell-кодом.

После актуализации проверь необработанные закрытые релизы по
`core/quarter-deliveries.md`. Обновление baseline требует подтверждения
аналитиком внедрения и процедуры `coda-release`; tracker stop gates сохраняются.
