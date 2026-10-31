"""Real bubblewrap proof of snapshot inputs and private command writes."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from execution import ExecutionPolicy
from jobs import JobManager
from workspace_paths import WorkspaceError
from workspace_tools import WorkspaceTools


class WorkspaceExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-exec-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = root / "projects/alpha"
        self.project.mkdir(parents=True)
        self.project.joinpath("source.txt").write_text("original")
        self.project.joinpath(".env").write_text("synthetic-real-host-secret")
        self.owner = root / "owner"
        self.owner.mkdir(mode=0o700)
        policy_file = self.owner / "workspace-policy.json"
        policy_file.write_text(json.dumps({"version": 1, "base_root": str(self.project.parent), "artifacts": []}))
        policy_file.chmod(0o600)
        self.tools = WorkspaceTools({"alpha": self.project}, self.owner / "authority", config_path=policy_file)
        self.tools.authority.initialize(self.tools.policy.load(required=True).digest, {"a" * 64: 1})

    def test_ro_snapshot_and_scratch_never_write_host_source(self):
        policy = ExecutionPolicy({"alpha": self.project}, self.owner / "missing-execution.json",
                                 workspace=self.tools)
        if not policy._bwrap:
            self.skipTest("bubblewrap unavailable")
        spec = policy.prepare("alpha", argv=["bash", "-c",
            "set -eu; test \"$(cat source.txt)\" = original; test ! -e .env; "
            "if printf changed > source.txt 2>/dev/null; then exit 81; fi; "
            "printf scratch > /scratch/scratch.txt; printf result > /output/result.txt; "
            "test ! -e late-secret.txt"])
        self.project.joinpath("late-secret.txt").write_text("created after snapshot")
        completed = subprocess.run(spec.command, cwd=spec.cwd, env=spec.env,
                                   capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(self.project.joinpath("source.txt").read_text(), "original")
        self.assertFalse(self.project.joinpath("result.txt").exists())
        self.assertEqual(Path(spec.workspace["output"]).joinpath("result.txt").read_text(), "result")

    def test_writable_cwd_is_a_private_copy_not_the_host_repo(self):
        policy = ExecutionPolicy({"alpha": self.project}, self.owner / "missing-execution.json",
                                 workspace=self.tools)
        if not policy._bwrap:
            self.skipTest("bubblewrap unavailable")
        spec = policy.prepare("alpha", argv=["bash", "-c", "set -eu; printf edited > source.txt; printf new > added.txt"],
                              writable_cwd=True)
        completed = subprocess.run(spec.command, cwd=spec.cwd, env=spec.env,
                                   capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(self.project.joinpath("source.txt").read_text(), "original")
        self.assertEqual(Path(spec.workspace["scratch"]).joinpath("workspace/source.txt").read_text(), "edited")
        self.assertFalse(self.project.joinpath("added.txt").exists())
        self.assertEqual(Path(spec.workspace["scratch"]).joinpath("workspace/added.txt").read_text(), "new")


class WorkspaceJobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workspace-lifecycle-")
        root = Path(self.temporary.name)
        self.project = root / "projects/alpha"
        self.project.mkdir(parents=True)
        self.project.joinpath("source.txt").write_text("original")
        self.owner = root / "owner"
        self.owner.mkdir(mode=0o700)
        config = self.owner / "workspace-policy.json"
        config.write_text(json.dumps({"version": 1, "base_root": str(self.project.parent), "artifacts": []}))
        config.chmod(0o600)
        self.tools = WorkspaceTools({"alpha": self.project}, self.owner / "authority", config_path=config)
        self.tools.authority.initialize(self.tools.policy.load(required=True).digest, {"a" * 64: 1})
        policy = ExecutionPolicy({"alpha": self.project}, self.owner / "missing.json", workspace=self.tools)
        if not policy._bwrap:
            self.temporary.cleanup()
            self.skipTest("bubblewrap unavailable")
        self.manager = JobManager({"alpha": self.project}, policy, self.owner / "jobs")

    async def asyncTearDown(self):
        if hasattr(self, "manager"):
            await self.manager.close()
        self.temporary.cleanup()

    async def finish(self, job):
        task = self.manager.tasks.get(job["job_id"])
        if task:
            await asyncio.wait_for(asyncio.shield(task), 10)
        return self.manager.get("alpha", job["job_id"])

    async def test_completed_output_requires_explicit_cas_publication_and_hides_private_paths(self):
        job = await self.manager.start("alpha", request_id="output", argv=[
            "bash", "-c", "printf edited > /output/result.txt"], writable_cwd=True)
        self.assertNotIn(str(self.owner), json.dumps(job))
        with self.assertRaisesRegex(WorkspaceError, "JOB_INCOMPLETE"):
            await self.manager.publish_output("alpha", job["job_id"], "result.txt", "source.txt", "output",
                                              expected_sha256=hashlib.sha256(b"original").hexdigest())
        final = await self.finish(job)
        self.assertEqual(final["state"], "succeeded", final)
        self.assertTrue(final["termination_verified"])
        self.assertEqual(self.project.joinpath("source.txt").read_text(), "original")
        # A completed job exports a sealed copy, even if mutable staging changes.
        private = self.manager.jobs[job["job_id"]]["_workspace"]
        Path(private["output"]).joinpath("result.txt").write_text("tampered staging")
        output = self.manager.read_output("alpha", job["job_id"], "result.txt")
        self.assertEqual(output["content"], "edited")
        digest = hashlib.sha256(b"original").hexdigest()
        receipt = await self.manager.publish_output("alpha", job["job_id"], "result.txt", "source.txt", "output",
                                                    expected_sha256=digest)
        retry = await self.manager.publish_output("alpha", job["job_id"], "result.txt", "source.txt", "output",
                                                  expected_sha256=digest)
        self.assertEqual(receipt, retry)
        self.assertEqual(self.project.joinpath("source.txt").read_text(), "edited")
        self.assertNotIn(str(self.owner), json.dumps(self.manager.list("alpha")))

    async def test_epoch_change_before_launch_prevents_execution_and_running_job_is_stopped(self):
        job = await self.manager.start("alpha", request_id="queued", argv=["true"])
        self.tools.authority.advance(self.tools.policy.load(required=True).digest)
        final = await self.finish(job)
        self.assertEqual(final["state"], "failed", final)
        self.assertEqual(final["error"], "JOB_STALE")
        self.assertIsNone(final["started_at"])
        running = await self.manager.start("alpha", request_id="running", argv=[
            "bash", "-c", "printf result > /output/result.txt; sleep 30"])
        for _ in range(100):
            if self.manager.get("alpha", running["job_id"])["state"] == "running":
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.manager.get("alpha", running["job_id"])["state"], "running")
        self.tools.authority.advance(self.tools.policy.load(required=True).digest)
        final = await self.finish(running)
        self.assertEqual(final["state"], "failed", final)
        self.assertEqual(final["error"], "JOB_STALE")
        with self.assertRaisesRegex(WorkspaceError, "JOB_"):
            await self.manager.publish_output("alpha", running["job_id"], "result.txt", "new.txt", "running")
        self.assertFalse(self.project.joinpath("new.txt").exists())

    async def test_host_lane_blocks_publication_and_canceled_job_never_publishes(self):
        from host_runtime_config import HostRuntimeError
        from workspace_host import host_operation_lane
        job = await self.manager.start("alpha", request_id="lane", argv=[
            "bash", "-c", "printf result > /output/result.txt"])
        await self.finish(job)
        with host_operation_lane(self.tools):
            with self.assertRaisesRegex(HostRuntimeError, "WORKSPACE_HOST_LANE_BUSY"):
                await self.manager.publish_output("alpha", job["job_id"], "result.txt", "new.txt", "lane")
        canceled = await self.manager.start("alpha", request_id="cancel", argv=["sleep", "30"])
        await self.manager.cancel("alpha", canceled["job_id"])
        with self.assertRaisesRegex(WorkspaceError, "JOB_INCOMPLETE"):
            await self.manager.publish_output("alpha", canceled["job_id"], "result.txt", "new.txt", "cancel")

    async def test_granted_input_is_a_snapshot_and_revocation_stops_only_dependent_job(self):
        outside = self.owner.parent / "outside.txt"
        outside.write_text("approved input")
        digest = self.tools.policy.load(required=True).digest
        request = self.tools.authority.request("alpha", str(outside), ["read"], 1800,
                                               "synthetic command input", digest)
        grant = self.tools.authority.confirm("alpha", request["request_id"], request["_confirmation"], digest)
        spec = self.manager.policy.prepare("alpha", argv=["cat", "/inputs/0/input"],
            workspace_inputs=[{"path": str(outside), "grant_id": grant["grant_id"]}])
        outside.write_text("changed original")
        actual = subprocess.run(spec.command, cwd=spec.cwd, env=spec.env, capture_output=True, timeout=10)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, b"approved input")
        job = await self.manager.start("alpha", request_id="granted", argv=["sleep", "30"],
            workspace_inputs=[{"path": str(outside), "grant_id": grant["grant_id"]}])
        for _ in range(100):
            if self.manager.get("alpha", job["job_id"])["state"] == "running":
                break
            await asyncio.sleep(.02)
        self.tools.authority.revoke("alpha", grant["grant_id"], digest)
        final = await self.finish(job)
        self.assertEqual(final["state"], "failed", final)
        self.assertNotIn(str(outside), json.dumps(final))
        ordinary = await self.manager.start("alpha", request_id="ordinary-after-revoke", argv=["true"])
        self.assertEqual((await self.finish(ordinary))["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
