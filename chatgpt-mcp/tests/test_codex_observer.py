import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_observer import (  # noqa: E402
    JsonRpcClient,
    _load_projects,
    discover_managed_endpoint,
    main,
    normalize_thread,
    observe_once,
)


NOW = "2026-10-12T10:15:00+00:00"


class RecordingStore:
    def __init__(self):
        self.calls = []

    def record_work_events(self, project, events, *, provenance):
        self.calls.append((project, events, provenance))
        return {"accepted": len(events)}


class FakeRpc:
    def __init__(self, threads, usage=None):
        self.threads = {thread["id"]: thread for thread in threads}
        self.usage = usage or {}
        self.calls = []
        self.notifications = []

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "initialize":
            return {"userAgent": "codex/0.162.1"}
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        if method == "thread/read":
            return {"thread": self.threads[params["threadId"]]}
        if method == "account/usage/read":
            return self.usage.get(params["threadId"], {"threadUsage": None})
        raise AssertionError(f"unexpected RPC method: {method}")

    async def notify(self, method, params=None):
        self.notifications.append((method, params))


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        return json.dumps({"id": 1, "result": {}})


class SilentSocket(FakeSocket):
    async def recv(self):
        await asyncio.Future()


class BlockedSendSocket(FakeSocket):
    async def send(self, payload):
        await asyncio.Future()


class NotificationSocket(FakeSocket):
    async def recv(self):
        await asyncio.sleep(0.005)
        return json.dumps({"method": "private/notification", "params": "private payload"})


class CodexObserverTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-observer-test-")
        self.root = Path(self.temporary.name)
        self.alpha = self.root / "repos" / "alpha"
        self.nested = self.alpha / "packages" / "nested"
        self.beta = self.root / "repos" / "beta"
        self.nested.mkdir(parents=True)
        self.beta.mkdir(parents=True)
        self.projects = {
            "alpha": self.alpha,
            "nested": self.nested,
            "beta": self.beta,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def thread(self, **overrides):
        return {
            "id": "0199d5ef-54b1-7112-9394-ea291c75ad21",
            "cwd": str(self.alpha),
            "status": {"type": "active", "activeFlags": []},
            "model": "gpt-6.1-sol",
            "reasoningEffort": "max",
            "parentThreadId": None,
            "agentRole": "executor",
            "agentNickname": "builder",
            "name": "private task name",
            "preview": "private prompt preview",
            "turns": [{"messages": ["private transcript"]}],
            **overrides,
        }

    def test_normalize_thread_maps_deepest_project_and_keeps_only_metadata(self):
        thread = self.thread(cwd=str(self.nested / "src"))
        (self.nested / "src").mkdir()

        project, event = normalize_thread(thread, self.projects, time=NOW)

        self.assertEqual(project, "nested")
        self.assertEqual(event, {
            "schema_version": 1,
            "project": "nested",
            "source_id": "codex-local",
            "source_kind": "codex-cli",
            "actor_id": "0199d5ef-54b1-7112-9394-ea291c75ad21",
            "actor_kind": "agent",
            "actor_label": "builder",
            "task_id": "session-activity",
            "task_kind": "agent",
            "task_label": "Codex session active",
            "action": "execute",
            "state": "running",
            "time": NOW,
            "model": "gpt-6.1-sol",
            "model_source": "configuration",
            "reasoning_effort": "max",
            "reasoning_effort_source": "configuration",
        })
        rendered = json.dumps(event)
        for private_value in ("private task name", "private prompt preview", "private transcript"):
            self.assertNotIn(private_value, rendered)

    def test_waiting_and_inactive_threads_never_claim_success_or_reasoning(self):
        waiting = self.thread(status={"type": "active", "activeFlags": ["waitingOnUserInput"]})
        unrelated = self.thread(status={"type": "active", "activeFlags": ["awaitingBackgroundTask"]})
        inactive = self.thread(status={"type": "notLoaded", "activeFlags": []})

        _, waiting_event = normalize_thread(waiting, self.projects, time=NOW)
        _, unrelated_event = normalize_thread(unrelated, self.projects, time=NOW)
        _, inactive_event = normalize_thread(inactive, self.projects, time=NOW)

        self.assertEqual((waiting_event["state"], waiting_event["task_label"]),
                         ("queued", "Codex session waiting"))
        _, approval_event = normalize_thread(
            self.thread(status={"type": "active", "activeFlags": ["waitingOnApproval"]}),
            self.projects,
            time=NOW,
        )
        self.assertEqual(approval_event["state"], "queued")
        self.assertEqual((unrelated_event["state"], unrelated_event["task_label"]),
                         ("running", "Codex session active"))
        self.assertEqual((inactive_event["state"], inactive_event["task_label"]),
                         ("unknown", "Codex session inactive"))
        for event in (waiting_event, inactive_event):
            self.assertEqual(event["action"], "execute")
            self.assertNotIn(event["state"], {"succeeded", "returned"})
            self.assertNotEqual(event["action"], "reason")

    def test_child_thread_keeps_its_identity_and_parent_actor(self):
        child = self.thread(
            id="0199d5ef-54b1-7112-9394-ea291c75ad22",
            parentThreadId="0199d5ef-54b1-7112-9394-ea291c75ad21",
            agentNickname="",
            agentRole="reviewer",
        )

        _, event = normalize_thread(child, self.projects, time=NOW)

        self.assertEqual(event["actor_id"], child["id"])
        self.assertEqual(event["parent_actor_id"], child["parentThreadId"])
        self.assertEqual(event["actor_label"], "reviewer")

    def test_reasoning_effort_keeps_independent_configuration_provenance(self):
        _, event = normalize_thread(
            self.thread(model=None, reasoningEffort="high"), self.projects, time=NOW,
        )

        self.assertNotIn("model", event)
        self.assertNotIn("model_source", event)
        self.assertEqual(event["reasoning_effort"], "high")
        self.assertEqual(event["reasoning_effort_source"], "configuration")

    def test_usage_is_omitted_when_unavailable_or_ambiguous(self):
        unavailable = self.thread(threadUsage=None)
        ambiguous = self.thread(threadUsage={
            "threadId": "0199d5ef-54b1-7112-9394-ea291c75ad21",
            "groups": [
                {"model": "gpt-6.1-sol", "reasoningEffort": "max", "totalTokens": 15},
                {"model": "gpt-6.1-sol", "reasoningEffort": "max", "totalTokens": 2},
            ],
        })
        matching = self.thread(threadUsage={
            "threadId": "0199d5ef-54b1-7112-9394-ea291c75ad21",
            "estimatedUsageCreditsMicros": 900,
            "estimatedUsageUsdMicros": None,
            "groups": [{
                "model": "gpt-6.1-sol",
                "reasoningEffort": "max",
                "speed": "fast",
                "inputTokens": 12,
                "outputTokens": 3,
                "totalTokens": 15,
                "cachedInputTokens": 7,
            }],
        })

        _, unavailable_event = normalize_thread(unavailable, self.projects, time=NOW)
        _, ambiguous_event = normalize_thread(ambiguous, self.projects, time=NOW)
        _, matching_event = normalize_thread(matching, self.projects, time=NOW)

        self.assertNotIn("usage", unavailable_event)
        self.assertNotIn("usage", ambiguous_event)
        self.assertEqual(matching_event["usage"], {
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
        })
        self.assertEqual(matching_event["usage_scope"], "session")
        self.assertTrue(matching_event["usage_estimated"])

    def test_usage_rejects_mismatched_identity_model_and_invalid_supplied_counter(self):
        cases = [
            {
                "threadId": "0199d5ef-54b1-7112-9394-ea291c75ad99",
                "groups": [{"model": "gpt-6.1-sol", "reasoningEffort": "max",
                            "totalTokens": 15}],
            },
            {
                "threadId": "0199d5ef-54b1-7112-9394-ea291c75ad21",
                "groups": [{"model": "other", "reasoningEffort": "max",
                            "totalTokens": 15}],
            },
            {
                "threadId": "0199d5ef-54b1-7112-9394-ea291c75ad21",
                "groups": [{"model": "gpt-6.1-sol", "reasoningEffort": "max",
                            "inputTokens": -1, "totalTokens": 15}],
            },
            {
                "threadId": "0199d5ef-54b1-7112-9394-ea291c75ad21",
                "groups": [{"model": "gpt-6.1-sol", "reasoningEffort": "max",
                            "inputTokens": None, "outputTokens": None}],
            },
        ]

        for usage in cases:
            with self.subTest(usage=usage):
                _, event = normalize_thread(
                    self.thread(threadUsage=usage), self.projects, time=NOW,
                )
                self.assertNotIn("usage", event)

    async def test_observe_once_uses_only_read_methods_and_records_observed_batches(self):
        thread = self.thread()
        rpc = FakeRpc([thread], usage={thread["id"]: {"threadUsage": None}})
        store = RecordingStore()

        result = await observe_once(
            rpc, self.projects, store, read_usage=True, time=NOW, tracked={},
        )

        self.assertEqual([method for method, _ in rpc.calls], [
            "initialize", "thread/loaded/list", "thread/read", "account/usage/read",
        ])
        self.assertEqual(rpc.notifications, [("initialized", {})])
        self.assertEqual(rpc.calls[2][1], {"threadId": thread["id"], "includeTurns": False})
        self.assertNotIn("thread/resume", [method for method, _ in rpc.calls])
        self.assertEqual(store.calls[0][0], "alpha")
        self.assertEqual(store.calls[0][2], "observed")
        self.assertLessEqual(len(store.calls[0][1]), 20)
        self.assertEqual(result["recorded"], 1)

    async def test_native_activity_is_observed_without_resuming_or_replaying_history(self):
        from codex_activity import CodexActivityReader
        home = self.root / "codex"
        folder = home / "sessions" / "2026" / "10" / "12"
        folder.mkdir(parents=True)
        native_id = self.thread()["id"]
        rollout = folder / f"rollout-2026-10-12T10-00-00-{native_id}.jsonl"
        rollout.write_text(json.dumps({"type": "response_item", "timestamp": NOW,
                                      "payload": {"type": "function_call", "name": "old_tool",
                                                  "call_id": "old-call"}}) + "\n")
        thread = self.thread(path=str(rollout))
        reader = CodexActivityReader(home)
        store = RecordingStore()
        first = await observe_once(FakeRpc([thread]), self.projects, store,
                                   time=NOW, tracked={}, activity=reader)
        self.assertEqual(first["recorded"], 1)
        stamp = "2026-10-12T10:15:01+00:00"
        with rollout.open("a") as stream:
            for payload in [{"type": "function_call", "name": "exec_command",
                             "call_id": "new-call", "arguments": "PRIVATE COMMAND"},
                            {"type": "function_call_output", "call_id": "new-call",
                             "output": "PRIVATE OUTPUT"}]:
                stream.write(json.dumps({"timestamp": stamp, "type": "response_item",
                                         "payload": payload}) + "\n")
        rpc = FakeRpc([thread])
        second = await observe_once(rpc, self.projects, store, time=stamp,
                                    tracked=first["tracked"], activity=reader)
        calls = [row for _, batch, provenance in store.calls for row in batch
                 if row["task_kind"] == "call" and provenance == "observed"]
        self.assertEqual([row["state"] for row in calls], ["queued", "returned"])
        self.assertEqual({row["actor_id"] for row in calls}, {native_id})
        self.assertEqual({row["project"] for row in calls}, {"alpha"})
        self.assertEqual(second["activity_recorded"], 2)
        self.assertNotIn("PRIVATE", json.dumps(store.calls))
        self.assertNotIn("old-call", json.dumps(store.calls))
        self.assertTrue(all(method in {"initialize", "thread/loaded/list", "thread/read"}
                            for method, _ in rpc.calls))
        third = await observe_once(FakeRpc([thread]), self.projects, store,
                                   time="2026-10-12T10:15:02+00:00",
                                   tracked=second["tracked"], activity=reader)
        self.assertEqual(third["recorded"], 0)

    async def test_disappeared_allowed_thread_is_cleared_as_unknown(self):
        store = RecordingStore()
        first = await observe_once(
            FakeRpc([self.thread()]), self.projects, store, time=NOW, tracked={},
        )

        result = await observe_once(
            FakeRpc([]), self.projects, store, time=NOW,
            tracked=first["tracked"],
        )

        cleared = store.calls[1][1][0]
        self.assertEqual(cleared["actor_id"], self.thread()["id"])
        self.assertEqual(cleared["state"], "unknown")
        self.assertEqual(cleared["task_label"], "Codex session inactive")
        for stale_field in (
            "model", "model_source", "reasoning_effort", "reasoning_effort_source",
            "usage", "usage_scope", "usage_estimated",
        ):
            self.assertNotIn(stale_field, cleared)
        self.assertIn(self.thread()["id"], result["tracked"])

        await observe_once(
            FakeRpc([]), self.projects, store, time="2026-10-12T10:16:00+00:00",
            tracked=result["tracked"],
        )
        self.assertEqual(len(store.calls), 2)

    async def test_repeated_inactive_thread_is_recorded_only_once(self):
        inactive = self.thread(status={"type": "notLoaded", "activeFlags": []})
        store = RecordingStore()

        first = await observe_once(
            FakeRpc([inactive]), self.projects, store, time=NOW, tracked={},
        )
        second = await observe_once(
            FakeRpc([inactive]), self.projects, store,
            time="2026-10-12T10:16:00+00:00", tracked=first["tracked"],
        )

        self.assertEqual(len(store.calls), 1)
        self.assertEqual(second["recorded"], 0)

    async def test_active_thread_heartbeats_at_most_once_per_thirty_seconds(self):
        store = RecordingStore()

        first = await observe_once(
            FakeRpc([self.thread()]), self.projects, store,
            time="2026-10-12T10:15:00+00:00", tracked={},
        )
        early = await observe_once(
            FakeRpc([self.thread()]), self.projects, store,
            time="2026-10-12T10:15:29+00:00", tracked=first["tracked"],
        )
        heartbeat = await observe_once(
            FakeRpc([self.thread()]), self.projects, store,
            time="2026-10-12T10:15:30+00:00", tracked=early["tracked"],
        )

        self.assertEqual(early["recorded"], 0)
        self.assertEqual(heartbeat["recorded"], 1)
        self.assertEqual(len(store.calls), 2)
        self.assertEqual(store.calls[0][1][0]["time"], "2026-10-12T10:15:00+00:00")
        self.assertEqual(store.calls[1][1][0]["time"], "2026-10-12T10:15:30+00:00")

    async def test_real_status_and_model_changes_record_immediately(self):
        store = RecordingStore()
        first = await observe_once(
            FakeRpc([self.thread()]), self.projects, store,
            time="2026-10-12T10:15:00+00:00", tracked={},
        )
        waiting = self.thread(status={"type": "active", "activeFlags": ["waitingOnApproval"]})
        second = await observe_once(
            FakeRpc([waiting]), self.projects, store,
            time="2026-10-12T10:15:01+00:00", tracked=first["tracked"],
        )
        changed_model = self.thread(
            status={"type": "active", "activeFlags": ["waitingOnApproval"]},
            model="gpt-6-sol",
        )
        third = await observe_once(
            FakeRpc([changed_model]), self.projects, store,
            time="2026-10-12T10:15:02+00:00", tracked=second["tracked"],
        )

        self.assertEqual((second["recorded"], third["recorded"]), (1, 1))
        self.assertEqual([call[1][0]["state"] for call in store.calls],
                         ["running", "queued", "queued"])
        self.assertEqual(store.calls[-1][1][0]["model"], "gpt-6-sol")

    async def test_usage_change_records_but_unread_or_null_usage_does_not_fake_change(self):
        thread = self.thread()
        usage = {
            "threadUsage": {
                "threadId": thread["id"],
                "groups": [{"model": "gpt-6.1-sol", "reasoningEffort": "max",
                            "inputTokens": 10, "outputTokens": 2, "totalTokens": 12}],
            },
        }
        changed_usage = {
            "threadUsage": {
                "threadId": thread["id"],
                "groups": [{"model": "gpt-6.1-sol", "reasoningEffort": "max",
                            "inputTokens": 14, "outputTokens": 3, "totalTokens": 17}],
            },
        }
        store = RecordingStore()
        first = await observe_once(
            FakeRpc([thread], usage={thread["id"]: usage}), self.projects, store,
            read_usage=True, time="2026-10-12T10:15:00+00:00", tracked={},
        )
        unread = await observe_once(
            FakeRpc([thread]), self.projects, store, read_usage=False,
            time="2026-10-12T10:15:01+00:00", tracked=first["tracked"],
        )
        changed = await observe_once(
            FakeRpc([thread], usage={thread["id"]: changed_usage}), self.projects, store,
            read_usage=True, time="2026-10-12T10:15:02+00:00", tracked=unread["tracked"],
        )
        missing = await observe_once(
            FakeRpc([thread], usage={thread["id"]: {"threadUsage": None}}),
            self.projects, store, read_usage=True,
            time="2026-10-12T10:15:03+00:00", tracked=changed["tracked"],
        )

        self.assertEqual((unread["recorded"], changed["recorded"], missing["recorded"]),
                         (0, 1, 0))
        self.assertEqual(len(store.calls), 2)
        self.assertEqual(store.calls[-1][1][0]["usage"]["total_tokens"], 17)
        self.assertNotIn(0, store.calls[-1][1][0]["usage"].values())

    async def test_unread_heartbeat_and_state_change_do_not_refresh_usage_timestamp(self):
        thread = self.thread()
        usage = {
            "threadUsage": {
                "threadId": thread["id"],
                "groups": [{"model": "gpt-6.1-sol", "reasoningEffort": "max",
                            "inputTokens": 10, "outputTokens": 2, "totalTokens": 12}],
            },
        }
        store = RecordingStore()
        first = await observe_once(
            FakeRpc([thread], usage={thread["id"]: usage}), self.projects, store,
            read_usage=True, time="2026-10-12T10:15:00+00:00", tracked={},
        )

        heartbeat = await observe_once(
            FakeRpc([thread]), self.projects, store, read_usage=False,
            time="2026-10-12T10:15:30+00:00", tracked=first["tracked"],
        )
        waiting = self.thread(
            status={"type": "active", "activeFlags": ["waitingOnUserInput"]},
        )
        changed = await observe_once(
            FakeRpc([waiting]), self.projects, store, read_usage=False,
            time="2026-10-12T10:15:31+00:00", tracked=heartbeat["tracked"],
        )

        self.assertEqual((heartbeat["recorded"], changed["recorded"]), (1, 1))
        for _, events, _ in store.calls[1:]:
            self.assertNotIn("usage", events[0])
            self.assertNotIn("usage_scope", events[0])
            self.assertNotIn("usage_estimated", events[0])
        tracked = changed["tracked"][thread["id"]]["current_event"]
        self.assertEqual(tracked["usage"]["total_tokens"], 12)

    async def test_rpc_client_rejects_mutating_or_unknown_methods_before_sending(self):
        transport = FakeSocket()
        client = JsonRpcClient(transport)

        with self.assertRaises(ValueError):
            await client.call("thread/resume", {"threadId": "x"})

        self.assertEqual(transport.sent, [])
        self.assertEqual(await client.call("initialize"), {})

    async def assert_rpc_timeout(self, operation):
        with mock.patch("codex_observer._RPC_TIMEOUT_SECONDS", 0.03, create=True):
            task = asyncio.create_task(operation)
            try:
                done, _ = await asyncio.wait({task}, timeout=0.3)
                self.assertIn(task, done, "Unresponsive RPC must fail within its deadline")
                with self.assertRaisesRegex(RuntimeError, "^Codex RPC timed out$"):
                    task.result()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_rpc_client_bounds_an_unanswered_request(self):
        await self.assert_rpc_timeout(JsonRpcClient(SilentSocket()).call("initialize"))

    async def test_rpc_client_bounds_a_blocked_request_send(self):
        await self.assert_rpc_timeout(JsonRpcClient(BlockedSendSocket()).call("initialize"))

    async def test_rpc_client_notifications_do_not_extend_request_deadline(self):
        await self.assert_rpc_timeout(JsonRpcClient(NotificationSocket()).call("initialize"))

    async def test_rpc_client_bounds_a_blocked_initialization_notification(self):
        await self.assert_rpc_timeout(JsonRpcClient(BlockedSendSocket()).notify("initialized"))


class EndpointDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-daemon-observer-")
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "codex"
        self.pid_dir = self.codex_home / "app-server-daemon"
        self.pid_dir.mkdir(parents=True)
        self.pid_file = self.pid_dir / "app-server.pid"
        self.pid_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.pid_file.chmod(0o600)
        self.proc = self.root / "proc"
        (self.proc / str(os.getpid()) / "fd").mkdir(parents=True)
        (self.proc / str(os.getpid()) / "fd" / "7").symlink_to("socket:[4242]")
        (self.proc / "net").mkdir(parents=True)
        self.socket_path = self.root / "codex-daemon-test.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.extra_listeners = []
        (self.proc / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"0000: 00000002 00000000 00010000 0001 01 4242 {self.socket_path}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        for listener in self.extra_listeners:
            listener.close()
        self.listener.close()
        self.temporary.cleanup()

    def make_control_dir(self):
        control_dir = self.codex_home / "app-server-control"
        control_dir.mkdir(mode=0o700)
        control_dir.chmod(0o700)
        return control_dir

    def bind_socket(self, path, mode=0o600):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(mode)
        self.extra_listeners.append(listener)
        return path

    def test_discovers_one_stable_user_owned_listener(self):
        endpoint = discover_managed_endpoint(
            self.codex_home, proc_root=self.proc, socket_prefix=str(self.root / "codex-daemon-"),
        )

        self.assertEqual(endpoint, self.socket_path)
        self.assertTrue(stat.S_ISSOCK(endpoint.lstat().st_mode))

    def test_empty_safe_control_directory_preserves_legacy_pid_discovery(self):
        self.make_control_dir()

        endpoint = discover_managed_endpoint(
            self.codex_home, proc_root=self.proc,
            socket_prefix=str(self.root / "codex-daemon-"),
        )

        self.assertEqual(endpoint, self.socket_path)

    def test_accepts_managed_pid_file_with_host_default_read_permissions(self):
        self.pid_file.chmod(0o664)

        endpoint = discover_managed_endpoint(
            self.codex_home, proc_root=self.proc, socket_prefix=str(self.root / "codex-daemon-"),
        )

        self.assertEqual(endpoint, self.socket_path)

    def test_rejects_symlink_pid_file(self):
        real = self.pid_dir / "real.pid"
        self.pid_file.replace(real)
        self.pid_file.symlink_to(real)

        with self.assertRaisesRegex(RuntimeError, "PID file"):
            discover_managed_endpoint(
                self.codex_home, proc_root=self.proc,
                socket_prefix=str(self.root / "codex-daemon-"),
            )

    def test_discovers_stable_control_symlink_without_legacy_pid(self):
        self.pid_file.unlink()
        control_dir = self.make_control_dir()
        control_path = control_dir / "app-server-control.sock"
        control_path.symlink_to(self.socket_path)
        self.socket_path.chmod(0o600)

        endpoint = discover_managed_endpoint(
            self.codex_home, proc_root=self.proc,
            socket_prefix=str(self.root / "codex-daemon-"),
        )

        self.assertEqual(endpoint, self.socket_path)

    def test_discovers_safe_direct_control_socket_without_legacy_pid(self):
        self.pid_file.unlink()
        control_path = self.make_control_dir() / "app-server-control.sock"
        self.bind_socket(control_path)

        endpoint = discover_managed_endpoint(
            self.codex_home, proc_root=self.proc,
            socket_prefix=str(self.root / "codex-daemon-"),
        )

        self.assertEqual(endpoint, control_path)

    def test_present_unsafe_control_directory_never_falls_back_to_legacy_pid(self):
        control_dir = self.make_control_dir()
        control_dir.chmod(0o770)
        (control_dir / "app-server-control.sock").symlink_to(self.socket_path)
        self.socket_path.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "control listener"):
            discover_managed_endpoint(
                self.codex_home, proc_root=self.proc,
                socket_prefix=str(self.root / "codex-daemon-"),
            )

    def test_control_symlink_must_resolve_to_safe_owned_socket_under_prefix(self):
        cases = ("dangling", "outside-prefix", "unsafe-mode")
        for case in cases:
            with self.subTest(case=case):
                control_dir = self.make_control_dir()
                control_path = control_dir / "app-server-control.sock"
                try:
                    if case == "dangling":
                        target = self.root / "codex-daemon-missing.sock"
                    elif case == "outside-prefix":
                        target = self.bind_socket(self.root / "outside.sock")
                    else:
                        target = self.bind_socket(
                            self.root / "codex-daemon-unsafe.sock", 0o660,
                        )
                    control_path.symlink_to(target)
                    self.pid_file.unlink(missing_ok=True)

                    with self.assertRaisesRegex(RuntimeError, "control listener"):
                        discover_managed_endpoint(
                            self.codex_home, proc_root=self.proc,
                            socket_prefix=str(self.root / "codex-daemon-"),
                        )
                finally:
                    control_path.unlink(missing_ok=True)
                    control_dir.rmdir()

    def test_symlinked_control_directory_is_rejected(self):
        real_control = self.codex_home / "real-control"
        real_control.mkdir(mode=0o700)
        real_control.chmod(0o700)
        (real_control / "app-server-control.sock").symlink_to(self.socket_path)
        (self.codex_home / "app-server-control").symlink_to(real_control, target_is_directory=True)
        self.socket_path.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "control listener"):
            discover_managed_endpoint(
                self.codex_home, proc_root=self.proc,
                socket_prefix=str(self.root / "codex-daemon-"),
            )

    def test_control_directory_entry_and_target_must_be_owned(self):
        control_path = self.make_control_dir() / "app-server-control.sock"
        control_path.symlink_to(self.socket_path)
        self.socket_path.chmod(0o600)
        self.pid_file.unlink()
        original_lstat = Path.lstat

        for unowned in (control_path.parent, control_path, self.socket_path):
            with self.subTest(unowned=unowned):
                def wrong_owner_lstat(path):
                    info = original_lstat(path)
                    if Path(path) != unowned:
                        return info
                    changed = mock.Mock()
                    for field in ("st_mode", "st_ino", "st_dev", "st_ctime_ns"):
                        setattr(changed, field, getattr(info, field))
                    changed.st_uid = os.getuid() + 1
                    return changed

                with mock.patch("pathlib.Path.lstat", new=wrong_owner_lstat):
                    with self.assertRaisesRegex(RuntimeError, "control listener"):
                        discover_managed_endpoint(
                            self.codex_home, proc_root=self.proc,
                            socket_prefix=str(self.root / "codex-daemon-"),
                        )

    def test_control_symlink_identity_rotation_during_discovery_is_rejected(self):
        self.pid_file.unlink()
        control_path = self.make_control_dir() / "app-server-control.sock"
        control_path.symlink_to(self.socket_path)
        self.socket_path.chmod(0o600)
        original_lstat = Path.lstat
        calls = 0

        def rotating_lstat(path):
            nonlocal calls
            info = original_lstat(path)
            if Path(path) == control_path:
                calls += 1
                if calls == 2:
                    changed = mock.Mock(wraps=info)
                    changed.st_ino = info.st_ino + 1
                    return changed
            return info

        with mock.patch("pathlib.Path.lstat", new=rotating_lstat):
            with self.assertRaisesRegex(RuntimeError, "changed during discovery"):
                discover_managed_endpoint(
                    self.codex_home, proc_root=self.proc,
                    socket_prefix=str(self.root / "codex-daemon-"),
                )

    def test_control_symlink_target_rotation_during_discovery_is_rejected(self):
        self.pid_file.unlink()
        other = self.bind_socket(self.root / "codex-daemon-other.sock")
        control_path = self.make_control_dir() / "app-server-control.sock"
        control_path.symlink_to(self.socket_path)
        self.socket_path.chmod(0o600)
        original_readlink = os.readlink
        calls = 0

        def rotating_readlink(path, *args, **kwargs):
            nonlocal calls
            if Path(path) == control_path:
                calls += 1
                if calls == 2:
                    control_path.unlink()
                    control_path.symlink_to(other)
            return original_readlink(path, *args, **kwargs)

        with mock.patch("codex_observer.os.readlink", new=rotating_readlink):
            with self.assertRaisesRegex(RuntimeError, "changed during discovery"):
                discover_managed_endpoint(
                    self.codex_home, proc_root=self.proc,
                    socket_prefix=str(self.root / "codex-daemon-"),
                )


