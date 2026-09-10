# Task 4 — Rate-limiting and model fallback router

A routing module for LLM completion requests: a token-aware sliding-window rate limiter backed
by on-disk SQLite, automatic failover from a saturated primary to a secondary, and a single
error boundary that never lets upstream detail reach a client.

```
admit (reserve) ──► primary ──[429 | timeout 3s]──► secondary ──► reconcile
       │                                                              │
       └──────────────── release, on any failure ─────────────────────┘
```

## How to run it

```bash
uv run uvicorn mock_provider.llm:app --port 8100     # terminal 1 — the provider
uv run uvicorn task4_router.app:app --port 8500      # terminal 2 — the router
```

```bash
curl -s localhost:8500/v1/chat/completions \
  -H 'Authorization: Bearer tenant-acme' \
  -d '{"mode":"non_stream","messages":[{"role":"user","content":"hi"}]}' | jq ._gateway
# { "request_id": "...", "served_by": "primary", "attempts": ["primary"], "tokens_charged": 150 }

# Force the primary to 429 and watch the failover. `failing_model` matters: both
# providers point at the same mock here, so without it the secondary would be
# rate limited too and failover could never be seen to succeed.
curl -s localhost:8500/v1/chat/completions -H 'Authorization: Bearer t1' \
  -d '{"mode":"rate_limited","failing_model":"mock-primary","messages":[]}' | jq ._gateway
# { ..., "served_by": "secondary", "attempts": ["primary", "secondary"], ... }

# Primary hangs for 30s behind a 3s budget -- failover, and the wall clock proves it
time curl -s localhost:8500/v1/chat/completions -H 'Authorization: Bearer t2' \
  -d '{"mode":"hang","hang_seconds":30,"failing_model":"mock-primary","messages":[]}' | jq ._gateway.served_by
# "secondary"
# real  0m3.035s

# Fill the window, then get refused
for i in 1 2; do curl -s -o /dev/null -D - -H 'Authorization: Bearer heavy' \
  -d '{"mode":"non_stream","prompt_tokens":45000,"completion_tokens":5000,"messages":[]}' \
  localhost:8500/v1/chat/completions | grep -iE '^HTTP|retry-after'; done
# HTTP/1.1 200 OK
# HTTP/1.1 429 Too Many Requests
# retry-after: 53
```

Configuration: `RATE_LIMIT_DB`, `TOKEN_LIMIT_PER_MINUTE`, `PRIMARY_URL`, `SECONDARY_URL`,
`PRIMARY_TIMEOUT_SECONDS`, `PRIMARY_MODEL`, `SECONDARY_MODEL`.

## How to test it

```bash
uv run pytest tests/test_task4_router.py -v
for i in $(seq 10); do uv run pytest tests/test_task4_router.py -k concurrent -q; done
```

---

## Design decisions

### 1. A sliding window, because a fixed bucket is worse than no limit at all

A per-minute bucket that resets on the clock boundary lets a tenant spend the full limit at
11:59:59 and the full limit again at 12:00:00 — double the intended rate, delivered as a burst,
at exactly the moment your provider quota is least able to absorb it. And it *passes* the
obvious test.

So usage is `(tenant_key, ts, tokens)` rows, and admission sums the trailing 60 seconds.
`test_sliding_window_rejects_a_spend_that_a_fixed_bucket_would_allow` is the discriminating
case: spend 40k, advance 30s, spend 40k — the second must be refused. The clock is **injected**,
not slept through; a test that sleeps 60 seconds is a test that gets marked slow and then
skipped, and a skipped test catches nothing.

Eviction (`DELETE WHERE ts < cutoff`) is global rather than per-tenant, so an idle tenant's rows
do not sit in the table forever waiting for a request that never comes. Index on
`(tenant_key, ts)` makes both the sum and the sweep index range scans.

### 2. `BEGIN IMMEDIATE` — and what it actually buys

The check and the insert are one read-modify-write. Fifty concurrent requests can each read
"45,000 used" and each conclude there is room. Measured, 50 concurrent 5,000-token requests
against a 50,000 limit:

| Approach | Admitted | Notes |
|---|---|---|
| No transaction | **65,000 – 85,000** | The race, plainly. |
| `BEGIN DEFERRED` | 50,000 | …but raises `database is locked`. |
| `BEGIN IMMEDIATE` | **exactly 50,000** | Correct and available. |

The middle row is the one worth understanding, and it is not what I assumed before measuring it.
A deferred transaction that reads and *then* writes cannot be upgraded once another writer has
committed underneath its snapshot — SQLite refuses rather than corrupting it. So correctness is
safe either way; what you lose is **availability**, and `busy_timeout` cannot rescue you because
there is nothing to wait for. `BEGIN IMMEDIATE` takes the write lock *before* the read, so
contenders queue on `busy_timeout` and succeed.

`test_without_a_transaction_the_check_and_insert_race` runs the guarded and unguarded limiters
side by side under the same load, so the guard is demonstrated rather than asserted.

Also: `PRAGMA journal_mode=WAL` so that writer does not block readers, and every SQLite call
goes through `asyncio.to_thread` — the driver is blocking, and blocking the event loop in a
gateway turns one slow query into a global stall.

`synchronous=NORMAL`, not `FULL`: this is a rate-limit ledger, and an fsync per request to
protect against losing the last few milliseconds of it in a power cut is not a good trade. The
failure mode is a tenant briefly getting slightly more quota.

