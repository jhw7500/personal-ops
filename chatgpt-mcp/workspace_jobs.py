"""Private command snapshots and explicit single-file publication.

Publication serializes cooperating workspace writers through the authority
lock and commits one target plus one receipt.  It does not claim atomicity
across multiple targets or isolation from an uncooperative process running as
the same host UID.
"""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from pathspec import GitIgnoreSpec

from workspace_paths import WorkspaceError, is_sensitive, open_directory, read_file, write_file


MAX_FILE_BYTES = 1024 * 1024
MAX_JOB_INPUTS = 16


def _require_text(value, code):
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkspaceError(code)
    return value


def _relative(value, code):
    value = _require_text(value, code)
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(
            part in ("", ".", "..") for part in path.parts):
        raise WorkspaceError(code)
    return path


def _private_directory(path, *, create=True):
    path = Path(path)
    created = False
    if create:
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError("JOB_STATE_UNSAFE") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()):
        raise WorkspaceError("JOB_STATE_UNSAFE")
    if created:
        os.chmod(path, 0o700)
        info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise WorkspaceError("JOB_STATE_UNSAFE")
    return path.resolve(strict=True)


def _write_private_json(path, document):
    path = Path(path)
    data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory)
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise WorkspaceError("PUBLICATION_RECEIPT_INVALID")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except OSError as exc:
        raise WorkspaceError("PUBLICATION_RECEIPT_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _read_private_json(path):
    path = Path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    data = read_file(path.parent, path.name, max_bytes=64 * 1024)
    if data.mode != 0o600:
        raise WorkspaceError("PUBLICATION_RECEIPT_INVALID")
    try:
        document = json.loads(data.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("PUBLICATION_RECEIPT_INVALID") from exc
    if not isinstance(document, dict):
        raise WorkspaceError("PUBLICATION_RECEIPT_INVALID")
    return document


class WorkspaceJobFiles:
    """Build filtered snapshots and publish one verified output at a time.

    The returned record is private trusted job metadata.  Public MCP results
    must never accept a caller-provided replacement for it.
    """

    def __init__(self, policy, authority, state_dir):
        self.policy = policy
        self.authority = authority
        self.state_dir = Path(state_dir)

    def _state(self):
        root = _private_directory(self.state_dir)
        jobs = _private_directory(root / "jobs")
        receipts = _private_directory(root / "publications")
        return root, jobs, receipts

    @staticmethod
    def _binding_matches(record, binding, snapshot):
        recorded = record.get("binding") if isinstance(record, dict) else None
        if not isinstance(recorded, dict) or recorded != binding:
            raise WorkspaceError("JOB_STALE")
        if binding.get("policy_digest") != snapshot.digest:
            raise WorkspaceError("JOB_STALE")

    def require_current(self, record):
        if not isinstance(record, dict):
            raise WorkspaceError("JOB_RECORD_INVALID")
        snapshot = self.policy.load(required=True)
        try:
            with self.authority.admission(snapshot.digest) as binding:
                self._binding_matches(record, binding, snapshot)
                has_inputs = "inputs" in record
                has_bindings = "input_bindings" in record
                if has_inputs != has_bindings:
                    raise WorkspaceError("JOB_RECORD_INVALID")
                bindings = record.get("input_bindings", [])
                if not isinstance(bindings, list) or len(bindings) > MAX_JOB_INPUTS:
                    raise WorkspaceError("JOB_RECORD_INVALID")
                for index, input_binding in enumerate(bindings):
                    expected_path = f"/inputs/{index}/input"
                    if (not isinstance(input_binding, dict)
                            or set(input_binding) != {
                                "path", "grant_id", "sha256", "sandbox_path"}
                            or input_binding.get("sandbox_path") != expected_path
                            or not self._valid_digest(input_binding.get("sha256"))):
                        raise WorkspaceError("JOB_RECORD_INVALID")
                    try:
                        path = self._input_path(input_binding.get("path"))
                    except WorkspaceError as exc:
                        raise WorkspaceError("JOB_RECORD_INVALID") from exc
                    grant_id = input_binding.get("grant_id")
                    if not self._valid_grant_id(grant_id):
                        raise WorkspaceError("JOB_RECORD_INVALID")
                    self.authority.require(
                        record.get("project"), str(path), "read", grant_id,
                        snapshot.digest)
        except WorkspaceError as exc:
            if exc.code == "AUTHORITY_BINDING_STALE":
                raise WorkspaceError("JOB_STALE") from exc
            raise
        return snapshot

    @staticmethod
    def _valid_digest(value):
        return (isinstance(value, str) and len(value) == 64
                and all(character in "0123456789abcdef" for character in value))

    @staticmethod
    def _valid_grant_id(value):
        return (isinstance(value, str) and len(value) == 32
                and all(character in "0123456789abcdef" for character in value))

    @staticmethod
    def _input_path(value):
        if (not isinstance(value, str) or not 0 < len(value) <= 4096
                or not value.isprintable() or "\0" in value
                or not Path(value).is_absolute()
                or value != os.path.normpath(value)):
            raise WorkspaceError("JOB_INPUTS_INVALID")
        return Path(value)

    @classmethod
    def _input_specs(cls, inputs):
        if inputs is None:
            return None
        if not isinstance(inputs, list) or len(inputs) > MAX_JOB_INPUTS:
            raise WorkspaceError("JOB_INPUTS_INVALID")
        result = []
        for value in inputs:
            if not isinstance(value, dict) or set(value) != {"path", "grant_id"}:
                raise WorkspaceError("JOB_INPUTS_INVALID")
            path = cls._input_path(value.get("path"))
            grant_id = value.get("grant_id")
            if not cls._valid_grant_id(grant_id):
                raise WorkspaceError("JOB_INPUTS_INVALID")
            result.append((path, grant_id))
        return result

    @staticmethod
    def _within(path, root):
        return path == root or path.is_relative_to(root)

    def _check_input_path(self, snapshot, path):
        resolved = Path(os.path.realpath(path))
        if resolved != path:
            raise WorkspaceError("UNSAFE_PATH")
        relative_absolute = "/".join(resolved.parts[1:])
        if is_sensitive(relative_absolute):
            raise WorkspaceError("SENSITIVE_PATH")
        protected = tuple(Path(os.path.realpath(value)) for value in snapshot.protected_paths)
        authority_root = Path(os.path.realpath(self.authority.state_dir.parent))
        runtime_root = Path(os.path.realpath(
            Path.home() / ".local" / "lib" / "chatgpt-coding-mcp"))
        if (any(self._within(resolved, root) for root in protected)
                or self._within(resolved, authority_root)
                or self._within(resolved, runtime_root)):
            raise WorkspaceError("PROTECTED_PATH")

        from workspace_files import is_ignored

        for project, root_value in self.policy.projects.items():
            root = Path(root_value).resolve(strict=True)
            if resolved == root or not resolved.is_relative_to(root):
                continue
            relative = resolved.relative_to(root).as_posix()
            try:
                self.policy.artifact_rule(snapshot, project, relative)
            except WorkspaceError as exc:
                if exc.code != "ARTIFACT_NOT_REGISTERED":
                    raise
            else:
                raise WorkspaceError("WORKSPACE_SOURCE_EXCLUDED")
            if is_ignored(root, relative):
                raise WorkspaceError("WORKSPACE_SOURCE_EXCLUDED")
        return resolved

    @staticmethod
    def _copy_input(path, destination):
        file_data = read_file(path.parent, path.name, max_bytes=MAX_FILE_BYTES)
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400)
        try:
            view = memoryview(file_data.data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise WorkspaceError("WRITE_FAILED")
                view = view[written:]
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return file_data

    @staticmethod
    def _ignored(specs, relative, is_directory=False):
        ignored = False
        for base, spec in specs:
            try:
                local = relative.relative_to(base).as_posix()
            except ValueError:
                continue
            if is_directory:
                local += "/"
            decision = spec.check_file(local)
            if decision.include is not None:
                ignored = decision.include
        return bool(ignored)

    @staticmethod
    def _gitignore(root, relative):
        try:
            file_data = read_file(root, (relative / ".gitignore").as_posix())
        except WorkspaceError as exc:
            if exc.code in {"FILE_NOT_FOUND", "UNSAFE_PATH", "UNSAFE_FILE",
                            "SENSITIVE_PATH", "FILE_TOO_LARGE"}:
                return None
            raise
        try:
            lines = file_data.data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return None
        return GitIgnoreSpec.from_lines(lines)

    def _copy_snapshot(self, snapshot, project, source):
        project_root = Path(self.policy.projects[project]).resolve(strict=True)
        protected = tuple(Path(path) for path in snapshot.protected_paths)
        manifest = []

        def visit(relative, specs, directory):
            local_specs = list(specs)
            ignore = self._gitignore(project_root, relative)
            if ignore is not None:
                local_specs.append((relative, ignore))
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise WorkspaceError("SNAPSHOT_SOURCE_UNSAFE") from exc
            for entry in entries:
                child = relative / entry.name
                child_text = child.as_posix()
                if is_sensitive(child_text) or entry.is_symlink():
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise WorkspaceError("SNAPSHOT_SOURCE_UNSAFE") from exc
                absolute = project_root.joinpath(*child.parts)
                if any(absolute == protected_path or absolute.is_relative_to(protected_path)
                       for protected_path in protected):
                    continue
                try:
                    self.policy.artifact_rule(snapshot, project, child_text)
                except WorkspaceError as exc:
                    if exc.code != "ARTIFACT_NOT_REGISTERED":
                        raise
                else:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if self._ignored(local_specs, child, True):
                        continue
                    destination = source.joinpath(*child.parts)
                    destination.mkdir(mode=0o700)
                    try:
                        child_directory = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
                    except OSError as exc:
                        raise WorkspaceError("SNAPSHOT_SOURCE_UNSAFE") from exc
                    try:
                        visit(child, local_specs, child_directory)
                    finally:
                        os.close(child_directory)
                    os.chmod(destination, 0o500)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                if self._ignored(local_specs, child):
                    continue
                try:
                    self.policy.check(project, child_text, "read", snapshot=snapshot)
                    file_data = read_file(project_root, child_text, max_bytes=MAX_FILE_BYTES)
                except WorkspaceError as exc:
                    if exc.code in {"SENSITIVE_PATH", "PROTECTED_PATH", "UNSAFE_FILE",
                                    "UNSAFE_PATH", "FILE_TOO_LARGE", "FILE_NOT_FOUND"}:
                        continue
                    raise
                destination = source.joinpath(*child.parts)
                descriptor = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600)
                try:
                    view = memoryview(file_data.data)
                    while view:
                        view = view[os.write(descriptor, view):]
                    snapshot_mode = 0o500 if file_data.mode & 0o111 else 0o400
                    os.fchmod(descriptor, snapshot_mode)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                manifest.append({
                    "path": child_text,
                    "sha256": file_data.sha256,
                    "size": file_data.size,
                    "mode": file_data.mode,
                })

        root_directory = open_directory(project_root)
        try:
            visit(PurePosixPath(), [], root_directory)
        finally:
            os.close(root_directory)
        os.chmod(source, 0o500)
        return manifest

    def create(self, project, request_id, *, inputs=None):
        _require_text(project, "UNKNOWN_PROJECT")
        _require_text(request_id, "JOB_REQUEST_INVALID")
        input_specs = self._input_specs(inputs)
        snapshot = self.policy.load(required=True)
        if project not in self.policy.projects:
            raise WorkspaceError("UNKNOWN_PROJECT")
        with ExitStack() as stack:
            stack.enter_context(self.authority.project_lock(project))
            try:
                binding = stack.enter_context(self.authority.admission(snapshot.digest))
            except WorkspaceError as exc:
                if exc.code == "AUTHORITY_BINDING_STALE":
                    raise WorkspaceError("JOB_STALE") from exc
                raise
            _, jobs, _ = self._state()
            job_id = uuid.uuid4().hex
            job_root = _private_directory(jobs / job_id)
            source = _private_directory(job_root / "source")
            scratch = _private_directory(job_root / "scratch")
            output = _private_directory(job_root / "output")
            manifest = self._copy_snapshot(snapshot, project, source)
            record = {
                "id": job_id,
                "project": project,
                "request_id": request_id,
                "source": str(source),
                "scratch": str(scratch),
                "output": str(output),
                "binding": binding,
                "source_manifest": manifest,
            }
            if input_specs is not None:
                inputs_root = _private_directory(job_root / "inputs")
                input_bindings = []
                for index, (path, grant_id) in enumerate(input_specs):
                    resolved = self._check_input_path(snapshot, path)
                    self.authority.require(
                        project, str(resolved), "read", grant_id,
                        snapshot.digest)
                    input_root = _private_directory(inputs_root / str(index))
                    file_data = self._copy_input(resolved, input_root / "input")
                    os.chmod(input_root, 0o500)
                    input_bindings.append({
                        "path": str(path),
                        "grant_id": grant_id,
                        "sha256": file_data.sha256,
                        "sandbox_path": f"/inputs/{index}/input",
                    })
                os.chmod(inputs_root, 0o500)
                record["inputs"] = str(inputs_root)
                record["input_bindings"] = input_bindings

            current = self.policy.load(required=True)
            if current.digest != snapshot.digest:
                raise WorkspaceError("JOB_STALE")
            _write_private_json(job_root / "record.json", record)
            return record

    def freeze_outputs(self, record):
        """Seal the export set only after the caller verifies process termination."""
        if "output_manifest" in record:
            return
        source = Path(record["output"])
        frozen = _private_directory(source.parent / "completed-output")
        manifest, errors = {}, {}
        count = total = 0

        def visit(fd, relative, depth):
            nonlocal count, total
            if depth > 32:
                raise WorkspaceError("JOB_OUTPUT_LIMIT")
            for entry in sorted(os.scandir(fd), key=lambda value: value.name):
                count += 1
                if count > 2000:
                    raise WorkspaceError("JOB_OUTPUT_LIMIT")
                child = relative / entry.name
                name = child.as_posix()
                info = entry.stat(follow_symlinks=False)
                if is_sensitive(name):
                    errors[name] = "SENSITIVE_PATH"
                    continue
                if stat.S_ISDIR(info.st_mode):
                    destination = frozen.joinpath(*child.parts)
                    destination.mkdir(mode=0o700)
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=fd)
                    try:
                        visit(child_fd, child, depth + 1)
                    finally:
                        os.close(child_fd)
                    destination.chmod(0o500)
                    continue
                try:
                    data = read_file(source, name)
                except WorkspaceError as exc:
                    errors[name] = exc.code
                    continue
                total += data.size
                if total > 16 * MAX_FILE_BYTES:
                    raise WorkspaceError("JOB_OUTPUT_LIMIT")
                destination = frozen.joinpath(*child.parts)
                output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o400)
                try:
                    view = memoryview(data.data)
                    while view:
                        written = os.write(output_fd, view)
                        if written <= 0:
                            raise WorkspaceError("JOB_OUTPUT_INVALID")
                        view = view[written:]
                    os.fchmod(output_fd, 0o400)
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
                manifest[name] = {"sha256": data.sha256, "size": data.size}

        descriptor = open_directory(source)
        try:
            visit(descriptor, PurePosixPath(), 0)
        finally:
            os.close(descriptor)
        frozen.chmod(0o500)
        record.update(frozen_output=str(frozen), output_manifest=manifest, output_errors=errors)
        _write_private_json(source.parent / "record.json", record)

    def read_output(self, record, relative_path):
        relative = _relative(relative_path, "INVALID_PATH").as_posix()
        if "output_manifest" not in record:
            raise WorkspaceError("JOB_INCOMPLETE")
        if relative in record.get("output_errors", {}):
            raise WorkspaceError(record["output_errors"][relative])
        expected = record["output_manifest"].get(relative)
        if expected is None:
            raise WorkspaceError("FILE_NOT_FOUND")
        data = read_file(record["frozen_output"], relative)
        if data.sha256 != expected["sha256"] or data.size != expected["size"] or data.mode != 0o400:
            raise WorkspaceError("JOB_OUTPUT_STALE")
        return data

    @staticmethod
    def _target_root(target):
        target = Path(target)
        return target.parent, target.name

    @staticmethod
    def _receipt_key(project, record, relative_output, target, request_id):
        identity = json.dumps({
            "project": project,
            "job_id": record["id"],
            "request_id": request_id,
            "output": relative_output,
            "target": str(target),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _current_target(target):
        root, relative = WorkspaceJobFiles._target_root(target)
        try:
            return read_file(root, relative)
        except WorkspaceError as exc:
            if exc.code == "FILE_NOT_FOUND":
                return None
            raise

    @staticmethod
    def _public_receipt(receipt):
        return {key: receipt[key] for key in (
            "publication_id", "project", "job_id", "request_id", "output",
            "target", "sha256", "size", "mode", "status")}

    def publish(self, project, record, relative_output, target_path, request_id, *,
                expected_sha256=None, grant_id=None, completion_verified=False):
        if completion_verified is not True:
            raise WorkspaceError("JOB_INCOMPLETE")
        if not isinstance(record, dict) or record.get("project") != project:
            raise WorkspaceError("JOB_RECORD_INVALID")
        if record.get("request_id") != request_id:
            raise WorkspaceError("JOB_REQUEST_MISMATCH")
        relative = _relative(relative_output, "INVALID_PATH")
        if expected_sha256 is not None and (
                not isinstance(expected_sha256, str) or len(expected_sha256) != 64
                or any(character not in "0123456789abcdef" for character in expected_sha256)):
            raise WorkspaceError("INVALID_DIGEST")

        snapshot = self.policy.load(required=True)
        with ExitStack() as stack:
            stack.enter_context(self.authority.project_lock(project))
            try:
                binding = stack.enter_context(self.authority.admission(snapshot.digest))
            except WorkspaceError as exc:
                if exc.code == "AUTHORITY_BINDING_STALE":
                    raise WorkspaceError("JOB_STALE") from exc
                raise
            self._binding_matches(record, binding, snapshot)

            try:
                target = self.policy.check(
                    project, target_path, "write", snapshot=snapshot)
            except WorkspaceError as exc:
                if exc.code != "WORKSPACE_SCOPE_REQUIRED" or grant_id is None:
                    raise
                target = Path(target_path).resolve(strict=False)
                self.authority.require(
                    project, str(target), "write", grant_id, snapshot.digest)
            else:
                if grant_id is not None:
                    self.authority.require(
                        project, str(target), "write", grant_id, snapshot.digest)
            if is_sensitive(str(target)):
                raise WorkspaceError("SENSITIVE_PATH")

            _, _, receipts = self._state()
            receipt_key = self._receipt_key(
                project, record, relative.as_posix(), target, request_id)
            receipt_path = receipts / f"{receipt_key}.json"
            receipt = _read_private_json(receipt_path)
            if receipt is not None:
                if receipt.get("expected_sha256") != expected_sha256:
                    raise WorkspaceError("PUBLICATION_STALE")
                if receipt.get("status") == "failed":
                    raise WorkspaceError("PUBLICATION_STALE")
                current = self._current_target(target)
                if current is not None and current.sha256 == receipt.get("sha256"):
                    if receipt.get("status") == "complete":
                        return self._public_receipt(receipt)
                    receipt["status"] = "complete"
                    receipt["mode"] = current.mode
                    _write_private_json(receipt_path, receipt)
                    return self._public_receipt(receipt)
                if receipt.get("status") == "complete":
                    raise WorkspaceError("PUBLICATION_STALE")

            if receipt is not None:
                # A crash before target write leaves an immutable pending hash.
                # Recover that exact artifact, never reread mutable staging.
                output = read_file(Path(record["output"]).parent / "frozen", receipt["sha256"])
                if output.sha256 != receipt["sha256"] or output.mode != 0o400:
                    raise WorkspaceError("PUBLICATION_FROZEN_INVALID")
            elif "output_manifest" in record:
                output = self.read_output(record, relative.as_posix())
            else:
                # Owner-level callers also supply verified completion; production
                # JobManager always freezes the full export set at termination.
                output = read_file(record["output"], relative.as_posix(), max_bytes=MAX_FILE_BYTES)
            if b"\0" in output.data:
                raise WorkspaceError("PUBLICATION_BINARY")
            try:
                output.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkspaceError("PUBLICATION_BINARY") from exc

            frozen_dir = _private_directory(
                Path(record["output"]).parent / "frozen")
            frozen_path = frozen_dir / output.sha256
            try:
                frozen = read_file(frozen_dir, output.sha256)
            except WorkspaceError as exc:
                if exc.code != "FILE_NOT_FOUND":
                    raise
                descriptor = os.open(
                    frozen_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o400)
                try:
                    view = memoryview(output.data)
                    while view:
                        view = view[os.write(descriptor, view):]
                    os.fchmod(descriptor, 0o400)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            else:
                if frozen.sha256 != output.sha256 or frozen.mode != 0o400:
                    raise WorkspaceError("PUBLICATION_FROZEN_INVALID")

            pending = {
                "publication_id": receipt_key,
                "project": project,
                "job_id": record["id"],
                "request_id": request_id,
                "output": relative.as_posix(),
                "target": str(target),
                "sha256": output.sha256,
                "size": output.size,
                "mode": None,
                "expected_sha256": expected_sha256,
                "status": "pending",
            }
            if receipt is None:
                _write_private_json(receipt_path, pending)
            target_root, target_relative = self._target_root(target)
            try:
                result = write_file(
                    target_root, target_relative, output.data,
                    expected_sha256=expected_sha256,
                    create=expected_sha256 is None)
            except WorkspaceError:
                pending["status"] = "failed"
                _write_private_json(receipt_path, pending)
                raise
            pending.update(result, status="complete")
            _write_private_json(receipt_path, pending)
            return self._public_receipt(pending)
