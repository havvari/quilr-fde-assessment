# Task 1 — MCP server with strict validation and stdio isolation

An MCP server exposing one tool, `trigger_refund`, over stdio transport.

## How to run it

```bash
uv run python -m task1_mcp_server
```

It speaks JSON-RPC on stdin/stdout, so running it in a terminal looks like it has hung.
That is correct — it is waiting for a client. To drive it by hand:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | uv run python -m task1_mcp_server 2>/dev/null
```

As an MCP server entry in a client config:

```json
{ "command": "uv", "args": ["run", "--directory", "/path/to/quilr", "python", "-m", "task1_mcp_server"] }
```

## How to test it

```bash
uv run pytest tests/test_task1_stdout.py tests/test_task1_validation.py -v
```

`tests/stdio_harness.py` spawns the server as a real subprocess and owns the pipe directly
rather than going through the SDK client — the property under test is *"nothing but JSON-RPC
ever reaches fd 1"*, and a client that parses fd 1 for you is not in a position to prove it.

## SDK version

Pinned to **`mcp==2.2.0`**. v2 is a breaking change from v1 and most examples online are still
v1. `FastMCP` is now `MCPServer`, wire types moved to the standalone `mcp-types` package
(imported as `mcp_types`), and fields are snake_case. `mcp.server.fastmcp` still exists purely
to raise `ModuleNotFoundError` with a pointer to the migration guide, which is how you find out.

---

## Design decisions

### 1. Malformed input returns `-32602`; a declined refund returns `isError: true`

The SDK's own default is *not* what the brief asks for. `mcp/server/mcpserver/tools/base.py`
catches pydantic's `ValidationError` and raises `ToolError`, which means the call **succeeds**
and carries `isError: true`. Its comment says why: *"The caller's arguments don't match the
input schema: the model's mistake to read and correct."*

That reasoning is good, and in production I would often agree with it. The deciding question is
**"could a smarter model have avoided this?"** If yes — the model invented `CUST-ABCDE` when the
schema plainly says five digits — then the failure belongs in front of the model, as an
`isError` result it can read and retry from. A `-32602` is invisible to the model; the host
handles it and the model just sees its turn fail.

But the brief says *"Reject invalid formats with standard MCP JSON-RPC error codes,"* and that
is unambiguous. So `StrictParamsExtension` (in `server.py`) validates arguments against
`RefundRequest` **before** the tool runs and raises `MCPError(INVALID_PARAMS)`, which the SDK
propagates untouched into a top-level JSON-RPC error object. Because our check runs first, the
SDK's `ToolError` path is never reached for bad arguments.

What stays a `ToolError`:

| Case | Outcome | Why |
|---|---|---|
| `customer_id` is `CUST-ABCDE` | `-32602` | Schema violation. Literal spec compliance. |
| `customer_id` is `CUST-00000`, nobody has it | `isError: true` | Well-formed. The world said no. |
| `amount` exceeds the refundable balance | `isError: true` | Well-formed. A business rule declined. |

The second and third are execution failures a model can genuinely act on — pick a smaller
amount, re-confirm the id — so the message is written for the model to read, naming the id and
the remaining balance.

The extension is scoped to a named allowlist (`STRICT_TOOL_SCHEMAS`) rather than applied
globally, so the override is opt-in and greppable rather than ambient. Its cost, stated plainly:
an extension is advertised under `capabilities.extensions`, so this server is visibly not a
stock one. That is honest — it genuinely behaves differently — but it is a deviation.

### 2. stdout isolation — what the SDK gives you, and what it doesn't

Under stdio, **fd 1 is the JSON-RPC wire**. One stray byte desynchronises the client's parser
for the rest of the session.

The interesting finding: **MCP SDK v2 already defends this**. `mcp/server/stdio.py` claims fd 1
at session start — it `dup2`s the real wire onto a private descriptor and points fd 1 at
**stderr** for the session's lifetime (`"While serving, fd 0 points at the null device and fd 1
at stderr, so handlers…"`). A `print()` inside a tool body therefore lands on stderr and is
harmless. `test_print_inside_a_handler_cannot_reach_the_wire` asserts this rather than assuming it.

