"""Talking to model providers, and classifying the ways that goes wrong.

Every upstream failure is translated into one of a handful of exception types
here, at the edge. Nothing above this module ever sees an `httpx2` exception or
an upstream response body -- which is what makes the sanitization guarantee in
`common/errors.py` structural rather than a matter of remembering.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx2

from common.logging import get_logger

logger = get_logger(__name__)

# The brief's failover trigger: HTTP 429, or a timeout. Deliberately not 5xx --
# see the router's docstring for why a server error is not retried elsewhere.
FAILOVER_STATUSES = frozenset({429})


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    url: str
    model: str
    timeout_seconds: float = 3.0


@dataclass
class ProviderResult:
    provider: str
    model: str
    payload: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ProviderError(Exception):
    """Base for every upstream failure. Never carries an upstream body."""

    def __init__(self, provider: str, detail: str) -> None:
        self.provider = provider
        self.detail = detail
        super().__init__(f"{provider}: {detail}")


class ProviderRateLimited(ProviderError):
    """HTTP 429. The premise of failover."""

    def __init__(self, provider: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(provider, "rate limited")


class ProviderTimeout(ProviderError):
    def __init__(self, provider: str, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(provider, f"no response within {seconds}s")


class ProviderUnavailable(ProviderError):
    """Anything else: connection refused, 5xx, a body that will not parse."""


@dataclass
class CallLog:
    """Which providers were tried, in order. Used by tests and by the audit log."""

    attempts: list[str] = field(default_factory=list)

    def record(self, name: str) -> None:
        self.attempts.append(name)


async def call_provider(
    client: httpx2.AsyncClient,
    spec: ProviderSpec,
    body: dict[str, Any],
) -> ProviderResult:
    """One attempt against one provider, with its own timeout.

    `asyncio.wait_for` wraps the whole request rather than relying on httpx's
    own timeouts alone: the brief specifies a single 3000 ms budget for the
    attempt, and httpx splits its budget across connect/read/write phases, so a
    slow connect plus a slow read can exceed the total the caller asked for.
    """
    payload = {**body, "model": spec.model}
    try:
        response = await asyncio.wait_for(
            client.post(spec.url, json=payload),
            timeout=spec.timeout_seconds,
        )
    except TimeoutError as exc:
        # asyncio.wait_for cancels the inner task, which unwinds httpx's own
        # context managers and returns the connection to the pool. Asserted by
        # test_timed_out_primary_does_not_leak_a_connection.
        raise ProviderTimeout(spec.name, spec.timeout_seconds) from exc
    except httpx2.HTTPError as exc:
        # Only the class name: the message can contain the provider URL.
        raise ProviderUnavailable(spec.name, type(exc).__name__) from exc

    if response.status_code in FAILOVER_STATUSES:
        raise ProviderRateLimited(spec.name, _retry_after(response))

    if response.status_code >= 400:
        # The body is read but never propagated: a provider's error text
        # routinely carries internal hostnames and stack traces.
        raise ProviderUnavailable(spec.name, f"HTTP {response.status_code}")

    try:
        parsed = response.json()
    except ValueError as exc:
        raise ProviderUnavailable(spec.name, "response was not JSON") from exc

    usage = parsed.get("usage") or {}
    return ProviderResult(
        provider=spec.name,
        model=spec.model,
        payload=parsed,
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
    )


def _retry_after(response: httpx2.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # The header also permits an HTTP-date. Not parsed: the caller only uses
        # this as a hint, and a wrong number is worse than none.
        return None
