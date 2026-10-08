"""Bind the analyst-owned SDD input to the existing delivery audit."""
from pathlib import Path
import hashlib
import json

import sdd_bundle

LEGACY = "requirements-only-v1"


def working_hash(feature_root):
    root = Path(feature_root) / "sdd"
    if not root.exists() and not root.is_symlink():
        return None
    files = sdd_bundle._files(root)
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(feature_root, state, code_root=None, *, verify_source=False):
    root = Path(feature_root) / "sdd"
    profile = state.get("input_profile", LEGACY)
    if profile == LEGACY:
        if root.exists() or root.is_symlink():
            raise ValueError("SDD input exists: begin-preparation --input-profile openspec-spec-driven-v1 is required")
        return None
    if profile != sdd_bundle.PROFILE:
        raise ValueError("Unsupported delivery input profile")
    text = (Path(feature_root) / "requirements.md").read_text(encoding="utf-8")
    descriptor = sdd_bundle.inspect(root, text)
    if verify_source:
        if code_root is None:
            raise ValueError("SDD audit requires the registered code repository and verified source evidence")
        sdd_bundle.verify_sources(root, descriptor, Path(code_root))
    return descriptor


def require_unchanged(feature_root, state):
    current = snapshot(feature_root, state)
    if state.get("delivery_audit", {}).get("sdd_input") != current:
        raise ValueError("SDD input changed after audit; repeat audit and confirmation")
    return current
