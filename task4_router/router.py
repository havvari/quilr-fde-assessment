"""The routing entry point: rate limit, call, fail over, settle, sanitize.

    admit -> primary -> [429 or timeout] -> secondary -> reconcile

Three decisions are worth stating up front, because each is a place where the
obvious implementation is wrong:

**The primary is not retried.** It returned 429 or it timed out; both mean it is
saturated. Retrying adds load to the thing that is already failing and spends the
client's latency budget to do it. That is the entire premise of having a secondary.

**Fallback usage counts against the same tenant budget.** The tenant asked one
question and got one answer; which provider served it is the gateway's business,
not a second allowance. Otherwise failing over becomes a way to double your quota.

**The reservation is settled on every path**, including the failure paths. A
reservation that is neither reconciled nor released is a slow leak that shrinks
the tenant's effective limit until the window rolls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2

from common.errors import ErrorType, GatewayError, new_request_id, sanitize
from common.logging import get_logger
from task4_router.limiter import RateLimitExceeded, TokenRateLimiter
from task4_router.providers import (
    CallLog,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
    call_provider,
)
from task4_router.settings import RouterConfig

logger = get_logger(__name__)


@dataclass
class RoutedResponse:
    payload: dict[str, Any]
    provider: str
    attempts: list[str]
    tokens_charged: int
    request_id: str


def estimate_tokens(request: dict[str, Any], config: RouterConfig) -> int:
    """A rough input-token estimate, used only as a reservation.

    Characters over four. It is wrong -- a real tokeniser is not -- but it is
    wrong for a few hundred milliseconds, until `reconcile()` replaces it with
    the provider's reported usage. Being cheap matters more than being right,
    because this runs before the request is admitted.
    """
    text_parts: list[str] = []
    for message in request.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            text_parts.extend(part.get("text", "") for part in content if isinstance(part, dict))
    characters = sum(len(part) for part in text_parts)
    estimate = int(characters / config.chars_per_token)
    # Whatever the caller asked to generate is part of what it will spend.
    estimate += int(request.get("max_tokens", 0) or 0)
    return max(estimate, config.minimum_reservation)


class CompletionRouter:
    """Composes the limiter, the providers and the error boundary."""

    def __init__(
        self,
        config: RouterConfig,
        client: httpx2.AsyncClient,
        limiter: TokenRateLimiter | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.limiter = limiter or TokenRateLimiter(
            config.database_path,
            limit=config.token_limit,
            window_seconds=config.window_seconds,
        )

    async def route_completion(self, tenant_key: str, request: dict[str, Any]) -> RoutedResponse:
        """Admit, call, fail over if needed, settle. Raises `GatewayError` only."""
        request_id = new_request_id()
        estimate = estimate_tokens(request, self.config)

        try:
            reservation = await self.limiter.reserve(tenant_key, estimate)
        except RateLimitExceeded as exc:
            logger.info("rate limited %s: %s", tenant_key, exc)
            raise GatewayError(
                ErrorType.RATE_LIMITED,
                request_id=request_id,
                detail=str(exc),
                retry_after=exc.retry_after,
            ) from exc

        log = CallLog()
        try:
            result = await self._call_with_failover(request, log)
        except BaseException as exc:
            # Every exit that is not a successful response gives the tokens back,
            # cancellation included -- a client that hangs up must not leave a
            # reservation pinned until the window rolls.
            await self.limiter.release(reservation)
            if isinstance(exc, ProviderError):
                raise self._as_gateway_error(exc, request_id) from exc
            raise sanitize(exc, request_id=request_id) from exc

        charged = result.total_tokens or estimate
        await self.limiter.reconcile(reservation, charged)

        logger.info(
            "[%s] %s served by %s after %s, charged %d tokens",
            request_id,
            tenant_key,
            result.provider,
            " -> ".join(log.attempts),
            charged,
        )
        return RoutedResponse(
            payload=result.payload,
            provider=result.provider,
            attempts=log.attempts,
            tokens_charged=charged,
            request_id=request_id,
        )

    async def _call_with_failover(self, request: dict[str, Any], log: CallLog) -> ProviderResult:
        log.record(self.config.primary.name)
        try:
            return await call_provider(self.client, self.config.primary, request)
        except (ProviderRateLimited, ProviderTimeout) as exc:
            # The only two triggers, per the brief. A 5xx is deliberately not one:
            # it is as likely to be the request's fault as the provider's, and
            # sending the same malformed request to a second provider just buys a
            # second failure at twice the cost.
            logger.warning("failing over from %s: %s", exc.provider, exc.detail)

        log.record(self.config.secondary.name)
        return await call_provider(self.client, self.config.secondary, request)

    @staticmethod
    def _as_gateway_error(exc: ProviderError, request_id: str) -> GatewayError:
        error_type = (
            ErrorType.UPSTREAM_TIMEOUT if isinstance(exc, ProviderTimeout) else ErrorType.UPSTREAM_UNAVAILABLE
        )
        # `detail` -- which names the provider -- goes to the log only. The client
        # sees the fixed message for the type and nothing else.
        return GatewayError(error_type, request_id=request_id, detail=str(exc))


def response_body(routed: RoutedResponse) -> dict[str, Any]:
    """The client-facing payload, with routing metadata under a namespaced key.

    The provider *name* is included deliberately: it is this gateway's own label
    (`primary`/`secondary`), not the vendor, so it aids debugging without
    disclosing which vendor served the request.
    """
    body = dict(routed.payload)
    body["_gateway"] = {
        "request_id": routed.request_id,
        "served_by": routed.provider,
        "attempts": routed.attempts,
        "tokens_charged": routed.tokens_charged,
    }
    return body


def sanitized_json(error: GatewayError) -> str:
    return json.dumps(error.to_response())
