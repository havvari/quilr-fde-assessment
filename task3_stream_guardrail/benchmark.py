"""Measure the guardrail's TTFT overhead against a passthrough baseline.

    uv run python -m task3_stream_guardrail.benchmark

Timed on the pipeline directly, not through an HTTP client: `TestClient` drains
a response before returning it, so it cannot observe time-to-first-token at all.
The numbers in the README come from this script.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from collections.abc import AsyncIterator

from task3_stream_guardrail.redactor import StreamingRedactor
from task3_stream_guardrail.sse import redact_stream

DELAY = 0.02
RUNS = 15
PARTS = [
    "Here are the details. ",
    "Email: ada.lovelace@example.com. ",
    "SSN: 123-45-6789. ",
    "Card: 4111-1111-1111-1111. ",
    "Anything else?",
]


def _frame(text: str) -> bytes:
    payload = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _source() -> AsyncIterator[bytes]:
    for part in PARTS:
        await asyncio.sleep(DELAY)
        yield _frame(part)
    yield b"data: [DONE]\n\n"


async def _measure(pipeline: AsyncIterator[bytes]) -> tuple[float, float]:
    started = time.perf_counter()
    first: float | None = None
    async for frame in pipeline:
        if first is None and b"content" in frame:
            first = time.perf_counter() - started
    return first or 0.0, time.perf_counter() - started


async def main() -> None:
    baseline: list[tuple[float, float]] = []
    guarded: list[tuple[float, float]] = []
    for _ in range(RUNS):
        baseline.append(await _measure(_source()))
        guarded.append(await _measure(redact_stream(_source(), StreamingRedactor())))

    base_ttft = statistics.median(run[0] for run in baseline) * 1000
    base_total = statistics.median(run[1] for run in baseline) * 1000
    guard_ttft = statistics.median(run[0] for run in guarded) * 1000
    guard_total = statistics.median(run[1] for run in guarded) * 1000

    lines = [
        f"{len(PARTS)} chunks, {DELAY * 1000:.0f} ms between them, median of {RUNS} runs",
        f"  passthrough   TTFT {base_ttft:7.2f} ms   total {base_total:7.2f} ms",
        f"  guardrail     TTFT {guard_ttft:7.2f} ms   total {guard_total:7.2f} ms",
        f"  TTFT overhead      {guard_ttft - base_ttft:+7.2f} ms",
        f"  buffer-then-redact would show TTFT = total = {base_total:.2f} ms",
    ]
    # stdout is fine here: this is a CLI, not a stdio MCP server.
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
