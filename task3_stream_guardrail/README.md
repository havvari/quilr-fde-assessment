# Task 3 — Streaming PII redaction guardrail

An LLM gateway endpoint that proxies chat completions to a provider, streams the response back,
and redacts emails, SSNs and credit card numbers **in flight** — without ever holding the whole
response.

## How to run it

```bash
make run-mock-llm        # terminal 1 — the provider, :8100
make run-task3           # terminal 2 — the guardrail, :8400
```

```bash
# PII deliberately split across provider chunk boundaries
curl -N localhost:8400/v1/chat/completions -d '{"mode":"split_pii"}'
# data: {"choices":[{"index":0,"delta":{"content":"Here are the details you asked for. Email: "},...
# data: {"choices":[{"index":0,"delta":{"content":"[REDACTED]"},...
```

Configuration: `UPSTREAM_LLM_URL`, `GUARDRAIL_HOLD`, `GUARDRAIL_REQUIRE_LUHN`,
`GUARDRAIL_TIMEOUT_SECONDS`.

## How to test it

```bash
uv run pytest tests/test_task3_guardrail.py -v
uv run python -m task3_stream_guardrail.benchmark     # the TTFT numbers below
```

---

## Design decisions

### 1. Both obvious implementations are wrong

**Regex per chunk** misses everything that straddles a boundary. A provider will split
`4111-1111-1111-1111` into `...Card: 4111-1111-1` and `111-1111. Let me...` whenever it feels
like it — neither chunk contains a card number, nothing matches, the card ships.
`test_a_per_chunk_regex_would_have_missed_this` asserts that the fixture really does defeat the
naive approach, so the rest of the suite is testing something.

**Buffer everything, then redact** is correct and destroys the product: no token reaches the
client until the model has finished, so TTFT becomes total generation time, and memory grows
with the response.

### 2. A held-back tail, sized from the patterns

The redactor keeps a **hold**: text within `hold` characters of the end might still be part of a
match that future input will complete, so it waits. Everything older is provably safe and goes
out immediately. At end of stream `flush()` runs a final pass and releases the tail.

`hold` is **derived, not guessed** (`max_pattern_length()`): it is the longest string any
alternative in the pattern can match, which is why every quantifier in the regex is bounded.
An unbounded `+` would mean there is no safe boundary and no correct hold size at all.

| Pattern | Longest match |
|---|---|
| email | 64 local + `@` + 63 domain + 4×64 subdomains + `.` + 24 TLD = **409** |
| card | 19 digits + 15 separators = **34** |
| SSN | 9 digits + 2 separators = **11** |

Too small and matches straddling a boundary are missed *silently*, which is the worst possible
failure for a guardrail. `test_shrinking_the_hold_below_the_pattern_length_breaks_correctness`
sets `hold=4` and shows the card sailing through — the constant is load-bearing, and that test
proves it rather than asserting it.

### 3. …but 409 characters of latency on every stream would be unacceptable

So the hold is a **worst-case cap, not a fixed delay**. `_unsafe_start()` walks back from the
end of the buffer over characters that could still be part of a match, and stops at the first
one that could not be. A match is a contiguous run over a small alphabet, so any run ending at a
character no pattern can contain cannot be extended across it.

In prose the run ends at the previous space, so the delay is **one partial word**. Mid-card-
number the run is the whole number, which is exactly when waiting is the right answer. The
subtlety is that a space *is* a valid separator inside a card or SSN — so a space counts as
match-eligible only when it sits between digits. That single check is what stops ordinary
English from being held for the full window.

Measured (`uv run python -m task3_stream_guardrail.benchmark`, 5 chunks, 20 ms apart, median of 15):

```
  passthrough   TTFT   21.15 ms   total  105.49 ms
  guardrail     TTFT   21.31 ms   total  105.83 ms
  TTFT overhead        +0.16 ms
  buffer-then-redact would show TTFT = total = 105.49 ms
```

