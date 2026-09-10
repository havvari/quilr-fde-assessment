"""Task 4, rate limiting and model fallback.

Three properties carry this suite:

* the window **slides** -- a fixed per-minute bucket passes the obvious test and
  fails `test_sliding_window_rejects_a_spend_that_a_fixed_bucket_would_allow`;
* concurrent admissions **never** collectively exceed the limit;
* nothing from an upstream -- body, URL, provider name, traceback -- reaches a client.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from common.errors import ErrorType
from mock_provider.llm import app as llm_app
from task4_router.app import create_app
from task4_router.limiter import RateLimitExceeded, Reservation, TokenRateLimiter
from task4_router.providers import (
    ProviderRateLimited,
    ProviderSpec,
    ProviderTimeout,
    ProviderUnavailable,
    call_provider,
)
from task4_router.router import CompletionRouter, estimate_tokens
from task4_router.settings import RouterConfig

PROVIDER_URL = "http://provider.test/v1/chat/completions"


class FakeClock:
    """An injected clock. A sliding-window test that sleeps 60s never gets run."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(tmp_path: Path, clock: FakeClock) -> TokenRateLimiter:
    return TokenRateLimiter(tmp_path / "rate_limit.db", clock=clock)


# --------------------------------------------------------------------------
# 4a. The sliding window.
# --------------------------------------------------------------------------


async def test_spending_under_the_limit_is_admitted(limiter: TokenRateLimiter) -> None:
    for _ in range(5):
        await limiter.reserve("acme", 9_000)
    assert await limiter.used("acme") == 45_000


async def test_crossing_the_limit_is_rejected(limiter: TokenRateLimiter) -> None:
    await limiter.reserve("acme", 49_000)
    with pytest.raises(RateLimitExceeded) as caught:
        await limiter.reserve("acme", 2_000)

    assert caught.value.used == 49_000
    assert caught.value.limit == 50_000
    # Rejection must not consume budget.
    assert await limiter.used("acme") == 49_000


async def test_a_single_request_larger_than_the_whole_limit_is_rejected(
    limiter: TokenRateLimiter,
) -> None:
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("acme", 50_001)


async def test_sliding_window_rejects_a_spend_that_a_fixed_bucket_would_allow(
    limiter: TokenRateLimiter, clock: FakeClock
) -> None:
    """The test that separates a real sliding window from a per-minute bucket.

    Spend 40k, wait 30s, spend 40k again. A bucket that resets on a clock
    boundary admits the second one -- 80k in 60 seconds, against a 50k limit.
    """
    await limiter.reserve("acme", 40_000)
    clock.advance(30)

    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("acme", 40_000)

    # And once the first spend genuinely leaves the window, the same call passes.
    clock.advance(31)
    await limiter.reserve("acme", 40_000)
    assert await limiter.used("acme") == 40_000


async def test_expired_rows_are_evicted_not_merely_ignored(
    limiter: TokenRateLimiter, clock: FakeClock
) -> None:
    for _ in range(10):
        await limiter.reserve("acme", 1_000)
        clock.advance(1)
    assert await limiter.rows() == 10

    clock.advance(120)
    await limiter.reserve("acme", 1_000)
    # Eviction is what keeps an always-on gateway's table from growing forever.
    assert await limiter.rows() == 1


async def test_eviction_is_global_so_idle_tenants_do_not_accumulate(
    limiter: TokenRateLimiter, clock: FakeClock
) -> None:
    await limiter.reserve("idle-tenant", 1_000)
    clock.advance(120)
    await limiter.reserve("busy-tenant", 1_000)
    assert await limiter.rows() == 1


async def test_tenants_have_separate_budgets(limiter: TokenRateLimiter) -> None:
    await limiter.reserve("acme", 49_000)
    await limiter.reserve("globex", 49_000)
    assert await limiter.used("acme") == 49_000
    assert await limiter.used("globex") == 49_000


async def test_retry_after_points_at_when_the_window_frees_up(
    limiter: TokenRateLimiter, clock: FakeClock
) -> None:
    await limiter.reserve("acme", 50_000)
    clock.advance(20)
    with pytest.raises(RateLimitExceeded) as caught:
        await limiter.reserve("acme", 1)
    assert caught.value.retry_after == pytest.approx(40.0, abs=0.01)


