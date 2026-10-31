"""Pin and invalidate legacy host authority before first Workspace activation.

This module operates only on the six existing typed stores supplied by the owner
transaction.  Construction and inventory are read-only: callers create the store
objects and pass their already-resolved private roots explicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from agent_tasks import ACTIVE as AGENT_ACTIVE, TERMINAL as AGENT_TERMINAL
from host_command_operations import (
    ACTIVE as COMMAND_ACTIVE, PENDING as COMMAND_PENDING, TERMINAL as COMMAND_TERMINAL,
)
from host_registered_operations import TERMINAL as REGISTERED_TERMINAL
from host_runtime_operations import TERMINAL as RUNTIME_TERMINAL, _lock, _save_private_json
from runtime_rollout_web import TERMINAL as ROLLOUT_TERMINAL


VERSION = 1
MAX_RECORD_BYTES = 4 * 1024 * 1024


class LegacyInvalidationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _private_directory(path: Path) -> Path:
    path = Path(path).absolute()
    try:
        info = path.lstat()
    except OSError as exc:
        raise LegacyInvalidationError("LEGACY_STORE_UNSAFE") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise LegacyInvalidationError("LEGACY_STORE_UNSAFE")
    return path


def _exact_directory(value, root: Path, name: str) -> None:
    path = _private_directory(Path(value))
    if path != root / name:
        raise LegacyInvalidationError("LEGACY_STORE_MISMATCH")


def _exact_lock(value, root: Path, name: str) -> None:
    if Path(value).absolute() != root / name:
        raise LegacyInvalidationError("LEGACY_STORE_MISMATCH")


def _pinned_file(path: Path, root: Path) -> dict:
    path = Path(path).absolute()
    try:
        if path.parent != root:
            raise LegacyInvalidationError("LEGACY_STORE_UNSAFE")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or not 0 <= info.st_size <= MAX_RECORD_BYTES):
            raise LegacyInvalidationError("LEGACY_RECORD_UNSAFE")
        raw = path.read_bytes()
        after = path.lstat()
    except LegacyInvalidationError:
        raise
    except OSError as exc:
        raise LegacyInvalidationError("LEGACY_RECORD_UNSAFE") from exc
    if (len(raw) != info.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
        raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
    return {
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode), "nlink": info.st_nlink,
        "device": info.st_dev, "inode": info.st_ino, "bytes": len(raw),
    }


def _same_pin(pin: dict) -> bool:
    try:
        path = Path(pin["path"])
        current = _pinned_file(path, path.parent)
    except (KeyError, LegacyInvalidationError):
        return False
    return all(current.get(key) == pin.get(key) for key in (
        "sha256", "uid", "mode", "nlink", "device", "inode", "bytes"))


def _receipt(path: Path, root: Path) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _pinned_file(path, root)


def _routing_fields(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as exc:
        raise LegacyInvalidationError("LEGACY_RECORD_INVALID") from exc
    if not isinstance(value, dict):
        raise LegacyInvalidationError("LEGACY_RECORD_INVALID")
    return value


class LegacyAuthorityInvalidator:
    """Store-specific inventory/CAS invalidation for the legacy authority surface."""

    def __init__(
            self, *, host_access_state_dir, host_command_state_dir, host_runtime_state_dir,
            registered_state_dir, agent_task_state_dir, rollout_state_dir,
            host_access_store, host_command_store, host_runtime_store, registered_store,
            agent_task_store, rollout_store):
        self.roots = {
            "host_access": _private_directory(Path(host_access_state_dir)),
            "host_commands": _private_directory(Path(host_command_state_dir)),
            "host_runtime": _private_directory(Path(host_runtime_state_dir)),
            "registered": _private_directory(Path(registered_state_dir)),
            "agent_tasks": _private_directory(Path(agent_task_state_dir)),
            "runtime_rollout": _private_directory(Path(rollout_state_dir)),
        }
        self.access = host_access_store
        self.commands = host_command_store
        self.runtime = host_runtime_store
        self.registered = registered_store
        self.agents = agent_task_store
        self.rollout = rollout_store
        expected = {
            "host_access": Path(self.access.state_dir).absolute(),
            "host_commands": Path(self.commands.state_dir).absolute(),
            "host_runtime": Path(self.runtime.state_dir).absolute(),
            "registered": Path(self.registered.registered_dir).absolute(),
            "agent_tasks": Path(self.agents.root).absolute(),
            "runtime_rollout": Path(self.rollout.state_root).absolute(),
        }
        if expected != self.roots:
            raise LegacyInvalidationError("LEGACY_STORE_MISMATCH")
        _exact_directory(self.access.records_dir, self.roots["host_access"], "records")
        _exact_directory(self.access.locks_dir, self.roots["host_access"], "locks")
        _exact_directory(self.commands.plans_dir, self.roots["host_commands"], "plans")
        _exact_directory(self.commands.receipts_dir, self.roots["host_commands"], "receipts")
        _exact_lock(self.commands.lock_path, self.roots["host_commands"], "operations.lock")
        _exact_directory(self.runtime.operations_dir, self.roots["host_runtime"], "operations")
        _exact_lock(self.runtime.lock_path, self.roots["host_runtime"], "operations.lock")
        _exact_directory(self.runtime.target_authorizations_dir,
                         self.roots["host_runtime"], "target-authorizations")
        _exact_lock(self.runtime.target_authorizations_lock_path,
                    self.roots["host_runtime"], "target-authorizations.lock")
        _exact_directory(self.registered.operations_dir, self.roots["registered"], "operations")
        _exact_directory(self.registered.receipts_dir, self.roots["registered"], "receipts")
        _exact_directory(self.registered.events_dir, self.roots["registered"], "events")
        _exact_lock(self.registered.lock_path, self.roots["registered"], "operations.lock")
        _exact_directory(self.agents.tasks_dir, self.roots["agent_tasks"], "tasks")
        _exact_directory(self.agents.receipts_dir, self.roots["agent_tasks"], "receipts")
        _exact_lock(self.agents.lock, self.roots["agent_tasks"], "agent-tasks.lock")
        _exact_directory(self.rollout.plans_dir, self.roots["runtime_rollout"], "plans")
        _exact_directory(self.rollout.receipts_dir, self.roots["runtime_rollout"], "receipts")
        _exact_lock(self.rollout.lock, self.roots["runtime_rollout"], "rollout.lock")

    @staticmethod
    def _entry(store, identifier, state, classification, pin, *, project=None,
               target=None, receipt=None, actions=(), invariant_sha256=None):
        return {
            "store": store, "id": identifier, "project": project, "target": target,
            "state": state, "classification": classification, "record": pin,
            "receipt": receipt, "actions": list(actions),
            "invariant_sha256": invariant_sha256,
        }

    @staticmethod
    def _invariant(store, record):
        value = copy.deepcopy(record)
        if store == "host_access":
            value["requests"] = []
            if value.get("grant") is not None:
                value["grant"]["revoked_at"] = None
        elif store == "target_authorization":
            value["enabled"] = False
            value.pop("updated_at", None)
        else:
            for key in ("state", "phase", "code", "finished_at", "termination_verified"):
                value.pop(key, None)
        return _digest(value)

    def _access_entries(self):
        entries = []
        records = self.access.records_dir
        expected = set()
        for project, root in sorted(self.access.projects.items()):
            path = self.access._record_path(project)
            if not path.exists() and not path.is_symlink():
                continue
            expected.add(path.name)
            before = _pinned_file(path, records)
            record = self.access._load(project, root)
            after = _pinned_file(path, records)
            if before != after:
                raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
            actions = []
            if record["requests"]:
                actions.append("clear_requests")
            if record["grant"] is not None and record["grant"]["revoked_at"] is None:
                actions.append("revoke_grant")
            entries.append(self._entry(
                "host_access", project, "authority", "invalidate" if actions else "preserve",
                before, project=project, actions=actions,
                invariant_sha256=self._invariant("host_access", record)))
        unknown = {path.name for path in records.glob("*.json")} - expected
        if unknown:
            raise LegacyInvalidationError("LEGACY_UNKNOWN_RECORD")
        return entries

    def _plan_entries(self):
        entries = []

        command_ids = {path.stem for path in self.commands.plans_dir.glob("*.json")}
        command_receipts = {path.stem for path in self.commands.receipts_dir.glob("*.json")}
        if not command_receipts <= command_ids:
            raise LegacyInvalidationError("LEGACY_ORPHAN_RECEIPT")

        for path in sorted(self.commands.plans_dir.glob("*.json")):
            pin = _pinned_file(path, self.commands.plans_dir)
            state = self.commands._load(path.stem)
            classification = self._classification(
                state["state"], COMMAND_PENDING, COMMAND_ACTIVE, COMMAND_TERMINAL)
            receipt = _receipt(self.commands._receipt_path(path.stem), self.commands.receipts_dir)
            entries.append(self._entry(
                "host_commands", path.stem, state["state"], classification, pin,
                project=state["project"], receipt=receipt,
                actions=("cancel_plan", "remove_receipt") if classification == "invalidate" else (),
                invariant_sha256=self._invariant("host_commands", state)))

        runtime_records = {path.stem for path in self.runtime.operations_dir.glob("*.json")
                           if not path.name.endswith(".approval.json")}
        runtime_receipts = {path.name.removesuffix(".approval.json")
                            for path in self.runtime.operations_dir.glob("*.approval.json")}
        if not runtime_receipts <= runtime_records:
            raise LegacyInvalidationError("LEGACY_ORPHAN_RECEIPT")
        for path in sorted(self.runtime.operations_dir.glob("*.json")):
            if path.name.endswith(".approval.json"):
                continue
            pin = _pinned_file(path, self.runtime.operations_dir)
            raw = _routing_fields(path)
            state = self.runtime._load(raw.get("project"), raw.get("target"), path.stem)
            classification = self._classification(
                state.get("state"), {"pending_host_approval", "approved"},
                {"queued", "running"}, RUNTIME_TERMINAL)
            receipt = _receipt(
                self.runtime.operations_dir / (path.stem + ".approval.json"),
                self.runtime.operations_dir)
            entries.append(self._entry(
                "host_runtime", path.stem, state["state"], classification, pin,
                project=state["project"], target=state["target"], receipt=receipt,
                actions=("reject_plan", "remove_receipt") if classification == "invalidate" else (),
                invariant_sha256=self._invariant("host_runtime", state)))

        authorization_path = self.runtime._target_authorization_path(
            "tossApp", "tarororo-test")
        expected_authorizations = set()
        if authorization_path.exists() or authorization_path.is_symlink():
            expected_authorizations.add(authorization_path.name)
            pin = _pinned_file(authorization_path, self.runtime.target_authorizations_dir)
            record = self.runtime._read_target_authorization_record(
                "tossApp", "tarororo-test")
            enabled = record["enabled"]
            entries.append(self._entry(
                "target_authorization", "tossApp/tarororo-test",
                "enabled" if enabled else "revoked", "invalidate" if enabled else "preserve",
                pin, project="tossApp", target="tarororo-test",
                actions=("revoke_target_authorization",) if enabled else (),
                invariant_sha256=self._invariant("target_authorization", record)))
        observed_authorizations = {
            path.name for path in self.runtime.target_authorizations_dir.glob("*.json")}
        if observed_authorizations != expected_authorizations:
            raise LegacyInvalidationError("LEGACY_UNKNOWN_RECORD")

        registered_ids = {path.stem for path in self.registered.operations_dir.glob("*.json")}
        registered_receipts = {path.stem for path in self.registered.receipts_dir.glob("*.json")}
        if not registered_receipts <= registered_ids:
            raise LegacyInvalidationError("LEGACY_ORPHAN_RECEIPT")
        for path in sorted(self.registered.operations_dir.glob("*.json")):
            pin = _pinned_file(path, self.registered.operations_dir)
            raw = _routing_fields(path)
            state = self.registered._load(raw.get("project"), raw.get("target"), path.stem)
            classification = self._classification(
                state["state"], {"pending_host_approval", "approved"},
                {"queued", "running"}, REGISTERED_TERMINAL)
            receipt = _receipt(self.registered._receipt_path(path.stem), self.registered.receipts_dir)
            entries.append(self._entry(
                "registered", path.stem, state["state"], classification, pin,
                project=state["project"], target=state["target"], receipt=receipt,
                actions=("reject_plan", "remove_receipt") if classification == "invalidate" else (),
                invariant_sha256=self._invariant("registered", state)))

        agent_ids = {path.stem for path in self.agents.tasks_dir.glob("*.json")}
        agent_receipts = {path.stem for path in self.agents.receipts_dir.glob("*.json")}
        if not agent_receipts <= agent_ids:
            raise LegacyInvalidationError("LEGACY_ORPHAN_RECEIPT")
        for path in sorted(self.agents.tasks_dir.glob("*.json")):
            pin = _pinned_file(path, self.agents.tasks_dir)
            raw = _routing_fields(path)
            state = self.agents._load(raw.get("project"), path.stem)
            classification = self._classification(
                state["state"], {"pending_host_approval", "approved"},
                AGENT_ACTIVE, AGENT_TERMINAL)
            receipt = _receipt(self.agents.receipts_dir / (path.stem + ".json"),
                               self.agents.receipts_dir)
            entries.append(self._entry(
                "agent_tasks", path.stem, state["state"], classification, pin,
                project=state["project"], receipt=receipt,
                actions=("cancel_plan", "remove_receipt") if classification == "invalidate" else (),
                invariant_sha256=self._invariant("agent_tasks", state)))

        rollout_ids = {path.stem for path in self.rollout.plans_dir.glob("*.json")}
        rollout_receipts = {path.stem for path in self.rollout.receipts_dir.glob("*.json")}
        if not rollout_receipts <= rollout_ids:
            raise LegacyInvalidationError("LEGACY_ORPHAN_RECEIPT")
        for path in sorted(self.rollout.plans_dir.glob("*.json")):
            pin = _pinned_file(path, self.rollout.plans_dir)
            state = self.rollout._load(path.stem)
            classification = self._classification(
                state["state"], {"pending_host_approval", "approved"},
                {"queued", "running"}, ROLLOUT_TERMINAL)
            receipt = _receipt(self.rollout.receipts_dir / (path.stem + ".json"),
                               self.rollout.receipts_dir)
            entries.append(self._entry(
                "runtime_rollout", path.stem, state["state"], classification, pin,
                project=state["project"], receipt=receipt,
                actions=("cancel_plan", "remove_receipt") if classification == "invalidate" else (),
                invariant_sha256=self._invariant("runtime_rollout", state)))
        return entries

    @staticmethod
    def _classification(state, pending, active, terminal):
        if state in pending:
            return "invalidate"
        if state in active:
            return "block"
        if state in terminal:
            return "preserve"
        raise LegacyInvalidationError("LEGACY_UNKNOWN_STATE")

    def inventory(self) -> dict:
        entries = self._access_entries() + self._plan_entries()
        blockers = [{"code": "LEGACY_EXECUTION_ACTIVE", "store": item["store"],
                     "id": item["id"], "state": item["state"]}
                    for item in entries if item["classification"] == "block"]
        core = {"version": VERSION, "entries": entries, "blockers": blockers}
        actions = sum(len(item["actions"]) for item in entries)
        return {**core, "action_count": actions, "digest": _digest(core)}

    @staticmethod
    def _validate_inventory(inventory):
        if (not isinstance(inventory, dict) or inventory.get("version") != VERSION
                or not isinstance(inventory.get("entries"), list)
                or not isinstance(inventory.get("blockers"), list)):
            raise LegacyInvalidationError("LEGACY_INVENTORY_INVALID")
        core = {key: inventory[key] for key in ("version", "entries", "blockers")}
        if inventory.get("digest") != _digest(core):
            raise LegacyInvalidationError("LEGACY_INVENTORY_INVALID")

    @staticmethod
    def _receipt_matches(expected, current):
        if expected is None:
            return current is None
        if current is None:
            return True
        return current["sha256"] == expected["sha256"] and current["inode"] == expected["inode"]

    @staticmethod
    def _desired_state(item, current):
        if item["store"] == "host_access":
            return (current["state"] == "authority" and not current["actions"]
                    and current["invariant_sha256"] == item["invariant_sha256"])
        if item["store"] == "target_authorization":
            return (current["state"] == "revoked"
                    and current["invariant_sha256"] == item["invariant_sha256"])
        desired = {
            "host_commands": "cancelled", "host_runtime": "rejected",
            "registered": "rejected", "agent_tasks": "cancelled",
            "runtime_rollout": "cancelled",
        }[item["store"]]
        return (current["state"] == desired
                and current["invariant_sha256"] == item["invariant_sha256"])

    def _preflight(self, inventory):
        self._validate_inventory(inventory)
        if inventory["blockers"]:
            raise LegacyInvalidationError("LEGACY_EXECUTION_ACTIVE")
        current = self.inventory()
        if current["blockers"]:
            raise LegacyInvalidationError("LEGACY_EXECUTION_ACTIVE")
        expected_by_key = {(item["store"], item["id"]): item
                           for item in inventory["entries"]}
        current_by_key = {(item["store"], item["id"]): item
                          for item in current["entries"]}
        if set(expected_by_key) != set(current_by_key):
            raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
        progress = {}
        for key, expected in expected_by_key.items():
            observed = current_by_key[key]
            if expected["classification"] == "preserve":
                if (observed["record"] != expected["record"]
                        or observed["receipt"] != expected["receipt"]):
                    raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
                continue
            if (observed["record"]["sha256"] == expected["record"]["sha256"]
                    and self._receipt_matches(expected["receipt"], observed["receipt"])):
                continue
            if self._desired_state(expected, observed):
                if not self._receipt_matches(expected["receipt"], observed["receipt"]):
                    raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
                progress[key] = "done" if observed["receipt"] is None else "receipt_only"
                continue
            raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
        return progress

    @staticmethod
    def _remove_receipt(pin):
        if pin is None:
            return
        path = Path(pin["path"])
        directory = None
        try:
            directory = os.open(
                path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            parent_info = os.fstat(directory)
            if (not stat.S_ISDIR(parent_info.st_mode)
                    or parent_info.st_uid != os.getuid()
                    or stat.S_IMODE(parent_info.st_mode) != 0o700):
                raise LegacyInvalidationError("LEGACY_STORE_UNSAFE")

            try:
                os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                os.fsync(directory)
                return

            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode)
                        or before.st_uid != os.getuid()
                        or stat.S_IMODE(before.st_mode) != 0o600
                        or before.st_nlink != 1
                        or not 0 <= before.st_size <= MAX_RECORD_BYTES):
                    raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
                raw = bytearray()
                while len(raw) <= MAX_RECORD_BYTES:
                    chunk = os.read(descriptor, min(64 * 1024, MAX_RECORD_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                after = os.fstat(descriptor)
                named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            finally:
                os.close(descriptor)

            current = {
                "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                "uid": before.st_uid, "mode": stat.S_IMODE(before.st_mode),
                "nlink": before.st_nlink, "device": before.st_dev,
                "inode": before.st_ino, "bytes": len(raw),
            }
            stable = (
                stat.S_ISREG(before.st_mode)
                and not stat.S_ISLNK(before.st_mode)
                and before.st_uid == os.getuid()
                and stat.S_IMODE(before.st_mode) == 0o600
                and before.st_nlink == 1
                and 0 <= before.st_size <= MAX_RECORD_BYTES
                and len(raw) == before.st_size
                and (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                == (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                and (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
                == (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            )
            if not stable or current != pin:
                raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")
            os.unlink(path.name, dir_fd=directory)
            os.fsync(directory)
        except LegacyInvalidationError:
            raise
        except OSError as exc:
            raise LegacyInvalidationError("LEGACY_INVALIDATION_FAILED") from exc
        finally:
            if directory is not None:
                os.close(directory)

    def invalidate(self, expected_inventory: dict) -> dict:
        progress = self._preflight(expected_inventory)
        changed = []
        for item in expected_inventory["entries"]:
            if item["classification"] != "invalidate":
                continue
            key = (item["store"], item["id"])
            if progress.get(key) == "done":
                changed.append({"store": item["store"], "id": item["id"]})
                continue
            if progress.get(key) == "receipt_only":
                self._remove_receipt(item["receipt"])
                changed.append({"store": item["store"], "id": item["id"]})
                continue
            if item["store"] == "host_access":
                self._invalidate_access(item)
            elif item["store"] == "host_commands":
                self._invalidate_command(item)
            elif item["store"] == "target_authorization":
                self._invalidate_target_authorization(item)
            elif item["store"] == "host_runtime":
                self._invalidate_runtime(item)
            elif item["store"] == "registered":
                self._invalidate_registered(item)
            elif item["store"] == "agent_tasks":
                self._invalidate_agent(item)
            elif item["store"] == "runtime_rollout":
                self._invalidate_rollout(item)
            else:
                raise LegacyInvalidationError("LEGACY_INVENTORY_INVALID")
            changed.append({"store": item["store"], "id": item["id"]})
        after = self.inventory()
        if (after["blockers"] or after["action_count"]
                or any(item["classification"] != "preserve" for item in after["entries"])):
            raise LegacyInvalidationError("LEGACY_INVALIDATION_UNVERIFIED")
        receipt_core = {
            "version": VERSION, "before_digest": expected_inventory["digest"],
            "after_digest": after["digest"], "changed": changed,
            "preserved": [{"store": item["store"], "id": item["id"],
                           "sha256": item["record"]["sha256"]}
                          for item in expected_inventory["entries"]
                          if item["classification"] == "preserve"],
        }
        return {**receipt_core, "changed_count": len(changed), "digest": _digest(receipt_core)}

    def _assert_item(self, item, path):
        if str(path.absolute()) != item["record"]["path"] or not _same_pin(item["record"]):
            raise LegacyInvalidationError("LEGACY_RECORD_CHANGED")

    def _invalidate_access(self, item):
        project = item["project"]
        root = self.access.projects[project]
        path = self.access._record_path(project)
        with _lock(self.access._lock_path(project)):
            self._assert_item(item, path)
            record = self.access._load(project, root)
            record["requests"] = []
            if record["grant"] is not None and record["grant"]["revoked_at"] is None:
                record["grant"]["revoked_at"] = _now()
            _save_private_json(path, record)

    def _invalidate_command(self, item):
        with _lock(self.commands.lock_path):
            path = self.commands._state_path(item["id"])
            self._assert_item(item, path)
            state = self.commands._load(item["id"], item["project"])
            state.update(state="cancelled", phase="COMPLETE", code="WORKSPACE_ACTIVATED",
                         finished_at=_now(), termination_verified=True)
            self.commands._save(state)
            self._remove_receipt(item["receipt"])

    def _invalidate_target_authorization(self, item):
        with _lock(self.runtime.target_authorizations_lock_path):
            path = self.runtime._target_authorization_path(item["project"], item["target"])
            self._assert_item(item, path)
            record = self.runtime._read_target_authorization_record(
                item["project"], item["target"])
            record["enabled"] = False
            record["updated_at"] = _now()
            _save_private_json(path, record)

    def _invalidate_runtime(self, item):
        with _lock(self.runtime.lock_path):
            path = self.runtime._path(item["id"])
            self._assert_item(item, path)
            state = self.runtime._load(item["project"], item["target"], item["id"])
            state.update(state="rejected", phase="APPROVAL", code="WORKSPACE_ACTIVATED",
                         finished_at=_now())
            self.runtime._save(state)
            self._remove_receipt(item["receipt"])

    def _invalidate_registered(self, item):
        with _lock(self.registered.lock_path):
            path = self.registered._path(item["id"])
            self._assert_item(item, path)
            state = self.registered._load(item["project"], item["target"], item["id"])
            self._remove_receipt(item["receipt"])
            self.registered._transition(
                state, ("APPROVAL", "WORKSPACE_ACTIVATED", {}), state="rejected",
                phase="APPROVAL", code="WORKSPACE_ACTIVATED", finished_at=_now())

    def _invalidate_agent(self, item):
        with _lock(self.agents.lock):
            path = self.agents._path(item["id"])
            self._assert_item(item, path)
            state = self.agents._load(item["project"], item["id"])
            state.update(state="cancelled", phase="CANCEL", code="WORKSPACE_ACTIVATED",
                         finished_at=_now(), termination_verified=True)
            self.agents._save(state)
            self._remove_receipt(item["receipt"])

    def _invalidate_rollout(self, item):
        with _lock(self.rollout.lock):
            path = self.rollout._plan_path(item["id"])
            self._assert_item(item, path)
            state = self.rollout._load(item["id"])
            state.update(state="cancelled", phase="APPROVAL", code="WORKSPACE_ACTIVATED",
                         finished_at=_now())
            self.rollout._save(state)
            self._remove_receipt(item["receipt"])

    def verify_invalidated(self, receipt: dict) -> dict:
        if not isinstance(receipt, dict):
            raise LegacyInvalidationError("LEGACY_RECEIPT_INVALID")
        core = {key: receipt.get(key) for key in (
            "version", "before_digest", "after_digest", "changed", "preserved")}
        if (receipt.get("version") != VERSION or receipt.get("digest") != _digest(core)
                or receipt.get("changed_count") != len(receipt.get("changed", []))):
            raise LegacyInvalidationError("LEGACY_RECEIPT_INVALID")
        current = self.inventory()
        if (current["digest"] != receipt["after_digest"] or current["blockers"]
                or current["action_count"]
                or any(item["classification"] != "preserve" for item in current["entries"])):
            raise LegacyInvalidationError("LEGACY_INVALIDATION_UNVERIFIED")
        by_key = {(item["store"], item["id"]): item for item in current["entries"]}
        for changed in receipt["changed"]:
            item = by_key.get((changed.get("store"), changed.get("id")))
            if item is None or item["classification"] != "preserve" or item["receipt"] is not None:
                raise LegacyInvalidationError("LEGACY_INVALIDATION_UNVERIFIED")
        for preserved in receipt["preserved"]:
            item = by_key.get((preserved.get("store"), preserved.get("id")))
            if item is None or item["record"]["sha256"] != preserved.get("sha256"):
                raise LegacyInvalidationError("LEGACY_INVALIDATION_UNVERIFIED")
        return {"verified": True, "inventory_digest": current["digest"],
                "changed_count": receipt["changed_count"]}
