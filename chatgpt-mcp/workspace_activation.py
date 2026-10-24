"""Owner-only first Workspace activation and recoverable local evidence.

The journal records intent and pinned observations. It never replaces process
drain, native-store validation or stable launch admission with a success flag.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from workspace_paths import (
    MAX_FILE_BYTES, WorkspaceError, read_owner_file, validate_private_directory,
    write_file,
)


_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PHASE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FIELDS = {
    "version", "transaction_id", "source_digest", "revision", "phase", "plan",
    "prepared", "effects", "evidence", "error",
}
_PLAN_FIELDS = {
    "policy_candidate", "policy", "projects", "minimum_abi_file",
    "target_release", "target_digest", "target_abi", "rollback_release",
    "rollback_digest", "rollback_abi", "allowed_releases", "minimum_abi",
    "authority_dir", "controller_dir", "controller_manifest",
    "controller_before", "source_dir", "legacy_inventory",
}


class ActivationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code, cause=None):
    raise ActivationError(code) from cause


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        _fail("ACTIVATION_JOURNAL_INVALID", exc)


def _identifier(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("ACTIVATION_ID_INVALID")
    return value


def _source(value):
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("ACTIVATION_SOURCE_INVALID")
    return value


def _parse(raw):
    def exact(pairs):
        result = dict(pairs)
        if len(pairs) != len(result):
            _fail("ACTIVATION_JOURNAL_INVALID")
        return result
    try:
        value = json.loads(raw, object_pairs_hook=exact,
                           parse_constant=lambda _: _fail("ACTIVATION_JOURNAL_INVALID"))
    except (ValueError, TypeError, UnicodeError) as exc:
        _fail("ACTIVATION_JOURNAL_INVALID", exc)
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or value.get("version") != 1
            or type(value.get("revision")) is not int or value["revision"] < 0
            or not isinstance(value.get("phase"), str)
            or _PHASE.fullmatch(value["phase"]) is None
            or not isinstance(value.get("plan"), dict)
            or not isinstance(value.get("effects"), dict)
            or not isinstance(value.get("evidence"), dict)
            or value.get("prepared") is not None and not isinstance(value["prepared"], dict)
            or value.get("error") is not None and not isinstance(value["error"], str)):
        _fail("ACTIVATION_JOURNAL_INVALID")
    _identifier(value.get("transaction_id"))
    _source(value.get("source_digest"))
    return value


def _parse_plan_seal(raw):
    def exact(pairs):
        result = dict(pairs)
        if len(pairs) != len(result):
            _fail("ACTIVATION_PLAN_SEAL_INVALID")
        return result
    try:
        value = json.loads(
            raw, object_pairs_hook=exact,
            parse_constant=lambda _: _fail("ACTIVATION_PLAN_SEAL_INVALID"),
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        _fail("ACTIVATION_PLAN_SEAL_INVALID", exc)
    if (not isinstance(value, dict)
            or set(value) != {
                "version", "transaction_id", "source_digest", "plan_sha256"}
            or value.get("version") != 1
            or not isinstance(value.get("transaction_id"), str)
            or _ID.fullmatch(value["transaction_id"]) is None
            or not isinstance(value.get("source_digest"), str)
            or _DIGEST.fullmatch(value["source_digest"]) is None
            or not isinstance(value.get("plan_sha256"), str)
            or _DIGEST.fullmatch(value["plan_sha256"]) is None):
        _fail("ACTIVATION_PLAN_SEAL_INVALID")
    return value


class _JournalSession:
    def __init__(self, journal, descriptor, transaction_id):
        self.journal = journal
        self.descriptor = descriptor
        self.transaction_id = transaction_id
        self.snapshot, self.state = journal._read(transaction_id)

    def save(self, phase, **changes):
        self.journal._check_lock(self.descriptor)
        current, _ = self.journal._read(self.transaction_id)
        if (current.sha256, current.identity) != (self.snapshot.sha256, self.snapshot.identity):
            _fail("ACTIVATION_JOURNAL_CHANGED")
        if not set(changes) <= {"prepared", "effects", "evidence", "error"}:
            _fail("ACTIVATION_JOURNAL_INVALID")
        value = copy.deepcopy(self.state)
        value.update(changes)
        value.update(phase=phase, revision=value["revision"] + 1)
        payload = _canonical(value)
        _parse(payload)
        try:
            write_file(self.journal.directory, self.transaction_id + ".json", payload,
                       expected_sha256=self.snapshot.sha256)
        except WorkspaceError as exc:
            _fail("ACTIVATION_JOURNAL_CHANGED", exc)
        self.snapshot, self.state = self.journal._read(self.transaction_id)
        return copy.deepcopy(self.state)


class ActivationJournal:
    """Private fixed-lock journal outside all configured project roots."""

    def __init__(self, directory, projects):
        self.directory = Path(directory).absolute()
        self.projects = {name: Path(root).absolute() for name, root in projects.items()}

    def _validate_location(self):
        if any(self.directory == root or self.directory.is_relative_to(root)
               or root.is_relative_to(self.directory) for root in self.projects.values()):
            _fail("ACTIVATION_JOURNAL_IN_PROJECT")
        try:
            validate_private_directory(self.directory)
        except WorkspaceError as exc:
            if not self.directory.exists() and not self.directory.is_symlink():
                _fail("ACTIVATION_JOURNAL_MISSING", exc)
            _fail("ACTIVATION_JOURNAL_UNSAFE", exc)

    def _create_directory(self):
        if any(self.directory == root or self.directory.is_relative_to(root)
               or root.is_relative_to(self.directory) for root in self.projects.values()):
            _fail("ACTIVATION_JOURNAL_IN_PROJECT")
        try:
            validate_private_directory(self.directory.parent)
        except WorkspaceError as exc:
            _fail("ACTIVATION_JOURNAL_UNSAFE", exc)
        try:
            self.directory.mkdir(mode=0o700)
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            parent = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except FileExistsError:
            pass
        except OSError as exc:
            _fail("ACTIVATION_JOURNAL_UNSAFE", exc)
        self._validate_location()

    def _read(self, transaction_id):
        transaction_id = _identifier(transaction_id)
        self._validate_location()
        path = self.directory / (transaction_id + ".json")
        try:
            snapshot = read_owner_file(path, max_bytes=MAX_FILE_BYTES,
                                       forbidden_roots=self.projects.values())
        except WorkspaceError as exc:
            if not path.exists() and not path.is_symlink():
                _fail("ACTIVATION_JOURNAL_MISSING", exc)
            _fail("ACTIVATION_JOURNAL_UNSAFE", exc)
        value = _parse(snapshot.data)
        if value["transaction_id"] != transaction_id:
            _fail("ACTIVATION_JOURNAL_INVALID")
        self._verify_plan_seal(transaction_id, value)
        return snapshot, value

    def _plan_seal(self, transaction_id, source_digest, plan):
        return {
            "version": 1,
            "transaction_id": transaction_id,
            "source_digest": source_digest,
            "plan_sha256": hashlib.sha256(_canonical(plan)).hexdigest(),
        }

    def _verify_plan_seal(self, transaction_id, value):
        path = self.directory / (transaction_id + ".plan.json")
        try:
            snapshot = read_owner_file(
                path, max_bytes=4096, forbidden_roots=self.projects.values())
            seal = _parse_plan_seal(snapshot.data)
        except (WorkspaceError, ActivationError) as exc:
            _fail("ACTIVATION_PLAN_SEAL_INVALID", exc)
        expected = self._plan_seal(
            transaction_id, value["source_digest"], value["plan"])
        if seal != expected:
            _fail("ACTIVATION_PLAN_CHANGED")

    def read(self, transaction_id):
        return self._read(transaction_id)[1]

    def _check_lock(self, descriptor):
        self._validate_location()
        try:
            info = os.fstat(descriptor)
            current = (self.directory / "activation.lock").lstat()
        except OSError as exc:
            _fail("ACTIVATION_LOCK_CHANGED", exc)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
            _fail("ACTIVATION_LOCK_CHANGED")

    @contextmanager
    def _locked(self):
        self._validate_location()
        descriptor = None
        try:
            path = self.directory / "activation.lock"
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            except FileExistsError:
                descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._check_lock(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._check_lock(descriptor)
            yield descriptor
            self._check_lock(descriptor)
        except BlockingIOError as exc:
            _fail("ACTIVATION_JOURNAL_BUSY", exc)
        except OSError as exc:
            _fail("ACTIVATION_JOURNAL_UNSAFE", exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def create(self, transaction_id, source_digest, plan):
        transaction_id = _identifier(transaction_id)
        source_digest = _source(source_digest)
        self._create_directory()
        value = {
            "version": 1, "transaction_id": transaction_id,
            "source_digest": source_digest, "revision": 0, "phase": "prepared",
            "plan": plan, "prepared": None, "effects": {}, "evidence": {}, "error": None,
        }
        payload = _canonical(value)
        _parse(payload)
        with self._locked():
            try:
                write_file(
                    self.directory, transaction_id + ".plan.json",
                    _canonical(self._plan_seal(transaction_id, source_digest, plan)),
                    create=True,
                )
                write_file(self.directory, transaction_id + ".json", payload, create=True)
            except WorkspaceError as exc:
                if exc.code == "TARGET_EXISTS":
                    _fail("ACTIVATION_JOURNAL_EXISTS", exc)
                _fail("ACTIVATION_JOURNAL_UNSAFE", exc)
        return copy.deepcopy(value)

    @contextmanager
    def locked(self, transaction_id, source_digest):
        transaction_id = _identifier(transaction_id)
        source_digest = _source(source_digest)
        with self._locked() as descriptor:
            current = _JournalSession(self, descriptor, transaction_id)
            if current.state["source_digest"] != source_digest:
                _fail("ACTIVATION_SOURCE_CHANGED")
            yield current


def pin_owner_file(path, projects, *, optional=False):
    path = Path(path).absolute()
    try:
        observed = read_owner_file(path, forbidden_roots=projects.values())
    except WorkspaceError as exc:
        if optional and exc.code == "OWNER_FILE_MISSING" and not path.is_symlink():
            validate_private_directory(path.parent)
            return {"path": str(path), "sha256": None, "identity": None}
        _fail("ACTIVATION_INPUT_UNSAFE", exc)
    return {"path": str(path), "sha256": observed.sha256,
            "identity": list(observed.identity)}


def verify_owner_pin(pin, projects):
    observed = pin_owner_file(pin["path"], projects, optional=pin["sha256"] is None)
    if observed != pin:
        _fail("ACTIVATION_INPUT_CHANGED")
    return observed


class ActivationCoordinator:
    """Coordinate observed native adapters; only the owner CLI constructs this."""

    def __init__(self, projects, *, journal, authority, host, legacy, rollout_lock,
                 controller_dir, source_dir=None):
        self.projects = {name: Path(root).absolute() for name, root in projects.items()}
        self.journal = journal
        self.authority = authority
        self.host = host
        self.legacy = legacy
        self.rollout_lock = rollout_lock
        self.controller_dir = Path(controller_dir).absolute()
        self.source_dir = Path(source_dir or Path(__file__).resolve().parent).absolute()

    def _source_digest(self):
        # All imported candidate files are pinned by the immutable target manifest.
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def _release(self, path):
        from workspace_control import verify_release_manifest
        observed = verify_release_manifest(path)
        manifest = json.loads(read_owner_file(Path(path) / "manifest.json").data)
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        if digest != observed["release_digest"]:
            _fail("ACTIVATION_RELEASE_CHANGED")
        return {**observed, "manifest": manifest}

    def prepare(self, transaction_id, *, policy_candidate, policy_path, projects_path,
                target_release, rollback_release, target_abi, rollback_abi, minimum_abi):
        from workspace_control import (
            ENFORCEMENT_ABI, WorkspaceControlError, minimum_abi_path,
            release_enforcement_abi, verify_controller,
        )
        from workspace_policy import WorkspacePolicy
        policy_path = Path(policy_path).absolute()
        projects_path = Path(projects_path).absolute()
        if (policy_path.name != "workspace-policy.json"
                or policy_path.parent != projects_path.parent):
            _fail("ACTIVATION_POLICY_DESTINATION_INVALID")
        self.authority._check_state_location()
        if (any(type(item) is not int or item < 1
                for item in (target_abi, rollback_abi, minimum_abi))
                or minimum_abi < ENFORCEMENT_ABI
                or min(target_abi, rollback_abi) < minimum_abi):
            _fail("ACTIVATION_ABI_INCOMPATIBLE")
        candidate = pin_owner_file(policy_candidate, self.projects)
        snapshot = WorkspacePolicy(self.projects, candidate["path"]).load(required=True)
        if (snapshot.version >= 2
                and min(target_abi, rollback_abi, minimum_abi) < 3):
            _fail("ACTIVATION_ABI_INCOMPATIBLE")
        if any("read" in rule["operations"]
               and rule.get("content_mode", "json_fields") == "json_fields"
               and not rule["fields"]
               for rule in snapshot.artifacts):
            _fail("ACTIVATION_ARTIFACT_FIELDS_REQUIRED")
        projects_pin = pin_owner_file(projects_path, self.projects)
        configured = json.loads(read_owner_file(Path(projects_pin["path"])).data)
        if (set(configured) != {"projects"} or configured["projects"] != {
                name: str(root) for name, root in self.projects.items()}):
            _fail("ACTIVATION_PROJECTS_CHANGED")
        target = self._release(Path(target_release).absolute())
        rollback = self._release(Path(rollback_release).absolute())
        try:
            declared_target_abi = release_enforcement_abi(target_release)
            declared_rollback_abi = release_enforcement_abi(rollback_release)
        except WorkspaceControlError as exc:
            _fail("ACTIVATION_ABI_INCOMPATIBLE", exc)
        if (declared_target_abi != target_abi
                or declared_rollback_abi != rollback_abi
                or (target["release_digest"] == rollback["release_digest"]
                    and target_abi != rollback_abi)):
            _fail("ACTIVATION_ABI_INCOMPATIBLE")
        target_path = Path(target_release).absolute()
        if (self.source_dir != target_path
                or target["manifest"].get("workspace_activation.py") != self._source_digest()
                or target["manifest"].get("workspace_control.py")
                != hashlib.sha256((self.source_dir / "workspace_control.py").read_bytes()).hexdigest()):
            _fail("ACTIVATION_SOURCE_NOT_INSTALLED")
        if self.authority.state_dir.exists() or self.authority.enforcement_path.exists():
            _fail("ACTIVATION_ALREADY_INITIALIZED")
        inventory = self.legacy.inventory()
        if inventory["blockers"]:
            _fail("ACTIVATION_EXECUTION_ACTIVE")
        controller_manifest = {
            name: hashlib.sha256((self.source_dir / name).read_bytes()).hexdigest()
            for name in ("workspace_control.py", "workspace-launcher.py")
        }
        controller_before = None
        if self.controller_dir.exists() or self.controller_dir.is_symlink():
            verify_controller(self.controller_dir)
            controller_before = {
                name: pin_owner_file(self.controller_dir / name, self.projects)
                for name in (*controller_manifest, "controller-manifest.json")
            }
        else:
            validate_private_directory(self.controller_dir.parent)
        plan = {
            "policy_candidate": candidate,
            "policy": pin_owner_file(policy_path, self.projects, optional=True),
            "projects": projects_pin,
            "minimum_abi_file": pin_owner_file(
                minimum_abi_path(self.authority.state_dir), self.projects, optional=True),
            "target_release": str(target_path),
            "target_digest": target["release_digest"],
            "target_abi": target_abi,
            "rollback_release": str(Path(rollback_release).absolute()),
            "rollback_digest": rollback["release_digest"],
            "rollback_abi": rollback_abi,
            "allowed_releases": {target["release_digest"]: target_abi,
                                 rollback["release_digest"]: rollback_abi},
            "minimum_abi": minimum_abi,
            "authority_dir": str(self.authority.state_dir),
            "controller_dir": str(self.controller_dir),
            "controller_manifest": controller_manifest,
            "controller_before": controller_before,
            "source_dir": str(self.source_dir),
            "legacy_inventory": inventory,
        }
        return self.journal.create(transaction_id, self._source_digest(), plan)

    def _validate_plan(self, plan):
        from workspace_control import ENFORCEMENT_ABI
        if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
            _fail("ACTIVATION_PLAN_INVALID")
        paths = (
            "target_release", "rollback_release", "authority_dir",
            "controller_dir", "source_dir",
        )
        controller_manifest = plan.get("controller_manifest")
        if (plan.get("source_dir") != plan.get("target_release")
                or any(not isinstance(plan.get(name), str)
                       or not Path(plan[name]).is_absolute() for name in paths)
                or not isinstance(plan.get("target_digest"), str)
                or _DIGEST.fullmatch(plan["target_digest"]) is None
                or not isinstance(plan.get("rollback_digest"), str)
                or _DIGEST.fullmatch(plan["rollback_digest"]) is None
                or any(type(plan.get(name)) is not int or plan[name] < 1
                       for name in ("target_abi", "rollback_abi", "minimum_abi"))
                or min(plan["target_abi"], plan["rollback_abi"])
                < plan["minimum_abi"]
                or plan["minimum_abi"] < ENFORCEMENT_ABI
                or not isinstance(plan.get("allowed_releases"), dict)
                or not isinstance(controller_manifest, dict)
                or set(controller_manifest) != {
                    "workspace_control.py", "workspace-launcher.py"}
                or any(not isinstance(value, str)
                       or _DIGEST.fullmatch(value) is None
                       for value in controller_manifest.values())
                or (plan.get("controller_before") is not None
                    and not isinstance(plan["controller_before"], dict))):
            _fail("ACTIVATION_PLAN_INVALID")
        expected = {plan["target_digest"]: plan["target_abi"]}
        if plan["rollback_digest"] in expected:
            if expected[plan["rollback_digest"]] != plan["rollback_abi"]:
                _fail("ACTIVATION_PLAN_INVALID")
        else:
            expected[plan["rollback_digest"]] = plan["rollback_abi"]
        if plan["allowed_releases"] != expected:
            _fail("ACTIVATION_PLAN_INVALID")

    def _revalidate(self, state):
        from workspace_control import WorkspaceControlError, release_enforcement_abi
        plan = state["plan"]
        self._validate_plan(plan)
        if (state["source_digest"] != self._source_digest()
                or plan["source_dir"] != str(self.source_dir)
                or plan["authority_dir"] != str(self.authority.state_dir)
                or plan["controller_dir"] != str(self.controller_dir)):
            _fail("ACTIVATION_SOURCE_CHANGED")
        verify_owner_pin(plan["policy_candidate"], self.projects)
        verify_owner_pin(plan["projects"], self.projects)
        for label in ("target", "rollback"):
            observed = self._release(Path(plan[label + "_release"]))
            if observed["release_digest"] != plan[label + "_digest"]:
                _fail("ACTIVATION_RELEASE_CHANGED")
            try:
                declared = release_enforcement_abi(plan[label + "_release"])
            except WorkspaceControlError as exc:
                _fail("ACTIVATION_ABI_INCOMPATIBLE", exc)
            if declared != plan[label + "_abi"]:
                _fail("ACTIVATION_ABI_INCOMPATIBLE")
        for name, expected in plan["controller_manifest"].items():
            if hashlib.sha256((self.source_dir / name).read_bytes()).hexdigest() != expected:
                _fail("ACTIVATION_SOURCE_CHANGED")

    def _publish(self, current, label, pin, payload):
        effects = copy.deepcopy(current.state["effects"])
        target = hashlib.sha256(payload).hexdigest()
        intent = {"before": pin, "target_sha256": target}
        if label not in effects:
            verify_owner_pin(pin, self.projects)
            effects[label] = intent
            current.save(label + "_intent", effects=effects)
        elif effects[label] != intent:
            _fail("ACTIVATION_JOURNAL_INVALID")
        path = Path(pin["path"])
        observed = pin_owner_file(path, self.projects, optional=True)
        if observed["sha256"] == target:
            return observed
        if observed != pin:
            _fail("ACTIVATION_INPUT_CHANGED")
        if pin["sha256"] is not None:
            backup = label + "-before-" + current.transaction_id + ".json"
            raw = read_owner_file(path).data
            backup_path = self.journal.directory / backup
            if backup_path.exists():
                if read_owner_file(backup_path).sha256 != pin["sha256"]:
                    _fail("ACTIVATION_BACKUP_CHANGED")
            else:
                write_file(self.journal.directory, backup, raw, create=True)
        try:
            write_file(path.parent, path.name, payload,
                       expected_sha256=pin["sha256"], create=pin["sha256"] is None)
        except WorkspaceError as exc:
            _fail("ACTIVATION_INPUT_CHANGED", exc)
        return pin_owner_file(path, self.projects)

    def _compatibility(self, plan, prepared):
        """Run only the verified release's local admission/authority readers."""
        from workspace_control import minimum_abi_path
        source = self.authority.state_dir.parent / prepared["stage_name"]
        if not source.exists():
            source = self.authority.state_dir
        self.authority._verify_prepared_tree(source, prepared, published=source == self.authority.state_dir)
        with tempfile.TemporaryDirectory(prefix=".compat-", dir=self.journal.directory) as folder:
            root = Path(folder)
            root.chmod(0o700)
            state = root / "authority"
            state.mkdir(mode=0o700)
            (state / "locks").mkdir(mode=0o700)
            for name in ("authority.json", "authority.lock", "cursor.key", "activated"):
                data = read_owner_file(source / name).data
                write_file(state, name, data, create=True)
            latch = state.parent / ("." + state.name + ".enforcement-required")
            write_file(root, latch.name,
                       (hashlib.sha256(str(state).encode()).hexdigest() + "\n").encode(), create=True)
            minimum = {"version": 1,
                       "authority_path_sha256": hashlib.sha256(str(state).encode()).hexdigest(),
                       "minimum_abi": plan["minimum_abi"]}
            write_file(root, minimum_abi_path(state).name, _canonical(minimum), create=True)
            script = (
                "import json,sys; sys.path.insert(0,sys.argv[1]); "
                "from workspace_control import (WorkspaceControlError,activation_admission,"
                "activation_maintenance,verify_launch); "
                "from workspace_authority import WorkspaceAuthority; "
                "p=json.loads(sys.argv[3]); "
                "a=WorkspaceAuthority(p,sys.argv[2]); "
                "b=a.binding(sys.argv[4]); "
                "m=activation_maintenance(sys.argv[2],'a'*32,'b'*64); m.__enter__(); "
                "m.allow_start(b); denied=False; "
                "\ntry:\n with activation_admission(sys.argv[2],create=True): pass"
                "\nexcept WorkspaceControlError as e:\n denied=(e.code=='WORKSPACE_ACTIVATION_IN_PROGRESS')"
                "\nassert denied; "
                "v=verify_launch(sys.argv[1],sys.argv[2],enforcement_required=True); "
                "m=activation_maintenance(sys.argv[2],'a'*32,'b'*64); m.__enter__(); "
                "m.finish(b); assert v['binding']==b; print(json.dumps(v,sort_keys=True))"
            )
            observations = {}
            for label in ("target", "rollback"):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", script, plan[label + "_release"],
                     str(state), _canonical({name: str(root) for name, root in self.projects.items()}).decode(),
                     plan["policy_candidate"]["sha256"]],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False,
                )
                if result.returncode or len(result.stdout) > 65536:
                    _fail("ACTIVATION_RELEASE_SEMANTICS_UNVERIFIED")
                try:
                    observation = json.loads(result.stdout)
                except (ValueError, UnicodeError) as exc:
                    _fail("ACTIVATION_RELEASE_SEMANTICS_UNVERIFIED", exc)
                if (observation.get("binding") != prepared["binding"]
                        or observation.get("release_digest") != plan[label + "_digest"]
                        or observation.get("release_abi")
                        != plan[label + "_abi"]
                        or observation.get("enforcement_abi") != plan[label + "_abi"]
                        or observation.get("minimum_abi") != plan["minimum_abi"]):
                    _fail("ACTIVATION_RELEASE_SEMANTICS_UNVERIFIED")
                observations[label] = {key: observation[key] for key in (
                    "release_digest", "release_abi", "binding")}
            return observations

    def _policy_binding(self, plan, binding):
        from workspace_policy import WorkspacePolicy
        snapshot = WorkspacePolicy(self.projects, plan["policy"]["path"]).load(required=True)
        if (snapshot.digest != plan["policy_candidate"]["sha256"]
                or self.authority.binding(snapshot.digest) != binding):
            _fail("ACTIVATION_BINDING_CHANGED")
        minimum = read_owner_file(Path(plan["minimum_abi_file"]["path"])).data
        expected = {"version": 1, "minimum_abi": plan["minimum_abi"],
                    "authority_path_sha256": hashlib.sha256(str(self.authority.state_dir).encode()).hexdigest()}
        if minimum != _canonical(expected):
            _fail("ACTIVATION_MINIMUM_ABI_CHANGED")

    def _install_controller(self, current):
        from workspace_control import install_controller
        plan = current.state["plan"]
        current.save("controller_intent")
        before = plan["controller_before"]
        if self.controller_dir.exists() or self.controller_dir.is_symlink():
            validate_private_directory(self.controller_dir)
            expected = dict(plan["controller_manifest"])
            expected["controller-manifest.json"] = hashlib.sha256(
                json.dumps(plan["controller_manifest"], sort_keys=True).encode()).hexdigest()
            if {path.name for path in self.controller_dir.iterdir()} - {
                    *expected, "install.lock"}:
                _fail("ACTIVATION_CONTROLLER_CHANGED")
            for name, digest in expected.items():
                path = self.controller_dir / name
                observed = pin_owner_file(path, self.projects, optional=True)
                original = before.get(name) if before is not None else {
                    "path": str(path), "sha256": None, "identity": None}
                if observed != original and observed["sha256"] != digest:
                    _fail("ACTIVATION_CONTROLLER_CHANGED")
        elif before is not None:
            _fail("ACTIVATION_CONTROLLER_CHANGED")
        installed = install_controller(self.controller_dir, self.source_dir, projects=self.projects)
        if installed["manifest"] != plan["controller_manifest"]:
            _fail("ACTIVATION_CONTROLLER_CHANGED")

    def apply(self, transaction_id):
        from workspace_control import (
            activation_maintenance, verify_controller,
        )
        with self.rollout_lock(), self.journal.locked(transaction_id, self._source_digest()) as current:
            self._revalidate(current.state)
            plan = current.state["plan"]
            if current.state["phase"] == "complete":
                self._policy_binding(plan, current.state["prepared"]["binding"])
                return self.host.verify_started(current.state["effects"]["host_plan"])
            try:
                with activation_maintenance(self.authority.state_dir, transaction_id,
                                            current.state["source_digest"]) as maintenance:
                    self._apply_stopped(current)
                    binding = current.state["prepared"]["binding"]
                    self._policy_binding(plan, binding)
                    controller = verify_controller(self.controller_dir)
                    if controller["manifest"] != plan["controller_manifest"]:
                        _fail("ACTIVATION_CONTROLLER_CHANGED")
                    maintenance.allow_start(binding)
                host_plan = current.state["effects"]["host_plan"]
                evidence = copy.deepcopy(current.state["evidence"])
                current.save("start_intent")
                evidence["started"] = self.host.start(host_plan)
                self._policy_binding(plan, binding)
                with activation_maintenance(
                        self.authority.state_dir, transaction_id,
                        current.state["source_digest"]) as maintenance:
                    maintenance.finish(binding)
                current.save("complete", evidence=evidence, error=None)
                return evidence["started"]
            except BaseException as exc:
                # After commit, recreating maintenance intent closes admission before
                # stopping only the exact newly started target processes.
                cleanup_error = None
                if current.state["prepared"] is not None and self.authority.state_dir.exists():
                    try:
                        with activation_maintenance(self.authority.state_dir, transaction_id,
                                                    current.state["source_digest"]):
                            host_plan = current.state["effects"].get("host_plan")
                            if host_plan is not None:
                                self.host.stop_started(host_plan)
                    except Exception as cleanup:
                        cleanup_error = getattr(cleanup, "code", "ACTIVATION_CLEANUP_UNVERIFIED")
                code = getattr(exc, "code", "ACTIVATION_INTERRUPTED")
                evidence = copy.deepcopy(current.state["evidence"])
                if cleanup_error is not None:
                    evidence["cleanup_error"] = str(cleanup_error)
                current.save("interrupted", error=str(code), evidence=evidence)
                raise

    def _apply_stopped(self, current):
        from workspace_control import minimum_abi_path
        plan = current.state["plan"]
        effects = copy.deepcopy(current.state["effects"])
        evidence = copy.deepcopy(current.state["evidence"])
        # Commit may have completed before its journal acknowledgement. Reobserve;
        # commit_initial never regenerates or rewinds an already published tree.
        if current.state["prepared"] is not None and self.authority.state_dir.exists():
            binding = self.authority.commit_initial(current.state["prepared"])
            if binding != current.state["prepared"]["binding"]:
                _fail("ACTIVATION_BINDING_CHANGED")
            if "host_plan" not in effects or "legacy" not in evidence:
                _fail("ACTIVATION_JOURNAL_INVALID")
            recovered = self.host.recover_committed_plan(effects["host_plan"])
            if recovered != effects["host_plan"]:
                effects["host_plan"] = recovered
                current.save("committed_recovery_plan", effects=effects)
            current.save("committed_recovery_intent")
            # Publication proves only authority identity. A crash may have left
            # target services running, or invalidated the earlier drain/legacy
            # observations. Recheck them under maintenance before any restart.
            evidence["drain"] = self.host.stop_started(effects["host_plan"])
            self.legacy.verify_invalidated(evidence["legacy"])
            evidence["compatibility"] = self._compatibility(plan, current.state["prepared"])
            self._revalidate(current.state)
            self.host.verify_drained(effects["host_plan"])
            self.host.verify_wired(effects["host_plan"])
            self.legacy.verify_invalidated(evidence["legacy"])
            current.save("authority_committed", evidence=evidence)
            return
        self._install_controller(current)
        if "host_plan" not in effects:
            effects["host_plan"] = self.host.prepare(
                plan["target_release"], plan["rollback_release"],
                self.authority.state_dir, self.controller_dir)
            current.save("host_prepared", effects=effects)
        host_plan = effects["host_plan"]
        current.save("drain_intent")
        evidence["drain"] = self.host.stop(host_plan)
        current.save("legacy_invalidation_intent", evidence=evidence)
        # Original complete inventory is already fsynced in the prepared journal.
        evidence["legacy"] = self.legacy.invalidate(plan["legacy_inventory"])
        current.save("legacy_invalidated", evidence=evidence)
        current.save("wiring_intent")
        evidence["wiring"] = self.host.wire(host_plan)
        current.save("wired", evidence=evidence)
        policy_raw = read_owner_file(Path(plan["policy_candidate"]["path"])).data
        self._publish(current, "policy", plan["policy"], policy_raw)
        minimum_payload = _canonical({
            "version": 1, "minimum_abi": plan["minimum_abi"],
            "authority_path_sha256": hashlib.sha256(str(self.authority.state_dir).encode()).hexdigest(),
        })
        if str(minimum_abi_path(self.authority.state_dir)) != plan["minimum_abi_file"]["path"]:
            _fail("ACTIVATION_INPUT_CHANGED")
        self._publish(current, "minimum_abi", plan["minimum_abi_file"], minimum_payload)
        if current.state["prepared"] is None:
            current.save("authority_prepare_intent")
            prepared = self.authority.prepare_initial(
                plan["policy_candidate"]["sha256"], plan["allowed_releases"], current.transaction_id)
            current.save("authority_prepared", prepared=prepared)
        prepared = current.state["prepared"]
        effects = copy.deepcopy(current.state["effects"])
        effects["host_plan"]["expected_binding"] = {
            "release_digest": plan["target_digest"],
            "release_abi": plan["allowed_releases"][plan["target_digest"]],
            "runtime": plan["target_release"], "binding": prepared["binding"],
        }
        current.save("compatibility_intent", effects=effects)
        evidence["compatibility"] = self._compatibility(plan, prepared)
        self._revalidate(current.state)
        self.host.verify_drained(effects["host_plan"])
        self.host.verify_wired(effects["host_plan"])
        self.legacy.verify_invalidated(evidence["legacy"])
        if read_owner_file(Path(plan["policy"]["path"])).sha256 != plan["policy_candidate"]["sha256"]:
            _fail("ACTIVATION_INPUT_CHANGED")
        current.save("authority_commit_intent", evidence=evidence)
        binding = self.authority.commit_initial(prepared)
        if binding != prepared["binding"]:
            _fail("ACTIVATION_BINDING_CHANGED")
        current.save("authority_committed")
