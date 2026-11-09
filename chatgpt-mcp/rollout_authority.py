"""Admit one exact runtime release under a consumed owner rollout approval.

This bridge does not initialize authority, change policy, issue path grants,
advance epochs, or migrate an ABI. The installed old workspace modules remain
cached during the first upgrade; their existing validated lock/load/save
primitives are deliberately reused. Only the exact reviewed release entry may
be added, while every other authority field remains unchanged.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import NoReturn

if __package__:
    from .rollout_layout import validate_layout
    from .rollout_target import (
        _runtime_manifest, _source_readers, _unique_object, verify_web_rollout_grant,
    )
else:
    from rollout_layout import validate_layout
    from rollout_target import (
        _runtime_manifest, _source_readers, _unique_object, verify_web_rollout_grant,
    )
from workspace_authority import WorkspaceAuthority
from workspace_control import (
    ENFORCEMENT_ABI, _minimum_enforcement_abi, activation_admission,
    release_enforcement_abi, release_host_access_capability_generation,
    verify_launch, verify_release_manifest,
)
from workspace_paths import read_owner_file


class RolloutAuthorityError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code) -> NoReturn:
    raise RolloutAuthorityError(code)


def _paths(layout):
    validate_layout(layout)
    home = layout['home']
    return (home / '.local/lib/chatgpt-coding-mcp/releases',
            home / '.local/state/chatgpt-coding-mcp/workspace-authority',
            home / '.config/chatgpt-coding-mcp/projects.json')


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def _positive(value):
    return type(value) is int and value >= 1


def _literal(tree, name, *, default=None):
    found = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(item, ast.Name) and item.id == name for item in node.targets)):
            value = node.value
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name) and node.target.id == name):
            value = node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or not _positive(value.value):
            _fail('ROLLOUT_RELEASE_INCOMPATIBLE')
        found.append(value.value)
    if not found and default is not None:
        return default
    if len(found) != 1:
        _fail('ROLLOUT_RELEASE_INCOMPATIBLE')
    return found[0]


def _candidate(root):
    root = Path(root)
    manifest = _runtime_manifest(root)
    _catalog, read, _static = _source_readers()
    raw, _mode = read(root / 'chatgpt-mcp/workspace_control.py', root)
    if manifest.get('workspace_control.py') != hashlib.sha256(raw).hexdigest():
        _fail('WEB_ROLLOUT_SOURCE_CHANGED')
    try:
        tree = ast.parse(raw, filename='workspace_control.py')
    except (SyntaxError, ValueError):
        _fail('ROLLOUT_RELEASE_INCOMPATIBLE')
    abi = _literal(tree, 'ENFORCEMENT_ABI', default=1)
    generation = _literal(tree, 'HOST_ACCESS_CAPABILITY_GENERATION') if abi >= 3 else None
    if abi > ENFORCEMENT_ABI:
        _fail('ROLLOUT_RELEASE_INCOMPATIBLE')
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return manifest, digest, abi, generation


def _projects(config_path):
    # Use the same installed loader as the worker, with a canonical owner path.
    # All existing project names are needed to validate and preserve old grants.
    from server import load_projects
    before = read_owner_file(config_path)
    projects = load_projects(config_path)
    after = read_owner_file(config_path)
    if (before.sha256 != after.sha256 or before.identity != after.identity
            or 'personal-ops' not in projects):
        _fail('ROLLOUT_AUTHORITY_CHANGED')
    return projects, before.sha256


def preflight_rollout_authority(layout, live_snapshot, *, source_root=None):
    """Pin existing authority and reject ABI/generation changes before install."""
    releases, authority_dir, config = _paths(layout)
    if not _hex(live_snapshot, 20):
        _fail('ROLLOUT_AUTHORITY_PREFLIGHT_INVALID')
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parent.parent
    _manifest, digest, abi, generation = _candidate(root)
    _configured, config_digest = _projects(config)
    current = verify_launch(releases / live_snapshot, authority_dir,
                            enforcement_required=layout['name'] == 'wsl-pc1')
    pinned = {
        'version': 1, 'layout': layout['name'], 'live_snapshot': live_snapshot,
        'authority_active': current['authority_active'],
        'projects_config_sha256': config_digest,
        'target_release_digest': digest, 'target_abi': abi,
        'target_capability_generation': generation,
    }
    if current['authority_active']:
        if (abi != current['release_abi'] or abi < current['minimum_abi']
                or generation != current['host_access_capability_generation']):
            _fail('ROLLOUT_RELEASE_INCOMPATIBLE')
        pinned.update(
            binding=dict(current['binding']),
            current_release_digest=current['release_digest'],
            release_abi=current['release_abi'], minimum_abi=current['minimum_abi'],
            capability_generation=current['host_access_capability_generation'])
    return pinned


def _preflight(value, layout):
    base = {'version', 'layout', 'live_snapshot', 'authority_active',
            'projects_config_sha256', 'target_release_digest', 'target_abi',
            'target_capability_generation'}
    active = {'binding', 'current_release_digest', 'release_abi', 'minimum_abi',
              'capability_generation'}
    if (not isinstance(value, dict) or type(value.get('authority_active')) is not bool
            or set(value) != base | (active if value.get('authority_active') else set())
            or type(value.get('version')) is not int or value['version'] != 1
            or value.get('layout') != layout['name']
            or not _hex(value.get('live_snapshot'), 20)
            or not _hex(value.get('projects_config_sha256'), 64)
            or not _hex(value.get('target_release_digest'), 64)
            or not _positive(value.get('target_abi'))
            or (value['target_abi'] >= 3 and not _positive(value.get('target_capability_generation')))
            or (value['target_abi'] < 3 and value.get('target_capability_generation') is not None)):
        _fail('ROLLOUT_AUTHORITY_PREFLIGHT_INVALID')
    if value['authority_active']:
        binding = value['binding']
        if (not isinstance(binding, dict)
                or set(binding) != {'installation_id', 'authority_epoch', 'policy_digest'}
                or not _hex(binding.get('installation_id'), 32)
                or not _hex(binding.get('policy_digest'), 64)
                or not _positive(binding.get('authority_epoch'))
                or not _hex(value.get('current_release_digest'), 64)
                or not _positive(value.get('release_abi'))
                or not _positive(value.get('minimum_abi'))
                or (value['release_abi'] >= 3 and not _positive(value.get('capability_generation')))
                or (value['release_abi'] < 3 and value.get('capability_generation') is not None)):
            _fail('ROLLOUT_AUTHORITY_PREFLIGHT_INVALID')
    return copy.deepcopy(value)


def _verify_target(root, runtime, pinned):
    manifest, digest, abi, generation = _candidate(root)
    observed = verify_release_manifest(runtime)
    try:
        installed_manifest = json.loads(read_owner_file(runtime / 'manifest.json').data,
                                        object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        _fail('ROLLOUT_RELEASE_MANIFEST_MISMATCH')
    if (installed_manifest != manifest or observed['release_digest'] != digest
            or digest != pinned['target_release_digest'] or runtime.name != digest[:20]):
        _fail('ROLLOUT_RELEASE_MANIFEST_MISMATCH')
    installed_abi = release_enforcement_abi(runtime)
    installed_generation = release_host_access_capability_generation(runtime) if installed_abi >= 3 else None
    if (installed_abi != abi or installed_generation != generation
            or abi != pinned['target_abi'] or generation != pinned['target_capability_generation']):
        _fail('ROLLOUT_RELEASE_INCOMPATIBLE')
    return digest, abi


def admit_rollout_release(grant, root, installer, inventory_fn, controls_fn, *, preflight):
    """Add only this genuine consumed rollout's exact installed release entry.

    Lock order is the caller's rollout lock, the shared activation admission
    gate, then the existing exclusive authority lock. No nested verify_launch
    occurs while the authority lock is held. The independent proof and target
    are rechecked inside that lock immediately before the sole authority save.
    """
    from runtime_rollout_web import WebRolloutGrant
    if type(grant) is not WebRolloutGrant:
        _fail('WEB_ROLLOUT_GRANT_INVALID')
    layout = verify_web_rollout_grant(grant, root, installer, inventory_fn, controls_fn)
    releases, authority_dir, config = _paths(layout)
    if Path(grant.state_root).absolute() != authority_dir.parent / 'runtime-rollout-web':
        _fail('WEB_ROLLOUT_GRANT_INVALID')
    pinned = _preflight(preflight, layout)
    runtime = releases / grant.target_snapshot
    digest, abi = _verify_target(root, runtime, pinned)
    with activation_admission(authority_dir, create=False):
        current = preflight_rollout_authority(layout, pinned['live_snapshot'], source_root=root)
        if current != pinned:
            _fail('ROLLOUT_AUTHORITY_CHANGED')
        projects, config_digest = _projects(config)
        if config_digest != pinned['projects_config_sha256']:
            _fail('ROLLOUT_AUTHORITY_CHANGED')
        if not pinned['authority_active']:
            verify_web_rollout_grant(grant, root, installer, inventory_fn, controls_fn)
            _verify_target(root, runtime, pinned)
            return {'status': 'not_required', 'release_digest': digest,
                    'release_abi': abi, 'authority_active': False}
        authority = WorkspaceAuthority(projects, state_dir=authority_dir)
        with authority._locked_state() as (root_fd, state):
            if (authority._binding(state) != pinned['binding']
                    or state['allowed_releases'].get(pinned['current_release_digest']) != pinned['release_abi']
                    or _minimum_enforcement_abi(authority_dir) != pinned['minimum_abi']
                    or read_owner_file(config).sha256 != pinned['projects_config_sha256']):
                _fail('ROLLOUT_AUTHORITY_CHANGED')
            if verify_web_rollout_grant(grant, root, installer, inventory_fn, controls_fn) != layout:
                _fail('ROLLOUT_AUTHORITY_CHANGED')
            _verify_target(root, runtime, pinned)
            existing = state['allowed_releases'].get(digest)
            if existing is not None and existing != abi:
                _fail('ROLLOUT_RELEASE_ADMISSION_CONFLICT')
            status = 'already_admitted'
            if existing is None:
                # The rollout receipt records this admission's time. Preserve
                # every prior authority field and grant exactly, including epoch.
                state['allowed_releases'][digest] = abi
                authority._save(root_fd, state)
                status = 'admitted'
        observed = verify_launch(runtime, authority_dir, enforcement_required=True)
        if observed['binding'] != pinned['binding'] or observed['release_digest'] != digest:
            _fail('ROLLOUT_AUTHORITY_CHANGED')
    return {'status': status, 'release_digest': digest,
            'release_abi': abi, 'authority_active': True}
