# Notes — ambiguities in the brief, and how each was resolved

The rule I followed: implement the **literal** reading, then document the alternative and say
which I would ship. Where a requirement and the SDK's own opinion disagreed, I did what the
brief said and explained the disagreement rather than quietly siding with the SDK.

---

## Task 1

**"Reject invalid formats with standard MCP JSON-RPC error codes."**
The MCP Python SDK deliberately does the opposite. `mcp/server/mcpserver/tools/base.py` catches
pydantic's `ValidationError` and raises `ToolError`, so the call *succeeds* carrying
`isError: true`. Its comment: *"the model's mistake to read and correct."*

Resolved literally: a `StrictParamsExtension` validates ahead of the tool and raises
`MCPError(INVALID_PARAMS)`, producing a genuine `-32602`. The counter-argument — that a model
which invented a bad `customer_id` can only recover from a failure it can *see*, and a `-32602`
is invisible to it — is in `task1_mcp_server/README.md`, along with the deciding question I
would use in production: *could a smarter model have avoided this?*

**`CUST-XXXXX` is never defined.** Assumed five decimal digits, uppercase prefix, no surrounding
whitespace: `^CUST-\d{5}$`. `" CUST-00042 "` is rejected rather than trimmed — silently
accepting padded identifiers is how you get two customers whose ids differ by a space.

**"reason string with minimum length of 10" — ten of what?**
`Field(min_length=10)` alone accepts ten spaces. Since the reason is an audit-trail entry a
human reads later, the length is measured **after stripping**, and the stripped value is what
gets stored. `min_length` is still declared so the published `inputSchema` advertises the floor;
an `AfterValidator` enforces the stricter rule the schema cannot express.

**"amount positive float" — is `Infinity` positive?**
Arguably. JSON has no `NaN`/`Infinity` literals, but Python's `json` module emits and accepts
the bare tokens, so a hand-rolled client can put either on the wire — and `gt=0` stops neither
(comparisons against NaN are all `False`; `+Infinity` really is greater than zero). Both are
rejected via `allow_inf_nan=False`, and the test sends the raw bytes rather than a Python float.

**Unknown arguments.** Not mentioned. Rejected (`extra="forbid"`): a caller sending `custmer_id`
has made a mistake, and accepting the call with a missing field is the worse outcome.

**"Ensure stdout is strictly reserved for JSON-RPC."**
Partly already true: SDK v2's `stdio_server()` repoints fd 1 at stderr for the session, so a
`print()` inside a handler is harmless. Rather than claim credit for that, the task targets the
gap it leaves — import-time writes, before the claim — and asserts *both* behaviours. Stating
which guarantee comes from where seemed more useful than a suite that passes without saying why.

**Scope.** The brief names only `trigger_refund`, so only `trigger_refund` is implemented. The
`ToolError`-vs-`MCPError` contrast is demonstrated through it alone: an unknown-but-well-formed
customer, and a refund exceeding the remaining balance.

---

## Task 2

**Node.js or Python.** Python, to share `common/` with the other three tasks.

**"return a JSON-RPC Error (-32001) ... If not authorized"** — but what about *unauthenticated*?
Not specified. Split deliberately: a missing, malformed or unknown token gets **HTTP 401** with
`WWW-Authenticate`, because there is no authenticated session in which a JSON-RPC error would
mean anything, and MCP's own authorization spec says so. An authenticated caller lacking the
role gets **HTTP 200 + `-32001`**. Returning 403 for the latter would let a client's HTTP layer
swallow the response before the JSON-RPC layer sees the `id` it needs to correlate.

**Batch requests.** Not mentioned. Rejected explicitly with `-32600`. A batch mixes
authorization outcomes inside one response array, which is easy to get subtly wrong, and MCP's
2025-06-18 revision removed JSON-RPC batching from the spec anyway.

**Notifications.** Not mentioned. A message with no `id` gets HTTP 204 and no body, per spec —
*including when it is refused*. `{"id": null}` is a request, not a notification, and does get a
response. There is a test named `silence is not permission` for the first case.

**A `tools/call` that names no tool.** Absent params, array params, missing or non-string
`name`. Denied rather than forwarded: if we cannot classify it, we do not pass it on.

**Forwarding the client's token.** Not mentioned, and the omission is the trap. The gateway
presents its own downstream credential. Forwarding the client's is the confused-deputy pattern —
the downstream would trust a token it did not issue and cannot revoke, making every
authorization decision here advisory.

