"""Historical packet generation for compatibility tests, never a production route.

The production CLI publishes only to documents. Legacy lifecycle tests use the
old prepare orchestration with the live validators/readers/transport primitives.
New transport tests invoke scripts/requirements-exchange.py directly.
"""
from pathlib import Path
import sys
_original_name = __name__
_source = Path(__file__).resolve().parents[1] / 'scripts/requirements-exchange.py'
sys.path.insert(0, str(_source.parent))
__file__ = str(_source)
__name__ = 'historical_packet_fixture'
exec(compile(_source.read_text(), str(_source), 'exec'), globals())
__name__ = _original_name
LEGACY_PREPARE = r'''
def prepare_command(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    validate_feature(args.feature)
    require_exchange_delivery_binding(project, args.feature)
    delivery_analysis_task(project, args.feature)
    feature_root = layout_feature_root(project, args.feature)
    requirements = feature_root / "requirements.md"
    if not requirements.is_file():
        raise ValueError(f"Требования не найдены: {requirements}")
    requirements_bytes = requirements.read_bytes()
    approved_hash = require_confirmed_audit(project, args.feature, requirements)
    if hashlib.sha256(requirements_bytes).hexdigest() != approved_hash:
        raise ValueError("Требования изменились во время проверки аудита")
    requirements_text = requirements_bytes.decode("utf-8")
    errors = validate_requirements_text(requirements_text)
    if errors:
        raise ValueError("; ".join(errors))
    run_requirement_guards(project, args.feature)
    if sha256(requirements) != approved_hash:
        raise ValueError("Требования изменились после проверки; повтори аудит")
    analyst = current_analyst(args.analyst, project)
    if not analyst:
        raise ValueError("Не задан идентификатор аналитика: используй --analyst или CODA_ANALYST_ID")
    code_root = resolve_code_root(project, args.code_root)
    state = load_json(feature_root / "requirements-state.json")
    sdd_audit.snapshot(feature_root, state, code_root, verify_source=True)
    pending = [item for item in stages.registry(state)["revisions"] if not item["publication_confirmed"]]
    # Cancelled history still forbids fallback to another destination; it is restored only in code.
    reason = "Репозиторий роли code отсутствует или не настроен"
    if code_root is not None:
        try:
            result = publish_to_code(
                project, code_root, args.feature, requirements_bytes, requirements_text, analyst,
                getattr(args, "target_branch", None),
            )
            record_prepared(project, args.feature, result)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        except CodeDestinationUnavailable as exc:
            if pending:
                raise ValueError("Незавершённая передача в code блокирует резервное размещение; сначала проверь её PR/MR") from exc
            reason = str(exc)
    elif pending:
        raise ValueError("Незавершённая передача в code блокирует резервное размещение; сначала проверь её PR/MR")
    exchange = project / EXCHANGE_DIR
    exchange.mkdir(parents=True, exist_ok=True)
    prepared = prepare_in_exchange(
        exchange, project, args.feature, requirements_bytes, requirements_text, analyst
    )
    result = {
        **prepared,
        "delivery_binding": exchange_binding(project, args.feature),
        "destination_role": "analytics",
        "publication_confirmed": True,
        "exchange_root": str(exchange),
        "manifest": str(prepared["manifest_path"]),
        "requirements": str(prepared["requirements_path"]),
        "selection_reason": reason,
        "message": (
            f"Редакция {prepared['revision']:03d} размещена в резервном каталоге роли analytics: "
            f"{exchange}. Причина: {reason}"
        ),
    }
    result.pop("manifest_path", None)
    result.pop("requirements_path", None)
    record_prepared(project, args.feature, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

'''
exec(LEGACY_PREPARE, globals())
if __name__ == '__main__':
    raise SystemExit(main())
