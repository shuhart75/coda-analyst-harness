# Устаревшие шаблоны передачи

Эти файлы сохранены только для проверки и чтения ранее созданных пакетов `features/*/handoffs/*`.

Новые передачи через них не формируются. Активный процесс использует:

- `templates/requirements/feature-requirements.readable.template.md`;
- `templates/exchange/`;
- `scripts/requirements-exchange.py`.

LLM не должна запускать `handoffctl.py init-feature`, `add-revision` или `publish` для новых требований.
