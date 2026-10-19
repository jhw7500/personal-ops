"""Normalized Docker Compose actions for registered version 3 targets.

Only fixed action names and exact parameter shapes are accepted. There is no
free-form flag, environment, path, target, volume-deletion or prune input.
"""
from __future__ import annotations

from host_runtime_config import HostRuntimeError

SERVICE_ACTIONS = ("compose.build", "compose.apply", "compose.up", "compose.restart",
                   "compose.stop")
ACTIONS = SERVICE_ACTIONS + ("compose.down", "compose.exec")
# db_service is not an action: it additionally permits interrupting or exec in DB services.
CAPABILITIES = ACTIONS + ("db_service",)


def validate_action(target: dict, action: str, parameters: dict) -> dict:
    """Return {"action", "parameters", "impact"} or raise a sanitized error."""
    if not isinstance(action, str) or action not in ACTIONS:
        raise HostRuntimeError("UNSUPPORTED_SCOPE")
    if action not in target["capabilities"]:
        raise HostRuntimeError("POLICY_DENIED")
    if not isinstance(parameters, dict):
        raise HostRuntimeError("UNSUPPORTED_SCOPE")
    services = target["compose"]["services"]
    exec_service = None
    if action == "compose.down":
        if parameters != {}:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        normalized, selected = {}, list(services)
    elif action == "compose.exec":
        profile = parameters.get("profile")
        if (set(parameters) != {"profile"} or not isinstance(profile, str)
                or profile not in target["exec_profiles"]):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        normalized, selected = {"profile": profile}, []
        exec_service = target["exec_profiles"][profile]["service"]
    else:
        requested = parameters.get("services")
        if (set(parameters) != {"services"} or not isinstance(requested, list) or not requested
                or not all(isinstance(name, str) and name in services for name in requested)
                or len(set(requested)) != len(requested)):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        selected = [name for name in services if name in requested]
        normalized = {"services": list(selected)}
    interrupted = [] if action in ("compose.build", "compose.exec") else list(selected)
    db_services = set(target["compose"]["db_services"])
    if ((set(interrupted) | {exec_service}) & db_services
            and "db_service" not in target["capabilities"]):
        raise HostRuntimeError("POLICY_DENIED")
    return {"action": action, "parameters": normalized, "impact": {
        "recreated": list(selected) if action in ("compose.apply", "compose.up") else [],
        "interrupted": interrupted,
        "stopped": list(selected) if action in ("compose.stop", "compose.down") else [],
        "preserved": [name for name in services if name not in interrupted],
        "exec_service": exec_service,
        "volumes": "preserved",
        "db_connections": "interrupted" if set(interrupted) & db_services else "unaffected",
    }}
