"""SOURCE ONLY: strict projection for a NEW Apps in Toss TEST deployment.

This module has NO OAuth, network, CLI, filesystem access or MCP tool registration.
The owner-side integration MUST read the operation/receipt from private trusted
storage; never expose receipt or deployment ID as caller-controlled tool args.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from toss_console_evidence import (
    APP_NAME, MINIAPP_ID, WORKSPACE_ID, DEPLOYMENT_ID, UX019_DEPLOYMENT_ID,
    ConsoleEvidenceError, _bundles, _bounded_string, _require_identifier,
)

UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
OPERATION = re.compile(r"[0-9a-f]{32}\Z")
PROFILE = "tossapp-upload-20261026"
EXPECTED_SCOPE = {"project": "tossApp", "target": "tarororo-test"}


@dataclass(frozen=True)
class OwnerUploadTarget:
    """Fingerprint of owner-reported evidence, NOT an authentication proof."""
    deployment_id: str
    operation_id: str
    source_digest: str
    artifact_sha256: str


def from_owner_verified_evidence(operation: dict, receipt: dict) -> OwnerUploadTarget:
    """Inputs must be read by a TRUSTED owner process, never by chat/user arguments."""
    if type(operation) is not dict or type(receipt) is not dict:
        raise ConsoleEvidenceError("OWNER_UPLOAD_EVIDENCE_MISSING")
    result = operation.get("result")
    if (operation.get("project") != "tossApp"
            or operation.get("profile_id") != PROFILE
            or operation.get("effect") != "external_publish"
            or operation.get("state") != "succeeded"
            or operation.get("phase") != "COMPLETE"
            or operation.get("termination_verified") is not True
            or type(result) is not dict or type(result.get("exit_code")) is not int
            or result["exit_code"] != 0
            or type(operation.get("operation_id")) is not str
            or OPERATION.fullmatch(operation["operation_id"]) is None
            or type(operation.get("source_digest")) is not str
            or SHA.fullmatch(operation["source_digest"]) is None):
        raise ConsoleEvidenceError("OWNER_HOST_COMMAND_NOT_VERIFIED")
    deployment = receipt.get("deployment_id")
    digest = receipt.get("artifact_sha256")
    if (receipt.get("status") != "cli_reported_complete"
            or receipt.get("external_attempted") is not True
            or type(receipt.get("cli_exit_code")) is not int
            or receipt["cli_exit_code"] != 0
            or receipt.get("app") != APP_NAME
            or type(deployment) is not str or UUID.fullmatch(deployment) is None
            or deployment in {DEPLOYMENT_ID, UX019_DEPLOYMENT_ID}
            or type(digest) is not str or SHA.fullmatch(digest) is None):
        raise ConsoleEvidenceError("OWNER_UPLOAD_RECEIPT_NOT_VERIFIED")
    return OwnerUploadTarget(deployment, operation["operation_id"],
                             operation["source_digest"], digest)


def reviewed_bundle_query(target: OwnerUploadTarget) -> dict:
    """Fixed scope, no arbitrary search terms or pagination from a model."""
    if type(target) is not OwnerUploadTarget:
        raise ConsoleEvidenceError("OWNER_UPLOAD_TARGET_REQUIRED")
    return {"workspaceId": 93125, "miniAppId": 75257,
            "page": 0, "size": 20, "search": target.deployment_id}


def project_new_test_bundle(response: object, *, target: OwnerUploadTarget) -> dict:
    """Project the single exact TEST row from an ALREADY authenticated bundle_list.

    This cannot prove provider authentication or test QR origin on its own.
    A caller MUST validate the official bundle_list tool schema, auth context,
    and operation/receipt provenance before setting provider_verified=True.
    """
    if type(target) is not OwnerUploadTarget:
        raise ConsoleEvidenceError("OWNER_UPLOAD_TARGET_REQUIRED")
    rows = [item for item in _bundles(response)
            if type(item.get("deploymentId")) is str
            and item["deploymentId"] == target.deployment_id]
    if len(rows) > 1:
        raise ConsoleEvidenceError("DUPLICATE_DEPLOYMENT_ID")
    if not rows:
        return {**EXPECTED_SCOPE, "app": APP_NAME, "workspace_id": WORKSPACE_ID,
                "miniapp_id": MINIAPP_ID, "deployment_id": target.deployment_id,
                "found": False, "test_bundle_ready": False,
                "provider_verified": False, "remote_digest_verified": False,
                "evidence": "BUNDLE_NOT_CONFIRMED_IN_PAGE"}
    item = rows[0]
    if type(response) is dict and "contents" in response:
        _require_identifier(item.get("miniAppId"), MINIAPP_ID)
    for key in ("workspaceId", "workspace_id"):
        if key in item: _require_identifier(item[key], WORKSPACE_ID)
    for key in ("miniAppId", "miniappId", "miniapp_id"):
        if key in item: _require_identifier(item[key], MINIAPP_ID)
    if "appName" in item and _bounded_string(item["appName"]) != APP_NAME:
        raise ConsoleEvidenceError("WRONG_CONSOLE_TARGET")
    review_status = _bounded_string(item.get("reviewStatus"))
    version_name = _bounded_string(item.get("versionName"))
    if type(item.get("isTested")) is not bool or type(item.get("deployed")) is not bool:
        raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
    ready = review_status == "CREATED" and item["isTested"] is True and item["deployed"] is False
    return {**EXPECTED_SCOPE, "app": APP_NAME, "workspace_id": WORKSPACE_ID,
            "miniapp_id": MINIAPP_ID, "deployment_id": target.deployment_id,
            "found": True, "review_status": review_status,
            "version_name": version_name, "is_tested": item["isTested"],
            "deployed": item["deployed"], "test_bundle_ready": ready,
            "provider_verified": False, "remote_digest_verified": False,
            "evidence": "MATCHED_CONSOLE_ENTRY" if ready else "BUNDLE_NOT_READY"}
