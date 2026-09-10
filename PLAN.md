# FDE Assessment — Build Plan

This is the working plan for a 4-task Forward Deployed Engineer assessment covering MCP
servers, MCP security gateways, LLM gateway stream guardrails, and a resilient model router.

Work through the milestones **in order**. Each milestone has a definition of done. Do not start
the next milestone until the current one's acceptance checks pass. Commit at the end of each
milestone with a message naming the milestone.

---

## 0. Ground rules (apply to every milestone)

**Stack**
- Python 3.11+, managed with `uv`.
- `pytest` + `pytest-asyncio` for tests. `httpx` for async HTTP. `fastapi` + `uvicorn` for HTTP servers.
- Pydantic v2 for all schema validation.
- No Docker required. Everything must run with `uv run <command>` on macOS.

**MCP SDK version — verify this before writing any MCP code**
The official Python SDK shipped a **v2** that is a breaking change from v1:
- `FastMCP` was renamed to `MCPServer`; import is `from mcp.server import MCPServer`.
- Wire protocol types moved to a standalone `mcp-types` package (imported as `mcp_types`).
- Fields normalized to snake_case.
- Docs: https://py.sdk.modelcontextprotocol.io/

Most training data and most blog posts still show v1 (`from mcp.server.fastmcp import FastMCP`).
**First action of Milestone 1: check the installed SDK version and read the current docs before
writing code.** Pin the exact version in `pyproject.toml` and record it in the README. If the
installed API differs from what's described here, follow the installed API and note the
discrepancy — do not silently write v1 code.

**Code quality bar**
- Type hints everywhere. Run `ruff` and `mypy`; both must pass clean.
- No bare `except:`. No swallowed exceptions.
- Structured logging via the `logging` module. Never `print()` in library or server code.
- Every milestone ships with tests that actually exercise the hard case, not just the happy path.

**Documentation bar — this is graded**
Each task directory gets its own `README.md` containing:
1. How to run it.
2. How to test it.
3. **Design decisions**: the 2–3 real choices made and why.
4. **Production gaps**: what you would do differently at scale.

The reasoning paragraphs matter as much as the code. Do not skip them.

**Ask before deviating.** If a requirement seems ambiguous, implement the literal reading,
then document the ambiguity and the alternative in the README. Do not silently reinterpret.

---

## Repo layout

```
fde-assessment/
├── README.md                    # index: what each task is, how to run everything
├── pyproject.toml
├── Makefile                     # make test, make lint, make run-task1, etc.
├── common/
│   ├── __init__.py
│   ├── jsonrpc.py               # JSON-RPC 2.0 types, error codes, response builders
│   ├── errors.py                # sanitized gateway error envelope
│   └── logging.py               # stderr-only logging setup
├── mock_provider/
│   ├── __init__.py
│   ├── llm.py                   # fake LLM: controllable SSE stream, 429s, hangs
│   └── mcp_downstream.py        # fake downstream MCP server for Task 2
├── task1_mcp_server/
├── task2_mcp_gateway/
├── task3_stream_guardrail/
├── task4_router/
└── tests/
```

---

## Milestone 0 — Scaffold + mock provider

Build this first. Tasks 3 and 4 cannot be tested deterministically without it.

**Deliverables**

1. `pyproject.toml`, `Makefile`, `ruff`/`mypy` config, directory skeleton.

2. `common/jsonrpc.py`:
   - Pydantic models for JSON-RPC request, response, error.
   - Constants for reserved codes: `PARSE_ERROR = -32700`, `INVALID_REQUEST = -32600`,
     `METHOD_NOT_FOUND = -32601`, `INVALID_PARAMS = -32602`, `INTERNAL_ERROR = -32603`.
   - Constant `UNAUTHORIZED_TOOL_CALL = -32001` with a comment noting that -32000..-32099 is the
     range JSON-RPC reserves for application-defined errors.
   - Helper `error_response(id, code, message, data=None)` that always echoes the request id.

3. `common/logging.py`: a `configure_logging()` that attaches a handler writing to **stderr only**.