class ProjectLoadingTest(unittest.TestCase):
    def test_deleted_worktree_is_skipped_without_exposing_its_path(self):
        with tempfile.TemporaryDirectory(prefix="codex-observer-projects-") as folder:
            root = Path(folder)
            available = root / "available"
            available.mkdir()
            missing = root / "private-deleted-worktree"
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": {
                "available": str(available),
                "removed-worktree": str(missing),
            }}), encoding="utf-8")
            original = config.read_bytes()
            warning = io.StringIO()

            try:
                with contextlib.redirect_stderr(warning):
                    loaded = _load_projects(config)
            except RuntimeError as exc:
                self.fail(f"A deleted worktree must be skipped: {exc}")

            self.assertEqual(loaded, {"available": available.resolve()})
            self.assertEqual(config.read_bytes(), original)
            self.assertIn("removed-worktree", warning.getvalue())
            self.assertIn("DIRECTORY_MISSING", warning.getvalue())
            self.assertNotIn(str(missing), warning.getvalue())

    def test_malformed_relative_file_permission_and_invalid_json_still_fail(self):
        with tempfile.TemporaryDirectory(prefix="codex-observer-projects-") as folder:
            root = Path(folder)
            available = root / "available"
            available.mkdir()
            ordinary_file = root / "ordinary-file"
            ordinary_file.write_text("not a directory", encoding="utf-8")
            unreadable = root / "unreadable"
            unreadable_child = unreadable / "child"
            unreadable_child.mkdir(parents=True)
            unreadable.chmod(0)
            config = root / "projects.json"
            cases = (
                {"projects": {"available": str(available), "../invalid": str(root / "missing")}},
                {"projects": {"available": str(available), "relative": "relative/path"}},
                {"projects": {"available": str(available), "file": str(ordinary_file)}},
                {"projects": {"available": str(available), "invalid": None}},
                {"projects": {
                    "available": str(available), "permission": str(unreadable_child),
                }},
            )
            try:
                for data in cases:
                    with self.subTest(data=data):
                        config.write_text(json.dumps(data), encoding="utf-8")
                        with self.assertRaises(RuntimeError):
                            _load_projects(config)
                config.write_text("{invalid", encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    _load_projects(config)
            finally:
                unreadable.chmod(0o700)

    def test_all_missing_projects_cannot_start(self):
        with tempfile.TemporaryDirectory(prefix="codex-observer-projects-") as folder:
            root = Path(folder)
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": {
                "first": str(root / "first-missing"),
                "second": str(root / "second-missing"),
            }}), encoding="utf-8")

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(
                    RuntimeError, "No configured project directories are available",
                ):
                    _load_projects(config)