**+0.16 ms** against a passthrough baseline; a buffering implementation would show 105 ms.
Timed on the pipeline directly, because `TestClient` drains a response before returning it and
so cannot observe TTFT at all — a subtlety that makes an HTTP-level TTFT test quietly meaningless.

### 4. One compiled regex, not three passes

`PATTERN` is a single precompiled alternation with named groups, so each character is examined
once per scan rather than three times. Named groups are what let the optional Luhn gate apply
to card matches only.

### 5. Frames are re-encoded, never byte-patched

`[REDACTED]` is a different length from what it replaces, so splicing it into the upstream bytes
would produce invalid JSON the moment a redaction changes size. `with_text()` shallow-copies the
frame down the one path that changes, so provider fields this proxy does not model survive the
rewrite — asserted by `test_sse_preserves_unmodelled_upstream_fields`.

### 6. Memory

At any moment the pipeline holds one partial SSE line plus the redactor's tail. Nothing else.
`test_peak_buffer_is_bounded_regardless_of_response_length` streams 10,000 deltas and asserts
the peak stays inside `2·hold + chunk` while output keeps flowing. `MAX_LINE_BYTES` caps the
line buffer so a malformed upstream that never sends a newline cannot grow it without bound —
the one remaining place this proxy could balloon.

### 7. Failure modes that are easy to get wrong

- **A stream that ends without a terminator** still flushes. Otherwise held-back text is
  silently dropped, and the client loses the end of the message without being told — worse than
  not redacting, because there is no signal at all.
- **The flush is emitted *before* the terminal frame**, so a client that stops reading at
  `finish_reason` does not lose the tail.
- **A `data:` frame that is not JSON fails the stream** rather than being forwarded. Text we
  cannot parse is text we cannot inspect, and forwarding it defeats the guardrail.
- **Upstream error bodies never reach the client.** A provider's 4xx/5xx text routinely carries
  internal hostnames and stack traces; the client gets the sanitized envelope from
  `common/errors.py` instead. Mid-stream the headers are long gone, so it arrives as an
  `event: error` frame.

### 8. Over-redaction is the cheaper error

The Luhn checksum gate is implemented and **off by default**. For a guardrail a false positive
costs a redacted order number; a false negative leaks a card. So the default over-redacts, and
`require_luhn` exists for corpora full of long digit runs that are not cards.
`test_luhn_gate_is_opt_in_and_over_redacts_by_default` pins both behaviours.

Deliberate non-matches, all asserted: `1234567` (too short), `90210-1234` (zip+4), `555-1234`
(phone), `1.2.3` (version). A bare 9-digit run is **not** treated as an SSN — it collides with
order numbers, and this is a text stream, not a form.

## Production gaps

- **The pattern set is three types and US-centric.** No phone numbers, addresses, dates of
  birth, passport or IBAN numbers, and no non-US national IDs. A real deployment wants a
  maintained detector set, and probably a classifier for the things regex cannot express (names,
  addresses) — which is a latency budget conversation, not a regex one.
- **`[REDACTED]` is not reversible.** Some products need format-preserving tokenisation so the
  model's own later turns stay coherent, or a vault mapping so support can un-redact.
- **Only `choices[0]` is inspected.** `n > 1` responses, tool-call arguments, and reasoning
  fields all carry text this proxy would pass through untouched. Tool-call arguments are the
  most likely real leak.
- **Nothing is logged about what was redacted** beyond a count. Compliance usually wants
  category, position and policy id — which means designing a log that records the *detection*
  without recording the *value*.
- **No inbound redaction.** PII in the user's prompt reaches the provider unchanged; this
  guards the response direction only.
- **UTF-8 is decoded with `errors="replace"`.** A multi-byte character split across a TCP chunk
  is handled correctly by the line buffer, but genuinely invalid bytes become `U+FFFD` rather
  than failing the stream.
- **Regex over adversarial text.** All quantifiers are bounded, so catastrophic backtracking is
  not a risk here, but a growing pattern set should be fuzzed for it rather than assumed safe.