**Tool-list filtering.** The brief says forward `tools/list` transparently, so that is the
default. The filter ships behind `FILTER_TOOL_LIST` because a tool the model can see is a tool
it will try, and a transparent list leaks the admin surface area to anything that can read the
model's context. In production I would default it **on**.

---

## Task 3

**Which patterns.** "Emails, SSNs, or credit card numbers" — all three, US SSN format. A bare
nine-digit run is *not* treated as an SSN: it collides with order numbers, and this is a text
stream, not a form. Deliberate non-matches are asserted: `1234567`, `90210-1234`, `555-1234`,
`1.2.3`.

**How large should the hold be?** Not specified, and it is the whole design. Derived from the
patterns (`max_pattern_length()`), which is why every quantifier in the regex is bounded — an
unbounded `+` means there is no safe boundary and no correct hold size. The worst case is 409
characters, which would be a poor TTFT, so the hold is a **cap** and a run-scan releases text
far sooner in practice: measured overhead is +0.16 ms.

**False positives vs false negatives.** Not addressed by the brief. A Luhn checksum gate is
implemented and **off by default**: for a guardrail a false positive costs a redacted order
number while a false negative leaks a card, so the default over-redacts.

**What if the stream ends mid-pattern?** Not addressed. Held text is flushed and delivered.
Dropping it would be worse than not redacting it, because the client loses text and nothing
says so.

**What about a `data:` frame that will not parse?** The stream is failed, not forwarded. Text we
cannot inspect is text we cannot redact.

**Provider.** Mock only, so the boundary-splitting fixture is deterministic. A real provider
cannot be asked to split a card number at byte 27 on demand.

---

## Task 4

**"50,000 tokens/minute"** — a rolling window, not a calendar minute. Read as sliding because a
boundary-resetting bucket permits double the intended rate across the boundary *and* passes the
naive test.

**Which tokens count?** Input and output both, charged to the tenant that asked. Since output
tokens do not exist until the response does, the design is reserve-then-reconcile.

**What if actual usage exceeds the reservation?** Not addressed. The overshoot is allowed and
the next request pays for it — the alternative is failing a request the provider has already
been paid for. There is a test asserting the overshoot and the immediate repayment.

**"429 ... or times out after 3000ms".** Implemented exactly, and no more: a **5xx does not
trigger failover**, because a server error is as likely to be the request's fault as the
provider's, and sending the same malformed request to a second provider buys a second failure at
twice the cost. Pinned by a test so it reads as a decision rather than an oversight.

**Does fallback usage count against the tenant?** Not stated. Yes — one question, one answer,
one charge. Otherwise failing over becomes a way to double your quota.

**Authentication.** The brief scopes the limit to "tenant API key" without specifying auth. An
unauthenticated caller is metered under a shared `anonymous` bucket rather than refused, which
is the conservative reading. Production should authenticate.

---

## Things I got wrong first, and corrected

Recorded because the corrections are the useful part.

- **`BEGIN IMMEDIATE` does not prevent over-admission.** I assumed it did. Measured: SQLite's
  snapshot isolation protects correctness even with `BEGIN DEFERRED` — a deferred read-then-write
  transaction that loses the race is refused with `database is locked` rather than committing a
  stale decision. What `IMMEDIATE` actually buys is **availability**: it takes the write lock
  before the read, so contenders queue on `busy_timeout` instead of failing. The genuine race
  needs no transaction at all, and then admits 65,000–85,000 against a 50,000 limit. A test now
  runs the guarded and unguarded versions side by side.
- **`(?![\w.-])` after an email TLD** silently failed to redact every address that ends a
  sentence, because the following `.` failed the lookahead. Found by printing sample output
  rather than by a test — which is why the pattern table in the suite now includes
  `"mail ada@example.com."` explicitly.
- **A groups-of-four credit card pattern misses Amex entirely** (4-6-5). Rewritten as "four
  digits then 9–15 more".
- **The mock provider applied its failure mode to both providers**, so the documented "primary
  is rate limited" demo rate-limited the secondary too and failover could never be observed to
  succeed. Found by running the README's own `curl` commands instead of trusting them. Fixed
  with a `failing_model` field.
- **The stdio test harness closed stdin after writing every frame**, which raced the server's
  shutdown: a synchronous tool body runs in a worker thread, and on EOF the session's task group
  cancelled it after the reply had been computed. Rewritten to hold stdin open until the last
  response arrives.
- **That harness then leaked the subprocess's pipes.** Caught only because `filterwarnings =
  ["error"]` turns `ResourceWarning` into a failure — one leaked descriptor per session is a
  leak in a long-lived proxy too.
