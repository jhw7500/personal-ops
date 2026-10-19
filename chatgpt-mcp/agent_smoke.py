"""One explicitly approved, disposable paid fixture; never a real project run."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re

from agent_profiles import profile_digest
from agent_proofs import verify_receipt
from host_runtime_config import HostRuntimeError, private_file, _no_parent_links
from host_runtime_operations import _lock, _read_private_json, _save_private_json, _timestamp

SMOKE_OBJECTIVE = (
    'This is an approved disposable fixture, not a project. Read fixture-input.txt. '
    'Use a shell command to create fixture-result.txt containing exactly '
    '"approved fixture executed\\n". Do not modify fixture-input.txt. '
    'Return the required JSON result with changed_files=["fixture-result.txt"], '
    'a validation describing the command, remaining=[], and approval_required=false.'
)
FIXTURE_INPUT = b'approved disposable fixture input\n'
FIXTURE_OUTPUT = b'approved fixture executed\n'
APPROVAL_KEYS = frozenset({
    'version', 'kind', 'plan_digest', 'profile_digest', 'workspace', 'task_id',
    'objective_digest', 'timeout_minutes', 'created_at', 'expires_at',
    'isolation_receipt', 'receipts_dir',
})


def consume_smoke_authority(profile, workspace, approval_path, *, plan_digest):
    """Called only by the trusted runner's smoke entry, before any process spawn."""
    workspace, approval_path = Path(workspace).absolute(), Path(approval_path).absolute()
    _no_parent_links(workspace)
    _no_parent_links(approval_path)
    private_file(approval_path)
    with _lock(approval_path.parent / 'smoke.lock'):
        document = _read_private_json(approval_path)
        try:
            now = datetime.now(timezone.utc)
            created, expires = _timestamp(document['created_at']), _timestamp(document['expires_at'])
            valid = (set(document) == APPROVAL_KEYS and document['version'] == 1
                     and document['kind'] == 'profile_smoke'
                     and re.fullmatch(r'[0-9a-f]{64}', plan_digest) is not None
                     and document['plan_digest'] == plan_digest
                     and document['profile_digest'] == profile_digest(profile)
                     and document['workspace'] == str(workspace)
                     and workspace.resolve(strict=True) == workspace
                     and re.fullmatch(r'[0-9a-f]{32}', document['task_id']) is not None
                     and document['objective_digest'] == hashlib.sha256(SMOKE_OBJECTIVE.encode()).hexdigest()
                     and type(document['timeout_minutes']) is int and document['timeout_minutes'] == 1
                     and created <= now < expires and expires - created <= timedelta(minutes=10)
                     and verify_receipt(document['receipts_dir'], document['isolation_receipt'],
                                        profile, 'isolation')
                     and profile['apply_policy'] == 'review_only' and profile['host_capabilities'] == [])
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise HostRuntimeError('SMOKE_APPROVAL_INVALID')
        consumed = approval_path.with_name('smoke-approval-consumed.json')
        if consumed.exists() or consumed.is_symlink():
            raise HostRuntimeError('SMOKE_APPROVAL_CONSUMED')
        _save_private_json(consumed, document)
        approval_path.unlink()
    return {'task_id': document['task_id'], 'objective': SMOKE_OBJECTIVE,
            'timeout_minutes': 1, 'project': profile['project']}


async def run_approved_fixture(profile, workspace, approval_path, *, plan_digest, runner=None):
    from agent_runner import CodexAgentRunner
    workspace = Path(workspace)
    private_file(workspace / 'fixture-input.txt')
    if (workspace / 'fixture-input.txt').read_bytes() != FIXTURE_INPUT:
        raise HostRuntimeError('SMOKE_FIXTURE_CHANGED')
    runner = runner or CodexAgentRunner()
    identity_observed = []
    # The runner consumes authority itself; this wrapper does not set ready=True
    # or fabricate a smoke receipt to cross the production activation gate.
    result = await runner.run_approved_smoke(
        profile, workspace, approval_path, plan_digest=plan_digest,
        on_identity=identity_observed.append,
    )
    try:
        private_file(workspace / 'fixture-result.txt')
        output = (workspace / 'fixture-result.txt').read_bytes()
        unchanged = (workspace / 'fixture-input.txt').read_bytes() == FIXTURE_INPUT
    except (HostRuntimeError, OSError):
        output, unchanged = None, False
    structured = result.get('structured_result')
    passed = (result.get('verified') is True and result.get('termination_verified') is True
              and len(identity_observed) == 1 and output == FIXTURE_OUTPUT and unchanged
              and isinstance(structured, dict)
              and structured.get('changed_files') == ['fixture-result.txt'])
    if not passed:
        raise HostRuntimeError('SMOKE_FIXTURE_FAILED')
    return {'execution_observed': True, 'structured_result_verified': True,
            'termination_verified': True, 'source_unchanged': True,
            'model_and_effort_pinned': True}
