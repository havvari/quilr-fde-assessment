# FDE Assessment — MCP servers, gateways, and LLM routing

Four independent deliverables in one Python project: an MCP server with strict validation over
stdio, an MCP security gateway proxy, a streaming PII-redaction guardrail, and a rate-limiting
model-fallback router.

| | Task | Where | The interesting part |
|---|---|---|---|
| 1 | MCP server, `trigger_refund` | [`task1_mcp_server/`](task1_mcp_server/README.md) | Malformed input becomes a real `-32602`, which the SDK does *not* do by default |
| 2 | MCP security gateway | [`task2_mcp_gateway/`](task2_mcp_gateway/README.md) | A denied `admin_` call never reaches the downstream server |
| 3 | Streaming PII guardrail | [`task3_stream_guardrail/`](task3_stream_guardrail/README.md) | PII split across chunk boundaries, redacted at +0.16 ms TTFT |
| 4 | Rate limiter + fallback router | [`task4_router/`](task4_router/README.md) | Sliding window, `BEGIN IMMEDIATE`, reserve-then-reconcile |

Each task directory has its own README covering how to run it, how to test it, the design
decisions behind it, and what I would do differently in production.
[`NOTES.md`](NOTES.md) records the ambiguities in the brief and how each was resolved.

## Setup

```bash
uv sync            # Python 3.12, pinned in .python-version and uv.lock
```

`uv` is the only prerequisite (`pip install uv`). If it is not on your `PATH`, the `Makefile`
falls back to `python3 -m uv`.

## Everything, in two commands

```bash
uv run pytest -q                                          # 175 tests
uv run ruff check . && uv run ruff format --check . && uv run mypy .
```

Both must be clean, with no skipped tests.

## Running the pieces

```bash
uv run uvicorn mock_provider.llm:app --port 8100            # fake LLM: 429s, hangs, split PII
uv run uvicorn mock_provider.mcp_downstream:app --port 8200 # fake downstream MCP server
uv run python -m task1_mcp_server                           # MCP server over stdio
uv run uvicorn task2_mcp_gateway.app:app --port 8300        # MCP security gateway
uv run uvicorn task3_stream_guardrail.app:app --port 8400   # streaming PII guardrail
uv run uvicorn task4_router.app:app --port 8500             # rate-limiting router
uv run python -m task3_stream_guardrail.benchmark           # the TTFT numbers in Task 3
```

Task 1 waits silently for a client — that is correct, it is speaking JSON-RPC on stdin/stdout.

A `Makefile` wraps all of the above (`make test`, `make lint`, `make run-task2`, `make bench`,
…). It is a convenience only; every target is a one-line `uv run` of the command above it. It is
**unverified** — `make` is broken on the machine this was built on (the Xcode Command Line Tools
are an x86_64 install on an arm64 Mac, so `/usr/bin/make` cannot start), so the `uv run` forms
above are the ones that were actually executed.

Each task README has verified `curl` invocations — every command printed there was run against
a live server, and two of them found real bugs while being checked.

## Repo map

```
common/            jsonrpc.py · errors.py · logging.py   — shared across tasks
mock_provider/     llm.py · mcp_downstream.py            — controllable failure modes
task1_mcp_server/  validation.py · ledger.py · server.py · __main__.py
task2_mcp_gateway/ config.py · auth.py · policy.py · app.py
task3_stream_guardrail/ redactor.py · sse.py · app.py · benchmark.py
task4_router/      limiter.py · providers.py · router.py · app.py
tests/             one module per task, plus stdio_harness.py and fixtures/
```

## MCP SDK version — read this before writing MCP code

Pinned to **`mcp==2.2.0`**. **v2 is a breaking change from v1, and almost every example online
is still v1.** What changed:

- `FastMCP` → `MCPServer` (`from mcp.server.mcpserver import MCPServer`).
- Wire types moved to the standalone `mcp-types` package, imported as `mcp_types`.
- Fields normalised to snake_case.

`mcp.server.fastmcp` still exists solely to raise `ModuleNotFoundError` with a pointer to the
migration guide, which is a kindness — the bare "No module named" would have given no hint that
the installed SDK is simply a different major version.

Two v2 behaviours shaped the design and are worth knowing before you read Task 1:

1. **`stdio_server()` claims fd 1** — it dup2s the real JSON-RPC wire onto a private descriptor
   and points fd 1 at stderr for the session. A `print()` inside a handler cannot corrupt the
   stream. The window *before* that claim is still exposed.
2. **Argument-validation failures raise `ToolError`, not `-32602`** — the call succeeds carrying
   `isError: true`, on the reasoning that the model chose the arguments and can correct them.
   Task 1 overrides this because the brief asks for JSON-RPC error codes; the argument for and
   against is in its README.

## Notes on the environment

- Python **3.12**. 3.11.0 was the system interpreter and segfaults in `traceback` formatting (a
  known CPython bug fixed in 3.11.1), which turns a failing test into a crashed runner.
- HTTP client is **`httpx2`** (module `httpx2`, distinct from `httpx`). `mcp` 2.x and
  `starlette` 1.6's `TestClient` both use it, so the project has one HTTP client rather than two.
- `ruff` bans `print()` repo-wide via the `T20` rule. Task 1's whole premise is that nothing in
  the import graph may write to stdout, and a linter enforces that better than a habit does.
