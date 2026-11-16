"""New runtime code gets a bounded retry budget without erasing saved work."""
import hashlib
from pathlib import Path


def runtime_revision():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob('*.py')):
        if path.name.startswith('test_'):
            continue
        digest.update(path.name.encode('utf-8') + b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def renew(state, defaults, revision):
    """Archive counters only; callers keep safety/external gates and receipts."""
    if state.get('runtime_revision') == revision:
        return False
    if any(state.get(key, default) != default for key, default in defaults.items()):
        state.setdefault('budget_history', []).append({
            'runtime_revision': state.get('runtime_revision', 'legacy'),
            'counters': {key: state.get(key) for key in defaults}})
    state.update(defaults)
    state['runtime_revision'] = revision
    return True
