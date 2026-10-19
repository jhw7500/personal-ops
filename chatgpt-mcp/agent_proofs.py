"""Private, content-addressed activation evidence produced by the host operator.

Unit tests and chat input cannot issue production proof. The trusted setup path
runs the pinned offline probe and separately approved paid fixture before saving
these records outside all writable projects. Same-UID operators remain trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess

from agent_bootstrap import BootstrapError, validate_bootstrap
from agent_profiles import isolation_contract_digest, profile_digest
from host_runtime_config import HostRuntimeError, private_dir, private_file, _no_parent_links
from host_runtime_operations import _read_private_json, _save_private_json, utc_now

HEX64 = re.compile(r'[0-9a-f]{64}\Z')
SOURCE_FILES = (
    'agent_profiles.py', 'agent_proofs.py', 'agent_tasks.py', 'agent_runner.py',
    'agent_supervisor.py', 'agent_bootstrap.py', 'agent-bootstrap.c', 'agent_probe.py',
    'agent_code_mode.py',
    'agent_events.py', 'agent_results.py', 'agent_host_broker.py', 'agent_broker_server.py',
    'agent_profile_setup.py', 'agent_smoke.py', 'host_runtime_config.py',
)
SUPERVISION_ASSERTIONS = frozenset({
    'termination_verified', 'cgroup_empty', 'descendant_gone', 'unrelated_alive',
    'bootstrap_environment_safe',
})
SMOKE_ASSERTIONS = frozenset({
    'execution_observed', 'structured_result_verified', 'termination_verified',
    'source_unchanged', 'model_and_effort_pinned',
})


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def source_contract_digest():
    here = Path(__file__).resolve().parent
    hashes = {}
    for name in SOURCE_FILES:
        path = here / name
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            raise HostRuntimeError('AGENT_SOURCE_UNVERIFIED')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(_canonical(hashes)).hexdigest()


def proof_binding(profile):
    binary = Path(profile['executable'])
    if (binary.is_symlink() or not binary.is_file()
            or hashlib.sha256(binary.read_bytes()).hexdigest() != profile['executable_sha256']):
        raise HostRuntimeError('EXECUTABLE_CHANGED')
    systemd = subprocess.run(
        ['/usr/bin/systemd', '--version'], env={'PATH': '/usr/bin:/bin', 'LANG': 'C'},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        timeout=5, check=True,
    ).stdout.splitlines()[0].decode('ascii')
    return {
        'profile_digest': profile_digest(profile),
        'isolation_contract_digest': isolation_contract_digest(profile),
        'source_contract_digest': source_contract_digest(),
        'bootstrap_sha256': validate_bootstrap(binary.parent / 'personal-ops-agent-bootstrap'),
        'platform': {'uid': os.getuid(), 'kernel': platform.release(),
                     'machine': platform.machine(), 'systemd': systemd},
    }


def _validate_evidence(kind, evidence):
    from agent_probe import REQUIRED_ASSERTIONS
    if not isinstance(evidence, dict):
        raise HostRuntimeError('PROOF_ASSERTIONS_INCOMPLETE')
    if kind == 'isolation':
        assertions, supervision = evidence.get('assertions'), evidence.get('supervision')
        valid = (set(evidence) == {'assertions', 'supervision'}
                 and isinstance(assertions, dict) and set(assertions) == set(REQUIRED_ASSERTIONS)
                 and all(value is True for value in assertions.values())
                 and isinstance(supervision, dict) and set(supervision) == SUPERVISION_ASSERTIONS
                 and all(value is True for value in supervision.values()))
    elif kind == 'smoke':
        valid = set(evidence) == SMOKE_ASSERTIONS and all(v is True for v in evidence.values())
    else:
        valid = False
    if not valid:
        raise HostRuntimeError('PROOF_ASSERTIONS_INCOMPLETE')


def record_receipt(root, profile, kind, evidence):
    """Record observed assertions; caller must be the trusted host setup CLI."""
    _validate_evidence(kind, evidence)
    root = Path(root).absolute()
    _no_parent_links(root)
    private_dir(root)
    document = {'version': 2, 'kind': kind, 'recorded_at': utc_now(),
                'binding': proof_binding(profile), 'evidence': evidence}
    receipt_id = hashlib.sha256(_canonical(document)).hexdigest()
    _save_private_json(root / (receipt_id + '.json'), document)
    return receipt_id


def verify_receipt(root, receipt_id, profile, kind):
    if not isinstance(receipt_id, str) or not HEX64.fullmatch(receipt_id):
        return False
    try:
        root = Path(root).absolute()
        _no_parent_links(root)
        info = root.lstat()
        if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
            return False
        path = root / (receipt_id + '.json')
        private_file(path)
        document = _read_private_json(path)
        if (set(document) != {'version', 'kind', 'recorded_at', 'binding', 'evidence'}
                or document['version'] != 2 or document['kind'] != kind
                or hashlib.sha256(_canonical(document)).hexdigest() != receipt_id
                or document['binding'] != proof_binding(profile)):
            return False
        _validate_evidence(kind, document['evidence'])
        return True
    except (BootstrapError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False
