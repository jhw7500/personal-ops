"""Bounded, offline transport tests for the pinned code-mode host."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import textwrap
import time
import unittest

from agent_code_mode import CodeModeProbeError, probe_code_mode_host


SUCCESS_HOST = r'''#!/usr/bin/python3
import json, os, struct, sys

def receive():
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        sys.exit(20)
    size = struct.unpack("<I", header)[0]
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        sys.exit(21)
    return json.loads(payload)

def send(value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(payload)) + payload)
    sys.stdout.buffer.flush()

expected_env = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "TMPDIR": os.path.join(os.getcwd(), "tmp"),
}
if os.environ != expected_env or (os.stat(".").st_mode & 0o777) != 0o700:
    sys.exit(22)
if receive() != {"type":"connection/hello","supportedVersions":[1],
                  "requiredCapabilities":[],"optionalCapabilities":[]}:
    sys.exit(23)
send({"type":"connection/ready","selectedVersion":1,"capabilities":[]})
if receive() != {"type":"operation/request","id":1,
                  "request":{"method":"session/open","sessionId":"offline-proof"}}:
    sys.exit(24)
send({"type":"operation/response","id":1,"result":{"status":"ok","value":{
    "type":"session/ready","sessionId":"offline-proof"}}})
execute = receive()
if execute != {"type":"operation/request","id":2,"request":{
    "method":"session/execute","sessionId":"offline-proof",
    "request":{"tool_call_id":"offline-call",
    "enabled_tools":[{"name":"probe","tool_name":{"name":"probe","namespace":None},
      "description":"Offline deterministic echo","kind":"function",
      "input_schema":{"type":"object"},"output_schema":None}],
    "source":"const r = await tools.probe({value:42}); text(r);",
    "yield_time_ms":1000,"max_output_tokens":100}}}:
    sys.exit(25)
send({"type":"operation/response","id":2,"result":{"status":"ok","value":{
    "type":"execution/started","cellId":"1"}}})
send({"type":"delegate/request","id":1,"sessionId":"offline-proof","request":{
    "type":"tool/invoke","invocation":{"cell_id":"1","runtime_tool_call_id":"tool-1",
    "tool_name":{"name":"probe","namespace":None},"tool_kind":"function",
    "input":{"value":42}}}})
if receive() != {"type":"delegate/response","id":1,"result":{"status":"ok","value":{
    "type":"tool/result","result":{"offline_delegate_ok":True,"value":42}}}}:
    sys.exit(26)
send({"type":"execute/initialResponse","id":2,"result":{"status":"ok","value":{
    "Result":{"cell_id":"1","content_items":[{"type":"input_text",
    "text":'{"offline_delegate_ok":true,"value":42}'}],"error_text":None,
    "code_mode_host_duration_ns":1}}}})
send({"type":"cell/closed","sessionId":"offline-proof","cellId":"1"})
if receive() != {"type":"operation/request","id":3,
                  "request":{"method":"session/shutdown","sessionId":"offline-proof"}}:
    sys.exit(27)
send({"type":"operation/response","id":3,"result":{"status":"ok","value":{
    "type":"session/closed","sessionId":"offline-proof"}}})
if sys.stdin.buffer.read(1) != b"":
    sys.exit(28)
'''


class AgentCodeModeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="code-mode-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        os.chmod(self.root, 0o700)

    def helper(self, source: str) -> Path:
        path = self.root / ("helper-" + str(len(list(self.root.iterdir()))))
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        path.chmod(0o700)
        return path

    def run_probe(self, source: str, name: str = "fixture") -> dict:
        return probe_code_mode_host(self.helper(source), self.root / name)

    def assert_code(self, code: str, source: str, name: str = "fixture") -> None:
        with self.assertRaises(CodeModeProbeError) as raised:
            self.run_probe(source, name)
        self.assertEqual(str(raised.exception), code)
        self.assertEqual(raised.exception.code, code)

    def test_exact_offline_exchange_verifies_delegate_shutdown_and_clean_exit(self):
        self.assertEqual(self.run_probe(SUCCESS_HOST), {
            "code_mode_delegate_executed": True,
            "code_mode_shutdown_verified": True,
        })

    def test_rejects_untrusted_helper_and_fixture_paths(self):
        helper = self.helper(SUCCESS_HOST)
        linked_helper = self.root / "linked-helper"
        linked_helper.symlink_to(helper)
        with self.assertRaisesRegex(CodeModeProbeError, "CODE_MODE_HELPER_UNSAFE"):
            probe_code_mode_host(linked_helper, self.root / "fixture-link-helper")

        open_parent = self.root / "open-parent"
        open_parent.mkdir(mode=0o755)
        open_parent.chmod(0o755)  # Explicitly insecure despite a restrictive host umask.
        with self.assertRaisesRegex(CodeModeProbeError, "CODE_MODE_FIXTURE_UNSAFE"):
            probe_code_mode_host(helper, open_parent / "fixture")
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(CodeModeProbeError, "CODE_MODE_FIXTURE_UNSAFE"):
            probe_code_mode_host(helper, linked_parent / "fixture")

    def test_invalid_oversized_and_truncated_frames_are_bounded(self):
        invalid = '''#!/usr/bin/python3
import struct, sys
sys.stdin.buffer.read(4); size = struct.unpack("<I", _)[0] if False else 0
sys.stdout.buffer.write(struct.pack("<I", 1) + b"{"); sys.stdout.buffer.flush()
'''
        self.assert_code("CODE_MODE_FRAME_INVALID", invalid, "invalid")
        oversized = '''#!/usr/bin/python3
import struct, sys
h=sys.stdin.buffer.read(4); sys.stdin.buffer.read(struct.unpack("<I",h)[0])
sys.stdout.buffer.write(struct.pack("<I", 1048577)); sys.stdout.buffer.flush()
'''
        self.assert_code("CODE_MODE_FRAME_TOO_LARGE", oversized, "oversized")
        truncated = '''#!/usr/bin/python3
import struct, sys
h=sys.stdin.buffer.read(4); sys.stdin.buffer.read(struct.unpack("<I",h)[0])
sys.stdout.buffer.write(struct.pack("<I", 20) + b"{}"); sys.stdout.buffer.flush()
'''
        self.assert_code("CODE_MODE_FRAME_INVALID", truncated, "truncated")

    def test_timeout_protocol_error_response_and_nonzero_exit_are_bounded(self):
        timeout = '''#!/usr/bin/python3
import struct, sys, time
h=sys.stdin.buffer.read(4); sys.stdin.buffer.read(struct.unpack("<I",h)[0]); time.sleep(10)
'''
        self.assert_code("CODE_MODE_TIMEOUT", timeout, "timeout")
        wrong = '''#!/usr/bin/python3
import json, struct, sys
h=sys.stdin.buffer.read(4); sys.stdin.buffer.read(struct.unpack("<I",h)[0])
p=json.dumps({"type":"connection/ready","selectedVersion":2,"capabilities":[]}).encode()
sys.stdout.buffer.write(struct.pack("<I",len(p))+p); sys.stdout.buffer.flush()
'''
        self.assert_code("CODE_MODE_PROTOCOL_ERROR", wrong, "wrong")
        explicit_error = '''#!/usr/bin/python3
import json, struct, sys
h=sys.stdin.buffer.read(4); sys.stdin.buffer.read(struct.unpack("<I",h)[0])
p=json.dumps({"type":"operation/response","id":1,"result":{"status":"error",
"error":"private raw detail"}}).encode()
sys.stdout.buffer.write(struct.pack("<I",len(p))+p); sys.stdout.buffer.flush()
'''
        self.assert_code("CODE_MODE_PROTOCOL_ERROR", explicit_error, "error")
        # Verify exit status after the complete exchange. Early EOF can become
        # observable before waitpid publishes the child's status, so it belongs
        # to the separate interrupted-handshake test below.
        exited = SUCCESS_HOST + "\nsys.exit(9)\n"
        self.assert_code("CODE_MODE_EXIT_FAILED", exited, "exit")

    def test_exit_during_handshake_remains_a_bounded_failure(self):
        exited = '''#!/usr/bin/python3
import json, struct, sys
h=sys.stdin.buffer.read(4); sys.stdin.buffer.read(struct.unpack("<I",h)[0])
p=json.dumps({"type":"connection/ready","selectedVersion":1,"capabilities":[]}).encode()
sys.stdout.buffer.write(struct.pack("<I",len(p))+p); sys.stdout.buffer.flush(); sys.exit(9)
'''
        with self.assertRaises(CodeModeProbeError) as raised:
            self.run_probe(exited, "early-exit")
        self.assertIn(raised.exception.code, {
            "CODE_MODE_EXIT_FAILED", "CODE_MODE_FRAME_INVALID", "CODE_MODE_PROTOCOL_ERROR"})
        self.assertEqual(str(raised.exception), raised.exception.code)

    def test_rejects_every_unexpected_delegate_field_without_echoing_it(self):
        variants = {
            "id": SUCCESS_HOST.replace(
                '"type":"delegate/request","id":1,"sessionId"',
                '"type":"delegate/request","id":9,"sessionId"', 1),
            "session": SUCCESS_HOST.replace(
                '"type":"delegate/request","id":1,"sessionId":"offline-proof"',
                '"type":"delegate/request","id":1,"sessionId":"other"', 1),
            "tool": SUCCESS_HOST.replace(
                '"tool_name":{"name":"probe","namespace":None},"tool_kind"',
                '"tool_name":{"name":"shell","namespace":None},"tool_kind"', 1),
            "kind": SUCCESS_HOST.replace(
                '"tool_name":{"name":"probe","namespace":None},"tool_kind":"function"',
                '"tool_name":{"name":"probe","namespace":None},"tool_kind":"command"', 1),
            "input": SUCCESS_HOST.replace(
                '"input":{"value":42}}}})', '"input":{"value":43}}}})', 1),
        }
        for label, source in variants.items():
            with self.subTest(label=label):
                self.assertNotEqual(source, SUCCESS_HOST)
                self.assert_code("CODE_MODE_PROTOCOL_ERROR", source,
                                 "delegate-" + label)

    def test_failure_terminates_only_owned_process_group(self):
        helper = self.helper('''#!/usr/bin/python3
import json, os, struct, subprocess, sys, time
child=subprocess.Popen(["/usr/bin/python3","-c","import time; time.sleep(30)"])
with open("descendant.pid","w") as stream: stream.write(str(child.pid))
p=json.dumps({"type":"unexpected"}).encode()
sys.stdout.buffer.write(struct.pack("<I",len(p))+p); sys.stdout.buffer.flush()
time.sleep(30)
''')
        fixture = self.root / "termination"
        with self.assertRaisesRegex(CodeModeProbeError, "CODE_MODE_PROTOCOL_ERROR"):
            probe_code_mode_host(helper, fixture)
        pid = int((fixture / "descendant.pid").read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("owned descendant remained alive")


if __name__ == "__main__":
    unittest.main()