4. `mock_provider/llm.py` — a FastAPI app exposing a chat-completions-shaped endpoint that
   supports, via request field or header:
   - `mode=stream`: streams SSE deltas from a configurable script of chunks.
   - `mode=rate_limited`: returns HTTP 429 immediately.
   - `mode=hang`: sleeps N seconds before responding (to trigger timeouts).
   - `mode=split_pii`: streams a response containing an email, an SSN, and a credit card number,
     **deliberately split across chunk boundaries mid-token**.
   - Reports a `usage` object with input/output token counts.

5. `mock_provider/mcp_downstream.py` — a minimal HTTP JSON-RPC server that answers `tools/list`
   with a fixed tool set including at least one `admin_`-prefixed tool, and answers `tools/call`
   with an echo result.

**Done when:** `make test` runs, `make lint` is clean, and the mock provider can be started and
hit with `curl` in each of its modes.

---

## Milestone 1 — MCP server with strict validation and stdio isolation

`task1_mcp_server/`

**Goal:** a runnable MCP server over stdio exposing two tools.

**Tools**

| Tool | Inputs |
|---|---|
| `get_customer_record` | `customer_id: str` matching `^CUST-\d{5}$` |
| `trigger_refund` | `customer_id` (same pattern), `amount: float` (> 0), `reason: str` (min length 10) |

Back them with an in-memory dict of fake customers. `get_customer_record` on an unknown but
well-formed ID should behave differently from a malformed ID — see below.

**Requirements**

1. **stdout isolation is the top-scored criterion.** Under stdio transport, stdout *is* the
   JSON-RPC wire. A single stray write corrupts the stream and kills the client's parser.
   - All logging goes to stderr via `common.logging`.
   - No `print()` anywhere in the import graph.
   - Add a defensive check at startup that warns (to stderr) if anything has rebound `sys.stdout`.

2. **Validation**, using Pydantic `Field` constraints on the tool signatures:
   - `customer_id`: regex `^CUST-\d{5}$`. Document the assumption that `XXXXX` means five digits.
   - `amount`: strictly positive; explicitly reject `0`, negatives, `NaN`, and `inf`.
   - `reason`: minimum length 10 **after stripping whitespace** — document this choice, since a
     string of 10 spaces should not count.

3. **Error mapping — implement both paths and explain the choice in the README.**
   The SDK distinguishes two failure modes:
   - `ToolError` (from `mcp.server.mcpserver.exceptions`) → the call *returns* with
     `is_error=True` and your message in `content`. The model reads it and can retry.
   - `MCPError` (from `mcp`) → the request itself fails with a real JSON-RPC error object.
     The model sees nothing; the host handles it.

   The task asks for "standard MCP JSON-RPC error codes," so **raise
   `MCPError(code=INVALID_PARAMS, ...)` for malformed input**. Then write a README paragraph
   explaining that in production a malformed `customer_id` is arguably better served by
   `ToolError`, because the model chose the argument and can self-correct — and that the
   deciding question is "could a smarter model have avoided this?"

   Use `MCPError` for malformed input (literal spec compliance). Use `ToolError` for
   "well-formed ID, no such customer" — that is an execution failure the model can recover from.

**Acceptance tests** (`tests/test_task1.py`)

- **stdout purity test (write this one first):** spawn the server as a subprocess over stdio,
  drive a full handshake plus several tool calls including failing ones, capture stdout, and
  assert **every single line parses as JSON**. This test is the deliverable.
- Table-driven validation tests: `CUST-1234` (too short), `cust-12345` (wrong case),
  `CUST-ABCDE`, empty string, `None`, amount `0` / `-5` / `NaN` / `inf`, reason of 9 chars,
  reason of 10 spaces.
- Assert the returned error code is `-32602` for malformed input.
- Assert `is_error=True` with a readable message for unknown-but-valid customer.

---

## Milestone 2 — MCP security gateway proxy

`task2_mcp_gateway/`

**Goal:** an HTTP/JSON-RPC reverse proxy between an agent client and `mock_provider/mcp_downstream.py`.

**Requirements**

1. Read `Authorization: Bearer <token>`. Map token → role via a static dict
   (`{"tok_admin": "admin", "tok_viewer": "viewer"}`). Add a README note that production would
   use a signed JWT with signature and expiry verification.

