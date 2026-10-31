#!/usr/bin/env python3
"""Owner-bound CAS registration of ONE exact, prebuilt tarororo TEST upload profile.

This script does NOT run any deployment, invite, network call, OAuth login,
service restart or review submission. No private config content is printed.
The existing UX019 upload-only profile is the immutable structural template.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile

from host_command_profiles import HostCommandProfiles, _private_config, _validate_profile
from host_runtime_config import config_write_lock

CONFIG = Path.home() / ".config/chatgpt-coding-mcp/host-commands.json"
ROOT = Path("/home/jhw/ai/opencode/projects/tossApp")
OLD_ID = "01a151bd-4015-75cf-8275-fa6b6aef9a47"
NEW_ID = "01a17791-4979-736a-a132-6b0973fe7587"
OLD_SHA = "b7f2cc40d10a090544aa62a7e8ffdd757358b6533130127d192a74651bb192eb"
NEW_SHA = "580d5307d2339104adcef7ccadfdc2301f382cea7e262473b7f8aa688aad852c"
OLD_REL = "build/android-test/gone-card-ux-review-20261019/tarororo.ait"
NEW_REL = "build/android-test/source-test-candidate-20261026T093608Z/tarororo.ait"
NEW_PROFILE = "tossapp-upload-20261026"
OLD_PROFILE = "tossapp-upload-ux019"
NEW_LABEL = "tossApp frozen TEST AIT upload only [580d5307]"
MAX_CONFIG = 64 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_ait(relative: str, expected_sha: str, expected_id: str) -> None:
    path = ROOT / relative
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o022 or path.is_symlink()
            or not 1 <= st.st_size <= 100_000_000):
        raise ValueError("AIT_UNSAFE")
    if sha(path.read_bytes()) != expected_sha:
        raise ValueError("AIT_SHA_MISMATCH")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("AIT_ZIP_CRC")
        bundle = json.loads(archive.read("bundle.json"))
    if bundle.get("deploymentId") != expected_id:
        raise ValueError("AIT_DEPLOYMENT_ID_MISMATCH")


def candidate_from(doc: dict) -> tuple[dict, dict]:
    if doc.get("enabled") is not True or doc.get("version") != 1:
        raise ValueError("REGISTRY_NOT_ENABLED")
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or OLD_PROFILE not in profiles:
        raise ValueError("OLD_UPLOAD_ONLY_PROFILE_MISSING")
    if NEW_PROFILE in profiles:
        raise ValueError("NEW_PROFILE_ALREADY_PRESENT")
    old = profiles[OLD_PROFILE]
    if (old.get("project") != "tossApp" or old.get("effect") != "external_publish"
            or type(old.get("timeout_seconds")) is not int
            or not 1 <= old["timeout_seconds"] <= 240
            or type(old.get("inputs")) is not dict
            or old["inputs"].get(OLD_REL) != OLD_SHA
            or type(old.get("argv")) is not list):
        raise ValueError("OLD_UPLOAD_ONLY_PROFILE_UNRECOGNIZED")
    if not any(arg == "test" for arg in old["argv"]):
        raise ValueError("OLD_PROFILE_NOT_TEST_ONLY")
    if any("invite" in arg.lower() or "review" in arg.lower()
           or "production" in arg.lower() for arg in old["argv"]):
        # "review" can legitimately occur inside old artifact directory;
        # only reject action-like argument spellings below.
        bad = {"invite", "--invite", "review", "--review", "production", "--production"}
        if any(arg.lower() in bad for arg in old["argv"]):
            raise ValueError("OLD_PROFILE_HAS_UNEXPECTED_SIDE_EFFECT")

    # Replace ONLY three frozen identity strings, never auth, executable,
    # toolchain, cwd, runtime flags or any unrelated profile.
    old_abs = str(ROOT / OLD_REL)
    new_abs = str(ROOT / NEW_REL)
    markers = {OLD_REL: NEW_REL, old_abs: new_abs,
               OLD_SHA: NEW_SHA, OLD_ID: NEW_ID}
    counts = {OLD_REL: 0, OLD_SHA: 0, OLD_ID: 0}

    def convert(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("PROFILE_ARGUMENTS_NOT_STRINGS")
        new = value
        # Absolute first, so the relative token is not doubly replaced.
        if old_abs in new:
            counts[OLD_REL] += new.count(old_abs)
            new = new.replace(old_abs, new_abs)
        elif OLD_REL in new:
            counts[OLD_REL] += new.count(OLD_REL)
            new = new.replace(OLD_REL, NEW_REL)
        if OLD_SHA in new:
            counts[OLD_SHA] += new.count(OLD_SHA)
            new = new.replace(OLD_SHA, NEW_SHA)
        if OLD_ID in new:
            counts[OLD_ID] += new.count(OLD_ID)
            new = new.replace(OLD_ID, NEW_ID)
        return new

    new = copy.deepcopy(old)
    new["label"] = NEW_LABEL
    new["argv"] = [convert(arg) for arg in old["argv"]]
    new_inputs = {}
    for key, value in old["inputs"].items():
        if key == OLD_REL:
            new_inputs[NEW_REL] = NEW_SHA
            counts[OLD_REL] += 1
            counts[OLD_SHA] += 1
        else:
            new_inputs[key] = value
    new["inputs"] = new_inputs
    if (counts[OLD_REL] < 2 or counts[OLD_SHA] < 2 or counts[OLD_ID] < 1
            or NEW_REL not in new["inputs"]
            or OLD_REL in new["inputs"]):
        raise ValueError("UPLOAD_PROFILE_ARGUMENT_PIN_MISSING")
    if (new["executable"] != old["executable"]
            or new["executable_sha256"] != old["executable_sha256"]
            or new["cwd"] != old["cwd"]
            or new["timeout_seconds"] != old["timeout_seconds"]
            or new["effect"] != old["effect"]):
        raise ValueError("UNEXPECTED_PROFILE_DRIFT")
    _validate_profile(new, {"tossApp": ROOT})
    candidate = copy.deepcopy(doc)
    candidate["profiles"][NEW_PROFILE] = new
    return candidate, {"new_profile_id": NEW_PROFILE, "old_profile_id": OLD_PROFILE,
                       "new_inputs": len(new["inputs"]),
                       "marker_occurrences": {"artifact": counts[OLD_REL],
                                              "sha256": counts[OLD_SHA],
                                              "deployment_id": counts[OLD_ID]},
                       "existing_profile_count": len(profiles)}


def write_file_synced(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def run(apply: bool) -> dict:
    if not ROOT.is_dir():
        raise ValueError("REPOSITORY_UNAVAILABLE")
    verify_ait(OLD_REL, OLD_SHA, OLD_ID)
    verify_ait(NEW_REL, NEW_SHA, NEW_ID)
    with config_write_lock(CONFIG):
        doc, old_config_sha = _private_config(CONFIG)
        candidate, summary = candidate_from(doc)
        data = json.dumps(candidate, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        if not 0 < len(data) <= MAX_CONFIG:
            raise ValueError("CONFIG_SIZE_LIMIT")
        parent = CONFIG.parent
        fd, tempname = tempfile.mkstemp(prefix=".toss-20261026-candidate-", dir=parent)
        os.close(fd)
        temp = Path(tempname)
        temp.unlink()
        backup = parent / ("host-commands.json.before-tossapp-20261026-" +
                           old_config_sha[:16] + ".bak")
        try:
            write_file_synced(temp, data)
            frozen = HostCommandProfiles({"tossApp": ROOT}, temp).freeze("tossApp", NEW_PROFILE)
            if (frozen["inputs"][NEW_REL] != NEW_SHA
                    or frozen["effect"] != "external_publish"
                    or frozen["timeout_seconds"] > 240):
                raise ValueError("PROFILE_FREEZE_MISMATCH")
            if not apply:
                return {"status": "DRY_RUN_VALID", **summary,
                        "candidate_sha256": sha(data),
                        "artifact_sha256": NEW_SHA, "deployment_id": NEW_ID,
                        "config_mutation": False, "external_publish": False}
            # CAS again with lock held; no merge or partial config rewrite.
            current, current_sha = _private_config(CONFIG)
            if current_sha != old_config_sha or current != doc:
                raise ValueError("OWNER_CONFIG_CHANGED")
            if backup.exists() or backup.is_symlink():
                raise ValueError("BACKUP_ALREADY_EXISTS")
            fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                config_data = b""
                while block := os.read(fd, 65536):
                    config_data += block
            finally:
                os.close(fd)
            if sha(config_data) != old_config_sha:
                raise ValueError("OWNER_CONFIG_CAS_MISMATCH")
            write_file_synced(backup, config_data)
            os.replace(temp, CONFIG)
            dirfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
            registered = HostCommandProfiles({"tossApp": ROOT}, CONFIG).freeze(
                "tossApp", NEW_PROFILE)
            if registered != frozen:
                raise ValueError("POST_COMMIT_PROFILE_DRIFT")
            return {"status": "REGISTERED_AND_FROZEN", **summary,
                    "candidate_sha256": sha(data), "artifact_sha256": NEW_SHA,
                    "deployment_id": NEW_ID, "old_config_sha256": old_config_sha,
                    "config_mutation": True, "external_publish": False,
                    "one_use_publish_approval_still_required": True}
        finally:
            if temp.exists():
                temp.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.apply), ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED",
                          "error_code": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                          "config_mutation_claimed": False}))
        raise SystemExit(2)
