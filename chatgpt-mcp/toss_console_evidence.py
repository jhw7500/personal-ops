"""Strict, data-only projection of an authenticated Apps in Toss console bundle_list result.

This module does NOT authenticate, contact Toss, or publish a bundle. The caller
must supply the result of a trusted, OAuth-authorized read-only console MCP call;
never accept a caller/model-provided payload as independent remote evidence.

The contents/currentPage/totalPage envelope was observed through an authenticated
Console call on 2026-10-19. This does not verify the inputSchema or OAuth setup
for an independent client. Unknown shapes and missing fields fail closed.
"""
from __future__ import annotations

from typing import Any

APP_NAME = "tarororo"
WORKSPACE_ID = "93125"
MINIAPP_ID = "75257"
DEPLOYMENT_ID = "01a13e37-27f3-7f9c-923d-6dbd690a5a09"
UX019_DEPLOYMENT_ID = "01a151bd-4015-75cf-8275-fa6b6aef9a47"
ALLOWED_DEPLOYMENT_IDS = frozenset({DEPLOYMENT_ID, UX019_DEPLOYMENT_ID})
MAX_BUNDLES = 200


class ConsoleEvidenceError(ValueError):
    """Constant error code suitable for public diagnostics."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require_identifier(value: Any, expected: str) -> None:
    # Explicitly refuse bool; bool is an int subclass.
    if type(value) not in (str, int) or str(value) != expected:
        raise ConsoleEvidenceError("WRONG_CONSOLE_TARGET")


def _bounded_string(value: Any, *, limit: int = 160) -> str:
    if type(value) is not str or not 0 < len(value) <= limit or not value.isprintable():
        raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
    return value


def _bundles(raw: Any) -> list[dict[str, Any]]:
    if type(raw) is dict and ("error" in raw or raw.get("isError") is True):
        raise ConsoleEvidenceError("PROVIDER_RESPONSE_ERROR")
    if type(raw) is list:
        items = raw
    elif type(raw) is dict:
        envelopes = set(raw) & {"contents", "bundles", "items"}
        if len(envelopes) > 1:
            raise ConsoleEvidenceError("AMBIGUOUS_CONSOLE_RESPONSE")
        if not envelopes:
            raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
        if set(raw) & {"nextCursor", "next_cursor", "hasMore", "has_more"}:
            raise ConsoleEvidenceError("INCOMPLETE_CONSOLE_PAGE")
        if "contents" in envelopes:
            current, last = raw.get("currentPage"), raw.get("totalPage")
            if (type(current) is not int or type(last) is not int
                    or not 0 <= current <= last):
                raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
            # totalPage is the LAST zero-based index: a complete single page
            # has currentPage=totalPage=0, even when it contains a bundle.
            # Multi-page traversal is not implemented or reviewed here.
            if current != 0 or last != 0:
                raise ConsoleEvidenceError("INCOMPLETE_CONSOLE_PAGE")
        elif set(raw) & {"currentPage", "totalPage"}:
            raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
        items = raw[next(iter(envelopes))]
    else:
        raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
    if type(items) is not list or len(items) > MAX_BUNDLES:
        raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
    if any(type(item) is not dict for item in items):
        raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
    return items


def project_bundle_list(
    response: Any, *,
    tool_name: str,
    workspace_id: str | int,
    miniapp_id: str | int,
    deployment_id: str = DEPLOYMENT_ID,
) -> dict[str, Any]:
    """Project one *already trusted* bundle_list tool result to safe evidence.

    Authentication provenance must be established by the calling MCP runtime.
    This pure function cannot verify that the supplied payload came from Toss.
    Never set provider_verified=True merely because this function returned.
    """
    if tool_name != "bundle_list":
        raise ConsoleEvidenceError("UNAUTHORIZED_CONSOLE_TOOL")
    _require_identifier(workspace_id, WORKSPACE_ID)
    _require_identifier(miniapp_id, MINIAPP_ID)
    if type(deployment_id) is not str or deployment_id not in ALLOWED_DEPLOYMENT_IDS:
        raise ConsoleEvidenceError("WRONG_CONSOLE_TARGET")

    matches: list[dict[str, Any]] = []
    for item in _bundles(response):
        identifier = item.get("deploymentId")
        if type(identifier) is str and identifier == deployment_id:
            matches.append(item)
    if not matches:
        return {
            "project": "tossApp", "app": APP_NAME,
            "workspace_id": WORKSPACE_ID, "miniapp_id": MINIAPP_ID,
            "deployment_id": deployment_id, "found": False,
            "test_bundle_ready": False,
            "remote_digest_verified": False,
            "evidence": "BUNDLE_NOT_CONFIRMED_IN_PAGE",
        }
    if len(matches) != 1:
        raise ConsoleEvidenceError("DUPLICATE_DEPLOYMENT_ID")

    item = matches[0]
    if type(response) is dict and "contents" in response:
        # The observed Console envelope carries this canonical field. Legacy
        # aliases cannot replace it or hide contradictory scope metadata.
        _require_identifier(item.get("miniAppId"), MINIAPP_ID)
    # Provider schemas can include explicit scope metadata; when supplied it
    # must agree with the owner-reviewed workspace and mini-app.  A matching
    # deployment ID alone cannot override contradictory provider metadata.
    for key in ("workspaceId", "workspace_id"):
        if key in item:
            _require_identifier(item[key], WORKSPACE_ID)
    for key in ("miniAppId", "miniappId", "miniapp_id"):
        if key in item:
            _require_identifier(item[key], MINIAPP_ID)
    review_status = _bounded_string(item.get("reviewStatus"))
    version_name = _bounded_string(item.get("versionName"))
    if type(item.get("isTested")) is not bool or type(item.get("deployed")) is not bool:
        raise ConsoleEvidenceError("UNVERIFIED_CONSOLE_RESPONSE")
    # Some bundle_list versions may omit appName. If present, it must match.
    app_name = item.get("appName")
    if app_name is not None and _bounded_string(app_name) != APP_NAME:
        raise ConsoleEvidenceError("WRONG_CONSOLE_TARGET")
    ready = (review_status == "CREATED"
             and item["isTested"] is True
             and item["deployed"] is False)
    return {
        "project": "tossApp", "app": APP_NAME,
        "workspace_id": WORKSPACE_ID, "miniapp_id": MINIAPP_ID,
        "deployment_id": deployment_id,
        "found": True,
        "test_bundle_ready": ready,
        "review_status": review_status,
        "version_name": version_name,
        "is_tested": item["isTested"],
        "deployed": item["deployed"],
        "remote_digest_verified": False,
        "evidence": "MATCHED_CONSOLE_ENTRY" if ready else "BUNDLE_NOT_READY",
    }