### 3. Reserve-then-reconcile

The amount to charge is not knowable when the decision has to be made — output tokens do not
exist until the response does. Three steps:

1. **Reserve** a cheap estimate (characters ÷ 4, plus `max_tokens`) *before* calling the
   provider. This is what makes concurrent requests see each other's in-flight spend; without
   it, fifty requests could each check an empty window and all fifty be admitted.
2. **Reconcile** the row to the provider's reported usage once the response arrives. The
   estimate is deliberately crude because it is wrong for only a few hundred milliseconds.
3. **Release** the row on any failure path — including cancellation. A reservation that is
   neither reconciled nor released is a slow leak that shrinks the tenant's effective limit
   until the window rolls. `test_a_failed_request_releases_its_reservation` pins this.

**Reconcile can push a tenant past the limit**, and that is the intended trade. The alternatives
are to overshoot by one request, or to fail a request the provider has already been paid for.
`test_actual_usage_above_the_estimate_is_allowed_to_overshoot` documents it and shows the
overshoot being paid back immediately: the next request is refused.

### 4. The primary is not retried

It returned 429 or it timed out. Both mean saturated. Retrying adds load to the thing that is
already failing, and spends the client's remaining latency budget to do it — that is the entire
premise of having a secondary. `test_the_primary_is_never_retried` asserts the primary is
contacted exactly once.

**A 5xx is deliberately *not* a failover trigger.** The brief specifies 429 and timeout, and the
reasoning holds: a server error is as likely to be the request's fault as the provider's, and
sending the same malformed request to a second provider buys a second failure at twice the cost.
`test_a_5xx_does_not_trigger_failover` pins it as a decision rather than an oversight.

**Fallback usage counts against the same tenant budget.** The tenant asked one question and got
one answer; which provider served it is the gateway's business, not a second allowance.
Otherwise failing over becomes a way to double your quota.

The timeout is `asyncio.wait_for` around the whole attempt rather than httpx's own timeouts
alone: httpx splits its budget across connect/read/write phases, so a slow connect plus a slow
read can exceed the single 3,000 ms the caller asked for.
`test_a_timeout_fails_over_at_the_deadline_not_at_the_hang_duration` asserts on elapsed time —
the primary hangs for 5s behind a 0.3s budget, and failover must fire at ~0.3s.
`test_timed_out_primary_does_not_leak_a_connection` asserts the pool is empty afterwards.

### 5. Sanitization is structural, not remembered

Every upstream failure is translated into a `ProviderError` subclass at the edge, in
`providers.py`. Nothing above that module ever sees an `httpx2` exception or an upstream
response body. So the guarantee is checkable by reading two files rather than by auditing every
handler.

The client-facing envelope is fixed:

```json
{"error": {"type": "upstream_unavailable", "message": "The model provider is unavailable. Please retry.", "request_id": "…"}}
```

`type` comes from a **closed** `StrEnum`. Closed because an open set drifts — someone eventually
formats an upstream string into the type field and leaks the thing the module exists to hide.
The `message` is a fixed string per type, deliberately *not* derived from the exception, since
an f-string over an upstream error is exactly how provider names and URLs escape.

The real exception and its traceback are logged server-side under the same `request_id` the
client is given, so an operator can join a user's complaint to the actual failure.
`test_upstream_stack_traces_never_reach_the_client` points the router at a provider whose 500
body contains a fake traceback and asserts that none of `Traceback`, `handler.py`, `shard`,
`RuntimeError` or `/srv/` appears anywhere in the response.

`served_by` in the success payload is this gateway's own label (`primary`/`secondary`), not the
vendor — useful for debugging, and it discloses nothing about who you buy inference from.

## Production gaps

- **Character-count token estimation is crude.** A real tokeniser (`tiktoken`, or the provider's
  count endpoint) would make the reservation accurate. It matters less than it looks, because
  reconcile corrects it within a request — but a badly wrong estimate can refuse a request that
  would have fitted.
- **SQLite is single-node.** This limits one gateway process, not a fleet: run three replicas
  and a tenant gets three times the limit. Distributed enforcement wants Redis with a Lua
  check-and-increment, or a sharded token-bucket service.
- **A connection is opened per operation.** Fine at this scale and simple to reason about, but a
  pool of per-thread connections would cut the syscall cost under load.
- **A crash between reserve and reconcile strands a row** at its estimated value until the
  window rolls. Bounded — 60 seconds — but a sweeper for rows in a `reserved` state older than
  the window would be more honest than relying on the window to hide it.
- **Only one secondary, with no health tracking.** Real routing wants a provider list, a circuit
  breaker so a dead primary is skipped rather than tried-and-timed-out on every request (right
  now every request pays the full 3s while the primary is down), and jittered backoff.
- **The limit is tokens only.** Real quotas are usually tokens *and* requests per minute, often
  with separate input and output pricing, and per-model limits rather than one global pool.
- **No streaming.** This routes non-streaming completions; a streaming router has to reconcile
  usage from the terminal SSE frame and decide what to do when a stream dies halfway.
- **`anonymous` is a shared bucket.** Callers with no `Authorization` header are metered
  together rather than refused, which is the conservative reading of a brief that scopes the
  limit to a tenant API key without specifying authentication. Production should authenticate.