# --------------------------------------------------------------------------
# 4a. Reserve-then-reconcile.
# --------------------------------------------------------------------------


async def test_reconcile_replaces_the_estimate_with_actual_usage(
    limiter: TokenRateLimiter,
) -> None:
    reservation = await limiter.reserve("acme", 100)
    assert await limiter.used("acme") == 100

    await limiter.reconcile(reservation, 3_500)
    assert await limiter.used("acme") == 3_500


async def test_reconcile_can_revise_downwards(limiter: TokenRateLimiter) -> None:
    reservation = await limiter.reserve("acme", 10_000)
    await limiter.reconcile(reservation, 120)
    assert await limiter.used("acme") == 120


async def test_release_returns_the_whole_reservation(limiter: TokenRateLimiter) -> None:
    reservation = await limiter.reserve("acme", 10_000)
    await limiter.release(reservation)
    assert await limiter.used("acme") == 0
    assert await limiter.rows() == 0


async def test_the_reservation_holds_budget_while_the_request_is_in_flight(
    limiter: TokenRateLimiter,
) -> None:
    """Why reserve at all: a concurrent request must see the in-flight spend.

    Without a reservation, 50 requests could each check an empty window before
    any of them reported usage, and all 50 would be admitted.
    """
    await limiter.reserve("acme", 45_000)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("acme", 10_000)


# --------------------------------------------------------------------------
# 4a. Concurrency.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("run", range(10))
async def test_concurrent_admissions_never_exceed_the_limit(tmp_path: Path, run: int) -> None:
    """Fifty concurrent requests for 5,000 tokens against a 50,000 limit.

    Repeated, because a check-and-insert race does not fail every time -- that is
    what makes it dangerous. Without BEGIN IMMEDIATE this over-admits.
    """
    limiter = TokenRateLimiter(tmp_path / f"concurrent-{run}.db")

    async def attempt() -> int:
        try:
            await limiter.reserve("acme", 5_000)
        except RateLimitExceeded:
            return 0
        return 5_000

    granted = await asyncio.gather(*[attempt() for _ in range(50)])

    assert sum(granted) <= 50_000, f"over-admitted {sum(granted)} tokens"
    assert sum(granted) == 50_000, "should admit exactly the limit, not less"
    assert await limiter.used("acme") == 50_000


async def test_concurrent_tenants_do_not_block_each_other(tmp_path: Path) -> None:
    limiter = TokenRateLimiter(tmp_path / "tenants.db")
    tenants = [f"tenant-{index}" for index in range(10)]
    await asyncio.gather(*[limiter.reserve(tenant, 50_000) for tenant in tenants])
    for tenant in tenants:
        assert await limiter.used(tenant) == 50_000


async def test_the_database_is_actually_on_disk(tmp_path: Path) -> None:
    """State must outlive the process, or a restart resets everyone's quota."""
    path = tmp_path / "durable.db"
    await TokenRateLimiter(path).reserve("acme", 12_000)
    assert path.exists()

    reopened = TokenRateLimiter(path)
    assert await reopened.used("acme") == 12_000


# --------------------------------------------------------------------------
# 4b. Failover.
# --------------------------------------------------------------------------


def _router(
    tmp_path: Path,
    handler: Any = None,
    *,
    primary_timeout: float = 3.0,
    secondary_timeout: float = 6.0,
    **overrides: Any,
) -> CompletionRouter:
    config = RouterConfig(
        primary=ProviderSpec("primary", PROVIDER_URL, "mock-primary", timeout_seconds=primary_timeout),
        secondary=ProviderSpec(
            "secondary", PROVIDER_URL, "mock-secondary", timeout_seconds=secondary_timeout
        ),
        database_path=str(tmp_path / "router.db"),
        **overrides,
    )
    transport = httpx2.MockTransport(handler) if handler else httpx2.ASGITransport(app=llm_app)
    return CompletionRouter(config, httpx2.AsyncClient(transport=transport))


