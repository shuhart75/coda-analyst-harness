"""Quarter delivery layout; legacy projects remain readable without migration.

The registry owns identity and paths. No symlinks or duplicate authored files.
Old feature slugs remain delivery lookup aliases, never new business identities.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

REGISTRY = "delivery-index.json"


def layout(project: Path) -> dict | None:
    path = project / REGISTRY
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported delivery layout; update the harness")
    return value


def safe_path(project: Path, relative: str) -> Path:
    path = project / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or path.resolve() != path.absolute():
        raise ValueError(f"Unsafe or linked delivery path: {relative}")
    return path


def feature_root(project: Path, key: str, quarter: str | None = None) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
        raise ValueError("Invalid feature/delivery identifier")
    index = layout(project)
    if index is None:
        return project / "features" / key
    deliveries = index["deliveries"]
    if quarter:
        matches = [d for d in deliveries.values() if d["feature_id"] == key and d.get("quarter") == quarter]
        if len(matches) > 1:
            raise ValueError("Several deliveries in this quarter; select an explicit delivery id")
        if len(matches) == 1:
            return safe_path(project, matches[0]["path"])
    if key in deliveries:
        return safe_path(project, deliveries[key]["path"])
    matches = [d for d in deliveries.values() if d["feature_id"] == key]
    if len(matches) != 1:
        raise ValueError(f"Select a delivery for feature: {key}")
    return safe_path(project, matches[0]["path"])


def quarter_root(project: Path, quarter: str) -> Path:
    if not re.fullmatch(r"\d{4}-Q[1-4]", quarter):
        raise ValueError("Invalid quarter")
    return project / ("quarters" if layout(project) else "planning") / quarter


def delivery_selection(project: Path, keys: list[str] | None = None, quarter: str | None = None) -> dict[str, dict]:
    index = layout(project)
    if index is None:
        raise ValueError('Explicit deliveries require delivery-index.json')
    if keys is not None and (not isinstance(keys, list) or not keys
                             or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)):
        raise ValueError('Select a nonempty list of distinct delivery keys')
    selected = keys if keys is not None else [key for key, entry in index['deliveries'].items()
                                              if entry.get('quarter') == quarter]
    result = {}
    for key in selected:
        binding = exchange_binding(project, key)
        if quarter and binding['quarter'] != quarter:
            raise ValueError(f"Delivery {key} belongs to {binding['quarter']}, not {quarter}")
        result[key] = binding
    return result


def delivery_title(project: Path, key: str, title: str) -> str:
    if layout(project) is None:
        return title
    binding = exchange_binding(project, key)
    suffix = f" — поставка {binding['delivery_id']} ({binding['quarter']})"
    return title if title.endswith(suffix) else title + suffix


def feature_roots(project: Path) -> list[Path]:
    index = layout(project)
    if index is None:
        return sorted(p for p in (project / "features").glob("*") if p.is_dir())
    return [safe_path(project, d["path"]) for _, d in sorted(index["deliveries"].items())]


def delivery_key(project: Path, path: Path) -> str:
    relative = path.relative_to(project).as_posix()
    index = layout(project)
    if index:
        for key, delivery in index["deliveries"].items():
            if relative == delivery["path"] or relative.startswith(delivery["path"] + "/"):
                return key
        raise ValueError(f"Path has no delivery owner: {relative}")
    return Path(relative).parts[1]


def project_path(project: Path, relative: str) -> Path:
    """Resolve historical references without rewriting immutable evidence."""
    parts = Path(relative).parts
    safe_path(project, relative)
    index = layout(project)
    if index and len(parts) >= 2:
        if parts[0] == "features" and parts[1] in index["deliveries"]:
            return feature_root(project, parts[1]).joinpath(*parts[2:])
        if parts[0] == "planning" and re.fullmatch(r"\d{4}-Q[1-4]", parts[1]):
            return quarter_root(project, parts[1]).joinpath(*parts[2:])
    return safe_path(project, relative)


def logical_path(project: Path, relative: str) -> str:
    """Compatibility for guarded ownership checks; actual Git paths stay physical."""
    index = layout(project)
    if index:
        for key, d in index["deliveries"].items():
            if relative.startswith(d["path"] + "/"):
                return "features/" + key + relative[len(d["path"]):]
        if relative.startswith("quarters/"):
            return "planning/" + relative[len("quarters/"):]
    return relative


def artifact_glob(project: Path, pattern: str) -> list[Path]:
    if not layout(project):
        return sorted(project.glob(pattern))
    if pattern.startswith("features/*/"):
        tail = pattern[len("features/*/"):]
        return sorted(p for root in feature_roots(project) for p in root.glob(tail))
    if pattern.startswith("planning/*/"):
        return sorted(project.glob("quarters/*/" + pattern[len("planning/*/"):]))
    return sorted(project.glob(pattern))


def exchange_binding(project: Path, key: str) -> dict | None:
    """Stable business identity, separate from the legacy exchange transport key."""
    index = layout(project)
    if index is None:
        return None
    delivery = index['deliveries'].get(key)
    if not delivery or not delivery.get('quarter'):
        raise ValueError('Register an explicit quarter delivery before publication')
    binding = {name: delivery[name] for name in ('feature_id', 'delivery_id', 'quarter')}
    binding['delivery_key'] = key
    validate_exchange_binding(binding, key)
    expected = f"quarters/{binding['quarter']}/features/{binding['feature_id']}/deliveries/{binding['delivery_id']}"
    if delivery['path'] != expected or not safe_path(project, expected).is_dir():
        raise ValueError('Delivery identity and authored path differ')
    return binding


def validate_exchange_binding(binding: dict, key: str) -> None:
    if not isinstance(binding, dict) or set(binding) != {'feature_id', 'delivery_id', 'quarter', 'delivery_key'}:
        raise ValueError('Invalid exchange delivery binding')
    if any(not isinstance(binding[field], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', binding[field])
           for field in ('feature_id', 'delivery_id', 'delivery_key')):
        raise ValueError('Invalid exchange delivery identifier')
    if binding['delivery_key'] != key or not isinstance(binding['quarter'], str) or not re.fullmatch(r'\d{4}-Q[1-4]', binding['quarter']):
        raise ValueError('Exchange delivery key or quarter differs')


def require_audit_binding(project: Path, key: str, audit: dict) -> None:
    binding = exchange_binding(project, key)
    if binding is not None and audit.get('delivery_binding') != binding:
        raise ValueError('delivery-audit-binding-required: повтори аудит для текущей фичи, поставки и квартала')


def require_manifest_binding(project: Path, key: str, manifest: dict) -> None:
    binding = exchange_binding(project, key)
    if manifest.get('schema_version') in {4, 5} and manifest.get('delivery_binding') != binding:
        raise ValueError('Exchange manifest belongs to a different feature or delivery')
    if manifest.get('schema_version') not in {4, 5} and manifest.get('delivery_binding') is not None:
        raise ValueError('Delivery binding requires exchange schema 4')
