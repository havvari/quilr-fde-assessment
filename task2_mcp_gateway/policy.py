"""Method-level authorization for MCP JSON-RPC payloads.

Pure functions over already-parsed payloads: no HTTP, no network, no I/O. That
is what makes the interesting assertion -- "the downstream was never contacted"
-- something you can also check here, at the level of a return value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.jsonrpc import UNAUTHORIZED_TOOL_CALL
from task2_mcp_gateway.auth import Principal
from task2_mcp_gateway.config import ADMIN_TOOL_PREFIX


@dataclass(frozen=True)
class Allow:
    """Forward the request downstream unchanged."""


@dataclass(frozen=True)
class Deny:
    """Answer from the gateway. The downstream is never contacted."""

    code: int
    message: str
    data: dict[str, Any] | None = None


Decision = Allow | Deny


def is_admin_tool(name: str) -> bool:
    return name.startswith(ADMIN_TOOL_PREFIX)


def decide(principal: Principal, method: str, params: Any) -> Decision:
    """Authorize one JSON-RPC call.

    Only `tools/call` is gated. `tools/list` and everything else forward, which
    is the brief's literal requirement -- the tool-list *filter* is a separate,
    opt-in concern in `app.py`.
    """
    if method != "tools/call":
        return Allow()

    if not isinstance(params, dict):
        # `tools/call` with array or absent params cannot name a tool. Denying is
        # the safe reading: a request we cannot classify is not one we forward.
        return Deny(
            code=UNAUTHORIZED_TOOL_CALL,
            message="Unauthorized Tool Call",
            data={"reason": "tools/call params must be an object naming a tool"},
        )

    name = params.get("name")
    if not isinstance(name, str) or not name:
        return Deny(
            code=UNAUTHORIZED_TOOL_CALL,
            message="Unauthorized Tool Call",
            data={"reason": "tools/call params.name is missing or not a string"},
        )

    if is_admin_tool(name) and not principal.is_admin:
        return Deny(
            code=UNAUTHORIZED_TOOL_CALL,
            message="Unauthorized Tool Call",
            data={
                "tool": name,
                "required_role": "admin",
                "role": principal.role,
                "reason": f"tools prefixed {ADMIN_TOOL_PREFIX!r} require the admin role",
            },
        )

    return Allow()


def visible_tools(tools: list[Any], principal: Principal) -> list[Any]:
    """The subset of a `tools/list` result this principal should be shown.

    Used only when `FILTER_TOOL_LIST` is on. Rationale in the README: a tool the
    model can see is a tool the model will try.
    """
    if principal.is_admin:
        return tools
    return [
        tool
        for tool in tools
        if not (isinstance(tool, dict) and isinstance(tool.get("name"), str) and is_admin_tool(tool["name"]))
    ]
