"""HTTP surface for the routing module, with one error boundary.

There is exactly one place an exception becomes a response body
(`_gateway_error_handler` plus the boundary in `CompletionRouter`), which is what
makes "no upstream detail ever reaches a client" checkable by reading one file
rather than by auditing every handler.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx2
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from common.errors import ErrorType, GatewayError, sanitize
from common.logging import get_logger
from task4_router.router import CompletionRouter, response_body
from task4_router.settings import RouterConfig

logger = get_logger(__name__)


def create_app(
    config: RouterConfig | None = None,
    client: httpx2.AsyncClient | None = None,
) -> FastAPI:
    settings = config or RouterConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = client is None
        http = client or httpx2.AsyncClient()
        app.state.router = CompletionRouter(settings, http)
        try:
            yield
        finally:
            if owned:
                await http.aclose()

    app = FastAPI(title="llm-gateway-router", lifespan=lifespan)

    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError) -> Response:
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after) + 1)
        return JSONResponse(exc.to_response(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(Exception)
    async def _catch_all(request: Request, exc: Exception) -> Response:
        """Nothing escapes uncategorised. `sanitize` logs the traceback."""
        error = sanitize(exc)
        return JSONResponse(error.to_response(), status_code=error.status_code)

    @app.post("/v1/chat/completions")
    async def completions(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        tenant_key = _tenant_from(authorization)
        try:
            body: dict[str, Any] = await request.json()
        except ValueError as exc:
            raise GatewayError(ErrorType.INVALID_REQUEST, detail="body was not JSON") from exc
        if not isinstance(body, dict):
            raise GatewayError(ErrorType.INVALID_REQUEST, detail="body was not an object")

        routed = await app.state.router.route_completion(tenant_key, body)
        return JSONResponse(response_body(routed))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _tenant_from(authorization: str | None) -> str:
    """The tenant is the API key. Absent one, everything shares an anonymous bucket.

    Rejecting instead would be defensible, but the brief scopes the limit to a
    tenant API key without specifying authentication, so an unauthenticated
    caller is metered rather than refused -- and shares a bucket with every other
    unauthenticated caller, which is the conservative reading.
    """
    if not authorization:
        return "anonymous"
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return "anonymous"
    return credential.strip()


app = create_app()
