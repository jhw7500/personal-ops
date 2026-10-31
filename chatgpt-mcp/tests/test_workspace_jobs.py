import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_authority import WorkspaceAuthority  # noqa: E402
import workspace_jobs  # noqa: E402
from workspace_jobs import WorkspaceJobFiles  # noqa: E402
from workspace_paths import WorkspaceError  # noqa: E402
from workspace_policy import WorkspacePolicy  # noqa: E402


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class WorkspaceJobFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.projects_root = self.base / "projects"
        self.repo = self.projects_root / "demo"
        self.repo.mkdir(parents=True)
        os.chmod(self.projects_root, 0o700)
        os.chmod(self.repo, 0o700)
        (self.repo / ".gitignore").write_text("build/\nnode_modules/\n", encoding="utf-8")
        (self.repo / "source.py").write_text("print('source')\n", encoding="utf-8")
        os.chmod(self.repo / "source.py", 0o755)
        (self.repo / ".env").write_text("TOKEN=must-not-copy\n", encoding="utf-8")
        (self.repo / "build").mkdir()
        (self.repo / "build/result.json").write_text('{"ignored":true}\n', encoding="utf-8")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/package.js").write_text("ignored\n", encoding="utf-8")

        self.policy_dir = self.base / "policy"
        self.policy_dir.mkdir(mode=0o700)
        self.policy_path = self.policy_dir / "workspace.json"
        self.policy_path.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.projects_root),
            "artifacts": [],
            "protected_paths": [str(self.repo / "protected")],
        }), encoding="utf-8")
        os.chmod(self.policy_path, 0o600)
        self.policy = WorkspacePolicy({"demo": self.repo}, self.policy_path)
        snapshot = self.policy.load(required=True)

        authority_parent = self.base / "authority-state"
        authority_parent.mkdir(mode=0o700)
        self.authority_dir = authority_parent / "authority"
        self.authority = WorkspaceAuthority(
            {"demo": self.repo}, state_dir=self.authority_dir)
        self.authority.initialize(snapshot.digest, {"f" * 64: 1})
        self.job_state = self.base / "job-state"
        self.jobs = WorkspaceJobFiles(self.policy, self.authority, self.job_state)

    def tearDown(self):
        self.temp.cleanup()

    def assert_code(self, code, call):
        with self.assertRaises(WorkspaceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def approved_read(self, path, *, duration=300):
        snapshot = self.policy.load(required=True)
        request = self.authority.request(
            "demo", str(path), ["read"], duration, "job input fixture",
            snapshot.digest)
        return self.authority.confirm(
            "demo", request["request_id"], request["_confirmation"],
            snapshot.digest)

    def test_create_freezes_a_filtered_private_snapshot(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.repo / "outside-link").symlink_to(outside)
        (self.repo / "hardlink-source").write_text("linked\n", encoding="utf-8")
        os.link(self.repo / "hardlink-source", self.repo / "hardlink.py")

        record = self.jobs.create("demo", "request-1")

        self.assertEqual(set(("id", "source", "scratch", "output", "binding",
                              "source_manifest")) - set(record), set())
        for field in ("source", "scratch", "output"):
            path = Path(record[field])
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_relative_to(self.job_state.resolve()))
        source = Path(record["source"])
        self.assertEqual((source / "source.py").read_text(encoding="utf-8"),
                         "print('source')\n")
        self.assertFalse((source / ".env").exists())
        self.assertFalse((source / "build").exists())
        self.assertFalse((source / "node_modules").exists())
        self.assertFalse((source / "outside-link").exists())
        self.assertFalse((source / "hardlink.py").exists())
        self.assertFalse((source / "hardlink-source").exists())
        self.assertEqual(stat.S_IMODE((source / "source.py").stat().st_mode), 0o500)
        manifest = {row["path"]: row for row in record["source_manifest"]}
        self.assertEqual(manifest["source.py"]["sha256"],
                         sha256(b"print('source')\n"))
        self.assertEqual(manifest["source.py"]["mode"], 0o755)

        (self.repo / "late-secret.txt").write_text("late\n", encoding="utf-8")
        self.assertFalse((source / "late-secret.txt").exists())
        (Path(record["scratch"]) / "source.py").write_text("scratch\n", encoding="utf-8")
        self.assertEqual((self.repo / "source.py").read_text(encoding="utf-8"),
                         "print('source')\n")

    def test_pending_publication_recovery_uses_original_frozen_bytes(self):
        record = self.jobs.create("demo", "crash-before-write")
        output = Path(record["output"]) / "result.txt"
        output.write_bytes(b"approved original")
        target = self.repo / "published.txt"
        with patch("workspace_jobs.write_file", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.publish("demo", record, "result.txt", str(target), "crash-before-write",
                                  completion_verified=True)
        output.write_bytes(b"mutated after pending receipt")
        receipt = self.jobs.publish("demo", record, "result.txt", str(target), "crash-before-write",
                                    completion_verified=True)
        self.assertEqual(target.read_bytes(), b"approved original")
        self.assertEqual(receipt["sha256"], sha256(b"approved original"))

    def test_create_copies_only_each_approved_input_to_a_private_stable_path(self):
        external = self.base / "external"
        external.mkdir()
        source = external / "selected.txt"
        source.write_bytes(b"approved input\n")
        (external / "sibling-secret.txt").write_bytes(b"not selected\n")
        grant = self.approved_read(source)

        record = self.jobs.create(
            "demo", "request-input", inputs=[{
                "path": str(source), "grant_id": grant["grant_id"],
            }])

        inputs_root = Path(record["inputs"])
        copied = inputs_root / "0" / "input"
        self.assertEqual(copied.read_bytes(), b"approved input\n")
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(copied.parent.stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE(inputs_root.stat().st_mode), 0o500)
        self.assertFalse((inputs_root / "0" / "sibling-secret.txt").exists())
        self.assertEqual(record["input_bindings"], [{
            "path": str(source),
            "grant_id": grant["grant_id"],
            "sha256": sha256(b"approved input\n"),
            "sandbox_path": "/inputs/0/input",
        }])
        self.assertNotIn("approved input", json.dumps(record))

        source.write_bytes(b"changed later\n")
        self.assertEqual(copied.read_bytes(), b"approved input\n")
        self.jobs.require_current(record)

    def test_create_validates_exact_input_schema_and_limits(self):
        source = self.base / "input.txt"
        source.write_text("input\n", encoding="utf-8")
        grant = self.approved_read(source)
        valid = {"path": str(source), "grant_id": grant["grant_id"]}

        cases = (
            None,
            [dict(valid, extra=True)],
            [{"path": "relative.txt", "grant_id": grant["grant_id"]}],
            [{"path": str(source), "grant_id": "F" * 32}],
            [valid] * 17,
        )
        for value in cases:
            with self.subTest(value=value):
                if value is None:
                    record = self.jobs.create("demo", "request-no-inputs")
                    self.assertNotIn("inputs", record)
                    self.assertNotIn("input_bindings", record)
                else:
                    self.assert_code(
                        "JOB_INPUTS_INVALID",
                        lambda value=value: self.jobs.create(
                            "demo", "request-invalid-input", inputs=value))

    def test_create_requires_a_read_grant_inside_workspace_and_rejects_unsafe_files(self):
        self.assert_code(
            "GRANT_UNKNOWN",
            lambda: self.jobs.create(
                "demo", "request-no-grant", inputs=[{
                    "path": str(self.repo / "source.py"),
                    "grant_id": "0" * 32,
                }]))

        external = self.base / "unsafe-input.txt"
        external.write_text("linked\n", encoding="utf-8")
        grant = self.approved_read(external)
        alias = self.base / "unsafe-input-alias.txt"
        os.link(external, alias)
        self.assert_code(
            "UNSAFE_FILE",
            lambda: self.jobs.create(
                "demo", "request-hardlink", inputs=[{
                    "path": str(external), "grant_id": grant["grant_id"],
                }]))

        symlink_input = self.base / "symlink-input.txt"
        symlink_input.write_text("target\n", encoding="utf-8")
        symlink_grant = self.approved_read(symlink_input)
        symlink_target = self.base / "symlink-target.txt"
        symlink_input.rename(symlink_target)
        symlink_input.symlink_to(symlink_target)
        self.assert_code(
            "UNSAFE_PATH",
            lambda: self.jobs.create(
                "demo", "request-symlink", inputs=[{
                    "path": str(symlink_input),
                    "grant_id": symlink_grant["grant_id"],
                }]))

        oversized = self.base / "oversized-input.txt"
        oversized.write_bytes(b"x" * (workspace_jobs.MAX_FILE_BYTES + 1))
        oversized_grant = self.approved_read(oversized)
        self.assert_code(
            "FILE_TOO_LARGE",
            lambda: self.jobs.create(
                "demo", "request-oversized", inputs=[{
                    "path": str(oversized),
                    "grant_id": oversized_grant["grant_id"],
                }]))

    def test_require_current_rechecks_only_the_recorded_input_grants(self):
        first = self.base / "first.txt"
        second = self.base / "second.txt"
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")
        first_grant = self.approved_read(first)
        second_grant = self.approved_read(second)
        first_record = self.jobs.create(
            "demo", "request-first", inputs=[{
                "path": str(first), "grant_id": first_grant["grant_id"],
            }])
        second_record = self.jobs.create(
            "demo", "request-second", inputs=[{
                "path": str(second), "grant_id": second_grant["grant_id"],
            }])

        snapshot = self.policy.load(required=True)
        self.authority.revoke("demo", first_grant["grant_id"], snapshot.digest)

        self.assert_code("GRANT_REVOKED", lambda: self.jobs.require_current(first_record))
        self.jobs.require_current(second_record)

    def test_require_current_rejects_an_expired_input_grant(self):
        source = self.base / "expiring.txt"
        source.write_text("short lived\n", encoding="utf-8")
        with patch("workspace_authority.time.time", return_value=1000.0):
            grant = self.approved_read(source, duration=1)
            record = self.jobs.create(
                "demo", "request-expiring", inputs=[{
                    "path": str(source), "grant_id": grant["grant_id"],
                }])

        with patch("workspace_authority.time.time", return_value=1001.01):
            self.assert_code(
                "GRANT_EXPIRED", lambda: self.jobs.require_current(record))

    def test_create_denies_ignored_or_registered_raw_input_from_any_project(self):
        ignored = self.repo / "build" / "result.json"
        ignored_grant = self.approved_read(ignored)
        self.assert_code(
            "WORKSPACE_SOURCE_EXCLUDED",
            lambda: self.jobs.create(
                "demo", "request-ignored", inputs=[{
                    "path": str(ignored), "grant_id": ignored_grant["grant_id"],
                }]))

        document = json.loads(self.policy_path.read_text(encoding="utf-8"))
        document["artifacts"] = [{
            "project": "demo",
            "path": "source.py",
            "kind": "file",
            "operations": ["read"],
            "fields": ["status"],
            "recursive": False,
            "max_file_bytes": 4096,
        }]
        self.policy_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.policy_path, 0o600)
        snapshot = self.policy.load(required=True)
        self.authority.advance(snapshot.digest)
        artifact_grant = self.approved_read(self.repo / "source.py")
        self.assert_code(
            "WORKSPACE_SOURCE_EXCLUDED",
            lambda: self.jobs.create(
                "demo", "request-artifact", inputs=[{
                    "path": str(self.repo / "source.py"),
                    "grant_id": artifact_grant["grant_id"],
                }]))

    def test_snapshot_and_inputs_exclude_nonignored_registered_artifacts(self):
        report = self.repo / "report.json"
        report.write_text(
            '{"status":"ok","hidden_token":"must-not-copy"}\n', encoding="utf-8")
        results = self.repo / "results"
        results.mkdir()
        result = results / "item.json"
        result.write_text(
            '{"status":"ok","hidden_detail":"must-not-copy"}\n', encoding="utf-8")
        document = json.loads(self.policy_path.read_text(encoding="utf-8"))
        document["artifacts"] = [
            {
                "project": "demo", "path": "report.json", "kind": "file",
                "operations": ["read"], "fields": ["status"],
                "recursive": False, "max_file_bytes": 4096,
            },
            {
                "project": "demo", "path": "results", "kind": "directory",
                "operations": ["list", "read"], "fields": ["status"],
                "recursive": True, "max_file_bytes": 4096,
            },
        ]
        self.policy_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.policy_path, 0o600)
        snapshot = self.policy.load(required=True)
        self.authority.advance(snapshot.digest)

        record = self.jobs.create("demo", "request-filter-artifacts")
        source = Path(record["source"])
        self.assertFalse((source / "report.json").exists())
        self.assertFalse((source / "results").exists())
        self.assertNotIn("must-not-copy", json.dumps(record))

        for index, artifact in enumerate((report, result)):
            grant = self.approved_read(artifact)
            with self.subTest(artifact=artifact):
                self.assert_code(
                    "WORKSPACE_SOURCE_EXCLUDED",
                    lambda artifact=artifact, grant=grant, index=index: self.jobs.create(
                        "demo", f"request-artifact-input-{index}", inputs=[{
                            "path": str(artifact), "grant_id": grant["grant_id"],
                        }]))

    def test_publish_requires_completion_and_is_idempotent_without_clobber(self):
        record = self.jobs.create("demo", "request-2")
        output = Path(record["output"]) / "report.txt"
        output.write_text("finished\n", encoding="utf-8")

        self.assert_code(
            "JOB_INCOMPLETE",
            lambda: self.jobs.publish(
                "demo", record, "report.txt", "published.txt", "request-2"))
        self.assertFalse((self.repo / "published.txt").exists())
        first = self.jobs.publish(
            "demo", record, "report.txt", "published.txt", "request-2",
            completion_verified=True)
        second = self.jobs.publish(
            "demo", record, "report.txt", "published.txt", "request-2",
            completion_verified=True)

        self.assertEqual(first, second)
        self.assertEqual((self.repo / "published.txt").read_bytes(), b"finished\n")
        self.assertEqual(stat.S_IMODE((self.repo / "published.txt").stat().st_mode), 0o600)
        (self.repo / "published.txt").write_text("external change\n", encoding="utf-8")
        self.assert_code(
            "PUBLICATION_STALE",
            lambda: self.jobs.publish(
                "demo", record, "report.txt", "published.txt", "request-2",
                completion_verified=True))

        receipt_files = list((self.job_state / "publications").glob("*.json"))
        self.assertEqual(len(receipt_files), 1)
        receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
        self.assertNotIn("data", receipt)
        self.assertNotIn("finished", receipt_files[0].read_text(encoding="utf-8"))

    def test_publish_replaces_by_digest_and_preserves_original_mode(self):
        target = self.repo / "script.sh"
        target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        os.chmod(target, 0o755)
        before = sha256(target.read_bytes())
        record = self.jobs.create("demo", "request-3")
        (Path(record["output"]) / "script.sh").write_text(
            "#!/bin/sh\necho new\n", encoding="utf-8")

        result = self.jobs.publish(
            "demo", record, "script.sh", "script.sh", "request-3",
            expected_sha256=before, completion_verified=True)
        repeated = self.jobs.publish(
            "demo", record, "script.sh", "script.sh", "request-3",
            expected_sha256=before, completion_verified=True)

        self.assertEqual(repeated, result)
        self.assertEqual(result["sha256"], sha256(b"#!/bin/sh\necho new\n"))
        self.assertEqual(target.read_bytes(), b"#!/bin/sh\necho new\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_failed_create_never_turns_preexisting_equal_bytes_into_success(self):
        (self.repo / "existing.txt").write_text("same\n", encoding="utf-8")
        record = self.jobs.create("demo", "request-existing")
        (Path(record["output"]) / "existing.txt").write_text("same\n", encoding="utf-8")

        self.assert_code(
            "TARGET_EXISTS",
            lambda: self.jobs.publish(
                "demo", record, "existing.txt", "existing.txt", "request-existing",
                completion_verified=True))
        self.assert_code(
            "PUBLICATION_STALE",
            lambda: self.jobs.publish(
                "demo", record, "existing.txt", "existing.txt", "request-existing",
                completion_verified=True))
        self.assertEqual((self.repo / "existing.txt").read_bytes(), b"same\n")

    def test_publish_outside_project_requires_exact_write_grant(self):
        target = self.base / "export" / "result.txt"
        target.parent.mkdir()
        snapshot = self.policy.load(required=True)
        request = self.authority.request(
            "demo", str(target), ["write"], 300, "publish fixture", snapshot.digest)
        grant = self.authority.confirm(
            "demo", request["request_id"], request["_confirmation"], snapshot.digest)
        record = self.jobs.create("demo", "request-grant")
        (Path(record["output"]) / "result.txt").write_text("exported\n", encoding="utf-8")

        self.assert_code(
            "WORKSPACE_SCOPE_REQUIRED",
            lambda: self.jobs.publish(
                "demo", record, "result.txt", str(target), "request-grant",
                completion_verified=True))
        result = self.jobs.publish(
            "demo", record, "result.txt", str(target), "request-grant",
            grant_id=grant["grant_id"], completion_verified=True)

        self.assertEqual(result["target"], str(target))
        self.assertEqual(target.read_bytes(), b"exported\n")

    def test_interrupted_receipt_recovers_from_the_single_written_target(self):
        record = self.jobs.create("demo", "request-interrupted")
        (Path(record["output"]) / "result.txt").write_text("durable\n", encoding="utf-8")
        real_write = workspace_jobs.write_file

        def interrupt_after_write(*args, **kwargs):
            real_write(*args, **kwargs)
            raise KeyboardInterrupt

        with patch("workspace_jobs.write_file", side_effect=interrupt_after_write):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.publish(
                    "demo", record, "result.txt", "interrupted.txt",
                    "request-interrupted", completion_verified=True)

        receipt_path = next((self.job_state / "publications").glob("*.json"))
        self.assertEqual(
            json.loads(receipt_path.read_text(encoding="utf-8"))["status"], "pending")
        self.assertEqual((self.repo / "interrupted.txt").read_bytes(), b"durable\n")
        recovered = self.jobs.publish(
            "demo", record, "result.txt", "interrupted.txt", "request-interrupted",
            completion_verified=True)
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(
            json.loads(receipt_path.read_text(encoding="utf-8"))["status"], "complete")

    def test_cross_project_record_is_rejected_before_any_publication(self):
        record = self.jobs.create("demo", "request-project")
        (Path(record["output"]) / "result.txt").write_text("wrong project\n", encoding="utf-8")

        self.assert_code(
            "JOB_RECORD_INVALID",
            lambda: self.jobs.publish(
                "other", record, "result.txt", "result.txt", "request-project",
                completion_verified=True))
        self.assertFalse((self.repo / "result.txt").exists())

    def test_publish_rejects_unsafe_output_destination_and_stale_authority(self):
        record = self.jobs.create("demo", "request-4")
        output_root = Path(record["output"])
        (output_root / "safe.txt").write_text("safe\n", encoding="utf-8")
        (output_root / "binary.bin").write_bytes(b"a\0b")
        (output_root / "link.txt").symlink_to("safe.txt")
        os.link(output_root / "safe.txt", output_root / "hardlink.txt")
        (self.repo / "protected").mkdir()
        (self.repo / "destination-source").write_text("old\n", encoding="utf-8")
        os.link(self.repo / "destination-source", self.repo / "destination-hardlink")

        cases = (
            ("SENSITIVE_PATH", "safe.txt", ".env"),
            ("PROTECTED_PATH", "safe.txt", "protected/result.txt"),
            ("PUBLICATION_BINARY", "binary.bin", "binary.bin"),
            ("UNSAFE_PATH", "link.txt", "link.txt"),
            ("UNSAFE_FILE", "hardlink.txt", "hardlink.txt"),
            ("UNSAFE_FILE", "safe.txt", "destination-hardlink"),
        )
        for code, relative_output, target in cases:
            with self.subTest(relative_output=relative_output):
                self.assert_code(
                    code,
                    lambda ro=relative_output, target=target: self.jobs.publish(
                        "demo", record, ro, target, "request-4",
                        completion_verified=True))
        self.assertFalse((self.repo / ".env").read_text(encoding="utf-8") == "safe\n")
        self.assertFalse((self.repo / "protected/result.txt").exists())
        self.assertFalse((self.repo / "binary.bin").exists())
        self.authority.advance(record["binding"]["policy_digest"])
        self.assert_code(
            "JOB_STALE",
            lambda: self.jobs.publish(
                "demo", record, "safe.txt", "after-advance.txt", "request-4",
                completion_verified=True))
        self.assertFalse((self.repo / "after-advance.txt").exists())


if __name__ == "__main__":
    unittest.main()
