"""Request cancellation must not orphan an owner callback's workspace lane."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from mcp.server.lowlevel import Server as SDKServer

import host_runtime_operations
import operation_approval_ui
import server


class OperationApprovalCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_blocked_runtime(self, *, fail_after_acquire=False):
        with tempfile.TemporaryDirectory(prefix="operation-cancel-") as folder:
            root = Path(folder)
            project = root / "alpha"
            project.mkdir()
            state = root / "runtime"
            state.mkdir(mode=0o700)
            gate = threading.Event()
            acquired = threading.Event()
            returned = threading.Event()
            events = []
            observation = {"fd": None, "admitted": False, "child": None, "reserved": False}

            @contextmanager
            def admission(*args, **kwargs):
                observation["admitted"] = True
                observation["child"] = asyncio.current_task()
                events.append("admission-enter")
                try:
                    yield
                finally:
                    observation["admitted"] = False
                    events.append("admission-exit")

            def acquire(workspace, **kwargs):
                descriptor = os.open(root / "workspace.lane", os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                observation.update(fd=descriptor, reserved=True)
                events.append("lane-acquired")
                acquired.set()
                if not gate.wait(5):
                    raise AssertionError("test acquisition was not released")
                events.append("lane-returned")
                returned.set()
                return descriptor

            def complete(workspace, operation_id, kind, **kwargs):
                observation["reserved"] = False
                events.append("reservation-completed")

            backend = object.__new__(host_runtime_operations.HostRuntimeOperations)
            backend.state_dir = state
            backend.lock_path = state / "engine.lock"
            backend.activation_authority_dir = root / "activation"
            backend.workspace = None
            backend._require_trusted_runtime = lambda: None
            backend._load = lambda *args: {
                "state": "pending_host_approval", "immutable": {"component": "api"}}

            async def accepted(*args, workspace_fd=None, **kwargs):
                os.fstat(workspace_fd)  # The real fd reaches a live cleanup owner.
                events.append("backend-received-lane")
                if fail_after_acquire:
                    raise ValueError("FIXTURE_BACKEND_FAILED")
                return {"state": "succeeded", "started_at": "2026-10-28T00:00:00+00:00"}

            backend._execute_admitted = accepted
            original_tools = operation_approval_ui.OperationApprovalTools

            class OwnerConfirmation:
                def confirm(self, **kwargs):
                    return {"content": [], "structuredContent": {
                        "state": "approved", "receipt_verified": True}}

            def tools_factory(projects, **kwargs):
                tools = original_tools(projects, legacy=backend)
                tools.managers["runtime"] = OwnerConfirmation()
                return tools

            @asynccontextmanager
            async def no_stdio():
                yield None, None

            case = self

            class CapturingServer(SDKServer):
                def call_tool(self, *args, **kwargs):
                    decorate = super().call_tool(*args, **kwargs)
                    def register(handler):
                        self.application_handler = handler
                        return decorate(handler)
                    return register

                async def run(self, *args, **kwargs):
                    request = asyncio.create_task(self.application_handler(
                        "confirm_and_execute_operation", {
                            "project": "alpha", "family": "runtime", "target": "fixture",
                            "operation_id": "1" * 32, "card_id": "2" * 32,
                            "confirmation_token": "3" * 64,
                        }))
                    during = None
                    cancelled = None
                    try:
                        case.assertTrue(await asyncio.to_thread(acquired.wait, 3))
                        request.cancel("first cancellation")
                        for _ in range(3):
                            await asyncio.sleep(0)
                        request.cancel("second cancellation")
                        for _ in range(3):
                            await asyncio.sleep(0)
                        during = {
                            "child_cancel_count": observation["child"].cancelling(),
                            "admitted": observation["admitted"],
                            "reserved": observation["reserved"],
                        }
                    finally:
                        gate.set()
                        case.assertTrue(await asyncio.to_thread(returned.wait, 3))
                        try:
                            await asyncio.wait_for(request, 3)
                        except asyncio.CancelledError as exc:
                            cancelled = exc
                    fd = observation["fd"]
                    try:
                        os.fstat(fd)
                        fd_closed = False
                    except OSError:
                        fd_closed = True
                    if not fd_closed:  # Prevent the deliberately failing RED test from leaking.
                        os.close(fd)
                    case.assertEqual(during["child_cancel_count"], 0,
                                     "request cancellation reached the protected owner callback")
                    case.assertTrue(during["admitted"], "activation admission released during acquisition")
                    case.assertTrue(during["reserved"])
                    case.assertTrue(fd_closed, "acquired workspace fd lost its cleanup owner")
                    case.assertFalse(observation["reserved"])
                    case.assertEqual(events, [
                        "admission-enter", "lane-acquired", "lane-returned",
                        "backend-received-lane", "reservation-completed", "admission-exit"])
                    case.assertIsInstance(cancelled, asyncio.CancelledError)
                    case.assertEqual(cancelled.args, ("first cancellation",))

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}), \
                    mock.patch("mcp.server.lowlevel.Server", CapturingServer), \
                    mock.patch("mcp.server.stdio.stdio_server", no_stdio), \
                    mock.patch.object(operation_approval_ui, "OperationApprovalTools", tools_factory), \
                    mock.patch.object(host_runtime_operations, "activation_admission", admission), \
                    mock.patch.object(host_runtime_operations, "acquire_host_operation_lane", acquire), \
                    mock.patch.object(host_runtime_operations, "complete_host_operation", complete):
                await server.serve({"alpha": project})

    async def test_two_cancellations_preserve_blocked_acquisition_until_fd_cleanup(self):
        await self.exercise_blocked_runtime()

    async def test_backend_error_after_cancellation_still_cleans_lane_and_preserves_first_cancel(self):
        await self.exercise_blocked_runtime(fail_after_acquire=True)


if __name__ == "__main__":
    unittest.main()
