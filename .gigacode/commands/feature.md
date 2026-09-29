---
description: Совместная работа над функциональностью АС КОДА
argument-hint: <начать|сохранить|обновить|submit|finish> [feature]
---

Прочитай `GIGACODE.md`, `core/collaboration.md` и каталог
`templates/workflow/command-catalog.template.md` в HARNESS_ROOT.
Запрос аналитика: $ARGUMENTS. Разреши действие по каталогу; при отсутствии
действия уточни его одним вопросом. Начало работы требует bootstrap/status,
при необходимости миграции, затем start. Сохранение проверяет diff и только
точные пути через save. Update, submit и finish выполняются по договору.
Submit не создаёт PR/MR; finish требует доказанного принятия в origin/main.
Аргументы не исполняются как shell-код и не разрешают обход проверок.