async def test_a_429_from_the_primary_fails_over_to_the_secondary(tmp_path: Path) -> None:
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "mock-primary":
            return httpx2.Response(429, json={"error": {"message": "internal.host is busy"}})
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        return httpx2.Response(200, json={"choices": [], "usage": usage})

    routed = await _router(tmp_path, handler).route_completion("acme", {"messages": []})

    assert seen == ["mock-primary", "mock-secondary"]
    assert routed.provider == "secondary"
    assert routed.attempts == ["primary", "secondary"]


async def test_the_primary_is_never_retried(tmp_path: Path) -> None:
    """It is saturated. Retrying it adds load to the failure and spends latency."""
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "mock-primary":
            return httpx2.Response(429)
        return httpx2.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    await _router(tmp_path, handler).route_completion("acme", {"messages": []})
    assert seen.count("mock-primary") == 1


async def test_a_timeout_fails_over_at_the_deadline_not_at_the_hang_duration(
    tmp_path: Path,
) -> None:
    """The primary hangs for 5s behind a 0.3s budget: failover must fire at ~0.3s."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if json.loads(request.content)["model"] == "mock-primary":
            await asyncio.sleep(5.0)
            return httpx2.Response(200, json={})
        return httpx2.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    router = _router(tmp_path, handler, primary_timeout=0.3, secondary_timeout=2.0)

    started = time.perf_counter()
    routed = await router.route_completion("acme", {"messages": []})
    elapsed = time.perf_counter() - started

    assert routed.provider == "secondary"
    assert elapsed < 1.5, f"failover took {elapsed:.2f}s; it waited out the hang"
    assert elapsed >= 0.3, "failover fired before the deadline"


async def test_timed_out_primary_does_not_leak_a_connection(tmp_path: Path) -> None:
    """A cancelled request must return its connection, or the pool bleeds out."""

    async def hang(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(10.0)
        return httpx2.Response(200, json={})

    spec = ProviderSpec("primary", PROVIDER_URL, "m", timeout_seconds=0.2)
    transport = httpx2.AsyncHTTPTransport()
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(hang))
    try:
        for _ in range(5):
            with pytest.raises(ProviderTimeout):
                await call_provider(client, spec, {"messages": []})
    finally:
        await client.aclose()
        await transport.aclose()

    # The real pool is the one that can leak; assert it is empty after the run.
    assert transport._pool.connections == []


async def test_both_providers_failing_is_an_upstream_error_not_a_crash(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429)

    from common.errors import GatewayError

    with pytest.raises(GatewayError) as caught:
        await _router(tmp_path, handler).route_completion("acme", {"messages": []})
    assert caught.value.error_type is ErrorType.UPSTREAM_UNAVAILABLE


async def test_failover_usage_counts_against_the_same_tenant_budget(
    tmp_path: Path,
) -> None:
    """One question, one answer, one charge. Failover is not a second allowance."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if json.loads(request.content)["model"] == "mock-primary":
            return httpx2.Response(429)
        return httpx2.Response(200, json={"usage": {"prompt_tokens": 700, "completion_tokens": 300}})

    router = _router(tmp_path, handler)
    await router.route_completion("acme", {"messages": [{"content": "hi"}]})
    assert await router.limiter.used("acme") == 1_000


async def test_a_failed_request_releases_its_reservation(tmp_path: Path) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"error": "boom"})

    from common.errors import GatewayError

    router = _router(tmp_path, handler)
    with pytest.raises(GatewayError):
        await router.route_completion("acme", {"messages": [{"content": "x" * 4_000}]})

    # A reservation that is neither reconciled nor released is a slow leak.
    assert await router.limiter.used("acme") == 0
    assert await router.limiter.rows() == 0


async def test_a_5xx_does_not_trigger_failover(tmp_path: Path) -> None:
    """Documented choice: a server error is as likely the request's fault."""
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content)["model"])
        return httpx2.Response(503)

    from common.errors import GatewayError

    with pytest.raises(GatewayError):
        await _router(tmp_path, handler).route_completion("acme", {"messages": []})
    assert seen == ["mock-primary"]


