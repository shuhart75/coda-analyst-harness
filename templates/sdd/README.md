# Производный вход OpenSpec

Используй эти шаблоны по `core/sdd-input.md`. Они не являются готовым входом:
замени все заполнители проверенными данными, SHA-256 и идентификаторами.
Сначала requirements, затем производные proposal/spec. Не копируй placeholder
в immutable revision и не создавай design/tasks на аналитической стороне.

- `package.template.json` → `<delivery-root>/sdd/package.json`.
- `openspec.template.yaml` → `sdd/<contour>/<change-id>/.openspec.yaml`.
- `proposal.template.md` → `sdd/<contour>/<change-id>/proposal.md`.
- `spec.template.md` → `sdd/<contour>/<change-id>/specs/<capability>/spec.md`.

Пример описывает новую capability. Для MODIFIED возьми полный существующий
requirement block, для REMOVED/RENAMED следуй точному синтаксису в `core/sdd-input.md`.
Локальные правила получателя могут требовать дополнительные сведения в proposal.
Незатронутый контур не создаётся. Новый профиль не заменяет требования,
а закрепляет совместное покрытие REQ и сценариев в одном пакете.
