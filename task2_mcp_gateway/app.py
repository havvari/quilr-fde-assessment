"""An MCP security gateway: a JSON-RPC reverse proxy that enforces tool-level authz.

Request flow, in order, because the order is the security property:

    authenticate -> parse -> classify -> [deny locally] or [forward downstream]

A denied `tools/call` returns from the second-to-last step. The downstream is
never contacted, which `tests/test_task2_gateway.py` asserts with a call-count
spy rather than by inspecting the response.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx2
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from common.jsonrpc import INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR, error_response, is_notification
from common.logging import get_logger
from task2_mcp_gateway.auth import AuthenticationError, Principal, authenticate
from task2_mcp_gateway.config import GatewayConfig
from task2_mcp_gateway.policy import Allow, Deny, decide, visible_tools

logger = get_logger(__name__)

# JSON-RPC errors ride on HTTP 200 with an `error` body. See the README: a 401
# means "the transport rejected you", and a client that conflates the two cannot
# tell a policy decision from a broken credential.
JSONRPC_HTTP_STATUS = 200


def create_app(config: GatewayConfig | None = None, client: httpx2.AsyncClient | None = None) -> FastAPI:
    """Build the gateway. `client` is injectable so tests can wire an ASGI transport."""
    settings = config or GatewayConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = client is None
        app.state.client = client or httpx2.AsyncClient(timeout=settings.request_timeout)
        try:
            yield
        finally:
            if owned:
                await app.state.client.aclose()

    app = FastAPI(title="mcp-security-gateway", lifespan=lifespan)
    app.state.config = settings

    @app.post("/mcp")
    async def proxy(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        # 1. Authenticate before reading the body. An unauthenticated caller
        #    should not get the gateway to parse arbitrary JSON on its behalf.
        try:
            principal = authenticate(authorization, settings.tokens)
        except AuthenticationError as exc:
            return _unauthenticated(str(exc))

        raw = await request.body()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.info("parse error from %s: %s", principal.role, exc)
            return JSONResponse(
                error_response(None, PARSE_ERROR, "Parse error"),
                status_code=JSONRPC_HTTP_STATUS,
            )

        if isinstance(payload, list):
            # Batches are rejected, not supported. See the README: a batch mixes
            # authorization outcomes inside one response, and the 2025-06-18 MCP
            # spec removed batching anyway.
            return JSONResponse(
                error_response(None, INVALID_REQUEST, "Batch requests are not supported by this gateway"),
                status_code=JSONRPC_HTTP_STATUS,
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                error_response(None, INVALID_REQUEST, "Invalid Request"),
                status_code=JSONRPC_HTTP_STATUS,
            )

        method = payload.get("method")
        if not isinstance(method, str):
            return JSONResponse(
                error_response(payload.get("id"), INVALID_REQUEST, "Invalid Request"),
                status_code=JSONRPC_HTTP_STATUS,
            )

        notification = is_notification(payload)
        request_id = payload.get("id")

        # 2. Classify. A Deny is answered here and goes no further.
        decision = decide(principal, method, payload.get("params"))
        if isinstance(decision, Deny):
            logger.warning(
                "denied method=%s role=%s: %s",
                method,
                principal.role,
                (decision.data or {}).get("reason", decision.message),
            )
            if notification:
                # A notification gets no response even when it is refused. The
                # refusal still happened -- it is in the log, and downstream was
                # not called -- but the spec forbids a body, and inventing one
                # would desynchronise a client that is not expecting it.
                return Response(status_code=204)
            return JSONResponse(
                error_response(request_id, decision.code, decision.message, decision.data),
                status_code=JSONRPC_HTTP_STATUS,
            )

        assert isinstance(decision, Allow)

        # 3. Forward.
        try:
            upstream = await _forward(app.state.client, settings, payload)
        except httpx2.HTTPError as exc:
            logger.error("downstream request failed: %s", type(exc).__name__)
            if notification:
                return Response(status_code=204)
            return JSONResponse(
                # The exception's text can carry the downstream URL, so only its
                # class name is logged and nothing of it reaches the client.
                error_response(request_id, INTERNAL_ERROR, "Downstream MCP server is unreachable"),
                status_code=JSONRPC_HTTP_STATUS,
            )

        if notification:
            return Response(status_code=204)

        return _relay(upstream, principal, settings, method)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _forward(
    client: httpx2.AsyncClient,
    settings: GatewayConfig,
    payload: dict[str, Any],
) -> httpx2.Response:
    """Send the payload downstream under the *gateway's* credential.

    The client's bearer token is deliberately absent. Forwarding it is the
    "confused deputy" / credential-passthrough anti-pattern the MCP security
    guidance calls out: the downstream would then be trusting a token it did not
    issue and cannot scope, and the gateway's authorization becomes advisory.
    """
    headers = {"content-type": "application/json"}
    if settings.downstream_token:
        headers["authorization"] = f"Bearer {settings.downstream_token}"
    return await client.post(
        settings.downstream_url,
        content=json.dumps(payload),
        headers=headers,
        timeout=settings.request_timeout,
    )


def _relay(
    upstream: httpx2.Response,
    principal: Principal,
    settings: GatewayConfig,
    method: str,
) -> Response:
    """Return the downstream response, optionally filtering a tools/list result."""
    if not (settings.filter_tool_list and method == "tools/list"):
        # Transparent by default: the body is relayed byte-for-byte rather than
        # re-serialised, so a field this gateway does not model survives the trip.
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    try:
        body = upstream.json()
    except ValueError:
        logger.warning("tools/list response was not JSON; relaying unfiltered")
        return Response(content=upstream.content, status_code=upstream.status_code)

    tools = body.get("result", {}).get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return Response(content=upstream.content, status_code=upstream.status_code)

    permitted = visible_tools(tools, principal)
    if len(permitted) != len(tools):
        logger.info("hid %d admin tool(s) from role=%s", len(tools) - len(permitted), principal.role)
    body["result"]["tools"] = permitted
    return JSONResponse(body, status_code=upstream.status_code)


def _unauthenticated(reason: str) -> Response:
    """HTTP 401 -- deliberately *not* a JSON-RPC error. See the README.

    A missing or unrecognised credential is a transport-layer failure: there is
    no authenticated session in which a JSON-RPC error would mean anything, and
    MCP's own auth spec says to answer 401 with `WWW-Authenticate`. Contrast
    -32001, which is an answer *within* an authenticated session.
    """
    return JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway"'},
    )


app = create_app()
