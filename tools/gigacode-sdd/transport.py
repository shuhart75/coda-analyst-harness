"""Streamed headless GigaCode sessions with private logs and owned process trees."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(x.get("text", "") for x in value
                         if isinstance(x, dict) and isinstance(x.get("text"), str))
    return ""


def parse_events(path: Path) -> dict:
    """Retain complete JSONL events before interruption; fail closed on errors.

    Diagnostics contain tool identifiers rather than potentially secret arguments.
    The original private log remains evidence for each failed call.
    """
    path = Path(path)
    errors, denials, tool_calls, tool_results = [], [], {}, []
    session_id, final_text, terminal = None, "", None
    event_count = 0
    if not path.exists():
        errors.append("Missing stdout.jsonl")
    else:
        with path.open("rb") as source:
            for number, raw in enumerate(source, 1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode("utf-8-sig"))
                    if not isinstance(event, dict):
                        raise ValueError("Event is not an object")
                except (ValueError, UnicodeError):
                    errors.append(f"Malformed stream event on line {number}")
                    continue
                event_count += 1
                root = event.get("parent_tool_use_id") is None
                if root and isinstance(event.get("session_id"), str):
                    session_id = event["session_id"]
                raw_denials = event.get("permission_denials") or []
                if not isinstance(raw_denials, list):
                    raw_denials = [raw_denials]
                for denial in raw_denials:
                    if isinstance(denial, dict):
                        denials.append({k: denial[k] for k in ("tool_name", "tool_use_id")
                                        if isinstance(denial.get(k), str)})
                    else:
                        denials.append({"reason": "CLI reported permission denial"})
                message = event.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else []
                if not isinstance(content, list):
                    content = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use":
                        tool_id = item.get("id")
                        if isinstance(tool_id, str):
                            tool_calls[tool_id] = item.get("name", "unknown")
                    if item.get("type") != "tool_result":
                        continue
                    tool_id = item.get("tool_use_id", "unknown")
                    if not isinstance(tool_id, str):
                        tool_id = "unknown"
                    tool_results.append({"tool_use_id": tool_id,
                                         "is_error": item.get("is_error"),
                                         "has_content": bool(item.get("content"))})
                    body = _text(item.get("content", "")).lower()
                    denied = any(pattern in body for pattern in (
                        "non-interactive mode cannot prompt", "permission denied",
                        "requires approval", "requires confirmation", "not allowed by",
                        "shell.python3.wrapper", "invalid_shell_projection",
                        "shell syntax could not be validated"))
                    if denied:
                        denials.append({"tool_use_id": tool_id,
                                        "tool_name": tool_calls.get(tool_id, "unknown"),
                                        "reason": "Tool permission/confirmation denied"})
                    if item.get("is_error") is True:
                        errors.append(f"Tool {tool_calls.get(tool_id, 'unknown')} "
                                      f"({tool_id}) returned an error")
                if root and event.get("type") == "assistant":
                    text = _text(content)
                    if text:
                        final_text = text
                if root and event.get("type") == "result":
                    terminal = event
                    text = _text(event.get("result", ""))
                    if text:
                        final_text = text
                if root and event.get("type") == "error":
                    errors.append("CLI emitted a root error event")
    if terminal is None:
        errors.append("Missing terminal root result event")
    elif terminal.get("is_error") is True or terminal.get("subtype", "success") != "success":
        errors.append("Terminal root result reports failure")
    elif terminal.get("is_error") is not False and terminal.get("subtype") != "success":
        errors.append("Terminal root result does not explicitly confirm success")
    if denials:
        errors.append("CLI reported denied permissions; interactive approval is unavailable")
    return {"ok": not errors, "returncode": None, "timed_out": False,
            "cancelled": False, "permission_denials": denials,
            "session_id": session_id, "text": final_text,
            "events_path": str(path), "errors": errors,
            "event_count": event_count, "terminal_result": terminal is not None,
            "tool_calls": tool_calls, "tool_results": tool_results}


def _private_file(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), "wb", buffering=0)


def _stop_group(process: subprocess.Popen):
    """Terminate this session's descendants even after its group leader exits."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.025)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if process.poll() is None:
            process.kill()


def run_session(cli: list[str], cwd: Path, prompt: str, log_dir: Path,
                approval_mode="auto-edit", timeout_seconds=3600,
                env: dict | None = None,
                stop_event: threading.Event | None = None) -> dict:
    """Run a fresh session with stdin task and stream-json output.

    cli is an argv prefix, normally ["gigacode"]. env is a complete environment
    when supplied; omitted variables must not leak back from the parent process.
    log_dir is private (0700), logs are 0600 and never overwritten. Validation
    errors raise ValueError; launch/CLI failures return unsuccessful diagnostics.
    No shell interpolation, token limits or iteration limits are introduced.
    """
    if not cli or not all(isinstance(x, str) and x for x in cli):
        raise ValueError("cli must be a nonempty argv list")
    if "--safe-mode" in cli:
        raise ValueError("safe-mode disables the required SDD context")
    if approval_mode not in ("plan", "default", "auto-edit", "auto"):
        raise ValueError("Unsupported approval mode")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    cwd, log_dir = Path(cwd), Path(log_dir)
    if not cwd.is_dir():
        raise ValueError("Session cwd does not exist")
    if log_dir.is_symlink():
        raise ValueError("Session log directory must not be a symlink")
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_dir, 0o700)
    events_path = log_dir / "stdout.jsonl"
    argv = list(cli) + ["--output-format", "stream-json", "--approval-mode", approval_mode,
                        "--prompt", "Read the complete task from stdin and execute it."]
    timed_out, cancelled, process, launch_error = False, False, None, None
    with _private_file(log_dir / "prompt.txt") as task:
        task.write(prompt.encode("utf-8"))
    try:
        with (log_dir / "prompt.txt").open("rb") as task, \
                _private_file(events_path) as stdout, \
                _private_file(log_dir / "stderr.log") as stderr:
            environment = None if env is None else dict(env)
            kwargs = {"start_new_session": True} if os.name == "posix" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            try:
                process = subprocess.Popen(argv, cwd=cwd, stdin=task, stdout=stdout,
                                           stderr=stderr, env=environment, **kwargs)
                started = time.monotonic()
                while process.poll() is None:
                    if stop_event is not None and stop_event.is_set():
                        cancelled = True
                        break
                    if time.monotonic() - started >= timeout_seconds:
                        timed_out = True
                        break
                    time.sleep(0.05)
            except OSError as exc:
                launch_error = f"Cannot start CLI: {type(exc).__name__}"
            finally:
                if process is not None:
                    _stop_group(process)
                    process.wait()
    except BaseException:
        if process is not None:
            _stop_group(process)
        raise
    result = parse_events(events_path)
    result.update(returncode=process.returncode if process is not None else None,
                  timed_out=timed_out, cancelled=cancelled)
    if launch_error:
        result["errors"].append(launch_error)
    if timed_out:
        result["errors"].append("Session process timeout; partial events preserved")
    if cancelled:
        result["errors"].append("Session cancelled; partial events preserved")
    if process is not None and process.returncode != 0:
        result["errors"].append(f"CLI exited with code {process.returncode}")
    result["ok"] = not result["errors"]
    return result
