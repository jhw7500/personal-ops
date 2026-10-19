import asyncio
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
    discover_managed_endpoint,
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
        (self.proc / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"0000: 00000002 00000000 00010000 0001 01 4242 {self.socket_path}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.listener.close()
        self.temporary.cleanup()

    def test_discovers_one_stable_user_owned_listener(self):
        endpoint = discover_managed_endpoint(
            self.codex_home, proc_root=self.proc, socket_prefix=str(self.root / "codex-daemon-"),
        )

        self.assertEqual(endpoint, self.socket_path)
        self.assertTrue(stat.S_ISSOCK(endpoint.lstat().st_mode))

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


if __name__ == "__main__":
    unittest.main()
