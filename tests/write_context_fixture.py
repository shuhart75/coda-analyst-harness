"""Configure a real, isolated write context for baseline operation fixtures."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def configure_write_context(case, project, branch, feature="baseline"):
    temporary = tempfile.TemporaryDirectory()
    case.addCleanup(temporary.cleanup)
    state = Path(temporary.name)
    (state / "workspace.json").write_text(json.dumps({"roles": {"analytics": {"path": str(project)}}}))
    (state / "collaboration.json").write_text(json.dumps({"mode": "multi-user-branches", "active_work": {
        "feature": feature, "branch": branch, "status": "active"}}))
    (state / "active-mode.md").write_text("mode: release-finalization\n")
    environment = patch.dict(os.environ, {"ANALYST_HARNESS_STATE_ROOT": str(state)})
    environment.start()
    case.addCleanup(environment.stop)
    return state
