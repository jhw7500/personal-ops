"""Process-shared exclusion between broad host work and workspace publication."""

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import time

from host_runtime_config import HostRuntimeError
from workspace_paths import WorkspaceError


LOCK_NAME = ".host-publication.lock"
REGISTRY_LOCK_NAME = ".host-active.lock"
REGISTRY_NAME = ".host-active"
_POLL_SECONDS = 0.02
_MAX_TIMEOUT_SECONDS = 30.0
_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")


def resolve_workspace(projects, workspace=None):
    """Return an explicit or safely discovered active workspace without creating state."""
    if workspace is None:
        if not isinstance(projects, dict) or not projects:
            return None
        try:
            from workspace_tools import WorkspaceTools
            discovered = WorkspaceTools(projects)
            return discovered if discovered.enabled else None
        except (WorkspaceError, OSError, TypeError, ValueError) as exc:
            raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID") from exc
    try:
        from workspace_tools import WorkspaceTools
        if not isinstance(workspace, WorkspaceTools):
            raise TypeError
        return workspace
    except (ImportError, TypeError) as exc:
        raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID") from exc


def _active(workspace):
    if workspace is None:
        return False
    try:
        return workspace.enabled is True
    except Exception as exc:
        raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID") from exc


def _authority_root(workspace):
    try:
        workspace._context()
        root = Path(workspace.authority.state_dir)
        info = root.lstat()
    except (WorkspaceError, OSError, AttributeError, TypeError) as exc:
        raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID")
    return root


def _acquire_lane(workspace, *, exclusive, timeout_seconds):
    if type(timeout_seconds) not in (int, float) or isinstance(timeout_seconds, bool):
        raise HostRuntimeError("INVALID_WORKSPACE_HOST_LANE_TIMEOUT")
    timeout = float(timeout_seconds)
    if not 0 <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise HostRuntimeError("INVALID_WORKSPACE_HOST_LANE_TIMEOUT")
    root = _authority_root(workspace)
    expected_root = root.lstat()
    directory = descriptor = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened_root = os.fstat(directory)
        if ((opened_root.st_dev, opened_root.st_ino) !=
                (expected_root.st_dev, expected_root.st_ino)
                or not stat.S_ISDIR(opened_root.st_mode)
                or opened_root.st_uid != os.getuid()
                or stat.S_IMODE(opened_root.st_mode) != 0o700):
            raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID")
        created = False
        try:
            descriptor = os.open(
                LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory)
        if created:
            os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, operation)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HostRuntimeError("WORKSPACE_HOST_LANE_BUSY") from None
                time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
    except HostRuntimeError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        code = ("WORKSPACE_HOST_LANE_BUSY" if exc.errno in (errno.EACCES, errno.EAGAIN)
                else "WORKSPACE_HOST_LANE_INVALID")
        raise HostRuntimeError(code) from exc
    finally:
        if directory is not None:
            os.close(directory)


def _registry_lock(root):
    try:
        directory = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID") from exc
    descriptor = None
    try:
        created = False
        try:
            descriptor = os.open(
                REGISTRY_LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=directory)
            created = True
        except FileExistsError:
            descriptor = os.open(
                REGISTRY_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory)
        if created:
            os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return directory, descriptor
    except HostRuntimeError:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID") from exc


