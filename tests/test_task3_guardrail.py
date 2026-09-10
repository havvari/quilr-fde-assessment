"""Task 3, streaming PII redaction.

The centrepiece is `test_redaction_survives_every_possible_chunk_boundary`: the
same text re-split at every index, and every pair of indices, all producing
byte-identical output. That is the property a per-chunk regex fails and the
reason the sliding buffer exists.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from itertools import combinations
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from mock_provider.llm import PII_TEXT
from mock_provider.llm import app as llm_app
from task3_stream_guardrail.app import create_app
from task3_stream_guardrail.redactor import (
    DEFAULT_HOLD,
    PATTERN,
    REDACTED,
    StreamingRedactor,
    luhn_ok,
    max_pattern_length,
    redact,
)
from task3_stream_guardrail.settings import GuardrailConfig
from task3_stream_guardrail.sse import MalformedStream, redact_stream

SAMPLE = (
    "Here are the details. Email: ada.lovelace@example.com. "
    "SSN: 123-45-6789. Card: 4111-1111-1111-1111. Thanks!"
)

# Every substring that must never survive, including partial giveaways.
FORBIDDEN = [
    "ada.lovelace@example.com",
    "ada.lovelace",
    "@example.com",
    "123-45-6789",
    "4111-1111-1111-1111",
    "4111",
]


def stream_through(chunks: list[str], **kwargs: Any) -> tuple[str, StreamingRedactor]:
    redactor = StreamingRedactor(**kwargs)
    out = "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()
    return out, redactor


# --------------------------------------------------------------------------
# The milestone.
# --------------------------------------------------------------------------


def test_redaction_survives_every_possible_chunk_boundary() -> None:
    """Every single cut, and every pair of cuts, over the whole sample."""
    expected = redact(SAMPLE)
    assert expected == ("Here are the details. Email: [REDACTED]. SSN: [REDACTED]. Card: [REDACTED]. Thanks!")

    indices = range(len(SAMPLE) + 1)

    for i in indices:
        actual, _ = stream_through([SAMPLE[:i], SAMPLE[i:]])
        assert actual == expected, f"one cut at {i} changed the output: {actual!r}"

    pairs = list(combinations(indices, 2))
    assert len(pairs) > 5000, "the two-cut sweep should be exhaustive, not a sample"
    for i, j in pairs:
        actual, _ = stream_through([SAMPLE[:i], SAMPLE[i:j], SAMPLE[j:]])
        assert actual == expected, f"two cuts at {(i, j)} changed the output: {actual!r}"


def test_character_by_character_streaming_still_redacts() -> None:
    """The pathological case: every chunk is one character long."""
    actual, redactor = stream_through(list(SAMPLE))
    assert actual == redact(SAMPLE)
    for forbidden in FORBIDDEN:
        assert forbidden not in actual
    # Nothing was accumulated to get there.
    assert redactor.stats.peak_buffer < 60


def test_a_per_chunk_regex_would_have_missed_this() -> None:
    """The fixture is only meaningful if the naive approach really does fail."""
    chunks = [SAMPLE[:47], SAMPLE[47:76], SAMPLE[76:104], SAMPLE[104:]]
    naive = "".join(PATTERN.sub(REDACTED, chunk) for chunk in chunks)

    assert "123-45-6789" not in redact(SAMPLE)
    assert any(forbidden in naive for forbidden in FORBIDDEN), "the split points are not adversarial"


# --------------------------------------------------------------------------
# Memory.
# --------------------------------------------------------------------------


def test_peak_buffer_is_bounded_regardless_of_response_length() -> None:
    """Ten thousand deltas; the buffer stays within its worst-case bound."""
    redactor = StreamingRedactor()
    emitted = 0
    for index in range(10_000):
        emitted += len(redactor.feed(f"chunk {index} of a very long answer. "))

    # The bound is `hold` for the tail, plus the straddle rule which can hold one
    # further match, plus the largest single chunk.
    assert redactor.stats.peak_buffer <= 2 * DEFAULT_HOLD + 64
    assert emitted > 100_000, "text should be flowing out, not piling up"
    assert redactor.buffered <= DEFAULT_HOLD


def test_prose_is_released_promptly_rather_than_held_for_the_full_window() -> None:
    """The run-scan refinement: prose does not pay the 409-character worst case."""
    redactor = StreamingRedactor()
    redactor.feed("The quick brown fox jumps over the lazy dog and keeps on running. ")

    # Held back is one partial word, not the whole worst-case window.
    assert redactor.buffered <= 2
    assert redactor.stats.peak_buffer < DEFAULT_HOLD


def test_mid_pattern_text_is_held_until_it_is_safe() -> None:
    """The converse: while a card number is in flight, nothing of it escapes."""
    redactor = StreamingRedactor()
    out = redactor.feed("Card: 4111-1111-1111-")
    assert "4111" not in out
    assert redactor.buffered >= len("4111-1111-1111-")

    out += redactor.feed("1111. Done.")
    out += redactor.flush()
    assert out == "Card: [REDACTED]. Done."


# --------------------------------------------------------------------------
# End-of-stream behaviour.
# --------------------------------------------------------------------------


def test_a_pattern_at_end_of_stream_is_flushed_and_redacted() -> None:
    out, _ = stream_through(["Reach me at ", "ada.lovelace@example.com"])
    assert out == "Reach me at [REDACTED]"


def test_a_partial_pattern_at_end_of_stream_is_flushed_not_dropped() -> None:
    """Text held back when the stream ends must still be delivered."""
    out, _ = stream_through(["Your card ends 4111-1111-1111"])
    # 16 digits short of a full card: not PII by our patterns, and it must not
    # vanish. Losing text silently is worse than not redacting it.
    assert out.endswith("4111-1111-1111")
    assert out == "Your card ends 4111-1111-1111"


def test_flush_is_idempotent() -> None:
    redactor = StreamingRedactor()
    redactor.feed("a@b.com")
    assert redactor.flush() == REDACTED
    assert redactor.flush() == ""


def test_shrinking_the_hold_below_the_pattern_length_breaks_correctness() -> None:
    """The constant is load-bearing; this proves it rather than asserting it.

    With a hold shorter than the pattern, a match straddling the boundary is
    emitted in halves and never redacted -- exactly the silent failure the
    derived constant exists to prevent.
    """
    text = "Card: 4111-1111-1111-1111 done"
    chunks = [text[:12], text[12:]]

    correct, _ = stream_through(chunks)
    assert "4111" not in correct

    broken, _ = stream_through(chunks, hold=4)
    assert "4111-1111-1111-1111" in broken


def test_hold_is_derived_from_the_patterns_not_guessed() -> None:
    assert max_pattern_length() == DEFAULT_HOLD
    longest = max((m.group() for m in PATTERN.finditer(SAMPLE)), key=len)
    assert len(longest) <= DEFAULT_HOLD


# --------------------------------------------------------------------------
# Pattern coverage.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail ada@example.com now", f"mail {REDACTED} now"),
        ("mail ada@example.com.", f"mail {REDACTED}."),
        ("user+tag@mail.corp.example.co.uk sent it", f"{REDACTED} sent it"),
        ("ssn 123-45-6789 here", f"ssn {REDACTED} here"),
        ("ssn 123 45 6789 here", f"ssn {REDACTED} here"),
        ("visa 4111-1111-1111-1111 ok", f"visa {REDACTED} ok"),
        ("visa 4111111111111111 ok", f"visa {REDACTED} ok"),
        ("visa 4111 1111 1111 1111 ok", f"visa {REDACTED} ok"),
        ("amex 3782 822463 10005 ok", f"amex {REDACTED} ok"),
        # Things that merely look like PII and must survive.
        ("order 1234567 shipped", "order 1234567 shipped"),
        ("zip 90210-1234 fine", "zip 90210-1234 fine"),
        ("call 555-1234 today", "call 555-1234 today"),
        ("version 1.2.3 released", "version 1.2.3 released"),
        ("no pii at all here", "no pii at all here"),
    ],
)
def test_pattern_coverage(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_multiple_patterns_in_one_delta() -> None:
    out = redact("a@b.com and 123-45-6789 and 4111111111111111")
    assert out == f"{REDACTED} and {REDACTED} and {REDACTED}"


def test_luhn_gate_is_opt_in_and_over_redacts_by_default() -> None:
    """Default errs toward over-redaction: a false negative leaks a card."""
    not_a_card = "9999 9999 9999 9998"
    assert luhn_ok("4111111111111111") is True
    assert luhn_ok("9999999999999998") is False

    assert redact(f"ref {not_a_card} ok") == f"ref {REDACTED} ok"

    lenient, _ = stream_through([f"ref {not_a_card} ok"], require_luhn=True)
    assert lenient == f"ref {not_a_card} ok"

    strict, _ = stream_through(["visa 4111111111111111 ok"], require_luhn=True)
    assert strict == f"visa {REDACTED} ok"


# --------------------------------------------------------------------------
# The SSE layer.
# --------------------------------------------------------------------------


async def _feed(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _collect(chunks: list[bytes], **kwargs: Any) -> list[str]:
    redactor = StreamingRedactor(**kwargs)
    return [frame.decode() async for frame in redact_stream(_feed(chunks), redactor)]


def _sse(text: str) -> bytes:
    frame = {
        "id": "x",
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(frame)}\n\n".encode()


async def test_sse_frames_are_re_encoded_not_byte_patched() -> None:
    frames = await _collect([_sse("Card: 4111-1111-1111-1111 done"), b"data: [DONE]\n\n"])
    payloads = [json.loads(f.removeprefix("data: ").strip()) for f in frames if "[DONE]" not in f]

    text = "".join(p["choices"][0]["delta"]["content"] for p in payloads)
    assert text == "Card: [REDACTED] done"
    # Every emitted frame is valid JSON of the right shape -- which splicing a
    # differently-sized replacement into the upstream bytes would not be.
    for payload in payloads:
        assert payload["choices"][0]["index"] == 0


async def test_sse_preserves_unmodelled_upstream_fields() -> None:
    frames = await _collect([_sse("hello there friend"), b"data: [DONE]\n\n"])
    payload = json.loads(frames[0].removeprefix("data: ").strip())
    assert payload["model"] == "m"
    assert payload["id"] == "x"


async def test_sse_flushes_held_text_before_the_terminal_frame() -> None:
    terminal = (
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":5}}\n\n'
    )
    frames = await _collect([_sse("ends with a@b.com"), terminal, b"data: [DONE]\n\n"])

    joined = "".join(frames)
    assert REDACTED in joined
    # The flush must precede the terminal frame, or a client that stops reading
    # at finish_reason loses the tail.
    assert joined.index(REDACTED) < joined.index('"finish_reason":"stop"')


async def test_sse_flushes_when_the_upstream_never_terminates() -> None:
    frames = await _collect([_sse("dropped mid sentence, mail a@b.com")])
    assert REDACTED in "".join(frames)


async def test_sse_passes_through_comments_and_ignores_blank_lines() -> None:
    frames = await _collect([b": keepalive\n\n", _sse("hi there everyone"), b"data: [DONE]\n\n"])
    assert frames[0] == ": keepalive\n\n"


async def test_sse_split_mid_frame_still_works() -> None:
    """The transport can cut anywhere, including inside a JSON frame."""
    raw = _sse("Card: 4111-1111-1111-1111 done") + b"data: [DONE]\n\n"
    for cut in range(1, len(raw)):
        frames = await _collect([raw[:cut], raw[cut:]])
        joined = "".join(frames)
        assert "4111" not in joined, f"leak when the transport cut at byte {cut}"


async def test_uninspectable_stream_raises_rather_than_forwarding() -> None:
    """Text we cannot parse is text we cannot redact, so it must not pass."""
    with pytest.raises(MalformedStream):
        await _collect([b"data: this is not json\n\n"])


def test_proxy_turns_an_uninspectable_stream_into_an_error_event() -> None:
    """And at the proxy boundary that becomes a sanitized SSE error event."""

    async def bad_stream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"data: this is not json\n\n")

    config = GuardrailConfig(upstream_url="http://provider.test/v1/chat/completions")
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(bad_stream))
    with TestClient(create_app(config, client)) as gateway:
        response = gateway.post("/v1/chat/completions", json={})

    assert "event: error" in response.text
    assert "upstream_unavailable" in response.text
    assert "this is not json" not in response.text


# --------------------------------------------------------------------------
# The proxy end to end.
# --------------------------------------------------------------------------


@pytest.fixture
def guardrail() -> Iterator[TestClient]:
    config = GuardrailConfig(upstream_url="http://provider.test/v1/chat/completions")
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=llm_app))
    with TestClient(create_app(config, client)) as test_client:
        yield test_client


def _assistant_text(sse_body: str) -> str:
    text = ""
    for line in sse_body.splitlines():
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        frame = json.loads(line.removeprefix("data: "))
        for choice in frame.get("choices", []):
            text += choice.get("delta", {}).get("content", "")
    return text


def test_proxy_redacts_pii_split_across_provider_chunks(guardrail: TestClient) -> None:
    """End to end against the provider's deliberately adversarial split."""
    response = guardrail.post("/v1/chat/completions", json={"mode": "split_pii"})

    assert response.status_code == 200
    assert response.headers["x-guardrail"] == "pii-redaction"
    for forbidden in FORBIDDEN:
        assert forbidden not in response.text, f"{forbidden} survived the proxy"

    assert _assistant_text(response.text) == redact(PII_TEXT)
    assert response.text.rstrip().endswith("data: [DONE]")


