"""Fixed-project MCP tools for a separately web-approved runtime rollout."""
from __future__ import annotations

_PROJECT = {"type": "string", "enum": ["personal-ops"]}
_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_REQUEST = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}


def _tool(name, title, description, extra=(), *, readonly):
    fields = {"project": _PROJECT}
    fields.update(extra)
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": fields,
                        "required": list(fields), "additionalProperties": False},
        "annotations": {"readOnlyHint": readonly, "destructiveHint": not readonly,
                        "idempotentHint": readonly, "openWorldHint": False},
    }


RUNTIME_ROLLOUT_TOOL_SPECS = [
    _tool("prepare_runtime_rollout", "MCP 런타임 전환 계획",
          "Freeze exactly one personal-ops source snapshot and expected runtime digest. "
          "Shows which existing user services will restart. No installation or restart. "
          "A separate one-use owner approval is required. Open the returned approval_card in chat; the authenticated administrator web is a fallback.",
          {"request_id": _REQUEST}, readonly=False),
    _tool("get_runtime_rollout", "MCP 런타임 전환 상태",
          "Read one durable rollout plan and post-restart status. Does not execute, "
          "approve or consume a plan.", {"operation_id": _ID}, readonly=True),
    _tool("execute_runtime_rollout", "승인된 MCP 전환 실행",
          "Start only a still-valid, one-use owner-approved immutable personal-ops rollout. "
          "Runs the full official preflight in its own systemd user unit so tunnel and "
          "admin restarts do not kill the worker. Does not change Docker, Postgres or Toss.",
          {"operation_id": _ID}, readonly=False),
]
