"""Fail-closed host-owned command profile validation (no host commands executed)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_command_profiles import HostCommandProfiles, public_profile_plan  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class HostCommandProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "build").mkdir()
        (self.repo / "build" / "result.ait").write_bytes(b"TEST-ARCHIVE")
        (self.repo / "build" / "result.ait").chmod(0o600)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.executable = self.bin / "toss-test-upload"
        self.script = b"#!/bin/sh\nprintf 'synthetic only'\n"
        self.executable.write_bytes(self.script)
        self.executable.chmod(0o700)
        self.config = self.base / "policy.json"
        self.profile = {
            "project": "tossApp",
            "label": "Toss TEST publish (approval required)",
            "executable": str(self.executable),
            "executable_sha256": sha(self.script),
            "argv": ["test"],
            "cwd": ".",
            "timeout_seconds": 900,
            "effect": "external_publish",
            "inputs": {"build/result.ait": sha(b"TEST-ARCHIVE")},
        }
        self.config_data = {"version": 1, "enabled": True,
                            "profiles": {"toss-test-upload": copy.deepcopy(self.profile)}}
        self.registry = HostCommandProfiles({"tossApp": self.repo, "personal-ops": self.base / "other"}, self.config)
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.config_data, ensure_ascii=False))
        self.config.chmod(0o600)

    def error(self, code, call):
        with self.assertRaises(HostRuntimeError) as exc:
            call()
        self.assertEqual(exc.exception.code, code)

    def test_listing_redacts_executable_argv_inputs_and_host_path(self):
        listed = self.registry.list_profiles("tossApp")
        self.assertTrue(listed["enabled"])
        self.assertEqual(listed["profiles"], [{"profile_id": "toss-test-upload",
                                                "label": self.profile["label"],
                                                "effect": "external_publish",
                                                "timeout_seconds": 900}])
        raw = json.dumps(listed)
        for secret in ("TEST-ARCHIVE", str(self.base), "result.ait", "test-upload"):
            if secret == "test-upload":  # profile id intentionally public
                continue
            self.assertNotIn(secret, raw)

    def test_freeze_contains_exact_fixed_command_but_public_plan_only_metadata(self):
        frozen = self.registry.freeze("tossApp", "toss-test-upload")
        self.assertEqual(frozen["argv"], ["test"])
        self.assertEqual(frozen["cwd"], str(self.repo))
        self.assertEqual(frozen["inputs"], self.profile["inputs"])
        self.assertEqual(frozen["project_root"], str(self.repo.resolve()))
        self.registry.verify_frozen(frozen)
        public = public_profile_plan(frozen)
        self.assertEqual(public["profile_id"], "toss-test-upload")
        self.assertEqual(public["input_count"], 1)
        for raw in (str(self.bin), str(self.repo), "result.ait"):
            self.assertNotIn(raw, json.dumps(public))

    def test_profile_scoped_to_project(self):
        self.error("HOST_COMMAND_PROFILE_NOT_FOUND",
                   lambda: self.registry.freeze("personal-ops", "toss-test-upload"))
        self.error("UNSUPPORTED_SCOPE", lambda: self.registry.freeze("tossApp", "bad/path"))
        self.error("UNSUPPORTED_SCOPE", lambda: self.registry.list_profiles("unknown"))

    def test_source_and_registry_drift_cannot_verify_old_plan(self):
        frozen = self.registry.freeze("tossApp", "toss-test-upload")
        (self.repo / "build" / "result.ait").write_bytes(b"CHANGED")
        self.error("HOST_COMMAND_INPUT_CHANGED", lambda: self.registry.verify_frozen(frozen))
        (self.repo / "build" / "result.ait").write_bytes(b"TEST-ARCHIVE")
        self.executable.write_bytes(self.script + b"\n#edited")
        self.error("HOST_COMMAND_EXECUTABLE_CHANGED", lambda: self.registry.verify_frozen(frozen))
        self.executable.write_bytes(self.script)
        self.config_data["profiles"]["toss-test-upload"]["timeout_seconds"] = 901
        self.save()
        self.error("HOST_COMMAND_PLAN_STALE", lambda: self.registry.verify_frozen(frozen))

    def test_disabled_profiles_fail_closed(self):
        self.config_data["enabled"] = False
        self.save()
        self.assertEqual(self.registry.list_profiles("tossApp")["profiles"], [])
        self.error("HOST_COMMAND_DISABLED", lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_source_input_symlinks_and_nonregular_inputs_denied(self):
        src = self.repo / "build" / "result.ait"
        src.unlink()
        src.symlink_to(self.executable)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.freeze("tossApp", "toss-test-upload"))
        src.unlink()
        src.mkdir()
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_directory_escape_or_symlinked_cwd_denied(self):
        self.config_data["profiles"]["toss-test-upload"]["cwd"] = "../other"
        self.save()
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.list_profiles("tossApp"))
        self.config_data["profiles"]["toss-test-upload"]["cwd"] = "nested"
        self.save()
        (self.repo / "nested").symlink_to(self.base)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_profile_config_never_resides_inside_user_repository(self):
        inside = self.repo / "policy.json"
        inside.write_text(json.dumps(self.config_data))
        inside.chmod(0o600)
        r = HostCommandProfiles({"tossApp": self.repo}, inside)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: r.list_profiles("tossApp"))

    def test_config_world_readable_symlink_and_bad_json_denied(self):
        self.config.chmod(0o644)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.list_profiles("tossApp"))
        self.config.chmod(0o600)
        destination = self.base / "moved.json"
        self.config.rename(destination)
        self.config.symlink_to(destination)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.list_profiles("tossApp"))
        self.config.unlink()
        self.config.write_text("{malformed")
        self.config.chmod(0o600)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.list_profiles("tossApp"))

    def test_executable_group_write_or_symlink_denied(self):
        self.executable.chmod(0o722)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_executable_inside_any_registered_project_is_denied(self):
        other = self.base / "other"
        other.mkdir()
        executable = other / "upload"
        executable.write_bytes(self.script)
        executable.chmod(0o700)
        self.config_data["profiles"]["toss-test-upload"]["executable"] = str(executable)
        self.config_data["profiles"]["toss-test-upload"]["executable_sha256"] = sha(self.script)
        self.save()
        self.error("INVALID_HOST_COMMAND_CONFIG",
                   lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_symlinked_executable_ancestor_is_denied(self):
        real_bin = self.base / "real-bin"
        real_bin.mkdir()
        executable = real_bin / "upload"
        executable.write_bytes(self.script)
        executable.chmod(0o700)
        linked_bin = self.base / "linked-bin"
        linked_bin.symlink_to(real_bin, target_is_directory=True)
        self.config_data["profiles"]["toss-test-upload"]["executable"] = str(linked_bin / "upload")
        self.config_data["profiles"]["toss-test-upload"]["executable_sha256"] = sha(self.script)
        self.save()
        self.error("INVALID_HOST_COMMAND_CONFIG",
                   lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_executable_metadata_drift_during_hash_is_denied(self):
        real_read = os.read
        changed = False

        def mutate_after_read(fd, count):
            nonlocal changed
            block = real_read(fd, count)
            if (block and not changed
                    and os.fstat(fd).st_ino == self.executable.stat().st_ino):
                changed = True
                self.executable.chmod(0o500)
            return block

        with mock.patch("host_command_profiles.os.read", side_effect=mutate_after_read):
            self.error("HOST_COMMAND_SOURCE_UNAVAILABLE",
                       lambda: self.registry.freeze("tossApp", "toss-test-upload"))
        self.executable.chmod(0o700)
        other = self.bin / "real-upload"
        self.executable.rename(other)
        self.executable.symlink_to(other)
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_unknown_fields_and_privilege_flags_rejected(self):
        bad = [
            ("extra", True),
            ("cwd", "/etc"),
            ("cwd", ".git"),
            ("cwd", "foo/../bar"),
            ("effect", "anything"),
            ("timeout_seconds", True),
            ("timeout_seconds", 3601),
            ("argv", ["test", "foo\nbar"]),
            ("executable_sha256", "0"*63),
            ("inputs", {}),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                original = copy.deepcopy(self.config_data)
                self.config_data["profiles"]["toss-test-upload"][key] = value
                self.save()
                self.error("INVALID_HOST_COMMAND_CONFIG",
                           lambda: self.registry.list_profiles("tossApp"))
                self.config_data = original

    def test_shell_evaluation_flags_rejected(self):
        self.profile["executable"] = "/bin/sh"
        self.profile["executable_sha256"] = "0"*64
        self.profile["argv"] = ["-c", "rm -rf /"]
        self.config_data["profiles"]["toss-test-upload"] = self.profile
        self.save()
        self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.list_profiles("tossApp"))

    def test_input_paths_are_bounded(self):
        bad = ["../private", "/etc/passwd", ".env", ".git/config", "foo//bar", ".ssh/key", "x/.env.local"]
        for path in bad:
            with self.subTest(path=path):
                data = copy.deepcopy(self.config_data)
                data["profiles"]["toss-test-upload"]["inputs"] = {path: "0"*64}
                self.config_data = data
                self.save()
                self.error("INVALID_HOST_COMMAND_CONFIG", lambda: self.registry.list_profiles("tossApp"))
        self.config_data["profiles"]["toss-test-upload"] = copy.deepcopy(self.profile)
        self.save()

    def test_symlinked_parent_of_input_is_rejected(self):
        (self.repo / "build").rename(self.repo / "real-build")
        (self.repo / "build").symlink_to(self.repo / "real-build", target_is_directory=True)
        self.error("INVALID_HOST_COMMAND_CONFIG",
                   lambda: self.registry.freeze("tossApp", "toss-test-upload"))

    def test_duplicate_json_keys_are_denied(self):
        self.config.write_text('{"version":1,"version":1,"enabled":false,"profiles":{}}')
        self.config.chmod(0o600)
        self.error("INVALID_HOST_COMMAND_CONFIG",
                   lambda: self.registry.list_profiles("tossApp"))

    def test_unregistered_service_change_effect_is_not_generic_host_shell(self):
        self.config_data["profiles"]["toss-test-upload"]["effect"] = "service_change"
        self.save()
        self.error("INVALID_HOST_COMMAND_CONFIG",
                   lambda: self.registry.list_profiles("tossApp"))

    def test_config_cannot_create_new_executable_argv_from_chat(self):
        with self.assertRaises(TypeError):
            self.registry.freeze("tossApp", "toss-test-upload", argv=["sh", "-c", "echo x"])


if __name__ == "__main__":
    unittest.main()
