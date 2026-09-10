"""Server-sent-event framing for the guardrail.

Frames are rebuilt, never byte-patched. `[REDACTED]` is a different length from
what it replaces, so splicing bytes into the upstream frame would desynchronise
`Content-Length`-style fields and produce invalid JSON the moment a redaction
changes size. Re-encoding is cheap -- one small object per delta -- and correct.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

from common.logging import get_logger
from task3_stream_guardrail.redactor import StreamingRedactor

logger = get_logger(__name__)

DATA_PREFIX = "data: "
DONE = "[DONE]"

# A single SSE frame from a chat-completions provider is a few hundred bytes.
# This cap exists so a malformed upstream that never sends a newline cannot grow
# the line buffer without bound -- the one place this proxy could still balloon.
MAX_LINE_BYTES = 1 << 20


class MalformedStream(Exception):
    """The upstream sent something this proxy will not forward."""


def encode(payload: dict[str, Any]) -> bytes:
    return f"{DATA_PREFIX}{json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def done_frame() -> bytes:
    return f"{DATA_PREFIX}{DONE}\n\n".encode()


def delta_text(frame: dict[str, Any]) -> str | None:
    """The assistant text in a chunk frame, or None if it carries none.

    Returns None rather than "" for a frame with no content at all -- a
    finish_reason or usage frame -- because those must be forwarded untouched,
    while an empty content string is a delta we can simply drop.
    """
    choices = frame.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return content if isinstance(content, str) else None


def with_text(frame: dict[str, Any], text: str) -> dict[str, Any]:
    """A copy of `frame` whose delta content is `text`.

    Shallow-copied down the one path that changes, so unrelated fields the
    provider sent -- and this proxy does not model -- survive the rewrite.
    """
    rebuilt = dict(frame)
    choices = [dict(choice) for choice in frame["choices"]]
    choices[0]["delta"] = {**choices[0].get("delta", {}), "content": text}
    rebuilt["choices"] = choices
    return rebuilt


def _is_terminal(frame: dict[str, Any]) -> bool:
    choices = frame.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    if isinstance(first, dict) and first.get("finish_reason") is not None:
        return True
    return "usage" in frame


def transform_line(line: str, redactor: StreamingRedactor) -> Iterator[bytes]:
    """Turn one upstream SSE line into zero or more downstream frames."""
    stripped = line.rstrip("\r")
    if not stripped:
        return
    if not stripped.startswith(DATA_PREFIX):
        # Comments (`: keepalive`) and any other field pass through untouched.
        yield f"{stripped}\n\n".encode()
        return

    payload = stripped[len(DATA_PREFIX) :].strip()
    if payload == DONE:
        tail = redactor.flush()
        if tail:
            yield encode(_synthetic_delta(tail))
        yield done_frame()
        return

    try:
        frame = json.loads(payload)
    except json.JSONDecodeError:
        # Not ours to interpret, and not safe to redact. Forwarding unparsed text
        # would defeat the guardrail, so the stream is failed instead.
        raise MalformedStream("upstream sent a data frame that is not JSON") from None

    if not isinstance(frame, dict):
        raise MalformedStream("upstream sent a data frame that is not an object")

    text = delta_text(frame)
    if text is None:
        # No content to inspect. If this is the end of the message, everything
        # still held back has to go out *before* the terminal frame.
        if _is_terminal(frame):
            tail = redactor.flush()
            if tail:
                yield encode(_synthetic_delta(tail))
        yield encode(frame)
        return

    safe = redactor.feed(text)
    if safe:
        yield encode(with_text(frame, safe))
    # An empty result means every character of this delta is still held back
    # pending a possible match. Emitting a frame with "" would be noise.


def _synthetic_delta(text: str) -> dict[str, Any]:
    """A frame to carry text released by a flush, which no upstream frame owns."""
    return {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


async def iter_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Split a byte stream into lines without waiting for the whole body.

    Yields each line as soon as its newline arrives, so a delta is forwarded the
    moment it is complete. Only a partial line is ever held.
    """
    pending = ""
    async for chunk in chunks:
        pending += chunk.decode("utf-8", errors="replace")
        if len(pending) > MAX_LINE_BYTES:
            raise MalformedStream("upstream line exceeded the size cap")
        while "\n" in pending:
            line, _, pending = pending.partition("\n")
            yield line
    if pending:
        yield pending


async def redact_stream(chunks: AsyncIterator[bytes], redactor: StreamingRedactor) -> AsyncIterator[bytes]:
    """The guardrail: upstream SSE bytes in, redacted SSE bytes out.

    Nothing is accumulated. At any moment this holds one partial line plus the
    redactor's tail.
    """
    saw_done = False
    async for line in iter_lines(chunks):
        for frame in transform_line(line, redactor):
            if frame == done_frame():
                saw_done = True
            yield frame

    if not saw_done:
        # The upstream ended without a terminator -- a dropped connection, or a
        # provider that simply does not send one. Held-back text must still be
        # released, or the client silently loses the end of the message.
        tail = redactor.flush()
        if tail:
            logger.info("flushing %d held character(s) after an unterminated stream", len(tail))
            yield encode(_synthetic_delta(tail))
