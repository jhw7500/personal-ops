import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace_authority import WorkspaceAuthority  # noqa: E402
from workspace_paths import WorkspaceError  # noqa: E402


POLICY_A = "a" * 64
POLICY_B = "b" * 64
RELEASE_A = "c" * 64


class WorkspaceAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "projects" / "alpha"
        self.project.mkdir(parents=True)
        self.state = self.root / "private-authority"
        self.outside = self.root / "external"
        self.outside.mkdir()
        self.file = self.outside / "result.json"
        self.file.write_text("{}", encoding="utf-8")
        self.authority = WorkspaceAuthority({"alpha": self.project}, self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, operation):
        with self.assertRaises(WorkspaceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def initialize(self):
        return self.authority.initialize(POLICY_A, {RELEASE_A: 1})

    def minimum_abi_path(self):
        return self.state.parent / ("." + self.state.name + ".minimum-enforcement-abi")

    def write_minimum_abi(self, minimum_abi):
        path = self.minimum_abi_path()
        path.write_text(json.dumps({
            "version": 1,
            "authority_path_sha256": hashlib.sha256(
                str(self.state.absolute()).encode()).hexdigest(),
            "minimum_abi": minimum_abi,
        }, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        return path

    def approved(self, *, path=None, operations=("read",), duration=1800):
        request = self.authority.request(
            "alpha", str(path or self.file), list(operations), duration,
            "inspect test result", POLICY_A,
        )
        grant = self.authority.confirm(
            "alpha", request["request_id"], request["_confirmation"], POLICY_A,
        )
        return request, grant

    def test_constructor_does_not_initialize_and_uninitialized_operations_fail(self):
        self.assertFalse(self.state.exists())
        self.assert_code("AUTHORITY_UNINITIALIZED", lambda: self.authority.binding(POLICY_A))
        self.assertFalse(self.state.exists())

    def test_writers_from_different_projects_share_external_target_lock_domain(self):
        beta = self.project.parent / "beta"
        beta.mkdir()
        authority = WorkspaceAuthority({"alpha": self.project, "beta": beta}, self.state)
        authority.initialize(POLICY_A, {RELEASE_A: 1})
        attempted, entered = threading.Event(), threading.Event()

        def writer():
            attempted.set()
            with authority.project_lock("beta"):
                entered.set()

        thread = threading.Thread(target=writer)
        try:
            with authority.project_lock("alpha"):
                thread.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(entered.wait(.1), "Cross-project writers can alias a granted external target")
        finally:
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(entered.is_set())

    def test_initialize_creates_private_installation_state_outside_projects(self):
        binding = self.initialize()

        self.assertRegex(binding["installation_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(binding["authority_epoch"], 1)
        self.assertEqual(binding["policy_digest"], POLICY_A)
        self.assertEqual(self.authority.cursor_key(POLICY_A), self.authority.cursor_key(POLICY_A))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for entry in self.state.rglob("*"):
            expected = 0o700 if entry.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), expected, entry)

        nested = WorkspaceAuthority({"alpha": self.project}, self.project / ".authority")
        self.assert_code(
            "AUTHORITY_STATE_IN_PROJECT",
            lambda: nested.initialize(POLICY_A, {RELEASE_A: 1}),
        )

    def test_initialize_creates_default_minimum_abi_before_enforcement(self):
        self.initialize()

        pin = self.minimum_abi_path()
        self.assertEqual(stat.S_IMODE(pin.stat().st_mode), 0o600)
        self.assertEqual(pin.stat().st_nlink, 1)
        self.assertEqual(json.loads(pin.read_text(encoding="utf-8")), {
            "version": 1,
            "authority_path_sha256": hashlib.sha256(
                str(self.state.absolute()).encode()).hexdigest(),
            "minimum_abi": 1,
        })

    def test_initialize_can_explicitly_create_abi_three_floor(self):
        self.authority.initialize(
            POLICY_A, {RELEASE_A: 3}, minimum_abi=3,
        )

        self.assertEqual(json.loads(
            self.minimum_abi_path().read_text(encoding="utf-8"),
        )["minimum_abi"], 3)

    def test_initialize_rejects_unknown_future_minimum_abi(self):
        self.assert_code(
            "INVALID_MINIMUM_ABI",
            lambda: self.authority.initialize(
                POLICY_A, {RELEASE_A: 4}, minimum_abi=4,
            ),
        )
        self.assertFalse(self.authority.enforcement_path.exists())

    def test_initialize_preserves_existing_floor_and_rejects_lower_release_abi(self):
        pin = self.write_minimum_abi(2)
        original = pin.read_bytes()

        pin.chmod(0o644)
        self.assert_code(
            "UNSAFE_AUTHORITY_STATE",
            lambda: self.authority.initialize(POLICY_A, {RELEASE_A: 2}),
        )
        self.assertFalse(self.authority.enforcement_path.exists())
        pin.chmod(0o600)

        self.assert_code(
            "INVALID_ALLOWED_RELEASES",
            lambda: self.authority.initialize(POLICY_A, {RELEASE_A: 1}),
        )
        self.assertEqual(pin.read_bytes(), original)
        self.assertFalse(self.authority.enforcement_path.exists())

        self.authority.initialize(POLICY_A, {RELEASE_A: 2})
        self.assertEqual(pin.read_bytes(), original)

    def test_commit_initial_creates_default_floor_and_missing_latched_floor_is_fatal(self):
        transaction = "d" * 32
        prepared = self.authority.prepare_initial(
            POLICY_A, {RELEASE_A: 1}, transaction)

        self.authority.commit_initial(prepared)
        pin = self.minimum_abi_path()
        self.assertTrue(pin.is_file())
        pin.unlink()

        self.assert_code(
            "AUTHORITY_MINIMUM_ABI_MISSING",
            lambda: self.authority.commit_initial(prepared),
        )

    def test_default_state_path_creates_private_parents_under_xdg_state(self):
        xdg = self.root / "new-xdg-state"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}):
            authority = WorkspaceAuthority({"alpha": self.project})
        authority.initialize(POLICY_A, {RELEASE_A: 1})

        self.assertEqual(stat.S_IMODE(xdg.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((xdg / "chatgpt-coding-mcp").stat().st_mode), 0o700,
        )

    def test_file_grant_is_bound_to_project_path_operation_and_duration(self):
        self.initialize()
        request, grant = self.approved(duration=37)

        self.assertEqual(set(request), {
            "request_id", "project", "path", "scope_kind", "operations",
            "duration_seconds", "purpose", "requested_at", "request_expires_at",
            "authority_epoch", "policy_digest", "_confirmation",
        })
        self.assertEqual(request["duration_seconds"], 37)
        self.assertEqual(request["scope_kind"], "file")
        self.assertNotIn("_confirmation", grant)
        allowed = self.authority.require(
            "alpha", str(self.file), "read", grant["grant_id"], POLICY_A,
        )
        self.assertEqual(allowed["path"], str(self.file))
        self.assert_code(
            "GRANT_SCOPE_MISMATCH",
            lambda: self.authority.require(
                "alpha", str(self.outside / "sibling.json"), "read",
                grant["grant_id"], POLICY_A,
            ),
        )
        self.assert_code(
            "GRANT_OPERATION_DENIED",
            lambda: self.authority.require(
                "alpha", str(self.file), "write", grant["grant_id"], POLICY_A,
            ),
        )
        self.assert_code(
            "GRANT_PROJECT_MISMATCH",
            lambda: self.authority.require(
                "other", str(self.file), "read", grant["grant_id"], POLICY_A,
            ),
        )

    def test_directory_grant_covers_descendants_but_not_prefix_siblings(self):
        self.initialize()
        child = self.outside / "nested" / "value.json"
        child.parent.mkdir()
        child.write_text("{}", encoding="utf-8")
        sibling = self.root / "external-other" / "value.json"
        sibling.parent.mkdir()
        sibling.write_text("{}", encoding="utf-8")
        _, grant = self.approved(path=self.outside, operations=("list", "read"))

        self.authority.require("alpha", str(child), "read", grant["grant_id"], POLICY_A)
        self.assert_code(
            "GRANT_SCOPE_MISMATCH",
            lambda: self.authority.require(
                "alpha", str(sibling), "read", grant["grant_id"], POLICY_A,
            ),
        )
        escape = self.outside / "escape"
        escape.symlink_to(sibling.parent, target_is_directory=True)
        self.assert_code(
            "GRANT_SCOPE_MISMATCH",
            lambda: self.authority.require(
                "alpha", str(escape / "value.json"), "read",
                grant["grant_id"], POLICY_A,
            ),
        )

    def test_request_rejects_unbounded_or_unrelated_authority(self):
        self.initialize()
        for duration in (0, 2592001, True):
            self.assert_code(
                "INVALID_GRANT_DURATION",
                lambda duration=duration: self.authority.request(
                    "alpha", str(self.file), ["read"], duration, "purpose", POLICY_A,
                ),
            )
        for operations in (["execute"], ["network"], ["read", "read"], []):
            self.assert_code(
                "INVALID_GRANT_OPERATIONS",
                lambda operations=operations: self.authority.request(
                    "alpha", str(self.file), operations, 1800, "purpose", POLICY_A,
                ),
            )
        self.assert_code(
            "PROTECTED_AUTHORITY_PATH",
            lambda: self.authority.request(
                "alpha", str(self.state / "authority.json"), ["read"], 1800,
                "purpose", POLICY_A,
            ),
        )

    def test_confirmation_capability_is_single_use_and_request_bound(self):
        self.initialize()
        first = self.authority.request(
            "alpha", str(self.file), ["read"], 1800, "first", POLICY_A,
        )
        second = self.authority.request(
            "alpha", str(self.file), ["read"], 1800, "second", POLICY_A,
        )
        self.assert_code(
            "CONFIRMATION_INVALID",
            lambda: self.authority.confirm(
                "alpha", second["request_id"], first["_confirmation"], POLICY_A,
            ),
        )
        self.authority.confirm(
            "alpha", first["request_id"], first["_confirmation"], POLICY_A,
        )
        self.assert_code(
            "CONFIRMATION_REPLAYED",
            lambda: self.authority.confirm(
                "alpha", first["request_id"], first["_confirmation"], POLICY_A,
            ),
        )

    def test_get_is_public_and_cancel_consumes_the_pending_capability(self):
        self.initialize()
        request = self.authority.request(
            "alpha", str(self.file), ["read"], None, "cancel me", POLICY_A,
        )

        public = self.authority.get("alpha", request["request_id"])
        self.assertNotIn("_confirmation", public)
        self.assertNotIn("confirmation_digest", repr(public))
        self.assertEqual(public["status"], "pending")
        self.assertEqual(request["duration_seconds"], 2592000)
        cancelled = self.authority.cancel(
            "alpha", request["request_id"], request["_confirmation"], POLICY_A,
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assert_code(
            "REQUEST_CANCELLED",
            lambda: self.authority.confirm(
                "alpha", request["request_id"], request["_confirmation"], POLICY_A,
            ),
        )
        self.assert_code(
            "CONFIRMATION_REPLAYED",
            lambda: self.authority.cancel(
                "alpha", request["request_id"], request["_confirmation"], POLICY_A,
            ),
        )

    def test_concurrent_confirmation_issues_exactly_one_grant(self):
        self.initialize()
        request = self.authority.request(
            "alpha", str(self.file), ["read"], 1800, "race", POLICY_A,
        )

        def confirm():
            try:
                return self.authority.confirm(
                    "alpha", request["request_id"], request["_confirmation"], POLICY_A,
                )["grant_id"]
            except WorkspaceError as exc:
                return exc.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: confirm(), range(8)))

        self.assertEqual(sum(value == "CONFIRMATION_REPLAYED" for value in outcomes), 7)
        self.assertEqual(sum(len(value) == 32 for value in outcomes), 1)

    def test_grant_expiry_and_revocation_are_enforced(self):
        with mock.patch("workspace_authority.time.time", return_value=1000.0):
            self.initialize()
            _, grant = self.approved(duration=1)
        with mock.patch("workspace_authority.time.time", return_value=1001.01):
            self.assert_code(
                "GRANT_EXPIRED",
                lambda: self.authority.require(
                    "alpha", str(self.file), "read", grant["grant_id"], POLICY_A,
                ),
            )

        _, current = self.approved()
        tombstone = self.authority.revoke("alpha", current["grant_id"], POLICY_A)
        self.assertEqual(tombstone["status"], "revoked")
        self.assert_code(
            "GRANT_REVOKED",
            lambda: self.authority.require(
                "alpha", str(self.file), "read", current["grant_id"], POLICY_A,
            ),
        )

    def test_pending_request_can_be_confirmed_after_six_days(self):
        requested_at = 1000.0
        with mock.patch("workspace_authority.time.time", return_value=requested_at):
            self.initialize()
            request = self.authority.request(
                "alpha", str(self.file), ["read"], None, "long review", POLICY_A,
            )

        self.assertEqual(
            request["request_expires_at"],
            "1970-01-08T00:16:40+00:00",
        )
        with mock.patch(
            "workspace_authority.time.time", return_value=requested_at + 6 * 86400,
        ):
            grant = self.authority.confirm(
                "alpha", request["request_id"], request["_confirmation"], POLICY_A,
            )
        self.assertEqual(grant["status"], "active")

    def test_default_grant_works_after_29_days_and_expires_at_30_days(self):
        issued_at = 1000.0
        with mock.patch("workspace_authority.time.time", return_value=issued_at):
            self.initialize()
            request = self.authority.request(
                "alpha", str(self.file), ["read"], None, "month access", POLICY_A,
            )
            grant = self.authority.confirm(
                "alpha", request["request_id"], request["_confirmation"], POLICY_A,
            )

        with mock.patch(
            "workspace_authority.time.time", return_value=issued_at + 29 * 86400,
        ):
            allowed = self.authority.require(
                "alpha", str(self.file), "read", grant["grant_id"], POLICY_A,
            )
        self.assertEqual(allowed["grant_id"], grant["grant_id"])
        with mock.patch(
            "workspace_authority.time.time", return_value=issued_at + 30 * 86400,
        ):
            self.assert_code(
                "GRANT_EXPIRED",
                lambda: self.authority.require(
                    "alpha", str(self.file), "read", grant["grant_id"], POLICY_A,
                ),
            )

    def test_advance_is_monotonic_and_tombstones_pending_and_grants(self):
        first = self.initialize()
        pending = self.authority.request(
            "alpha", str(self.file), ["read"], 1800, "pending", POLICY_A,
        )
        _, grant = self.approved()
        key = self.authority.cursor_key(POLICY_A)

        advanced = self.authority.advance(POLICY_B, {RELEASE_A: 2})
        self.assertEqual(advanced["installation_id"], first["installation_id"])
        self.assertEqual(advanced["authority_epoch"], 2)
        self.assertEqual(self.authority.cursor_key(POLICY_B), key)
        self.assert_code(
            "AUTHORITY_BINDING_STALE",
            lambda: self.authority.require(
                "alpha", str(self.file), "read", grant["grant_id"], POLICY_A,
            ),
        )
        self.assert_code(
            "REQUEST_TOMBSTONED",
            lambda: self.authority.confirm(
                "alpha", pending["request_id"], pending["_confirmation"], POLICY_B,
            ),
        )
        returned = self.authority.advance(POLICY_A)
        self.assertEqual(returned["authority_epoch"], 3)

    def test_advance_rejects_release_abi_below_existing_floor(self):
        self.write_minimum_abi(2)
        self.authority.initialize(POLICY_A, {RELEASE_A: 2})

        self.assert_code(
            "INVALID_ALLOWED_RELEASES",
            lambda: self.authority.advance(POLICY_B, {RELEASE_A: 1}),
        )

    def test_corrupt_missing_or_unsafe_activated_state_fails_closed(self):
        self.initialize()
        authority_file = self.state / "authority.json"
        authority_file.write_text("not-json", encoding="utf-8")
        self.assert_code("AUTHORITY_STATE_INVALID", lambda: self.authority.binding(POLICY_A))
        self.assert_code(
            "AUTHORITY_ALREADY_INITIALIZED",
            lambda: self.authority.initialize(POLICY_A, {RELEASE_A: 1}),
        )

        authority_file.write_text("{}", encoding="utf-8")
        os.chmod(authority_file, 0o644)
        self.assert_code("UNSAFE_AUTHORITY_STATE", lambda: self.authority.binding(POLICY_A))

        os.chmod(authority_file, 0o600)
        authority_file.unlink()
        self.assert_code("AUTHORITY_STATE_MISSING", lambda: self.authority.binding(POLICY_A))

    def test_nested_corruption_and_unsafe_fixed_lock_fail_closed(self):
        self.initialize()
        authority_file = self.state / "authority.json"
        payload = json.loads(authority_file.read_text(encoding="utf-8"))
        payload["requests"] = {"bad": {}}
        authority_file.write_text(json.dumps(payload), encoding="utf-8")
        self.assert_code("AUTHORITY_STATE_INVALID", lambda: self.authority.get("alpha"))

        payload["requests"] = {}
        authority_file.write_text(json.dumps(payload), encoding="utf-8")
        lock_file = self.state / "authority.lock"
        os.chmod(lock_file, 0o644)
        self.assert_code("UNSAFE_AUTHORITY_STATE", lambda: self.authority.binding(POLICY_A))
        self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o644)

    def test_missing_fixed_global_lock_is_not_silently_recreated(self):
        self.initialize()
        lock_file = self.state / "authority.lock"
        lock_file.unlink()

        self.assert_code("AUTHORITY_STATE_MISSING", lambda: self.authority.binding(POLICY_A))
        self.assertFalse(lock_file.exists())

    def test_enforcement_latch_survives_root_deletion_and_blocks_reinitialize(self):
        self.initialize()
        latch = self.authority.enforcement_path
        self.assertTrue(latch.is_file())
        self.assertEqual(stat.S_IMODE(latch.stat().st_mode), 0o600)
        for child in sorted(self.state.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.state.rmdir()

        self.assert_code("AUTHORITY_STATE_MISSING", lambda: self.authority.binding(POLICY_A))
        self.assert_code(
            "AUTHORITY_ALREADY_INITIALIZED",
            lambda: self.authority.initialize(POLICY_A, {RELEASE_A: 1}),
        )

    def test_admission_holds_binding_and_project_lock_serializes(self):
        self.initialize()
        _, grant = self.approved()
        entered = threading.Event()

        def advance():
            entered.set()
            return self.authority.advance(POLICY_B)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.authority.admission(POLICY_A) as binding:
                self.assertEqual(self.authority.binding(POLICY_A), binding)
                self.assertEqual(len(self.authority.cursor_key(POLICY_A)), 32)
                self.assertEqual(
                    self.authority.require(
                        "alpha", str(self.file), "read", grant["grant_id"], POLICY_A,
                    )["grant_id"],
                    grant["grant_id"],
                )
                future = pool.submit(advance)
                entered.wait(1)
                self.assertFalse(future.done())
                self.assertEqual(binding["authority_epoch"], 1)
            self.assertEqual(future.result(timeout=1)["authority_epoch"], 2)

        order = []
        first_entered = threading.Event()
        release_first = threading.Event()

        def first():
            with self.authority.project_lock("alpha"):
                order.append("first")
                first_entered.set()
                release_first.wait(1)

        def second():
            first_entered.wait(1)
            with self.authority.project_lock("alpha"):
                order.append("second")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first)
            two = pool.submit(second)
            first_entered.wait(1)
            self.assertEqual(order, ["first"])
            release_first.set()
            one.result(timeout=1)
            two.result(timeout=1)
        self.assertEqual(order, ["first", "second"])

        with self.authority.project_lock("alpha"):
            with self.authority.admission(POLICY_B):
                self.assertEqual(self.authority.binding(POLICY_B)["authority_epoch"], 2)
            self.assert_code(
                "AUTHORITY_LOCK_ORDER",
                lambda: self.authority.request(
                    "alpha", str(self.file), ["read"], 1800, "bad nesting", POLICY_B,
                ),
            )
            self.assert_code(
                "AUTHORITY_LOCK_REENTRANT",
                lambda: self._enter_project_lock_again(),
            )

    def _enter_project_lock_again(self):
        with self.authority.project_lock("alpha"):
            self.fail("a project lock must not be re-entered")

    def test_project_lock_serializes_across_processes(self):
        self.initialize()
        script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from workspace_authority import WorkspaceAuthority
authority = WorkspaceAuthority({'alpha': Path(sys.argv[2])}, Path(sys.argv[3]))
with authority.project_lock('alpha'):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(ROOT), str(self.project), str(self.state)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), "locked")

        entered = threading.Event()

        def acquire():
            with self.authority.project_lock("alpha"):
                entered.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(acquire)
            self.assertFalse(entered.wait(0.05))
            child.stdin.write("release\n")
            child.stdin.flush()
            future.result(timeout=2)
        stdout, stderr = child.communicate(timeout=2)
        self.assertEqual((child.returncode, stdout, stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