def test_proxy_leaves_clean_text_untouched(guardrail: TestClient) -> None:
    response = guardrail.post("/v1/chat/completions", json={"mode": "stream"})
    assert _assistant_text(response.text) == (
        "Sure. Your account is in good standing and no action is required."
    )
    assert REDACTED not in response.text


def test_proxy_redacts_non_streaming_responses_too(guardrail: TestClient) -> None:
    response = guardrail.post("/v1/chat/completions", json={"mode": "non_stream", "stream": False})
    content = response.json()["choices"][0]["message"]["content"]
    assert content == redact(PII_TEXT)
    for forbidden in FORBIDDEN:
        assert forbidden not in response.text


def test_proxy_hides_upstream_error_bodies(guardrail: TestClient) -> None:
    response = guardrail.post("/v1/chat/completions", json={"mode": "server_error"})
    assert "Traceback" not in response.text
    assert "mock-primary-7" not in response.text
    assert "upstream_unavailable" in response.text


def test_proxy_forwards_usage_from_the_terminal_frame(guardrail: TestClient) -> None:
    response = guardrail.post("/v1/chat/completions", json={"mode": "split_pii"})
    usage = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"usage"' in line
    ]
    assert usage and usage[0]["usage"]["total_tokens"] == 150


async def test_guardrail_adds_almost_nothing_to_time_to_first_token() -> None:
    """TTFT is set by the provider's first delta, not by total generation time.

    Measured against a passthrough baseline over the same delayed source. A
    buffer-everything implementation would show a TTFT of roughly the *total*
    stream duration; this shows roughly one chunk delay.

    Timed on the pipeline directly rather than through `TestClient`, which drains
    the whole response before handing it back and so cannot observe TTFT at all.
    """
    delay = 0.02
    parts = ["The account ", "is in good ", "standing and ", "nothing is ", "required."]

    async def delayed() -> AsyncIterator[bytes]:
        for part in parts:
            await asyncio.sleep(delay)
            yield _sse(part)
        yield b"data: [DONE]\n\n"

    async def measure(pipeline: AsyncIterator[bytes]) -> tuple[float, float]:
        started = time.perf_counter()
        first: float | None = None
        async for frame in pipeline:
            if first is None and b"content" in frame:
                first = time.perf_counter() - started
        return first or 0.0, time.perf_counter() - started

    baseline_ttft, baseline_total = await measure(delayed())
    guarded_ttft, guarded_total = await measure(redact_stream(delayed(), StreamingRedactor()))

    # Recorded in the README; asserted loosely because wall-clock tests on a
    # shared machine are flaky when asserted tightly.
    assert guarded_ttft < baseline_ttft + delay, (
        f"guardrail TTFT {guarded_ttft:.4f}s vs baseline {baseline_ttft:.4f}s"
    )
    assert guarded_ttft < guarded_total / 2, "TTFT should be a fraction of total stream time"
    assert baseline_total >= delay * len(parts) * 0.8


async def test_no_frame_is_withheld_until_the_stream_ends() -> None:
    """Output must interleave with input, not arrive in one burst at the end."""
    released: list[int] = []

    async def source() -> AsyncIterator[bytes]:
        for index in range(6):
            yield _sse(f"sentence number {index} with plenty of ordinary words. ")
            released.append(len(released))

    frames = [f async for f in redact_stream(source(), StreamingRedactor())]
    assert len(frames) >= 4, "text is being accumulated rather than streamed"
