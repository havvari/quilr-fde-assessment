"""An LLM gateway endpoint that proxies completions and redacts PII in flight.

The whole path is a pipe. `httpx2.AsyncClient.stream` yields upstream bytes,
`redact_stream` transforms them, and `StreamingResponse` writes them out -- no
stage holds more than a partial line plus the redactor's tail, regardless of how
long the model talks.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from common.errors import ErrorType, GatewayError
from common.logging import get_logger
from task3_stream_guardrail.redactor import StreamingRedactor, redact
from task3_stream_guardrail.settings import GuardrailConfig
from task3_stream_guardrail.sse import MalformedStream, redact_stream

logger = get_logger(__name__)


@dataclass
class _Upstream:
    client: httpx2.AsyncClient
    config: GuardrailConfig


def create_app(config: GuardrailConfig | None = None, client: httpx2.AsyncClient | None = None) -> FastAPI:
    settings = config or GuardrailConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = client is None
        app.state.upstream = _Upstream(
            client=client or httpx2.AsyncClient(timeout=settings.request_timeout),
            config=settings,
        )
        try:
            yield
        finally:
            if owned:
                await app.state.upstream.client.aclose()

    app = FastAPI(title="llm-gateway-pii-guardrail", lifespan=lifespan)

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Response:
        upstream: _Upstream = app.state.upstream
        try:
            body = await request.json()
        except json.JSONDecodeError:
            error = GatewayError(ErrorType.INVALID_REQUEST, detail="request body was not JSON")
            return JSONResponse(error.to_response(), status_code=error.status_code)

        if body.get("stream") is False:
            return await _proxy_buffered(upstream, body)

        return StreamingResponse(
            _proxy_streaming(upstream, body),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-guardrail": "pii-redaction"},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _proxy_streaming(upstream: _Upstream, body: dict[str, Any]) -> AsyncIterator[bytes]:
    """Open the upstream stream and pipe it through the redactor.

    The `stream()` context manager stays open for the life of the generator, so
    the connection is released back to the pool when the client disconnects
    mid-response as well as when the stream ends normally.
    """
    redactor = StreamingRedactor(
        hold=upstream.config.hold_characters,
        require_luhn=upstream.config.require_luhn,
    )
    try:
        async with upstream.client.stream(
            "POST",
            upstream.config.upstream_url,
            json=body,
            timeout=upstream.config.request_timeout,
        ) as response:
            if response.status_code >= 400:
                # Read the error body but never forward it: a provider's 4xx/5xx
                # text routinely carries internal hostnames and stack traces.
                await response.aread()
                logger.warning("upstream returned %d", response.status_code)
                yield _error_frame(ErrorType.UPSTREAM_UNAVAILABLE)
                return

            async for frame in redact_stream(response.aiter_bytes(), redactor):
                yield frame

    except MalformedStream as exc:
        logger.error("refusing to forward an uninspectable stream: %s", exc)
        yield _error_frame(ErrorType.UPSTREAM_UNAVAILABLE)
    except httpx2.HTTPError as exc:
        logger.error("upstream stream failed: %s", type(exc).__name__)
        yield _error_frame(ErrorType.UPSTREAM_UNAVAILABLE)
    finally:
        logger.info(
            "stream finished: %d chars emitted, %d redaction(s), peak buffer %d",
            redactor.stats.emitted_chars,
            redactor.stats.redactions,
            redactor.stats.peak_buffer,
        )


async def _proxy_buffered(upstream: _Upstream, body: dict[str, Any]) -> Response:
    """Non-streaming passthrough, redacted in one pass.

    Present because a client that asked for `stream: false` should still not
    receive PII, and because it makes the streaming path's correctness testable
    against a trivially-correct reference.
    """
    try:
        response = await upstream.client.post(
            upstream.config.upstream_url,
            json=body,
            timeout=upstream.config.request_timeout,
        )
    except httpx2.HTTPError as exc:
        error = GatewayError(ErrorType.UPSTREAM_UNAVAILABLE, detail=type(exc).__name__)
        return JSONResponse(error.to_response(), status_code=error.status_code)

    if response.status_code >= 400:
        error = GatewayError(ErrorType.UPSTREAM_UNAVAILABLE, detail=f"upstream {response.status_code}")
        return JSONResponse(error.to_response(), status_code=error.status_code)

    payload = response.json()
    for choice in payload.get("choices", []):
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = redact(message["content"])
    return JSONResponse(payload)


def _error_frame(error_type: ErrorType) -> bytes:
    """An error delivered inside an already-open SSE stream.

    Headers are long gone by the time an upstream fails mid-stream, so the status
    code cannot be changed. The client gets the same sanitized envelope it would
    have received as a body, as a final event.
    """
    error = GatewayError(error_type)
    return f"event: error\ndata: {json.dumps(error.to_response())}\n\n".encode()


app = create_app()
