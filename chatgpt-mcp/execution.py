"""Prepare tightly scoped commands for the ChatGPT repository MCP.

The policy deliberately has no unsandboxed fallback.  Package scripts are still
arbitrary code, but they run without a network, credentials, repository metadata,
or access to any repository other than the explicitly selected one.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600
MAX_ARG_COUNT = 128
MAX_ARG_LENGTH = 4096
MAX_COMMAND_LENGTH = 32768
PROFILE_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
SCRIPT_NAMES = ("build", "test", "test:e2e", "typecheck", "lint")
MINIMAL_ENV = {
    "HOME": "/home/sandbox",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "TMPDIR": "/tmp",
}
SANDBOX_NODE_PREFIX = "/opt/node"
SANDBOX_CHROMIUM_PREFIX = "/opt/chromium"
SANDBOX_POSTGRESQL_PREFIX = "/opt/postgresql"
SANDBOX_POSTGRES_JOB = "/opt/mcp/postgres_job.py"
TEST_DATABASES = {"tarororo_test", "tarororo_ci"}


@dataclass(frozen=True)
class CommandSpec:
    command: list[str]
    env: dict[str, str]
    cwd: str
    timeout_seconds: int
    label: str
    argv: list[str]
    execution_argv: list[str]
    relative_cwd: str


@dataclass(frozen=True)
class _Profile:
    name: str
    label: str
    argv: tuple[str, ...]
    relative_cwd: str
    timeout_seconds: int
    source: str
    test_database: str | None = None

    def public(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "label": self.label,
            "argv": list(self.argv),
            "relative_cwd": self.relative_cwd,
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
        }
        if self.test_database is not None:
            result["test_database"] = self.test_database
        return result


class ExecutionPolicy:
    """Validate a command and produce an isolated bubblewrap invocation."""

    def __init__(self, projects: dict[str, Path], config_path: Path | None = None):
        if not projects:
            raise ValueError("At least one project is required")
        self.projects: dict[str, Path] = {}
        for name, root in projects.items():
            if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
                raise ValueError("Project names must be simple identifiers")
            resolved = Path(root).resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError(f"Project {name!r} must be a directory")
            self.projects[name] = resolved

        roots = list(self.projects.items())
        for index, (left_name, left) in enumerate(roots):
            for right_name, right in roots[index + 1:]:
                if left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError(
                        "Allowed project roots must not overlap: "
                        f"{left_name!r} and {right_name!r}")

        self.config_path = (Path(config_path) if config_path is not None else
                            Path.home() / ".config/chatgpt-coding-mcp/execution.json")
        config_location = self.config_path.expanduser().resolve(strict=False)
        if any(config_location.is_relative_to(root) for root in self.projects.values()):
            raise ValueError("Execution config must be outside all writable projects")
        self._bwrap = shutil.which("bwrap")

    def list_profiles(self, project: str) -> list[dict[str, Any]]:
        root = self._project(project)
        self._reject_yocto_repository(root)
        config = self._load_config()
        self._node_prefix(project, config)
        self._chromium_prefix(project, config)
        postgresql_prefix = self._postgresql_prefix(project, config)
        profiles = self._profiles(project, root, config, postgresql_prefix)
        return [profiles[name].public() for name in sorted(profiles)]

    def prepare(
        self,
        project: str,
        *,
        argv: list[str] | None = None,
        preset: str | None = None,
        relative_cwd: str = ".",
        timeout_seconds: int | None = None,
    ) -> CommandSpec:
        if not self._bwrap:
            raise RuntimeError("bubblewrap is required; unrestricted execution is disabled")
        if (argv is None) == (preset is None):
            raise ValueError("Provide exactly one of argv or preset")

        root = self._project(project)
        self._reject_yocto_repository(root)
        config = self._load_config()
        node_prefix = self._node_prefix(project, config)
        chromium_prefix = self._chromium_prefix(project, config)
        postgresql_prefix = self._postgresql_prefix(project, config)
        test_database = None

        if preset is not None:
            profiles = {
                name: profile.public()
                for name, profile in self._profiles(
                    project, root, config, postgresql_prefix).items()
            }
            if preset not in profiles:
                raise ValueError(f"Unknown execution preset for {project!r}: {preset!r}")
            selected = profiles[preset]
            visible_argv = self._validate_argv(selected["argv"])
            selected_cwd = selected["relative_cwd"]
            selected_timeout = (selected["timeout_seconds"] if timeout_seconds is None
                                else self._validate_timeout(timeout_seconds))
            label = selected["label"]
            test_database = selected.get("test_database")
        else:
            visible_argv = self._validate_argv(argv)
            selected_cwd = relative_cwd
            selected_timeout = self._validate_timeout(
                DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds)
            label = "직접 명령 실행"

        execution_argv = visible_argv
        if test_database is not None:
            if postgresql_prefix is None:
                raise ValueError("A test_database profile requires a PostgreSQL toolchain")
            execution_argv = [
                "/usr/bin/python3", SANDBOX_POSTGRES_JOB, test_database, "--", *visible_argv]

        self._reject_bitbake_command(visible_argv)
        host_cwd, normalized_cwd = self._resolve_cwd(root, selected_cwd)
        self._reject_protected_working_tree(root, host_cwd)
        hides = self._secret_mounts(root)
        environment = dict(MINIMAL_ENV)
        if node_prefix is not None:
            environment["PATH"] = f"{SANDBOX_NODE_PREFIX}/bin:{MINIMAL_ENV['PATH']}"
        if chromium_prefix is not None:
            environment["PLAYWRIGHT_CHROMIUM_PATH"] = f"{SANDBOX_CHROMIUM_PREFIX}/chrome"
        if test_database is not None:
            environment["MCP_EPHEMERAL_POSTGRES"] = "1"
        self._reject_installed_bitbake(environment["PATH"], node_prefix)

        command = [
            self._bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        for system_path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(system_path).exists():
                command.extend(("--ro-bind", system_path, system_path))
        command.extend((
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/home",
            "--dir", "/home/sandbox",
            "--bind", str(root), "/workspace",
        ))
        if node_prefix is not None:
            command.extend(("--ro-bind", str(node_prefix), SANDBOX_NODE_PREFIX))
        if chromium_prefix is not None:
            command.extend(("--ro-bind", str(chromium_prefix), SANDBOX_CHROMIUM_PREFIX))
        if test_database is not None:
            wrapper = Path(__file__).resolve().with_name("postgres_job.py")
            if not wrapper.is_file() or wrapper.is_symlink():
                raise ValueError("PostgreSQL job wrapper is unavailable")
            command.extend((
                "--ro-bind", str(postgresql_prefix), SANDBOX_POSTGRESQL_PREFIX,
                "--dir", "/opt/mcp",
                "--ro-bind", str(wrapper), SANDBOX_POSTGRES_JOB,
                "--tmpfs", "/etc",
            ))
        command.extend(hides)
        for key, value in environment.items():
            command.extend(("--setenv", key, value))
        sandbox_cwd = "/workspace" if normalized_cwd == "." else f"/workspace/{normalized_cwd}"
        command.extend(("--chdir", sandbox_cwd, "--"))
        command.extend(execution_argv)

        return CommandSpec(
            command=command,
            env=environment,
            cwd=str(host_cwd),
            timeout_seconds=selected_timeout,
            label=label,
            argv=visible_argv,
            execution_argv=execution_argv,
            relative_cwd=normalized_cwd,
        )

    def _project(self, project: str) -> Path:
        try:
            return self.projects[project]
        except KeyError as exc:
            raise ValueError(f"Unknown project: {project!r}") from exc

    @staticmethod
    def _validate_argv(argv: list[str] | None) -> list[str]:
        if not isinstance(argv, list) or not argv or len(argv) > MAX_ARG_COUNT:
            raise ValueError(f"argv must contain 1 to {MAX_ARG_COUNT} strings")
        if any(not isinstance(arg, str) or not arg or "\0" in arg or
               len(arg) > MAX_ARG_LENGTH for arg in argv):
            raise ValueError("argv contains an invalid or oversized argument")
        if sum(len(arg) for arg in argv) > MAX_COMMAND_LENGTH:
            raise ValueError("Command is too long")
        return list(argv)

    @staticmethod
    def _validate_timeout(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("timeout_seconds must be an integer")
        if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and "
                f"{MAX_TIMEOUT_SECONDS}")
        return value

    @staticmethod
    def _resolve_cwd(root: Path, relative_cwd: str) -> tuple[Path, str]:
        if not isinstance(relative_cwd, str) or not relative_cwd:
            raise ValueError("relative_cwd must be a nonempty relative path")
        relative = Path(relative_cwd)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Working directory must stay inside the selected project")
        try:
            target = (root / relative).resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError("Working directory does not exist") from exc
        if not target.is_dir() or not target.is_relative_to(root):
            raise ValueError("Working directory must stay inside the selected project")
        normalized = target.relative_to(root).as_posix() or "."
        return target, normalized

    @staticmethod
    def _reject_installed_bitbake(path: str, node_prefix: Path | None = None) -> None:
        # Shell/Python commands could invoke an installed task runner indirectly.
        # Do not rely on token matching when such binaries are exposed by /usr.
        directories = [Path(directory) for directory in path.split(":")
                       if directory != f"{SANDBOX_NODE_PREFIX}/bin"]
        if node_prefix is not None:
            directories.append(node_prefix / "bin")
        for directory in directories:
            for binary in Path(directory).glob("bitbake*"):
                if binary.is_file() and os.access(binary, os.X_OK):
                    raise ValueError(
                        "Generic command execution is disabled while BitBake is installed "
                        "in the sandbox system PATH")

    @staticmethod
    def _reject_bitbake_command(argv: list[str]) -> None:
        for argument in argv:
            lowered = argument.casefold()
            words = re.findall(r"[a-z0-9_.+-]+", lowered)
            if "bitbake" in words or any(word.startswith("bitbake-") for word in words):
                raise ValueError("BitBake and Yocto task execution is disabled")

    @staticmethod
    def _walk(root: Path):
        errors: list[OSError] = []

        def record(error: OSError) -> None:
            errors.append(error)

        entries = list(os.walk(root, topdown=True, followlinks=False, onerror=record))
        if errors:
            raise ValueError(f"Cannot prove repository isolation: {errors[0]}")
        return entries

    def _reject_yocto_repository(self, root: Path) -> None:
        evidence: list[str] = []
        for directory, dirs, files in self._walk(root):
            current = Path(directory)
            relative = current.relative_to(root)
            names = set(dirs) | set(files)
            if relative.name == "conf" and ({"bblayers.conf", "layer.conf"} & set(files)):
                evidence.append(str(relative))
            if "oe-init-build-env" in files:
                evidence.append(str(relative / "oe-init-build-env"))
            if ".bitbake" in names or "sstate-cache" in names:
                marker = ".bitbake" if ".bitbake" in names else "sstate-cache"
                evidence.append(str(relative / marker))
            if "bitbake" in names:
                candidate = current / "bitbake"
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    evidence.append(str(candidate.relative_to(root)))
            if relative.name == "tmp" and "work" in names:
                evidence.append(str(relative / "work"))
            if relative.name == "tmp" and {"stamps", "deploy"}.issubset(names):
                evidence.append(str(relative))
        if evidence:
            raise ValueError(
                "Execution is disabled for repositories containing Yocto/BitBake "
                f"build evidence ({evidence[0]})")

    @staticmethod
    def _reject_protected_working_tree(root: Path, cwd: Path) -> None:
        protected = {".bitbake", "sstate-cache"}
        for part in (cwd.relative_to(root), *cwd.relative_to(root).parents):
            if part.name in protected:
                raise ValueError("Commands cannot run from a Yocto build/cache directory")

    def _secret_mounts(self, root: Path) -> list[str]:
        mounts: list[str] = []
        for directory, dirs, files in self._walk(root):
            current = Path(directory)
            relative_dir = current.relative_to(root)
            if any(part == ".git" or part == ".env" or part.startswith(".env.")
                   for part in relative_dir.parts):
                continue
            for name in sorted(set(dirs) | set(files)):
                if name != ".git" and name != ".env" and not name.startswith(".env."):
                    continue
                source = current / name
                if source.is_symlink():
                    raise ValueError(
                        f"Cannot safely hide symbolic secret path: {source.relative_to(root)}")
                target = Path("/workspace") / relative_dir / name
                if source.is_dir():
                    mounts.extend(("--tmpfs", target.as_posix()))
                elif source.is_file():
                    mounts.extend(("--ro-bind", "/dev/null", target.as_posix()))
                else:
                    raise ValueError(f"Cannot safely hide special path: {source.relative_to(root)}")
        return mounts

    def _load_config(self) -> dict[str, Any]:
        try:
            info = self.config_path.lstat()
        except FileNotFoundError:
            return {"version": 1, "profiles": {}, "toolchains": {}}
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Execution config must be a regular, non-symlink file")
        if info.st_uid != os.getuid():
            raise ValueError("Execution config must be owned by the current user")
        mode = stat.S_IMODE(info.st_mode)
        if mode not in {0o600, 0o644} or mode & 0o022:
            raise ValueError("Execution config permissions must be 0600 or 0644")
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Execution config is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Execution config version must be 1")
        if set(data) - {"version", "profiles", "toolchains"}:
            raise ValueError("Execution config contains unsupported fields")
        if not isinstance(data.get("profiles", {}), dict):
            raise ValueError("Execution config profiles must be an object")
        if not isinstance(data.get("toolchains", {}), dict):
            raise ValueError("Execution config toolchains must be an object")
        return data

    def _profiles(
        self, project: str, root: Path, config: dict[str, Any],
        postgresql_prefix: Path | None,
    ) -> dict[str, _Profile]:
        profiles = self._discovered_profiles(project, root)
        profiles.update(self._configured_profiles(
            project, root, config, postgresql_prefix))
        return profiles

    def _configured_profiles(
        self, project: str, root: Path, data: dict[str, Any],
        postgresql_prefix: Path | None,
    ) -> dict[str, _Profile]:
        configured = data.get("profiles", {})
        project_profiles = configured.get(project, {})
        if not isinstance(project_profiles, dict):
            raise ValueError("Project execution profiles must be an object")
        result: dict[str, _Profile] = {}
        for name, value in project_profiles.items():
            if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
                raise ValueError("Execution profile names must be simple identifiers")
            if not isinstance(value, dict) or set(value) - {
                    "argv", "relative_cwd", "timeout_seconds", "test_database"}:
                raise ValueError(f"Invalid execution profile: {name!r}")
            argv = tuple(self._validate_argv(value.get("argv")))
            _, relative_cwd = self._resolve_cwd(root, value.get("relative_cwd", "."))
            timeout = self._validate_timeout(value.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
            self._reject_bitbake_command(list(argv))
            test_database = value.get("test_database")
            if test_database is not None:
                if test_database not in TEST_DATABASES:
                    raise ValueError(
                        "test_database must be tarororo_test or tarororo_ci")
                if postgresql_prefix is None:
                    raise ValueError(
                        "A test_database profile requires a PostgreSQL toolchain")
            result[name] = _Profile(
                name=name,
                label=f"구성된 명령: {name}",
                argv=argv,
                relative_cwd=relative_cwd,
                timeout_seconds=timeout,
                source="config",
                test_database=test_database,
            )
        return result

    @staticmethod
    def _toolchain(project: str, data: dict[str, Any]) -> dict[str, Any]:
        toolchains = data.get("toolchains", {})
        project_toolchain = toolchains.get(project)
        if project_toolchain is None:
            return {}
        if (not isinstance(project_toolchain, dict) or
                not project_toolchain or set(project_toolchain) - {
                    "node", "chromium", "postgresql"}):
            raise ValueError(
                "Project toolchain supports only node, chromium and postgresql prefixes")
        return project_toolchain

    def _node_prefix(self, project: str, data: dict[str, Any]) -> Path | None:
        toolchain = self._toolchain(project, data)
        if "node" not in toolchain:
            return None
        configured = toolchain["node"]
        if not isinstance(configured, str) or not configured:
            raise ValueError("Node toolchain prefix must be an absolute path")
        prefix = Path(configured)
        if not prefix.is_absolute():
            raise ValueError("Node toolchain prefix must be an absolute path")
        try:
            info = prefix.lstat()
        except FileNotFoundError as exc:
            raise ValueError("Node toolchain prefix does not exist") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Node toolchain prefix must be a regular, non-symlink directory")
        resolved = prefix.resolve(strict=True)

        home = Path.home().resolve(strict=True)
        if home.is_relative_to(resolved):
            raise ValueError("Node toolchain prefix cannot expose the host home")
        nvm_root = home / ".nvm"
        if resolved == nvm_root or nvm_root.is_relative_to(resolved):
            raise ValueError("Node toolchain prefix cannot expose the complete NVM tree")
        if resolved.is_relative_to(nvm_root):
            nvm_relative = resolved.relative_to(nvm_root)
            if (len(nvm_relative.parts) != 3 or
                    nvm_relative.parts[:2] != ("versions", "node")):
                raise ValueError("NVM toolchain prefix must select one installed Node version")
        for root in self.projects.values():
            if resolved.is_relative_to(root) or root.is_relative_to(resolved):
                raise ValueError("Node toolchain prefix must be separate from writable projects")

        required = {
            "node": resolved / "bin/node",
            "npm": resolved / "bin/npm",
            "npm package": resolved / "lib/node_modules/npm",
        }
        for label, path in required.items():
            try:
                target = path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValueError(f"Node toolchain is missing {label}") from exc
            if not target.is_relative_to(resolved):
                raise ValueError(f"Node toolchain {label} escapes its prefix")
            if label == "npm package":
                if not target.is_dir():
                    raise ValueError("Node toolchain npm package must be a directory")
            elif not target.is_file():
                raise ValueError(f"Node toolchain {label} must be a regular file")
        if not os.access(required["node"], os.X_OK):
            raise ValueError("Node toolchain node executable is not executable")
        if not os.access(required["npm"], os.X_OK):
            raise ValueError("Node toolchain npm executable is not executable")
        return resolved

    def _chromium_prefix(self, project: str, data: dict[str, Any]) -> Path | None:
        toolchain = self._toolchain(project, data)
        if "chromium" not in toolchain:
            return None
        configured = toolchain["chromium"]
        if not isinstance(configured, str) or not configured or not Path(configured).is_absolute():
            raise ValueError("Chromium toolchain prefix must be an absolute path")
        prefix = Path(configured)
        try:
            info = prefix.lstat()
        except FileNotFoundError as exc:
            raise ValueError("Chromium toolchain prefix does not exist") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Chromium toolchain prefix must be a regular, non-symlink directory")
        resolved = prefix.resolve(strict=True)
        if Path.home().resolve(strict=True).is_relative_to(resolved):
            raise ValueError("Chromium toolchain prefix cannot expose the host home")
        for root in self.projects.values():
            if resolved.is_relative_to(root) or root.is_relative_to(resolved):
                raise ValueError("Chromium toolchain prefix must be separate from writable projects")
        for name in ("chrome", "icudtl.dat", "resources.pak"):
            path = resolved / name
            try:
                target = path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValueError(f"Chromium toolchain is missing {name}") from exc
            if not target.is_relative_to(resolved) or not target.is_file():
                raise ValueError(f"Chromium toolchain {name} must be a file inside its prefix")
        executable = resolved / "chrome"
        if not os.access(executable, os.X_OK):
            raise ValueError("Chromium toolchain chrome is not executable")
        with executable.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise ValueError("Chromium toolchain chrome must be a native ELF binary, not a wrapper")
        return resolved

    def _postgresql_prefix(self, project: str, data: dict[str, Any]) -> Path | None:
        toolchain = self._toolchain(project, data)
        if "postgresql" not in toolchain:
            return None
        configured = toolchain["postgresql"]
        if (not isinstance(configured, str) or not configured or
                not Path(configured).is_absolute()):
            raise ValueError("PostgreSQL toolchain prefix must be an absolute path")
        prefix = Path(configured)
        try:
            info = prefix.lstat()
        except FileNotFoundError as exc:
            raise ValueError("PostgreSQL toolchain prefix does not exist") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                "PostgreSQL toolchain prefix must be a regular, non-symlink directory")
        resolved = prefix.resolve(strict=True)
        if Path.home().resolve(strict=True).is_relative_to(resolved):
            raise ValueError("PostgreSQL toolchain prefix cannot expose the host home")
        for root in self.projects.values():
            if resolved.is_relative_to(root) or root.is_relative_to(resolved):
                raise ValueError(
                    "PostgreSQL toolchain prefix must be separate from writable projects")
        for name in ("postgres", "initdb", "pg_ctl", "createdb"):
            executable = resolved / "bin" / name
            try:
                target = executable.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValueError(f"PostgreSQL toolchain is missing {name}") from exc
            if not target.is_relative_to(resolved):
                raise ValueError(f"PostgreSQL toolchain {name} escapes its prefix")
            if not target.is_file() or not os.access(executable, os.X_OK):
                raise ValueError(
                    f"PostgreSQL toolchain {name} must be an executable regular file")
            with executable.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    raise ValueError(
                        f"PostgreSQL toolchain {name} must be a native ELF binary")
        resources = (resolved / "share/postgres.bki", resolved / "share/postgresql/postgres.bki")
        if not any(resource.is_file() for resource in resources):
            raise ValueError("PostgreSQL toolchain is missing its postgres.bki resource")
        return resolved

    def _discovered_profiles(self, project: str, root: Path) -> dict[str, _Profile]:
        result: dict[str, _Profile] = {}
        package_files = [root / "package.json"]
        package_files.extend(sorted(
            path / "package.json" for path in root.iterdir()
            if path.is_dir() and not path.is_symlink()))
        for package_file in package_files:
            if not package_file.is_file() or package_file.is_symlink():
                continue
            try:
                package = json.loads(package_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid package.json: {package_file.relative_to(root)}") from exc
            scripts = package.get("scripts", {})
            if not isinstance(scripts, dict):
                continue
            relative_dir = package_file.parent.relative_to(root).as_posix() or "."
            prefix = "npm" if relative_dir == "." else relative_dir.replace("/", ":")
            for script in SCRIPT_NAMES:
                if isinstance(scripts.get(script), str):
                    name = f"{prefix}:{script}"
                    result[name] = _Profile(
                        name=name,
                        label=f"npm {script} ({relative_dir})",
                        argv=("npm", "run", script),
                        relative_cwd=relative_dir,
                        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                        source="discovered",
                    )

        return result