2. Parse the JSON-RPC body and branch on `method`:
   - `tools/list` → forward transparently downstream, return the response.
   - `tools/call` → inspect `params.name`. If it starts with `admin_`, require role `admin`.
     If not authorized, return `-32001` "Unauthorized Tool Call" **without contacting downstream**.
   - Any other method → forward.

3. **Details that are easy to get wrong — get them right:**
   - The error response must echo the **original request `id`**, or the client cannot correlate it.
   - **Notifications** (requests with no `id`) cannot receive any response per spec. Handle that branch.
   - **Batch requests** are JSON arrays. Either support them or reject with `-32600` explicitly —
     do not crash. Document which you chose.
   - JSON-RPC errors ride on **HTTP 200** with an `error` body. Do not return HTTP 401 for an
     authorization failure at the JSON-RPC layer. Be consistent and say so in the README.
   - **Do not forward the client's bearer token downstream.** Credential passthrough is an MCP
     security anti-pattern; the gateway holds its own downstream credential.

4. **Bonus, behind a config flag `FILTER_TOOL_LIST`:** filter the `tools/list` *response* by role
   so a `viewer` never sees `admin_`-prefixed tools at all. Rationale for the README: if the tool
   is visible in the model's context, the agent will attempt it and waste a turn, and you have
   leaked your admin surface area to anything that can read that context. Implement the literal
   requirement (transparent forward) as the default; ship the filter as an opt-in.

**Acceptance tests**

- viewer calling `admin_reset_key` → `-32001`, correct id echoed, **downstream never called**
  (assert with a call-count spy on the mock).
- admin calling `admin_reset_key` → forwarded, downstream called once.
- viewer calling a non-admin tool → forwarded.
- Missing / malformed / unknown bearer token → sensible rejection.
- Notification with no `id` → no response body returned.
- `FILTER_TOOL_LIST=true` + viewer → `admin_` tools absent from `tools/list` result.

---

## Milestone 3 — Streaming PII redaction guardrail

`task3_stream_guardrail/`

**Goal:** an LLM gateway endpoint that proxies to the mock provider, streams the response back,
and redacts PII in flight.

**This milestone has a real algorithmic core. Read this section carefully.**

The naive implementations both fail:
- Regex per chunk → misses `4111-1111-` + `1111-1111` split across two chunks.
- Buffer the whole response, then redact → destroys TTFT and violates the memory requirement.

**Required approach: sliding buffer with a held-back tail.**

Maintain an accumulator. On each incoming delta:
1. Append the delta text to the accumulator.
2. Run the redaction regex over the accumulator.
3. Emit everything up to `len(accumulator) - N`; retain the last `N` characters.
4. On stream end, run one final pass over the retained tail and flush it.

`N` is the longest pattern you could be mid-match on. Define it as a named constant with a
comment justifying the number (a separator-formatted card is ~22 chars; SSN is 11; cap email
local+domain at 64). `N` too small silently misses matches; `N` too large hurts TTFT.

**Other requirements**
- **One precompiled regex with alternation** for email / SSN / credit card — not three passes.
- Replace matches with `[REDACTED]`.
- **Re-encode SSE frames, do not byte-patch them.** `[REDACTED]` has a different length than what
  it replaces, so rebuild the JSON delta object rather than splicing bytes.
- Fully async, using `httpx.AsyncClient.stream` and an async generator. Never materialize the
  full response.
- Keep a running assertion (or test) that peak retained buffer size stays bounded by `N` plus one
  chunk, regardless of response length.

**Acceptance tests**

- **The boundary-splitting test — this is the whole milestone:** take a known string containing
  an email, SSN, and credit card. Re-split it into chunks at **every possible index** (and at
  every pair of indices for two-cut cases), feed each variant through the redactor, and assert
  the output is fully redacted in all cases.
- Assert peak buffer size is bounded for a long response.
- Assert a partial pattern at end-of-stream is flushed and redacted, not dropped.
- Measure and record TTFT against a passthrough baseline; put the number in the README.

---

## Milestone 4 — Rate-limiting and model fallback router

`task4_router/`

**Goal:** a resilient routing module for completion requests. Largest milestone; four
semi-independent pieces.

**4a. Token-aware sliding window rate limiter, on-disk SQLite**