class MainDiagnosticTest(unittest.TestCase):
    def test_known_failure_logs_allowlisted_code_and_exception_class(self):
        async def fail(_args):
            raise RuntimeError("Invalid projects configuration")

        stderr = io.StringIO()
        with mock.patch("codex_observer._run", new=fail), contextlib.redirect_stderr(stderr):
            result = main([])

        self.assertEqual(result, 1)
        self.assertIn("error=RuntimeError", stderr.getvalue())
        self.assertIn("code=PROJECT_CONFIGURATION_INVALID", stderr.getvalue())

    def test_each_known_failure_has_its_public_code_without_raw_message(self):
        cases = (
            ("Invalid projects configuration", "PROJECT_CONFIGURATION_INVALID"),
            ("No configured project directories are available", "PROJECT_DIRECTORIES_UNAVAILABLE"),
            ("Invalid managed PID file", "MANAGED_PID_UNAVAILABLE"),
            ("Invalid managed Codex control listener", "CONTROL_LISTENER_INVALID"),
        )
        for message, expected_code in cases:
            with self.subTest(message=message):
                async def fail(_args, raw_message=message):
                    raise RuntimeError(raw_message)

                stderr = io.StringIO()
                with mock.patch("codex_observer._run", new=fail), \
                        contextlib.redirect_stderr(stderr):
                    result = main([])

                self.assertEqual(result, 1)
                self.assertIn("error=RuntimeError", stderr.getvalue())
                self.assertIn(f"code={expected_code}", stderr.getvalue())
                self.assertNotIn(message, stderr.getvalue())

    def test_unknown_failure_uses_generic_code_and_suppresses_raw_message(self):
        private_message = "private path /secret/worktree and token value"

        async def fail(_args):
            raise RuntimeError(private_message)

        stderr = io.StringIO()
        with mock.patch("codex_observer._run", new=fail), contextlib.redirect_stderr(stderr):
            result = main([])

        self.assertEqual(result, 1)
        self.assertIn("error=RuntimeError", stderr.getvalue())
        self.assertIn("code=OBSERVER_FAILED", stderr.getvalue())
        self.assertNotIn(private_message, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
