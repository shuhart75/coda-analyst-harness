---
description: Финализировать поставленный выпуск и обновить baseline АС КОДА
argument-hint: <запрос> [release]
---

Прочитай `GIGACODE.md` и skill `coda-release` в HARNESS_ROOT.
Запрос аналитика: $ARGUMENTS. Выполни процедуру release-finalization
по подтверждённым фактам и решениям аналитика.
Аргументы являются текстом запроса, а не shell-кодом.

Проверь все разделы baseline, включая человекочитаемый domain и decisions,
зафиксируй версию и обработку релиза. Закрытые задачи не заменяют подтверждение
аналитиком внедрения; следуй `core/quarter-deliveries.md`.