- Limit: 50,000 tokens per rolling 60 seconds, per tenant API key.
- **Sliding window, not a fixed bucket.** A per-minute bucket that resets on the clock boundary
  lets a tenant burn 100k tokens across the boundary. Store `(tenant_key, ts, tokens)` rows.
- On each request: `DELETE FROM usage WHERE ts < now - 60`, then `SUM(tokens)`, then compare.
- Index on `(tenant_key, ts)`.
- **Concurrency is scored.** On-disk SQLite with concurrent async requests means the
  check-and-insert is a read-modify-write race unless you guard it:
  - `PRAGMA journal_mode=WAL` at connection setup.
  - Wrap check-and-insert in a single `BEGIN IMMEDIATE` transaction.
  - Run SQLite calls off the event loop (`asyncio.to_thread` or `aiosqlite`) so you don't block it.
- **Reserve-then-reconcile:** output token count isn't known until the response completes.
  Reserve an estimate of input tokens up front, then reconcile the row with actual usage after.
  Implement it, and explain it in the README — it's the part most candidates miss.

**4b. Failover**

- Call primary with `asyncio.wait_for(..., timeout=3.0)`.
- On `asyncio.TimeoutError` **or** HTTP 429 → fail over to the secondary provider.
- **Do not retry the primary.** It is rate-limited or overloaded; that is the entire premise.
- Ensure the cancelled primary request is properly torn down so connections aren't leaked.
  Verify with a test that asserts the client's connection pool is drained after a timeout.
- Fallback token usage **counts against the same tenant budget**. Say so in the README.

**4c. Error sanitization**

- One boundary handler catches everything.
- Return a stable envelope: `{"error": {"type": "...", "message": "...", "request_id": "..."}}`
  with `type` drawn from a small closed set (`rate_limited`, `upstream_unavailable`,
  `upstream_timeout`, `internal_error`).
- Log the real exception and traceback server-side, keyed by that same `request_id`.
- **Never** let an upstream response body, provider name, URL, or Python traceback reach the client.

**4d. Wiring** — a single `route_completion(tenant_key, request)` entry point that composes all of the above.

**Acceptance tests**

- Rate limiter: sequential requests summing under the limit pass; crossing 50,000 rejects.
- **Sliding-window correctness:** spend 40k, advance clock 30s, spend 40k → second request is
  rejected. (A fixed bucket would wrongly allow it.) Inject a clock rather than sleeping.
- **Concurrency:** fire 50 concurrent requests that would collectively exceed the limit; assert
  the total admitted never exceeds it. Run it repeatedly to shake out races.
- Failover: mock in `mode=rate_limited` → secondary is called, client gets a normal 200.
- Timeout: mock in `mode=hang` with 5s → failover fires at ~3s, not 5s. Assert on elapsed time.
- Sanitization: force an upstream 500 with a body containing a fake stack trace; assert none of
  that text appears anywhere in the client-facing response.

---

## Milestone 5 — Documentation pass

- Top-level `README.md`: what each task is, the repo map, one-command setup, one command to run
  all tests.
- Per-task READMEs completed to the bar in Ground Rules, specifically covering:
  - Task 1: `ToolError` vs `MCPError`, and the `CUST-XXXXX` / whitespace assumptions.
  - Task 2: transparent forward vs tool-list filtering; HTTP-200-with-error-body convention;
    why the client token is not forwarded downstream.
  - Task 3: how `N` was chosen; measured TTFT vs baseline.
  - Task 4: reserve-then-reconcile; SQLite WAL and `BEGIN IMMEDIATE`; why the primary isn't retried.
- A short `NOTES.md` listing any ambiguities found in the assessment brief and how they were
  resolved.

---

## Definition of done

- [ ] `make lint` clean (ruff + mypy).
- [ ] `make test` green, with no skipped tests.
- [ ] Task 1 stdout-purity subprocess test passes.
- [ ] Task 2 downstream-never-called spy assertion passes.
- [ ] Task 3 every-index boundary-splitting test passes.
- [ ] Task 4 concurrent rate-limiter test passes on 10 consecutive runs.
- [ ] Every task has a README with a Design Decisions section.
- [ ] `NOTES.md` exists.
