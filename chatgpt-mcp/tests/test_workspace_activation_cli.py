"""Owner activation CLI parsing and read-only construction tests."""
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import workspace_activation_cli as cli  # noqa: E402
from workspace_activation_host import ActivationHostError  # noqa: E402


class WorkspaceActivationCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.home.mkdir(mode=0o700)
        self.config.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.environment = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.state),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def _proc_fixture(self):
        proc = self.root / "proc"
        boot = proc / "sys/kernel/random"
        boot.mkdir(parents=True)
        (boot / "boot_id").write_text("12345678-1234-1234-1234-123456789abc\n")
        return proc

    def _proc_process(self, proc, pid="321", *, uid=None, start_ticks="777",
                      argv=None):
        process = proc / pid
        process.mkdir()
        uid = os.getuid() if uid is None else uid
        (process / "status").write_text(
            f"Name:\tworker\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        fields = ["0"] * 20
        fields[0] = "S"
        fields[19] = start_ticks
        (process / "stat").write_text(f"{pid} (worker) " + " ".join(fields))
        (process / "cmdline").write_bytes(
            argv or (b"/usr/bin/python3\0/runtime/runtime_rollout_worker.py\0"
                     + b"a" * 32 + b"\0"))
        return process

    def _create_activation_journal(self, transaction, source_digest, projects):
        roots = cli.OwnerRoots.from_environment(self.environment)
        roots.journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        roots.journal.parent.chmod(0o700)
        cli.ActivationJournal(roots.journal, projects).create(
            transaction, source_digest, {})
        return roots.journal

    def test_parser_has_bounded_owner_commands(self):
        parser = cli.build_parser()
        common = ["--transaction-id", "a" * 32]
        prepared = parser.parse_args([
            "prepare", *common, "--policy-candidate", "/candidate",
            "--policy-path", "/policy", "--projects-json", "/projects",
            "--target-release", "/target", "--rollback-release", "/rollback",
            "--target-abi", "3", "--rollback-abi", "2", "--minimum-abi", "2",
        ])
        self.assertEqual(prepared.command, "prepare")
        self.assertEqual(prepared.minimum_abi, 2)
        for command in ("status", "apply", "resume"):
            self.assertEqual(parser.parse_args([command, *common]).command, command)

    def test_status_reads_journal_without_constructing_mutating_adapters(self):
        projects_path = self.config / "chatgpt-coding-mcp" / "projects.json"
        project = self.root / "project"
        project.mkdir()
        self._write(projects_path, {"projects": {"alpha": str(project)}})
        transaction = "b" * 32
        self._create_activation_journal(transaction, "c" * 64, {"alpha": project})
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        output = io.StringIO()
        result = cli.main([
            "status", "--transaction-id", transaction,
            "--projects-json", str(projects_path),
        ], environ=self.environment, stdout=output)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["phase"], "prepared")
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(after, before)

    def test_status_survives_missing_projects_file_without_creating_directories(self):
        projects_path = self.config / "chatgpt-coding-mcp" / "projects.json"
        project = self.root / "project"
        project.mkdir()
        self._write(projects_path, {"projects": {"alpha": str(project)}})
        transaction = "9" * 32
        self._create_activation_journal(transaction, "a" * 64, {"alpha": project})
        projects_path.unlink()
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        output = io.StringIO()
        result = cli.main([
            "status", "--transaction-id", transaction,
            "--projects-json", str(projects_path),
        ], environ=self.environment, stdout=output)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["transaction_id"], transaction)
        self.assertEqual(
            sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)

    def test_status_survives_disappeared_mapped_directory(self):
        projects_path = self.config / "chatgpt-coding-mcp" / "projects.json"
        project = self.root / "project"
        project.mkdir()
        self._write(projects_path, {"projects": {"alpha": str(project)}})
        transaction = "8" * 32
        self._create_activation_journal(transaction, "b" * 64, {"alpha": project})
        project.rmdir()
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        output = io.StringIO()
        result = cli.main([
            "status", "--transaction-id", transaction,
            "--projects-json", str(projects_path),
        ], environ=self.environment, stdout=output)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["transaction_id"], transaction)
        self.assertEqual(
            sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)

    def test_projects_file_requires_exact_private_layout(self):
        projects_path = self.config / "chatgpt-coding-mcp" / "projects.json"
        project = self.root / "project"
        project.mkdir()
        self._write(projects_path, {"projects": {"alpha": str(project)}})
        projects_path.chmod(0o644)
        with self.assertRaises(cli.OwnerCliError) as caught:
            cli.load_projects(projects_path)
        self.assertEqual(caught.exception.code, "ACTIVATION_PROJECTS_UNSAFE")

    def test_inert_legacy_factory_requires_existing_exact_roots_and_creates_nothing(self):
        project = self.root / "project"
        project.mkdir()
        projects = {"alpha": project}
        roots = cli.OwnerRoots.from_environment(self.environment)
        before = set(self.root.rglob("*"))
        with self.assertRaises(cli.OwnerCliError) as caught:
            cli.build_legacy_reader(projects, roots)
        self.assertEqual(caught.exception.code, "ACTIVATION_LEGACY_LAYOUT_MISSING")
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_inert_legacy_factory_binds_existing_layout_without_writes(self):
        project = self.root / "project"
        project.mkdir()
        projects = {"tossApp": project}
        roots = cli.OwnerRoots.from_environment(self.environment)
        directories = (
            "host-access/records", "host-access/locks",
            "host-runtime/commands/plans", "host-runtime/commands/receipts",
            "host-runtime/commands/locks",
            "host-runtime/operations", "host-runtime/target-authorizations",
            "host-runtime/registered/operations", "host-runtime/registered/receipts",
            "host-runtime/registered/events", "host-runtime/registered/locks",
            "agent-tasks/tasks", "agent-tasks/receipts", "agent-tasks/leases",
            "host-executions/plans", "host-executions/locks/owners",
            "runtime-rollout-web/plans",
            "runtime-rollout-web/receipts",
        )
        roots.state_root.mkdir(mode=0o700, parents=True)
        roots.state_root.chmod(0o700)
        for relative in directories:
            path = roots.state_root / relative
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
            current = path.parent
            while current != roots.state_root:
                current.chmod(0o700)
                current = current.parent
        def snapshot():
            result = []
            for path in self.root.rglob("*"):
                info = path.stat()
                result.append((str(path.relative_to(self.root)), info.st_dev, info.st_ino,
                               info.st_mode, info.st_nlink, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns))
            return sorted(result)
        before = snapshot()
        reader = cli.build_legacy_reader(projects, roots)
        self.assertEqual(reader.inventory()["blockers"], [])
        after = snapshot()
        self.assertEqual(after, before)

    def test_status_missing_journal_error_is_json_and_exit_two(self):
        output = io.StringIO()
        result = cli.main([
            "status", "--transaction-id", "d" * 32,
            "--projects-json", str(self.root / "missing.json"),
        ], environ=self.environment, stdout=output)
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue()), {
            "error": "ACTIVATION_JOURNAL_MISSING", "status": "error",
        })

    def test_apply_and_resume_dispatch_only_after_explicit_command(self):
        projects_path = self.config / "chatgpt-coding-mcp" / "projects.json"
        project = self.root / "project"
        project.mkdir()
        self._write(projects_path, {"projects": {"alpha": str(project)}})
        coordinator = mock.Mock()
        coordinator.apply.side_effect = lambda ident: {"transaction_id": ident}
        for command in ("apply", "resume"):
            output = io.StringIO()
            with mock.patch.object(cli, "_coordinator", return_value=coordinator) as factory:
                result = cli.main([
                    command, "--transaction-id", "e" * 32,
                    "--projects-json", str(projects_path),
                ], environ=self.environment, stdout=output)
            self.assertEqual(result, 0)
            factory.assert_called_once()
            self.assertTrue(factory.call_args.kwargs["require_host"])
            self.assertEqual(json.loads(output.getvalue())["transaction_id"], "e" * 32)
        self.assertEqual(coordinator.apply.call_count, 2)

    def test_build_host_preserves_symlinked_venv_interpreter_identity(self):
        base = self.root / "python-base/bin/python3"
        base.parent.mkdir(parents=True)
        base.write_bytes(b"python")
        base.chmod(0o700)
        venv = self.root / "venv/bin/python"
        venv.parent.mkdir(parents=True)
        venv.symlink_to(base)
        roots = cli.OwnerRoots.from_environment({"HOME": str(self.home)})
        with mock.patch.object(cli.sys, "executable", str(venv)):
            host = cli.build_host({}, roots, mock.Mock(), ROOT)
        self.assertEqual(host.interpreter, venv.absolute())

    def test_build_host_leaves_unapproved_interpreter_rejection_to_adapter(self):
        unapproved = self.root / "unapproved/bin/python"
        unapproved.parent.mkdir(parents=True)
        unapproved.write_bytes(b"python")
        unapproved.chmod(0o700)
        roots = cli.OwnerRoots.from_environment({"HOME": str(self.home)})
        with mock.patch.object(cli.sys, "executable", str(unapproved)):
            host = cli.build_host({}, roots, mock.Mock(), ROOT)
        with (mock.patch.object(host, "_release", side_effect=[{}, {}]),
              mock.patch("workspace_activation_host.verify_controller", return_value={}),
              self.assertRaises(ActivationHostError) as caught):
            host.prepare(
                self.root / "target", self.root / "rollback",
                roots.authority, roots.controller,
            )
        self.assertEqual(caught.exception.code, "UNAPPROVED_MCP_INTERPRETER")

    def test_existing_terminal_lease_probe_is_read_only_and_blocks(self):
        folder = self.root / "leases"
        folder.mkdir(mode=0o700)
        path = folder / ("f" * 32 + ".lock")
        path.write_bytes(b"")
        path.chmod(0o600)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = path.stat()
            self.assertTrue(cli._lock_busy_readonly(path))
            self.assertEqual(cli._locked_directory(folder, "agent", cli._HEX32), [
                {"kind": "agent", "id": "f" * 32},
            ])
            after = path.stat()
        finally:
            os.close(descriptor)
        self.assertEqual(
            (after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
             after.st_mtime_ns, after.st_ctime_ns),
            (before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
             before.st_mtime_ns, before.st_ctime_ns),
        )

    def test_unverified_terminal_uncertainty_blocks_without_mutation(self):
        item = {"state": "outcome_unknown", "termination_verified": False,
                "execution_identity": None}
        before = json.dumps(item, sort_keys=True)
        self.assertTrue(cli._uncertain_identity_active(item))
        self.assertEqual(json.dumps(item, sort_keys=True), before)
        item["termination_verified"] = True
        self.assertFalse(cli._uncertain_identity_active(item))

    def test_known_worker_process_is_pinned_by_boot_pid_and_startticks(self):
        proc = self._proc_fixture()
        process = self._proc_process(proc)
        first = cli._known_worker_processes(proc)
        self.assertEqual(first, [{
            "kind": "rollout",
            "id": "pid:321:777:12345678-1234-1234-1234-123456789abc",
        }])
        fields = ["0"] * 20
        fields[0] = "S"
        fields[19] = "778"
        (process / "stat").write_text("321 (worker) " + " ".join(fields))
        second = cli._known_worker_processes(proc)
        self.assertNotEqual(second[0]["id"], first[0]["id"])

    def test_live_owner_process_with_unreadable_data_blocks(self):
        proc = self._proc_fixture()
        process = self._proc_process(proc)
        (process / "cmdline").unlink()
        (process / "cmdline").symlink_to("missing")
        with self.assertRaises(cli.OwnerCliError) as caught:
            cli._known_worker_processes(proc)
        self.assertEqual(caught.exception.code, "WORKER_PROCESS_UNSAFE")

    def test_live_owner_process_with_malformed_data_blocks(self):
        proc = self._proc_fixture()
        process = self._proc_process(proc)
        (process / "status").write_text("Name:\tworker\nUid:\tnot-a-uid\n")
        with self.assertRaises(cli.OwnerCliError) as caught:
            cli._known_worker_processes(proc)
        self.assertEqual(caught.exception.code, "WORKER_PROCESS_UNSAFE")

    def test_vanished_owner_process_is_tolerated(self):
        proc = self._proc_fixture()
        process = self._proc_process(proc)
        real_read = cli._read_proc_file

        def vanish_on_status(path, limit=65536):
            if Path(path) == process / "status":
                shutil.rmtree(process)
                raise cli.OwnerCliError("WORKER_PROCESS_UNSAFE")
            return real_read(path, limit)

        with mock.patch.object(cli, "_read_proc_file", side_effect=vanish_on_status):
            self.assertEqual(cli._known_worker_processes(proc), [])

    def test_changed_start_ticks_blocks_as_pid_reuse(self):
        proc = self._proc_fixture()
        process = self._proc_process(proc)
        real_read = cli._read_proc_file
        stat_reads = 0

        def change_ticks(path, limit=65536):
            nonlocal stat_reads
            data = real_read(path, limit)
            if Path(path) == process / "stat":
                stat_reads += 1
                if stat_reads == 1:
                    fields = ["0"] * 20
                    fields[0] = "S"
                    fields[19] = "778"
                    (process / "stat").write_text("321 (worker) " + " ".join(fields))
            return data

        with mock.patch.object(cli, "_read_proc_file", side_effect=change_ticks):
            with self.assertRaises(cli.OwnerCliError) as caught:
                cli._known_worker_processes(proc)
        self.assertEqual(caught.exception.code, "WORKER_PROCESS_UNSAFE")

    def test_unrelated_user_process_is_excluded(self):
        proc = self._proc_fixture()
        self._proc_process(proc, uid=os.getuid() + 1)
        self.assertEqual(cli._known_worker_processes(proc), [])


if __name__ == "__main__":
    unittest.main()
