import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from dataclasses import replace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_paths import WorkspaceError, is_sensitive, read_file, write_file  # noqa: E402
from workspace_policy import WorkspacePolicy  # noqa: E402


class WorkspacePathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(WorkspaceError) as raised:
            operation(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_read_file_returns_bytes_digest_mode_and_stable_identity(self):
        source = self.root / "bin" / "tool"
        source.parent.mkdir()
        source.write_bytes(b"#!/bin/sh\nprintf safe\n")
        source.chmod(0o755)

        result = read_file(self.root, "bin/tool")

        self.assertEqual(result.data, b"#!/bin/sh\nprintf safe\n")
        self.assertEqual(result.sha256, hashlib.sha256(result.data).hexdigest())
        self.assertEqual(result.size, len(result.data))
        self.assertEqual(result.mode, 0o755)
        self.assertEqual(result.identity[:2], (source.stat().st_dev, source.stat().st_ino))

    def test_read_file_rejects_non_relative_and_sensitive_paths(self):
        for value in ("", ".", "../secret", "dir/../secret", "/etc/passwd", "dir//file"):
            with self.subTest(value=value):
                self.assert_code("INVALID_PATH", read_file, self.root, value)
        for value in (".env", ".env.local", ".git/config", ".ssh/id_ed25519", "sub/.aws/credentials"):
            with self.subTest(value=value):
                self.assertTrue(is_sensitive(value))
                self.assert_code("SENSITIVE_PATH", read_file, self.root, value)

    def test_read_file_does_not_accept_prefix_sibling_or_symlinks(self):
        sibling = self.base / "project-secret"
        sibling.mkdir()
        (sibling / "payload").write_text("secret")
        (self.root / "escape").symlink_to(sibling, target_is_directory=True)
        (self.root / "leaf").symlink_to(sibling / "payload")

        self.assert_code("UNSAFE_PATH", read_file, self.root, "escape/payload")
        self.assert_code("UNSAFE_PATH", read_file, self.root, "leaf")

    def test_read_file_holds_parent_fd_across_parent_swap(self):
        safe = self.root / "safe"
        safe.mkdir()
        (safe / "result.json").write_text("local")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "result.json").write_text("secret")
        real_open = os.open
        swapped = False

        def swap_before_leaf(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "result.json" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                safe.rename(self.root / "old-safe")
                safe.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with patch("workspace_paths.os.open", side_effect=swap_before_leaf):
            result = read_file(self.root, "safe/result.json")

        self.assertEqual(result.data, b"local")

    def test_absolute_root_with_interior_symlink_cannot_read_or_write(self):
        outside = self.base / "outside"
        outside.joinpath("nested").mkdir(parents=True)
        outside.joinpath("nested/payload").write_bytes(b"protected outside")
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        supplied_root = alias / "nested"
        self.assert_code("UNSAFE_PATH", read_file, supplied_root, "payload")
        self.assert_code("UNSAFE_PATH", write_file, supplied_root, "new", b"unsafe", create=True)
        self.assertFalse(outside.joinpath("nested/new").exists())

    def test_read_file_rejects_hardlinks_fifo_and_oversize_without_blocking(self):
        original = self.root / "original"
        original.write_bytes(b"payload")
        os.link(original, self.root / "hardlink")
        os.mkfifo(self.root / "pipe", 0o600)
        (self.root / "large").write_bytes(b"x" * 9)

        self.assert_code("UNSAFE_FILE", read_file, self.root, "hardlink")
        self.assert_code("UNSAFE_FILE", read_file, self.root, "pipe")
        self.assert_code("FILE_TOO_LARGE", read_file, self.root, "large", max_bytes=8)

    def test_write_file_creates_private_file_and_rejects_existing_target(self):
        result = write_file(self.root, "new.txt", b"new", create=True)

        self.assertEqual((self.root / "new.txt").read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE((self.root / "new.txt").stat().st_mode), 0o600)
        self.assertEqual(result["sha256"], hashlib.sha256(b"new").hexdigest())
        self.assertEqual(result["size"], 3)
        self.assertEqual(result["mode"], 0o600)
        self.assert_code("TARGET_EXISTS", write_file, self.root, "new.txt", b"again", create=True)

    def test_write_file_requires_matching_cas_and_preserves_executable_mode(self):
        target = self.root / "script.sh"
        target.write_bytes(b"old")
        target.chmod(0o755)
        digest = hashlib.sha256(b"old").hexdigest()

        self.assert_code("CAS_REQUIRED", write_file, self.root, "script.sh", b"new")
        self.assert_code(
            "STALE_FILE", write_file, self.root, "script.sh", b"new", expected_sha256="0" * 64
        )
        result = write_file(
            self.root, "script.sh", b"new", expected_sha256=digest
        )

        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(result["mode"], 0o755)

    def test_write_file_rejects_links_special_files_and_unsafe_modes(self):
        original = self.root / "original"
        original.write_bytes(b"old")
        os.link(original, self.root / "hardlink")
        (self.root / "link").symlink_to(original)
        os.mkfifo(self.root / "pipe", 0o600)
        unsafe = self.root / "world-writable"
        unsafe.write_bytes(b"old")
        unsafe.chmod(0o666)
        digest = hashlib.sha256(b"old").hexdigest()

        for value in ("hardlink", "link", "pipe", "world-writable"):
            with self.subTest(value=value):
                self.assert_code(
                    "UNSAFE_FILE", write_file, self.root, value, b"new", expected_sha256=digest
                )

    def test_write_file_holds_parent_fd_across_parent_swap(self):
        safe = self.root / "safe"
        safe.mkdir()
        target = safe / "target"
        target.write_bytes(b"old")
        outside = self.base / "outside"
        outside.mkdir()
        outside_target = outside / "target"
        outside_target.write_bytes(b"secret")
        digest = hashlib.sha256(b"old").hexdigest()
        real_replace = os.replace
        swapped = False

        def swap_before_replace(src, dst, *args, **kwargs):
            nonlocal swapped
            if dst == "target" and not swapped:
                swapped = True
                safe.rename(self.root / "old-safe")
                safe.symlink_to(outside, target_is_directory=True)
            return real_replace(src, dst, *args, **kwargs)

        with patch("workspace_paths.os.replace", side_effect=swap_before_replace):
            write_file(self.root, "safe/target", b"new", expected_sha256=digest)

        self.assertEqual((self.root / "old-safe" / "target").read_bytes(), b"new")
        self.assertEqual(outside_target.read_bytes(), b"secret")


class WorkspacePolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.projects_root = self.base / "projects"
        self.projects_root.mkdir(mode=0o700)
        self.project = self.projects_root / "repo"
        self.project.mkdir()
        self.other_project = self.projects_root / "other"
        self.other_project.mkdir()
        self.owner = self.base / "owner"
        self.owner.mkdir(mode=0o700)
        self.config = self.owner / "workspace-policy.json"

    def tearDown(self):
        self.temp.cleanup()

    def policy_document(self, **updates):
        document = {
            "version": 1,
            "base_root": str(self.projects_root),
            "artifacts": [
                {
                    "project": "repo",
                    "path": "build/summary.json",
                    "kind": "file",
                    "operations": ["read"],
                    "fields": ["status", "usage"],
                    "recursive": False,
                    "max_file_bytes": 4096,
                },
                {
                    "project": "repo",
                    "path": "build/results",
                    "kind": "directory",
                    "operations": ["list", "read"],
                    "fields": ["status"],
                    "recursive": True,
                    "max_file_bytes": 8192,
                },
                {
                    "project": "repo",
                    "path": "build/direct",
                    "kind": "directory",
                    "operations": ["list", "read"],
                    "fields": ["status"],
                    "recursive": False,
                    "max_file_bytes": 8192,
                },
            ],
            "protected_paths": [str(self.project / "protected")],
        }
        document.update(updates)
        return document

    def write_policy(self, document=None):
        self.config.write_text(json.dumps(document or self.policy_document()))
        self.config.chmod(0o600)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(WorkspaceError) as raised:
            operation(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_constructor_and_missing_load_do_not_create_live_state(self):
        missing = self.owner / "missing.json"
        policy = WorkspacePolicy({"repo": self.project}, config_path=missing)

        self.assertIsNone(policy.load())
        self.assertFalse(missing.exists())
        self.assert_code("POLICY_MISSING", policy.load, required=True)
        self.assertFalse(missing.exists())

    def test_missing_policy_does_not_hide_unsafe_or_project_owned_storage(self):
        linked_owner = self.base / "linked-owner"
        linked_owner.symlink_to(self.owner, target_is_directory=True)
        self.assert_code(
            "UNSAFE_POLICY_DIRECTORY",
            WorkspacePolicy(
                {"repo": self.project}, config_path=linked_owner / "missing.json"
            ).load,
        )
        self.assert_code(
            "UNSAFE_POLICY_LOCATION",
            WorkspacePolicy(
                {"repo": self.project}, config_path=self.project / "missing.json"
            ).load,
        )

    def test_load_validates_policy_and_exposes_stable_snapshot(self):
        self.write_policy()
        payload = self.config.read_bytes()

        snapshot = WorkspacePolicy({"repo": self.project}, config_path=self.config).load(required=True)

        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(snapshot.base_root, self.projects_root)
        self.assertEqual(len(snapshot.artifacts), 3)
        self.assertIn(self.owner, snapshot.protected_paths)
        self.assertIn(self.project / "protected", snapshot.protected_paths)

    def test_version_two_registers_not_yet_created_text_directory(self):
        document = self.policy_document(version=2)
        document["artifacts"] = [{
            "project": "repo",
            "path": "qa/reports",
            "kind": "directory",
            "operations": ["list", "read"],
            "fields": [],
            "content_mode": "text",
            "recursive": True,
            "max_file_bytes": 1048576,
        }]
        self.assertFalse(self.project.joinpath("qa/reports").exists())
        self.write_policy(document)

        snapshot = WorkspacePolicy(
            {"repo": self.project}, config_path=self.config,
        ).load(required=True)

        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.artifacts[0]["content_mode"], "text")
        self.assertEqual(snapshot.artifacts[0]["fields"], ())

    def test_policy_versions_keep_exact_content_mode_schemas(self):
        legacy_with_mode = self.policy_document()
        legacy_with_mode["artifacts"][0]["content_mode"] = "json_fields"
        version_two_without_mode = self.policy_document(version=2)
        version_two_text_fields = self.policy_document(version=2)
        for rule in version_two_text_fields["artifacts"]:
            rule["content_mode"] = "json_fields"
        version_two_text_fields["artifacts"][0].update(
            content_mode="text", fields=["status"]
        )
        invalid_documents = [
            legacy_with_mode,
            version_two_without_mode,
            version_two_text_fields,
        ]

        for document in invalid_documents:
            with self.subTest(document=document):
                self.write_policy(document)
                self.assert_code(
                    "INVALID_POLICY",
                    WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
                    required=True,
                )

        version_two_json = self.policy_document(version=2)
        for rule in version_two_json["artifacts"]:
            rule["content_mode"] = "json_fields"
        self.write_policy(version_two_json)
        snapshot = WorkspacePolicy(
            {"repo": self.project}, config_path=self.config,
        ).load(required=True)
        self.assertTrue(all(
            rule["content_mode"] == "json_fields" for rule in snapshot.artifacts
        ))

    def test_version_two_rejects_mixed_mode_nested_namespaces_in_both_directions(self):
        for parent_mode, child_mode in (
            ("json_fields", "text"),
            ("text", "json_fields"),
        ):
            with self.subTest(parent_mode=parent_mode, child_mode=child_mode):
                document = self.policy_document(version=2)
                document["artifacts"] = [
                    {
                        "project": "repo", "path": "qa/reports", "kind": "directory",
                        "operations": ["list", "read"],
                        "fields": ["status"] if parent_mode == "json_fields" else [],
                        "content_mode": parent_mode, "recursive": True,
                        "max_file_bytes": 1048576,
                    },
                    {
                        "project": "repo", "path": "qa/reports/detail",
                        "kind": "directory", "operations": ["list", "read"],
                        "fields": ["status"] if child_mode == "json_fields" else [],
                        "content_mode": child_mode, "recursive": True,
                        "max_file_bytes": 1048576,
                    },
                ]
                self.write_policy(document)

                self.assert_code(
                    "INVALID_POLICY",
                    WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
                    required=True,
                )

    def test_load_allows_omitted_optional_protected_paths(self):
        document = self.policy_document()
        del document["protected_paths"]
        self.write_policy(document)

        snapshot = WorkspacePolicy(
            {"repo": self.project}, config_path=self.config
        ).load(required=True)

        self.assertEqual(snapshot.protected_paths, (self.owner,))

    def test_load_rejects_config_inside_project_and_unsafe_config_storage(self):
        inside = self.project / "policy.json"
        inside.write_text(json.dumps(self.policy_document()))
        inside.chmod(0o600)
        self.assert_code(
            "UNSAFE_POLICY_LOCATION",
            WorkspacePolicy({"repo": self.project}, config_path=inside).load,
            required=True,
        )

        self.write_policy()
        self.config.chmod(0o644)
        self.assert_code(
            "UNSAFE_POLICY_FILE",
            WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
            required=True,
        )
        self.config.chmod(0o600)
        self.owner.chmod(0o755)
        self.assert_code(
            "UNSAFE_POLICY_DIRECTORY",
            WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
            required=True,
        )

    def test_load_rejects_symlink_ancestor_hardlink_and_special_config(self):
        self.write_policy()
        linked_owner = self.base / "linked-owner"
        linked_owner.symlink_to(self.owner, target_is_directory=True)
        self.assert_code(
            "UNSAFE_POLICY_DIRECTORY",
            WorkspacePolicy(
                {"repo": self.project}, config_path=linked_owner / self.config.name
            ).load,
            required=True,
        )

        hardlink = self.owner / "hardlink.json"
        os.link(self.config, hardlink)
        self.assert_code(
            "UNSAFE_POLICY_FILE",
            WorkspacePolicy({"repo": self.project}, config_path=hardlink).load,
            required=True,
        )

        fifo = self.owner / "fifo.json"
        os.mkfifo(fifo, 0o600)
        self.assert_code(
            "UNSAFE_POLICY_FILE",
            WorkspacePolicy({"repo": self.project}, config_path=fifo).load,
            required=True,
        )

    def test_load_fails_closed_for_broken_or_unknown_schema(self):
        invalid_documents = [
            {**self.policy_document(), "unknown": True},
            self.policy_document(version=True),
            self.policy_document(base_root="relative"),
            self.policy_document(artifacts="all"),
            self.policy_document(protected_paths=["relative"]),
            self.policy_document(
                artifacts=[
                    {
                        **self.policy_document()["artifacts"][0],
                        "operations": ["read", "write"],
                    }
                ]
            ),
            self.policy_document(
                artifacts=[
                    {
                        **self.policy_document()["artifacts"][0],
                        "max_file_bytes": 1048577,
                    }
                ]
            ),
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                self.write_policy(document)
                self.assert_code(
                    "INVALID_POLICY",
                    WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
                    required=True,
                )

        self.config.write_bytes(b"not json")
        self.config.chmod(0o600)
        self.assert_code(
            "INVALID_POLICY",
            WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
            required=True,
        )

    def test_external_registered_project_does_not_break_inside_base_access(self):
        outside = self.base / "outside"
        outside.mkdir()
        outside_source = outside / "notes.txt"
        outside_source.write_text("outside")
        inside_source = self.project / "notes.txt"
        inside_source.write_text("inside")
        document = self.policy_document()
        document["artifacts"].append({
            "project": "external",
            "path": "results/summary.json",
            "kind": "file",
            "operations": ["read"],
            "fields": ["status"],
            "recursive": False,
            "max_file_bytes": 4096,
        })
        document["protected_paths"].append(str(outside / "protected"))
        self.write_policy(document)
        policy = WorkspacePolicy(
            {"repo": self.project, "external": outside}, config_path=self.config,
        )

        snapshot = policy.load(required=True)

        self.assertEqual(
            policy.check("repo", "notes.txt", "read", snapshot=snapshot),
            inside_source,
        )
        self.assert_code(
            "WORKSPACE_SCOPE_REQUIRED", policy.check,
            "external", "notes.txt", "read", snapshot=snapshot,
        )
        self.assert_code(
            "WORKSPACE_SCOPE_REQUIRED", policy.check,
            "external", str(outside_source), "read", snapshot=snapshot,
        )
        self.assert_code(
            "SENSITIVE_PATH", policy.check,
            "external", ".env", "read", snapshot=snapshot,
        )
        self.assert_code(
            "PROTECTED_PATH", policy.check,
            "external", "protected/child.txt", "read", snapshot=snapshot,
        )
        external_rule = policy.artifact_rule(
            snapshot, "external", "results/summary.json",
        )
        self.assertEqual(external_rule["fields"], ("status",))
        self.assert_code(
            "WORKSPACE_SCOPE_REQUIRED", policy.check,
            "external", "results/summary.json", "read", snapshot=snapshot,
        )

    def test_load_rejects_unknown_artifact_projects(self):
        self.write_policy()

        document = self.policy_document()
        document["artifacts"][0]["project"] = "missing"
        self.write_policy(document)
        self.assert_code(
            "INVALID_POLICY",
            WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
            required=True,
        )

    def test_load_still_rejects_registering_the_base_as_a_project(self):
        self.write_policy()

        self.assert_code(
            "PROJECT_OUTSIDE_BASE",
            WorkspacePolicy(
                {"projects-root": self.projects_root}, config_path=self.config,
            ).load,
            required=True,
        )

    def test_artifact_rule_matches_only_registered_operation_boundaries(self):
        self.write_policy()
        policy = WorkspacePolicy({"repo": self.project}, config_path=self.config)
        snapshot = policy.load(required=True)

        exact = policy.artifact_rule(snapshot, "repo", "build/summary.json")
        nested = policy.artifact_rule(snapshot, "repo", "build/results/run/output.json")
        direct = policy.artifact_rule(snapshot, "repo", "build/direct/output.json")
        self.assertEqual(exact["operations"], ("read",))
        self.assertEqual(nested["path"], "build/results")
        self.assertEqual(direct["path"], "build/direct")
        self.assertTrue(policy.artifact_path(snapshot, "repo", "build/direct/nested/output.json"))
        self.assert_code(
            "WORKSPACE_ARTIFACT_DENIED", policy.artifact_rule, snapshot, "repo",
            "build/direct/nested/output.json",
        )
        for path in (
            "build/summary.json.backup",
            "build/resultset/run/output.json",
            "build/results-sibling/output.json",
            "build/results/../secret",
        ):
            with self.subTest(path=path):
                self.assert_code(
                    "ARTIFACT_NOT_REGISTERED", policy.artifact_rule, snapshot, "repo", path
                )

    def test_load_rejects_nested_artifact_namespaces(self):
        nested_rules = [
            {
                "project": "repo", "path": "build/results/one.json", "kind": "file",
                "operations": ["read"], "fields": ["status"], "recursive": False,
                "max_file_bytes": 4096,
            },
            {
                "project": "repo", "path": "build/direct/deep/files", "kind": "directory",
                "operations": ["list", "read"], "fields": ["status"], "recursive": True,
                "max_file_bytes": 4096,
            },
        ]
        for nested in nested_rules:
            with self.subTest(path=nested["path"]):
                document = self.policy_document()
                document["artifacts"].append(nested)
                self.write_policy(document)
                self.assert_code(
                    "INVALID_POLICY",
                    WorkspacePolicy({"repo": self.project}, config_path=self.config).load,
                    required=True,
                )

    def test_ambiguous_rules_and_nonrecursive_descendants_cannot_become_ordinary_source(self):
        self.write_policy()
        policy = WorkspacePolicy({"repo": self.project}, config_path=self.config)
        snapshot = policy.load(required=True)
        specific = {
            "project": "repo", "path": "build/results/one.json", "kind": "file",
            "operations": ("read",), "fields": ("status",), "recursive": False,
            "max_file_bytes": 4096,
        }
        ambiguous = replace(snapshot, artifacts=(*snapshot.artifacts, specific))

        self.assert_code(
            "ARTIFACT_AMBIGUOUS", policy.artifact_rule, ambiguous, "repo",
            "build/results/one.json",
        )

        from workspace_tools import WorkspaceTools
        tools = WorkspaceTools({"repo": self.project}, self.owner / "unused-authority",
                               config_path=self.config)
        tools.policy = policy
        self.assert_code(
            "ARTIFACT_AMBIGUOUS", tools.ordinary_path, ambiguous, "repo",
            self.project / "build/results/one.json",
        )
        self.assert_code(
            "WORKSPACE_ARTIFACT_DENIED", tools.ordinary_path, snapshot, "repo",
            self.project / "build/direct/nested/raw.json",
        )

    def test_check_uses_frozen_snapshot_and_protects_sensitive_and_owner_paths(self):
        self.write_policy()
        policy = WorkspacePolicy({"repo": self.project}, config_path=self.config)
        snapshot = policy.load(required=True)
        source = self.project / "src" / "main.py"
        source.parent.mkdir()
        source.write_text("print('ok')")

        self.assertEqual(policy.check("repo", ".", "list", snapshot=snapshot), self.project)
        self.assertEqual(policy.check("repo", "src/main.py", "read", snapshot=snapshot), source)
        self.assertEqual(policy.check("repo", str(source), "read", snapshot=snapshot), source)
        self.assert_code(
            "SENSITIVE_PATH", policy.check, "repo", ".env", "read", snapshot=snapshot
        )
        self.assert_code(
            "PROTECTED_PATH",
            policy.check,
            "repo",
            "protected/new.txt",
            "write",
            snapshot=snapshot,
        )
        self.assert_code(
            "UNKNOWN_PROJECT", policy.check, "missing", "src/main.py", "read", snapshot=snapshot
        )
        self.assert_code(
            "INVALID_OPERATION", policy.check, "repo", "src/main.py", "execute", snapshot=snapshot
        )
        outside = self.base / "outside.txt"
        outside.write_text("public")
        self.assert_code(
            "WORKSPACE_SCOPE_REQUIRED",
            policy.check,
            "repo",
            str(outside),
            "read",
            snapshot=snapshot,
        )
        self.assert_code(
            "PROTECTED_PATH",
            policy.check,
            "repo",
            str(self.owner / "future.json"),
            "read",
            snapshot=snapshot,
        )


if __name__ == "__main__":
    unittest.main()
