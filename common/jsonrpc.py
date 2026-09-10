"""JSON-RPC 2.0 wire types and helpers shared by the gateway tasks.

The reserved error codes are re-exported from `mcp_types` rather than redefined,
so there is exactly one definition of `-32602` in the repo. `mcp_types` is the
standalone wire-types package that MCP SDK v2 split out of `mcp`.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from pydantic import BaseModel, ConfigDict, Field

# JSON-RPC reserves -32000..-32099 for application-defined server errors. -32001 is
# this gateway's "the caller is authenticated but not entitled to this tool".
UNAUTHORIZED_TOOL_CALL = -32001

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "UNAUTHORIZED_TOOL_CALL",
    "JSONRPCErrorBody",
    "JSONRPCErrorResponse",
    "JSONRPCRequest",
    "RequestId",
    "error_response",
    "is_notification",
]

# Per spec an id is a string, a number, or null. `null` is legal but discouraged;
# it is distinct from *absent*, which makes the message a notification.
RequestId = str | int | float | None


class JSONRPCRequest(BaseModel):
    """An inbound request or notification.

    `params` is left as a raw object rather than a typed union: the gateway is a
    proxy, and re-serialising a narrowly-typed model would silently drop fields
    that a newer downstream understands. Only `params.name` is ever inspected.
    """

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any] | list[Any] | None = None
    # `id` absent means notification; `id: null` means a request with a null id.
    # Pydantic cannot distinguish those with a plain default, so callers must use
    # `is_notification()` against the raw payload rather than testing `req.id`.
    id: RequestId = None


class JSONRPCErrorBody(BaseModel):
    code: int
    message: str
    data: Any = None


class JSONRPCErrorResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    # Always echoed, including when it is null: a client correlates on this field,
    # and an error that drops the id is an error the client cannot attribute.
    id: RequestId = None
    error: JSONRPCErrorBody


class JSONRPCSuccessResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: RequestId = None
    result: Any = Field(default_factory=dict)


def error_response(
    request_id: RequestId,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """Build a JSON-RPC error object as a plain dict, echoing the request id."""
    body = JSONRPCErrorBody(code=code, message=message, data=data)
    response = JSONRPCErrorResponse(id=request_id, error=body)
    # `exclude_none` would drop a legitimately-null id and an absent `data`
    # differently across responses; only `data` is pruned, and only when unset.
    dumped = response.model_dump()
    if data is None:
        dumped["error"].pop("data", None)
    return dumped


def is_notification(payload: object) -> bool:
    """True when the payload is a notification, i.e. carries no `id` member at all.

    A notification must receive no response, ever. `{"id": null}` is *not* a
    notification -- it is a request whose id happens to be null -- so this checks
    for key presence rather than truthiness.
    """
    return isinstance(payload, dict) and "id" not in payload
