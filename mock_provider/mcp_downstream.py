"""A stand-in downstream MCP server, spoken over HTTP JSON-RPC.

The gateway in Task 2 is graded on what it does *not* forward, which is only
observable from the far side. So this mock records every call it receives --
count, method, tool name, and the Authorization header it saw -- and exposes that
through `CALL_LOG` for tests to assert against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from common.jsonrpc import METHOD_NOT_FOUND, error_response, is_notification
from common.logging import get_logger

logger = get_logger(__name__)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_order_status",
        "description": "Look up the status of an order.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "search_docs",
        "description": "Search the knowledge base.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate a tenant's API key. Destructive.",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant": {"type": "string"}},
            "required": ["tenant"],
        },
    },
    {
        "name": "admin_delete_tenant",
        "description": "Permanently delete a tenant. Destructive.",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant": {"type": "string"}},
            "required": ["tenant"],
        },
    },
]


@dataclass
class CallLog:
    """A spy. The interesting assertion in Task 2 is `count == 0`."""

    count: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, method: str, tool: str | None, authorization: str | None) -> None:
        self.count += 1
        self.entries.append({"method": method, "tool": tool, "authorization": authorization})

    def reset(self) -> None:
        self.count = 0
        self.entries.clear()


CALL_LOG = CallLog()

app = FastAPI(title="mock-mcp-downstream")


@app.post("/mcp")
async def mcp_endpoint(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    payload = await request.json()
    method = payload.get("method", "")
    params = payload.get("params") or {}
    tool = params.get("name") if isinstance(params, dict) else None
    CALL_LOG.record(method, tool, authorization)
    logger.info("downstream received method=%s tool=%s", method, tool)

    if is_notification(payload):
        return Response(status_code=204)

    request_id = payload.get("id")

    match method:
        case "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        case "tools/call":
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": f"downstream executed {tool} with {arguments}"}],
                        "isError": False,
                    },
                }
            )
        case "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-downstream", "version": "0.1.0"},
                    },
                }
            )
        case _:
            return JSONResponse(error_response(request_id, METHOD_NOT_FOUND, f"Method not found: {method}"))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
