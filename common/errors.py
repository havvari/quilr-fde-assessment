"""The single client-facing error shape for the LLM gateway (Task 4).

The rule this module exists to enforce: a client learns *what class of thing went
wrong*, and nothing else. No upstream response body, no provider name, no URL, no
Python traceback. Everything else is logged server-side under a `request_id` that
is echoed to the client, so an operator can join a user's complaint to the real
exception without the exception ever crossing the wire.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from common.logging import get_logger

logger = get_logger(__name__)


class ErrorType(StrEnum):
    """A closed set. Adding a member is a deliberate, reviewable act.

    Closed because an open set drifts: someone eventually formats an upstream
    string into the type field and leaks the thing this module exists to hide.
    """

    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    INVALID_REQUEST = "invalid_request"
    INTERNAL_ERROR = "internal_error"


# Fixed, human-readable text per type. Deliberately not derived from the exception:
# an f-string over an upstream error is exactly how provider names and URLs escape.
_MESSAGES: dict[ErrorType, str] = {
    ErrorType.RATE_LIMITED: "Token rate limit exceeded for this API key. Retry after the window rolls over.",
    ErrorType.UPSTREAM_UNAVAILABLE: "The model provider is unavailable. Please retry.",
    ErrorType.UPSTREAM_TIMEOUT: "The model provider did not respond in time. Please retry.",
    ErrorType.INVALID_REQUEST: "The request was malformed.",
    ErrorType.INTERNAL_ERROR: "An internal error occurred.",
}

# HTTP status per type, so the transport layer never has to re-derive it.
_STATUS: dict[ErrorType, int] = {
    ErrorType.RATE_LIMITED: 429,
    ErrorType.UPSTREAM_UNAVAILABLE: 502,
    ErrorType.UPSTREAM_TIMEOUT: 504,
    ErrorType.INVALID_REQUEST: 400,
    ErrorType.INTERNAL_ERROR: 500,
}


class GatewayErrorBody(BaseModel):
    type: ErrorType
    message: str
    request_id: str


class GatewayErrorEnvelope(BaseModel):
    error: GatewayErrorBody


class GatewayError(Exception):
    """Raised anywhere inside the gateway; rendered by the one boundary handler.

    `detail` is for the server log only and never reaches `to_response()`.
    """

    def __init__(
        self,
        error_type: ErrorType,
        *,
        request_id: str | None = None,
        detail: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.error_type = error_type
        self.request_id = request_id or new_request_id()
        self.detail = detail
        self.retry_after = retry_after
        # The Exception's own str() carries the internal detail on purpose: it is
        # what lands in the log. It is never used to build the client payload.
        super().__init__(f"[{self.request_id}] {error_type}: {detail or _MESSAGES[error_type]}")

    @property
    def status_code(self) -> int:
        return _STATUS[self.error_type]

    def to_response(self) -> dict[str, Any]:
        return GatewayErrorEnvelope(
            error=GatewayErrorBody(
                type=self.error_type,
                message=_MESSAGES[self.error_type],
                request_id=self.request_id,
            )
        ).model_dump(mode="json")


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize(exc: BaseException, *, request_id: str | None = None) -> GatewayError:
    """Map any exception to a `GatewayError`, logging the original with its traceback.

    A `GatewayError` passes through with its type intact. Anything else becomes
    `internal_error` -- the deliberately uninformative bucket -- because an
    unclassified exception is by definition one whose text we have not audited.
    """
    if isinstance(exc, GatewayError):
        logger.warning("%s", exc, exc_info=exc)
        return exc

    rid = request_id or new_request_id()
    logger.error("[%s] unhandled gateway exception", rid, exc_info=exc)
    return GatewayError(ErrorType.INTERNAL_ERROR, request_id=rid, detail=type(exc).__name__)