def _registry_directory(root_fd, *, create):
    created = False
    if create:
        try:
            os.mkdir(REGISTRY_NAME, 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
            created = True
        except FileExistsError:
            pass
    try:
        descriptor = os.open(
            REGISTRY_NAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd)
    except FileNotFoundError:
        return None
    info = os.fstat(descriptor)
    if created:
        os.fchmod(descriptor, 0o700)
        info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(descriptor)
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
    return descriptor


def _validate_reservation(operation_id, kind):
    if (not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None
            or not isinstance(kind, str) or _KIND.fullmatch(kind) is None):
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")


def _entry_name(operation_id, kind):
    return f"{operation_id}--{kind}"


def _register_operation(workspace, operation_id, kind, *, recovery):
    _validate_reservation(operation_id, kind)
    root = _authority_root(workspace)
    root_fd, lock_fd = _registry_lock(root)
    registry_fd = entry_fd = None
    try:
        registry_fd = _registry_directory(root_fd, create=True)
        entry_name = _entry_name(operation_id, kind)
        matches = [name for name in os.listdir(registry_fd)
                   if name.startswith(operation_id + "--")]
        if matches:
            if matches != [entry_name]:
                raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
            entry_fd = os.open(
                entry_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=registry_fd)
            info = os.fstat(entry_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != 0):
                raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
            if recovery is not True:
                raise HostRuntimeError("WORKSPACE_HOST_LANE_BUSY")
            return
        try:
            entry_fd = os.open(
                entry_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                | os.O_CLOEXEC, 0o600, dir_fd=registry_fd)
            os.fchmod(entry_fd, 0o600)
            os.fsync(entry_fd)
            os.fsync(registry_fd)
        except FileExistsError as exc:
            raise HostRuntimeError("WORKSPACE_HOST_LANE_BUSY") from exc
    except OSError as exc:
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID") from exc
    finally:
        if entry_fd is not None:
            os.close(entry_fd)
        if registry_fd is not None:
            os.close(registry_fd)
        os.close(lock_fd)
        os.close(root_fd)


def complete_host_operation(workspace, operation_id, kind, *, termination_verified):
    """Remove one durable reservation only after verified process termination."""
    if not _active(workspace):
        return
    _validate_reservation(operation_id, kind)
    if termination_verified is not True:
        return
    root = _authority_root(workspace)
    root_fd, lock_fd = _registry_lock(root)
    registry_fd = entry_fd = None
    try:
        registry_fd = _registry_directory(root_fd, create=False)
        if registry_fd is None:
            return
        entry_name = _entry_name(operation_id, kind)
        try:
            entry_fd = os.open(
                entry_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=registry_fd)
        except FileNotFoundError:
            return
        info = os.fstat(entry_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != 0):
            raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
        os.unlink(entry_name, dir_fd=registry_fd)
        os.fsync(registry_fd)
    except OSError as exc:
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID") from exc
    finally:
        if entry_fd is not None:
            os.close(entry_fd)
        if registry_fd is not None:
            os.close(registry_fd)
        os.close(lock_fd)
        os.close(root_fd)


def _require_empty_registry(workspace):
    root = _authority_root(workspace)
    root_fd, lock_fd = _registry_lock(root)
    registry_fd = None
    try:
        registry_fd = _registry_directory(root_fd, create=False)
        if registry_fd is not None and os.listdir(registry_fd):
            raise HostRuntimeError("WORKSPACE_HOST_LANE_BUSY")
    except OSError as exc:
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID") from exc
    finally:
        if registry_fd is not None:
            os.close(registry_fd)
        os.close(lock_fd)
        os.close(root_fd)


def acquire_host_operation_lane(workspace, *, timeout_seconds=0,
                                operation_id=None, kind=None, recovery=False):
    """Acquire the shared lane and return its descriptor to a synchronous owner."""
    if not _active(workspace):
        return None
    descriptor = _acquire_lane(
        workspace, exclusive=False, timeout_seconds=timeout_seconds)
    if descriptor is not None and operation_id is not None:
        try:
            _register_operation(
                workspace, operation_id, kind, recovery=recovery)
        except BaseException:
            os.close(descriptor)
            raise
    elif descriptor is not None and kind is not None:
        os.close(descriptor)
        raise HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")
    return descriptor


@contextmanager
def host_operation_lane(workspace, *, timeout_seconds=0):
    operation_id = secrets.token_hex(16)
    descriptor = acquire_host_operation_lane(
        workspace, timeout_seconds=timeout_seconds,
        operation_id=operation_id, kind="context")
    try:
        yield descriptor
    finally:
        complete_host_operation(
            workspace, operation_id, "context", termination_verified=True)
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def publication_lane(workspace, *, timeout_seconds=0):
    descriptor = None
    if _active(workspace):
        descriptor = _acquire_lane(
            workspace, exclusive=True, timeout_seconds=timeout_seconds)
        try:
            _require_empty_registry(workspace)
        except BaseException:
            os.close(descriptor)
            raise
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