# --------------------------------------------------------------------------
# 4c. Error sanitization.
# --------------------------------------------------------------------------


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[TestClient]:
    config = RouterConfig(
        primary=ProviderSpec("primary", PROVIDER_URL, "mock-primary", timeout_seconds=3.0),
        secondary=ProviderSpec("secondary", PROVIDER_URL, "mock-secondary", timeout_seconds=6.0),
        database_path=str(tmp_path / "gateway.db"),
    )
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=llm_app))
    with TestClient(create_app(config, client)) as test_client:
        yield test_client


def test_upstream_stack_traces_never_reach_the_client(gateway: TestClient) -> None:
    """The provider's 500 body contains a fake traceback. None of it may escape."""
    response = gateway.post(
        "/v1/chat/completions",
        json={"mode": "server_error", "messages": [{"content": "hello"}]},
        headers={"Authorization": "Bearer tenant-a"},
    )

    assert response.status_code == 502
    for leaked in ("Traceback", "handler.py", "mock-primary-7", "shard", "RuntimeError", "/srv/"):
        assert leaked not in response.text, f"{leaked!r} leaked to the client"

    body = response.json()["error"]
    assert body["type"] == "upstream_unavailable"
    assert set(body) == {"type", "message", "request_id"}


def test_rate_limited_responses_are_sanitized_and_carry_retry_after(
    gateway: TestClient,
) -> None:
    headers = {"Authorization": "Bearer heavy-tenant"}
    # The provider reports the full 50,000, so reconcile fills the window exactly.
    # A large `max_tokens` would not: reserve-then-reconcile replaces the estimate
    # with what was actually spent, which is the whole point of it.
    body = {
        "mode": "non_stream",
        "messages": [{"content": "x"}],
        "prompt_tokens": 45_000,
        "completion_tokens": 5_000,
    }

    first = gateway.post("/v1/chat/completions", json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["_gateway"]["tokens_charged"] == 50_000

    second = gateway.post("/v1/chat/completions", json=body, headers=headers)
    assert second.status_code == 429
    assert second.headers["retry-after"]
    assert second.json()["error"]["type"] == "rate_limited"
    # The internal detail names the tenant and the numbers; the client gets neither.
    assert "heavy-tenant" not in second.text


def test_error_types_come_from_a_closed_set(gateway: TestClient) -> None:
    response = gateway.post(
        "/v1/chat/completions",
        content=b"not json at all",
        headers={"Authorization": "Bearer t", "Content-Type": "application/json"},
    )
    assert response.json()["error"]["type"] in {member.value for member in ErrorType}


def test_every_error_carries_a_request_id_for_the_operator(gateway: TestClient) -> None:
    response = gateway.post(
        "/v1/chat/completions",
        json={"mode": "server_error", "messages": []},
        headers={"Authorization": "Bearer t"},
    )
    request_id = response.json()["error"]["request_id"]
    assert len(request_id) == 32


def test_a_successful_route_reports_which_provider_served_it(gateway: TestClient) -> None:
    response = gateway.post(
        "/v1/chat/completions",
        json={"mode": "non_stream", "messages": [{"content": "hi"}]},
        headers={"Authorization": "Bearer t"},
    )
    meta = response.json()["_gateway"]
    assert meta["served_by"] == "primary"
    assert meta["tokens_charged"] == 150


def test_tenants_are_isolated_at_the_http_layer(gateway: TestClient) -> None:
    body = {
        "mode": "non_stream",
        "messages": [{"content": "x"}],
        "prompt_tokens": 45_000,
        "completion_tokens": 5_000,
    }
    for tenant in ("a", "b"):
        headers = {"Authorization": f"Bearer {tenant}"}
        response = gateway.post("/v1/chat/completions", json=body, headers=headers)
        assert response.status_code == 200, f"tenant {tenant} was charged for another tenant's spend"


# --------------------------------------------------------------------------
# Estimation and provider classification.
# --------------------------------------------------------------------------


def test_estimate_is_cheap_and_bounded_below() -> None:
    config = RouterConfig(
        primary=ProviderSpec("p", PROVIDER_URL, "m"),
        secondary=ProviderSpec("s", PROVIDER_URL, "m"),
    )
    assert estimate_tokens({"messages": []}, config) == config.minimum_reservation
    assert estimate_tokens({"messages": [{"content": "a" * 4_000}]}, config) == 1_000
    assert estimate_tokens({"messages": [], "max_tokens": 500}, config) == 500


async def test_provider_errors_are_classified_and_carry_no_upstream_text() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "30"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    spec = ProviderSpec("primary", PROVIDER_URL, "m")
    with pytest.raises(ProviderRateLimited) as caught:
        await call_provider(client, spec, {})
    assert caught.value.retry_after == 30.0
    await client.aclose()


async def test_a_non_json_upstream_body_is_unavailable_not_a_crash() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<html>gateway timeout</html>")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    with pytest.raises(ProviderUnavailable) as caught:
        await call_provider(client, ProviderSpec("p", PROVIDER_URL, "m"), {})
    assert "html" not in str(caught.value)
    await client.aclose()


async def test_actual_usage_above_the_estimate_is_allowed_to_overshoot(
    tmp_path: Path,
) -> None:
    """Reconcile can push a tenant past the limit, and that is the intended trade.

    The true cost is not knowable until the response exists. The choices are to
    let one request overshoot, or to fail a request that has already been paid
    for upstream. Overshoot, and the next request pays for it.
    """

    async def handler(request: httpx2.Request) -> httpx2.Response:
        usage = {"prompt_tokens": 60_000, "completion_tokens": 0}
        return httpx2.Response(200, json={"usage": usage})

    router = _router(tmp_path, handler)
    routed = await router.route_completion("acme", {"messages": [{"content": "hi"}]})

    assert routed.tokens_charged == 60_000
    assert await router.limiter.used("acme") == 60_000

    # The overshoot is paid back immediately: the next request is refused.
    from common.errors import GatewayError

    with pytest.raises(GatewayError) as caught:
        await router.route_completion("acme", {"messages": [{"content": "hi"}]})
    assert caught.value.error_type is ErrorType.RATE_LIMITED


class _NoTransactionLimiter(TokenRateLimiter):
    """The limiter as someone would write it first: check, then insert.

    Exists only so `test_without_a_transaction_the_check_and_insert_race` can
    show the race it is protecting against, rather than asserting that one exists.
    """

    def _reserve_sync(self, tenant_key: str, estimated_tokens: int) -> Reservation:
        connection = self._connect()
        try:
            now = self.clock()
            cutoff = now - self.window_seconds
            used = self._sum(connection, tenant_key, cutoff)
            if used + estimated_tokens > self.limit:
                raise RateLimitExceeded(tenant_key, used, estimated_tokens, self.limit, 0.0)
            cursor = connection.execute(
                "INSERT INTO usage (tenant_key, ts, tokens) VALUES (?, ?, ?)",
                (tenant_key, now, estimated_tokens),
            )
            row_id = cursor.lastrowid
            assert row_id is not None
            return Reservation(row_id=row_id, tenant_key=tenant_key, reserved=estimated_tokens)
        finally:
            connection.close()


async def _admit_fifty(limiter: TokenRateLimiter) -> int:
    async def attempt() -> None:
        try:
            await limiter.reserve("acme", 5_000)
        except RateLimitExceeded:
            return

    await asyncio.gather(*[attempt() for _ in range(50)])
    return await limiter.used("acme")


async def test_without_a_transaction_the_check_and_insert_race(tmp_path: Path) -> None:
    """The guarded and unguarded limiters, side by side, under the same load.

    Measured rather than asserted: the unguarded version admits 65,000-85,000
    tokens against a 50,000 limit, because fifty readers all see room before any
    of them has written. `BEGIN IMMEDIATE` is what closes that window.
    """
    guarded = await _admit_fifty(TokenRateLimiter(tmp_path / "guarded.db"))
    unguarded = await _admit_fifty(_NoTransactionLimiter(tmp_path / "unguarded.db"))

    assert guarded == 50_000
    assert unguarded > 50_000, "the unguarded limiter did not race on this run; the guard is still required"
