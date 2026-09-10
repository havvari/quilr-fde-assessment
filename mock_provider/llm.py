"""A fake LLM provider with scriptable failure modes.

Tasks 3 and 4 are graded on behaviour that a real provider will not reproduce on
demand: a 429 exactly when you want one, a hang that outlives your timeout, and --
the one that matters most -- an SSE stream whose PII lands *across* chunk
boundaries. Every one of those is a deterministic mode here.

Mode is selected by the `mode` field in the request body, or by the
`X-Mock-Mode` header (the header wins, so a proxy test can force a mode without
rewriting the body it is forwarding).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from common.logging import get_logger

logger = get_logger(__name__)


class Mode(StrEnum):
    STREAM = "stream"
    RATE_LIMITED = "rate_limited"
    HANG = "hang"
    SPLIT_PII = "split_pii"
    NON_STREAM = "non_stream"
    SERVER_ERROR = "server_error"


class CompletionRequest(BaseModel):
    model: str = "mock-primary"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    mode: Mode = Mode.STREAM
    # Seconds to sleep in HANG mode. Default outlives Task 4's 3s timeout.
    hang_seconds: float = 10.0
    # Per-chunk delay, used to make TTFT measurable rather than instantaneous.
    chunk_delay: float = 0.0
    prompt_tokens: int = 100
    completion_tokens: int = 50


# A scripted answer for STREAM mode: nothing sensitive, just several deltas.
_PLAIN_SCRIPT: tuple[str, ...] = (
    "Sure. ",
    "Your account ",
    "is in good ",
    "standing and ",
    "no action is ",
    "required.",
)

# The PII payload for SPLIT_PII mode. Kept as one string so the test suite can
# import it and assert on exactly what should have been redacted.
PII_TEXT = (
    "Here are the details you asked for. "
    "Email: ada.lovelace@example.com. "
    "SSN: 123-45-6789. "
    "Card: 4111-1111-1111-1111. "
    "Let me know if you need anything else."
)

# Cut points chosen to land inside each pattern: mid local-part, mid SSN group,
# and between the third and fourth card group. A gateway that runs its regex
# per-chunk finds nothing in any of these.
_SPLIT_SCRIPT: tuple[str, ...] = (
    PII_TEXT[:47],  # ends inside "ada.love|lace@..."
    PII_TEXT[47:76],  # ends inside "123-45|-6789"
    PII_TEXT[76:104],  # ends inside "4111-1111-1111|-1111"
    PII_TEXT[104:],
)

app = FastAPI(title="mock-llm-provider")


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _delta_frame(text: str, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _final_frame(req: CompletionRequest) -> dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "model": req.model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": req.prompt_tokens,
            "completion_tokens": req.completion_tokens,
            "total_tokens": req.prompt_tokens + req.completion_tokens,
        },
    }


async def _stream(req: CompletionRequest, script: tuple[str, ...]) -> AsyncIterator[bytes]:
    for piece in script:
        if req.chunk_delay:
            await asyncio.sleep(req.chunk_delay)
        yield _sse(_delta_frame(piece, req.model))
    yield _sse(_final_frame(req))
    yield b"data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def completions(
    request: Request,
    x_mock_mode: Annotated[str | None, Header()] = None,
) -> Any:
    raw = await request.json()
    if x_mock_mode:
        raw["mode"] = x_mock_mode
    req = CompletionRequest.model_validate(raw)
    logger.info("mock provider serving mode=%s model=%s", req.mode, req.model)

    match req.mode:
        case Mode.RATE_LIMITED:
            # Body deliberately carries text that must never reach a client of the
            # Task 4 gateway, so the sanitization test has something to assert on.
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Rate limit reached for mock-primary on https://internal.mock/v1",
                        "type": "tokens",
                    }
                },
                headers={"Retry-After": "30"},
            )
        case Mode.SERVER_ERROR:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": (
                            'Traceback (most recent call last):\n  File "/srv/provider/handler.py", '
                            "line 42, in serve\n    raise RuntimeError('shard mock-primary-7 offline')"
                        )
                    }
                },
            )
        case Mode.HANG:
            await asyncio.sleep(req.hang_seconds)
            return JSONResponse(content={"note": "you waited this out"})
        case Mode.NON_STREAM:
            return JSONResponse(
                content={
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": PII_TEXT},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": _final_frame(req)["usage"],
                }
            )
        case Mode.SPLIT_PII:
            script = _SPLIT_SCRIPT
        case _:
            script = _PLAIN_SCRIPT

    return StreamingResponse(_stream(req, script), media_type="text/event-stream")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
