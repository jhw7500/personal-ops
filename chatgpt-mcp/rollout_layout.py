"""Pinned host layouts and inert parsing of the existing WSL launch wrappers.

No shell text is sourced or executed here. Only the observed wrapper template
and the canonical argv produced below are admitted. Runtime switches preserve
the live process's three wrapper exports and leave the wrappers untouched.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import stat

from workspace_control import WorkspaceControlError, verify_controller, verify_launch


ISOLATED_BOOTSTRAP = ('import runpy\nimport sys\n'
                      'release, entrypoint, *arguments = sys.argv[1:]\n'
                      'sys.path.insert(0, release)\n'
                      'sys.argv = [entrypoint, *arguments]\n'
                      'runpy.run_path(entrypoint, run_name="__main__")\n')
ENVIRONMENT_KEYS = ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'PATH')


class LayoutError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def known_layout(name='legacy', home=None):
    home = Path(home) if home is not None else Path.home()
    if (not home.is_absolute() or home != home.absolute()
            or any(part in {'.', '..'} for part in home.parts)
            or re.fullmatch(r'/[A-Za-z0-9_./-]+', str(home)) is None):
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAYOUT')
    if name == 'legacy':
        alias, service = 'personal-ops-coding', 'personal-ops-tunnel.service'
        client = home / '.local/bin/tunnel-client'
    elif name == 'wsl-pc1':
        alias, service = 'personal-ops-coding-wsl-pc1', 'personal-ops-coding-wsl-pc1.service'
        client = home / 'mcp-comparison/tools/tunnel-client-v0.0.16/tunnel-client'
    else:
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAYOUT')
    user_units = home / '.config/systemd/user'
    return {
        'name': name, 'home': home, 'alias': alias, 'service': service,
        'client': client, 'profile_path': home / '.config/tunnel-client' / (alias + '.yaml'),
        'override_path': user_units / (service + '.d') / '20-runtime.conf',
        'unit_path': user_units / service,
        'health_url_file': home / '.local/state/tunnel-client/health' / (alias + '.url'),
        'key_file': home / '.config/chatgpt-coding-mcp/runtime-key',
        'working_directory': home / 'projects/personal-ops',
    }


def validate_layout(layout):
    if (not isinstance(layout, dict) or layout.get('name') not in {'legacy', 'wsl-pc1'}
            or layout != known_layout(layout['name'], layout.get('home'))):
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAYOUT')
    return layout


def _safe_parents(path, home):
    for parent in path.parents:
        if not parent.is_relative_to(home):
            break
        info = parent.lstat()
        if (parent.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
            raise LayoutError('UNSAFE_RUNTIME_LAUNCH_PATH')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def freeze_owned_file(path, home, *, exact_mode=None, limit=16384):
    try:
        _safe_parents(path, home)
        before = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o022
                or (exact_mode is not None and stat.S_IMODE(before.st_mode) != exact_mode)):
            raise LayoutError('UNSAFE_RUNTIME_LAUNCH_PATH')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if _stamp(opened) != _stamp(before):
                raise LayoutError('RUNTIME_LAUNCH_CHANGED')
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > limit or _stamp(opened) != _stamp(after)
                or _stamp(path.lstat()) != _stamp(opened)):
            raise LayoutError('RUNTIME_LAUNCH_CHANGED')
    except OSError as exc:
        raise LayoutError('UNSAFE_RUNTIME_LAUNCH_PATH') from exc
    return {'path': path, 'home': home, 'bytes': raw, 'stamp': _stamp(opened),
            'sha256': hashlib.sha256(raw).hexdigest()}


def select_layout(home=None):
    candidates = []
    for name in ('legacy', 'wsl-pc1'):
        layout = known_layout(name, home)
        path = layout['unit_path']
        if path.exists() or path.is_symlink():
            freeze_owned_file(path, layout['home'], limit=131072)
            candidates.append(layout)
    if len(candidates) != 1:
        raise LayoutError('AMBIGUOUS_RUNTIME_LAYOUT' if candidates else 'RUNTIME_LAYOUT_NOT_FOUND')
    return candidates[0]


def _paths(home, entrypoint):
    known_layout('wsl-pc1', home)
    if entrypoint not in {'server.py', 'admin_server.py'}:
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAUNCH')
    return {
        'home': home,
        'interpreter': home / '.local/share/uv/tools/serena-agent/bin/python',
        'releases': home / '.local/lib/chatgpt-coding-mcp/releases',
        'controller': home / '.local/lib/chatgpt-coding-mcp/workspace-controller',
        'authority': home / '.local/state/chatgpt-coding-mcp/workspace-authority',
        'config': home / '.config/chatgpt-coding-mcp/projects.json',
        'wrapper': home / '.local/bin' / ('personal-ops-mcp' if entrypoint == 'server.py'
                                         else 'personal-ops-admin'),
        'path_prefix': (str(home / 'mcp-comparison/tools/node-v24.12.0-linux-x64/bin')
                        + ':' + str(home / '.local/bin') + ':'),
    }


def application_args(home, entrypoint):
    args = ['--config', str(_paths(home, entrypoint)['config'])]
    return args + (['--port', '8765'] if entrypoint == 'admin_server.py' else [])


def validate_environment(environment, home=None):
    home = Path(home) if home is not None else Path.home()
    if (not isinstance(environment, dict) or set(environment) != set(ENVIRONMENT_KEYS)
            or not all(isinstance(v, str) and v for v in environment.values())
            or environment['XDG_CONFIG_HOME'] != str(home / '.config')
            or environment['XDG_STATE_HOME'] != str(home / '.local/state')):
        raise LayoutError('UNRECOGNIZED_RUNTIME_ENVIRONMENT')
    path = environment['PATH']
    if (len(path) > 16384 or not path.startswith(_paths(home, 'server.py')['path_prefix'])
            or any(mark in path for mark in ('\n', '\r', '\0', '$', '%', '\\'))
            or any(not part.startswith('/') or '..' in Path(part).parts
                   for part in path.split(':'))):
        raise LayoutError('UNRECOGNIZED_RUNTIME_ENVIRONMENT')
    return dict(environment)


def process_environment(pid, proc_root=Path('/proc')):
    try:
        raw = (Path(proc_root) / str(pid) / 'environ').read_bytes()
        if len(raw) > 1024 * 1024:
            raise LayoutError('UNRECOGNIZED_RUNTIME_ENVIRONMENT')
        found = {}
        for item in raw.split(b'\0'):
            key, sep, value = item.partition(b'=')
            if key in tuple(os.fsencode(name) for name in ENVIRONMENT_KEYS):
                decoded = os.fsdecode(key)
                if not sep or decoded in found:
                    raise LayoutError('UNRECOGNIZED_RUNTIME_ENVIRONMENT')
                found[decoded] = os.fsdecode(value)
        return found
    except OSError as exc:
        raise LayoutError('RUNTIME_ENVIRONMENT_UNAVAILABLE') from exc


def isolated_argv(runtime, entrypoint='server.py', home=None):
    home = Path(home) if home is not None else Path.home()
    paths = _paths(home, entrypoint)
    runtime = Path(runtime)
    if runtime.parent != paths['releases'] or not re.fullmatch(r'[0-9a-f]{20}', runtime.name):
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAUNCH')
    return [str(paths['interpreter']), '-I', '-c', ISOLATED_BOOTSTRAP,
            str(runtime), str(runtime / entrypoint), *application_args(home, entrypoint)]


def decode_isolated_process(argv, entrypoint='server.py', home=None):
    if len(argv) < 6:
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAUNCH')
    home = Path(home) if home is not None else Path.home()
    runtime = Path(argv[4])
    if argv != isolated_argv(runtime, entrypoint, home):
        raise LayoutError('UNRECOGNIZED_RUNTIME_LAUNCH')
    return {'runtime': runtime, 'interpreter': Path(argv[0]),
            'application_args': list(argv[6:]), 'command_argv': list(argv),
            'isolated': True}


def verify_managed_interpreter(home=None):
    home = Path(home) if home is not None else Path.home()
    expected = _paths(home, 'server.py')['interpreter']
    python_root = home / '.local/share/uv/python'
    try:
        _safe_parents(expected, home)
        _safe_parents(python_root / 'interpreter', home)
        link = expected.lstat()
        target = expected.resolve(strict=True)
        _safe_parents(target, home)
        info = target.lstat()
        if (link.st_uid != os.getuid()
                or not (stat.S_ISREG(link.st_mode) or stat.S_ISLNK(link.st_mode))
                or not target.is_relative_to(python_root)
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022
                or not stat.S_IMODE(info.st_mode) & 0o100):
            raise LayoutError('UNSAFE_RUNTIME_INTERPRETER')
    except (OSError, RuntimeError) as exc:
        raise LayoutError('UNSAFE_RUNTIME_INTERPRETER') from exc


def _verify_managed(runtime, paths):
    verify_managed_interpreter(paths['home'])
    try:
        controller = verify_controller(paths['controller'])
        admission = verify_launch(runtime, paths['authority'], enforcement_required=True)
    except WorkspaceControlError as exc:
        raise LayoutError(exc.code) from exc
    return controller, admission


def _launcher_argv(runtime, entrypoint, paths):
    return [str(paths['interpreter']), str(paths['controller'] / 'workspace-launcher.py'),
            '--authority-dir', str(paths['authority']), '--release', str(runtime),
            '--interpreter', str(paths['interpreter']), '--isolated',
            '--entrypoint', entrypoint, '--', *application_args(paths['home'], entrypoint)]


def managed_command(runtime, environment, entrypoint='server.py', home=None):
    home = Path(home) if home is not None else Path.home()
    paths = _paths(home, entrypoint)
    isolated_argv(runtime, entrypoint, home)
    environment = validate_environment(environment, home)
    _verify_managed(runtime, paths)
    return ['/usr/bin/env', *(key + '=' + environment[key] for key in ENVIRONMENT_KEYS),
            *_launcher_argv(runtime, entrypoint, paths)]


def parse_managed_command(argv, entrypoint='server.py', home=None):
    """Parse only a pinned wrapper or this module's exact replacement argv."""
    home = Path(home) if home is not None else Path.home()
    paths = _paths(home, entrypoint)
    snapshot = None
    environment = None
    if argv == [str(paths['wrapper'])]:
        snapshot = freeze_owned_file(paths['wrapper'], home, exact_mode=0o700)
        try:
            text = snapshot['bytes'].decode('utf8', errors='strict')
            lines = text.splitlines()
            launch_line = lines[-1]
            if entrypoint == 'admin_server.py' and launch_line.endswith(' "$@"'):
                launch_line = launch_line[:-5]
            launch = shlex.split(launch_line)
        except (ValueError, IndexError, UnicodeError) as exc:
            raise LayoutError('UNRECOGNIZED_RUNTIME_WRAPPER') from exc
        if len(launch) < 6 or launch[:1] != ['exec']:
            raise LayoutError('UNRECOGNIZED_RUNTIME_WRAPPER')
        runtime = Path(launch[4])
        suffix = '' if entrypoint == 'server.py' else ' --entrypoint admin_server.py'
        extra = '' if entrypoint == 'server.py' else ' --port 8765 "$@"'
        expected = ('#!/bin/sh\nset -eu\n'
                    f'export XDG_CONFIG_HOME={home}/.config\n'
                    f'export XDG_STATE_HOME={home}/.local/state\n'
                    f'export PATH={paths["path_prefix"]}"$PATH"\n'
                    f'exec {paths["interpreter"]} {paths["controller"]}/workspace-launcher.py '
                    f'--release {runtime} --interpreter {paths["interpreter"]} '
                    f'--isolated{suffix} -- --config {paths["config"]}{extra}\n')
        if text != expected:
            raise LayoutError('UNRECOGNIZED_RUNTIME_WRAPPER')
        launcher = launch[1:]
    else:
        if len(argv) < 11 or argv[0] != '/usr/bin/env':
            raise LayoutError('UNRECOGNIZED_RUNTIME_LAUNCH')
        environment = {}
        for index, key in enumerate(ENVIRONMENT_KEYS, 1):
            if not argv[index].startswith(key + '='):
                raise LayoutError('UNRECOGNIZED_RUNTIME_ENVIRONMENT')
            environment[key] = argv[index][len(key) + 1:]
        environment = validate_environment(environment, home)
        launcher = argv[4:]
        runtime = Path(launcher[5])
        if launcher != _launcher_argv(runtime, entrypoint, paths):
            raise LayoutError('UNRECOGNIZED_RUNTIME_LAUNCH')
    expected_argv = isolated_argv(runtime, entrypoint, home)
    controller, admission = _verify_managed(runtime, paths)
    return {'runtime': runtime, 'interpreter': paths['interpreter'],
            'application_args': application_args(home, entrypoint),
            'command_argv': expected_argv, 'launcher_argv': launcher,
            'wrapper': snapshot, 'environment': environment, 'home': home,
            'entrypoint': entrypoint, 'authority_dir': paths['authority'],
            'controller_dir': paths['controller'], 'admission': admission,
            'controller_manifest': controller['manifest']}


def assert_wrapper(snapshot):
    if snapshot is None:
        return
    current = freeze_owned_file(snapshot['path'], snapshot['home'], exact_mode=0o700)
    if current != snapshot:
        raise LayoutError('RUNTIME_WRAPPER_CHANGED')


def validate_wsl_unit(unit_text, *, admin=False, home=None):
    """Require the observed service semantics, including control-group stop."""
    home = Path(home) if home is not None else Path.home()
    expected = {'Type': 'simple', 'WorkingDirectory': str(home / 'projects/personal-ops'),
                'UMask': '0077', 'Restart': 'on-failure'}
    if admin:
        expected.update(Environment='PYTHONUNBUFFERED=1', RestartSec='5')
    else:
        expected.update(Environment=f'PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin',
                        RestartSec='15s', TimeoutStopSec='20s', KillMode='control-group')
    actual = {}
    section = ''
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line
        elif section == '[Service]':
            key, sep, value = line.partition('=')
            if key == 'ExecStart':
                continue
            if not sep or key not in expected or key in actual:
                raise LayoutError('UNRECOGNIZED_WSL_UNIT')
            actual[key] = value
    if actual != expected:
        raise LayoutError('UNRECOGNIZED_WSL_UNIT')
