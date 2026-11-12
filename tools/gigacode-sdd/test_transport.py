import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest

from transport import parse_events, run_session


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "cli.py"

    def fake(self, code):
        self.cli.write_text("import json,sys,os,time,subprocess\n" + code)
        return [sys.executable, str(self.cli)]

    def run_cli(self, code, **kwargs):
        return run_session(self.fake(code), self.root, "Task `literal` $(literal)",
                           self.root / "logs", **kwargs)

    def parse(self, events, tail=""):
        path = self.root / "events.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events) + tail)
        return parse_events(path)

    def test_success_arguments_stdin_and_private_logs(self):
        result = self.run_cli('''
assert sys.stdin.read() == "Task `literal` $(literal)"
assert "stream-json" in sys.argv and "--safe-mode" not in sys.argv
print(json.dumps({"type":"system", "session_id":"s-1"}), flush=True)
print(json.dumps({"type":"result", "subtype":"success", "is_error":False,
                  "result":"done"}), flush=True)
''')
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["session_id"], "s-1")
        self.assertEqual(result["text"], "done")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((self.root / "logs").stat().st_mode), 0o700)
            for name in ("prompt.txt", "stdout.jsonl", "stderr.log"):
                self.assertEqual(stat.S_IMODE((self.root / "logs" / name).stat().st_mode), 0o600)

    def test_tool_denial_overrules_successful_final_prose(self):
        result = self.parse([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "agent", "input": {"secret": "secret"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "is_error": True,
                 "content": "non-interactive mode cannot prompt for confirmation"}]}},
            {"type": "result", "subtype": "success", "result": "Everything passed"},
        ])
        self.assertFalse(result["ok"])
        self.assertTrue(result["permission_denials"])
        self.assertEqual(result["tool_calls"], {"a": "agent"})
        self.assertNotIn("secret", str(result))

    def test_shell_security_policy_denial_classified_as_permission_blocker(self):
        for body in ("Inline execution blocked: shell.python3.wrapper",
                     "Shell syntax could not be validated: invalid_shell_projection"):
            result = self.parse([
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "x", "is_error": True,
                     "content": body}]}},
                {"type": "result", "subtype": "success"}])
            self.assertTrue(result["permission_denials"])

    def test_structured_permission_denials(self):
        result = self.parse([{"type": "result", "subtype": "success", "result": "done",
                              "permission_denials": [{"tool_name": "shell", "input": "secret"}]}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["permission_denials"], [{"tool_name": "shell"}])

    def test_plain_tool_failure_is_detected(self):
        result = self.parse([
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "is_error": True,
                 "content": [{"type": "text", "text": "command failed"}]}]}},
            {"type": "result", "subtype": "success"},
        ])
        self.assertFalse(result["ok"])
        self.assertIn("returned an error", " ".join(result["errors"]))

    def test_missing_result_and_nested_child_result_do_not_pass(self):
        result = self.parse([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "parent_tool_use_id": "agent-call", "subtype": "success"},
        ])
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "done")
        self.assertFalse(result["terminal_result"])

    def test_failure_and_ambiguous_result_do_not_pass(self):
        for terminal in ({"type": "result", "is_error": True},
                         {"type": "result", "subtype": "error_max_turns"},
                         {"type": "result", "result": "done"}):
            with self.subTest(terminal=terminal):
                self.assertFalse(self.parse([terminal])["ok"])

    def test_malformed_trailing_line_preserves_prior_result(self):
        result = self.parse([{"type": "result", "is_error": False, "result": "done"}], '{"type":')
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "done")
        self.assertEqual(result["event_count"], 1)

    def test_nonzero_exit_overrules_success(self):
        result = self.run_cli('print(json.dumps({"type":"result","is_error":False})); sys.exit(7)')
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)

    def test_timeout_preserves_stream(self):
        result = self.run_cli('''
print(json.dumps({"type":"system","session_id":"partial"}), flush=True)
time.sleep(10)
''', timeout_seconds=0.15)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["session_id"], "partial")
        self.assertEqual(result["event_count"], 1)

    def test_cancellation(self):
        stop = threading.Event()
        timer = threading.Timer(0.15, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = self.run_cli('print(json.dumps({"type":"system"}),flush=True);time.sleep(10)',
                              stop_event=stop)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_child_is_stopped_even_after_parent_exits(self):
        marker = self.root / "escaped.txt"
        child = f"import time,pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('escaped')"
        result = self.run_cli(f'''
subprocess.Popen([sys.executable,"-c",{child!r}])
print(json.dumps({{"type":"result","is_error":False}}),flush=True)
''')
        self.assertTrue(result["ok"], result)
        time.sleep(1.6)
        self.assertFalse(marker.exists())

    def test_missing_cli_is_diagnostic_failure(self):
        result = run_session([str(self.root / "absent")], self.root, "task", self.root / "logs")
        self.assertFalse(result["ok"])
        self.assertTrue(any("Cannot start CLI" in error for error in result["errors"]))

    def test_existing_logs_are_not_overwritten(self):
        code = 'print(json.dumps({"type":"result","is_error":False}))'
        self.run_cli(code)
        before = (self.root / "logs" / "stdout.jsonl").read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_cli(code)
        self.assertEqual((self.root / "logs" / "stdout.jsonl").read_bytes(), before)

    def test_environment_is_complete_not_overlay(self):
        os.environ["SDD_SHOULD_NOT_LEAK"] = "present"
        self.addCleanup(os.environ.pop, "SDD_SHOULD_NOT_LEAK", None)
        result = self.run_cli('''
assert os.environ['SDD_TEST_VALUE'] == 'value'
assert 'SDD_SHOULD_NOT_LEAK' not in os.environ
print(json.dumps({'type':'result','is_error':False}))
''', env={"SDD_TEST_VALUE": "value"})
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
