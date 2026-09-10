# Task 2 — MCP security gateway proxy

An HTTP/JSON-RPC reverse proxy between an agent client and a downstream MCP server. It
authenticates the caller from a `Bearer` token, and refuses `admin_`-prefixed tool calls from
non-admins **before** the request reaches the downstream server.

## How to run it

```bash
make run-mock-downstream                       # terminal 1 — the downstream MCP server, :8200
make run-task2                                 # terminal 2 — the gateway, :8300
```

```bash
# viewer calling an admin tool -> -32001, and the downstream never sees it
curl -s localhost:8300/mcp -H 'Authorization: Bearer tok_viewer' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"tenant":"acme"}}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call", ...}}

# the same call as admin -> forwarded
curl -s localhost:8300/mcp -H 'Authorization: Bearer tok_admin' -d '{...}'

# no credential -> HTTP 401, not a JSON-RPC error
curl -si localhost:8300/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -1
```

Configuration: `DOWNSTREAM_MCP_URL`, `DOWNSTREAM_MCP_TOKEN`, `FILTER_TOOL_LIST`,
`GATEWAY_TIMEOUT_SECONDS`.

## How to test it

```bash
uv run pytest tests/test_task2_gateway.py -v
```

Tests wire the gateway to the mock downstream through `httpx2.ASGITransport`, so the whole
proxy path runs in-process with no sockets and no ports to collide on.

---

## Design decisions

### 1. The assertion that matters is "downstream was never called"

A gateway that forwards a privileged call and then discards the response has already leaked the
call — the side effect happened. So the tests do not assert on the error body alone; the mock
downstream keeps a `CALL_LOG`, and the central test asserts `CALL_LOG.count == 0`.

That is also why authorization is a **pure function** (`policy.py`) over an already-parsed
payload, with no HTTP and no network in it. `decide()` returns `Allow` or `Deny`; only `Allow`
reaches the forwarding code. The structure makes "did not forward" a property of the return
value, not of a mock's call history.

### 2. HTTP 401 for authentication; JSON-RPC `-32001` for authorization

These are different failures and they get different answers:

| Situation | Response |
|---|---|
| No token, wrong scheme, unknown token | **HTTP 401** + `WWW-Authenticate: Bearer` |
| Valid token, insufficient role for this tool | **HTTP 200** + `error.code = -32001` |

A JSON-RPC error is an answer *within* a session. If the credential is missing or unrecognised
there is no session, nothing has been authenticated, and a 200 carrying a JSON-RPC error would
tell an HTTP client that its request succeeded. MCP's own authorization spec says to answer 401
with `WWW-Authenticate` for exactly this case.

Once authenticated, everything else rides on **HTTP 200 with an `error` body**, per JSON-RPC.
Returning 403 for a tool-level denial would mean a client's HTTP layer swallows the response
before the JSON-RPC layer ever sees the id it needs to correlate.

### 3. The client's bearer token is not forwarded downstream

The gateway presents its **own** credential (`DOWNSTREAM_MCP_TOKEN`). Passing the client's token
through is the confused-deputy pattern that MCP's security guidance names explicitly: the
downstream would be trusting a token it did not issue, cannot scope, and cannot revoke, and
every authorization decision the gateway makes becomes advisory — anyone who reaches the
downstream directly with the same token bypasses it entirely.
`test_client_bearer_token_is_not_forwarded_downstream` asserts on what the downstream actually
received.

### 4. Details that are easy to get wrong

- **The error echoes the original `id`.** A client correlates on it; an error that drops the id
  is an error the client cannot attribute to a request. Asserted explicitly.
- **A notification is a message with no `id` member at all** — not one whose id is `null`.
  `{"id": null}` is a request and gets a response; an absent `id` gets HTTP 204 and no body,
  even when the call was *refused*. The refusal still happened — it is in the log and the
  downstream was not contacted — but inventing a response body would desynchronise a client that
  is not expecting one. `test_denied_notification_gets_no_body_but_is_still_blocked` covers this:
  silence is not permission.
- **Batches are rejected with `-32600`, not supported.** Two reasons. A batch mixes authorization
  outcomes inside one response array, so a partial denial is easy to get subtly wrong. And MCP's
  2025-06-18 revision removed JSON-RPC batching from the spec, so supporting it here would be
  building for a wire format the protocol has dropped. Rejecting explicitly beats crashing.
- **A `tools/call` the gateway cannot classify is denied, not forwarded.** Absent params, array
  params, a missing or non-string `name` — if we cannot tell whether the tool is privileged, we
  do not forward it. Fail closed.
- **Non-`tools/call` methods forward untouched**, including ones this gateway has never heard of.
  It is an authorization layer, not a method allowlist; a new MCP method should not require a
  gateway release.
- **Responses are relayed byte-for-byte** rather than re-serialised, so a field the gateway does
  not model survives the round trip. The one exception is the tool-list filter below, which has
  to rebuild the body.
- **A downstream connection failure returns `-32603` with a fixed message.** The exception's text
  contains the internal URL, so only its class name is logged and none of it reaches the client.

### 5. Tool-list filtering is opt-in, behind `FILTER_TOOL_LIST`

The brief says forward `tools/list` transparently, so that is the default and the behaviour the
tests assert. But transparent forwarding means a viewer's model *sees* `admin_reset_key` in its
context, and a tool a model can see is a tool the model will try — it burns a turn discovering
what the gateway already knows, and you have handed the model, and anything that can read its
context, a map of your admin surface area.

With the flag on, `visible_tools()` strips `admin_`-prefixed entries from a non-admin's
`tools/list` **response**. It is defence in depth, not the control: the call-time check still
runs, which `test_filter_does_not_change_call_authorization` asserts. Hiding a tool from a
cooperative model does nothing against a hostile one.

I would default this **on** in production and treat the transparent mode as the compatibility
option — but the brief's literal reading is the default here.

## Production gaps

- **Static token map.** `{"tok_admin": "admin"}` is a fixture. Production wants a signed JWT with
  signature, `exp`, `aud` and `iss` all verified, or an introspection endpoint — plus key
  rotation. `auth.py` is the only file that changes.
- **The `admin_` prefix is a weak contract.** It is case-sensitive and literal, so a downstream
  tool named `Admin_reset` is not gated — `test_policy_decisions` pins that as known behaviour
  rather than pretending otherwise. Worse, the *downstream* controls tool names, so it can
  rename a privileged tool out of the gated namespace. Real authorization should key on tool
  metadata (an annotation, a registry) that the gateway or a trusted registry controls, not on a
  string the thing being guarded chose for itself.
- **Roles are flat.** Two roles and one prefix does not survive contact with real tenancy —
  per-tenant scoping, per-tool grants, and "admin of *this* tenant" all need a real policy engine.
- **No audit trail.** Denials are logged as text. Security decisions want a structured, tamper-
  evident record: actor, tool, arguments hash, decision, timestamp.
- **No rate limiting or request size cap**, so the gateway will happily parse a 500 MB body.
- **Arguments are never inspected.** The gateway authorizes the *tool*, not what is being asked
  of it. `get_order_status(order_id="…")` for someone else's order is authorized today.
- **No streaming or SSE support** — this proxies single request/response JSON-RPC over POST only.