So "we were careful not to call `print()`" is not the claim worth making. **The residual hole is
the window before the claim**: anything written at import time — a module-level `print`, a
dependency announcing itself, a deprecation banner — goes onto the real fd 1 and lands in front
of the first frame. `tests/fixtures/noisy_server.py` is exactly that, and
`test_import_time_write_corrupts_the_wire` drives it to show the corruption. That test is also
what proves the purity suite is capable of failing.

Three layers, in order of how much they actually buy:

1. **`ruff` bans `print()` repo-wide** via the `T20` rule, in `pyproject.toml`. A linter enforces
   this better than a habit does, and it covers the import-time window that the SDK cannot.
2. **`common/logging.py` writes to stderr only**, and *removes* handlers another library may have
   attached — a library that called `basicConfig` first would otherwise keep its own handler.
3. **`assert_stdout_unclaimed()`** runs in `__main__.py` before the transport starts, which is
   the only moment the check can still tell you anything. It warns rather than exits: refusing
   to start would turn a cosmetic dependency banner into an outage.

### 3. One definition of each constraint

`validation.py` holds the constraints as `Annotated` aliases used in two places: as the tool
function's parameter annotations (which is what MCP publishes as `inputSchema`) and as the
fields of `RefundRequest` (which the extension validates against). Two copies of "amount must be
> 0" would eventually disagree, and the copy in the published schema is the one clients trust.

## Assumptions, stated because the brief did not

- **`CUST-XXXXX` means five decimal digits**, uppercase prefix, no surrounding whitespace:
  `^CUST-\d{5}$`. `" CUST-00042 "` is rejected rather than trimmed — silently accepting padded
  identifiers is how you end up with two customers whose ids differ by a space.
- **`reason` is measured after stripping.** `Field(min_length=10)` alone accepts ten spaces. A
  reason is an audit-trail entry a human reads later, so whitespace does not count toward the
  floor. `min_length` is still declared so the published schema advertises it; an
  `AfterValidator` enforces the stricter rule the schema cannot express, and returns the
  stripped value, so the ledger never stores a reason whose leading newlines change how it renders.
- **`NaN` and `Infinity` are rejected explicitly.** JSON has no such literals, but Python's
  `json` module emits and accepts the bare tokens by default, so a hand-rolled client can put
  either on the wire. `gt=0` stops neither: every comparison against NaN is `False`, and
  `+Infinity` really is greater than zero. `allow_inf_nan=False` is what does it.
  `test_nan_and_infinity_on_the_wire_are_rejected` sends the raw bytes, not a Python float.
- **Unknown arguments are rejected** (`extra="forbid"`). A caller sending `custmer_id` has made a
  mistake; accepting the call with a missing field is worse than saying so.
- **Money is `Decimal` in the ledger**, never `float`.

## Production gaps

- **The ledger is a dict.** A real refund is a write to a payment processor and needs an
  **idempotency key**, so a retried `tools/call` — which will happen, agents retry — does not
  refund twice. Today, two identical calls issue two refunds. This is the largest gap.
- **No authorization.** Any client that can spawn the process can refund any customer any amount.
  Refunds want an actor identity, a per-actor ceiling above which a human approves, and an audit
  record of who asked, not just why.
- **`amount` has no currency and no upper bound.** `Decimal(str(amount))` is a reasonable bridge
  from a JSON number, but a real API should take minor units as an integer and a currency code,
  so `0.1 + 0.2` never enters the conversation.
- **The `-32602` choice should probably be revisited with real traffic.** If logs show models
  repeatedly failing on `customer_id` format and having no way to recover, the `ToolError`
  reading of the spec is the better product decision, and the argument above becomes the reason
  to change it.
- **`assert_stdout_unclaimed()` inspects a private `_write_buf` attribute** to detect buffered
  bytes. That is a CPython implementation detail; a supported API would be better, and the check
  degrades to a no-op rather than breaking if it disappears.
