"""An MCP server exposing `trigger_refund` over stdio.

Two decisions carry this module; both are argued at length in the README.

1. Malformed input becomes a real JSON-RPC `-32602`, not an `is_error` result.
   The SDK's default is the opposite: `mcp/server/mcpserver/tools/base.py`
   catches pydantic's `ValidationError` and raises `ToolError`, so the call
   *succeeds* carrying `isError: true`. Its reasoning is sound -- the model chose
   the arguments and can read the failure and retry -- but the brief asks for
   standard JSON-RPC error codes, so `StrictParamsExtension` below validates
   first and raises `MCPError(INVALID_PARAMS)`, which the SDK propagates
   untouched into a top-level error object.

2. Business-rule failures stay as `ToolError`. "No such customer" is not a
   protocol violation; it is the world declining, and it is exactly the kind of
   thing a model can recover from on its own.
"""

from __future__ import annotations

from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, CallToolRequestParams
from pydantic import ValidationError

from common.logging import get_logger
from task1_mcp_server.ledger import Ledger, RefundRejected
from task1_mcp_server.validation import CustomerId, RefundAmount, RefundReason, RefundRequest

logger = get_logger(__name__)

SERVER_NAME = "refund-desk"

# Tools whose arguments the strict extension pre-validates, and the model to
# validate each against. A tool absent from this map keeps the SDK's default
# behaviour, which makes the override opt-in and visible rather than ambient.
STRICT_TOOL_SCHEMAS: dict[str, type[RefundRequest]] = {"trigger_refund": RefundRequest}


class StrictParamsExtension(Extension):
    """Turn argument-validation failures into JSON-RPC `-32602` errors.

    Runs at the handler layer, ahead of the tool's own schema check, so for a
    registered tool the SDK's `ToolError` path is never reached for bad
    arguments -- this validation fails first, on the identical model.

    Trade-off worth naming: an extension is advertised under
    `capabilities.extensions`, so clients can see it. That is honest -- the
    server genuinely does behave differently from a stock one -- but it is a
    visible deviation, which is why it is scoped to a named allowlist above.
    """

    identifier = "com.quilr.fde/strict-params"

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        model = STRICT_TOOL_SCHEMAS.get(params.name)
        if model is None:
            return await call_next(ctx)

        try:
            model.model_validate(params.arguments or {})
        except ValidationError as exc:
            logger.info("rejecting %s: %d validation error(s)", params.name, exc.error_count())
            raise MCPError(
                code=INVALID_PARAMS,
                message=f"Invalid arguments for {params.name}",
                # `errors()` is pydantic's own structured report: field location,
                # failed rule, and the offending input. It is the caller's own
                # data coming back, so returning it leaks nothing of ours, and it
                # is what makes the error actionable rather than merely correct.
                data={"errors": _serialisable_errors(exc)},
            ) from exc

        return await call_next(ctx)


def _serialisable_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic error records, trimmed to what survives JSON.

    `ctx` can hold arbitrary Python objects (a compiled pattern, an exception),
    and `url` is a docs link that would be noise on the wire.
    """
    trimmed: list[dict[str, Any]] = []
    for error in exc.errors(include_url=False):
        record: dict[str, Any] = {
            "field": ".".join(str(part) for part in error["loc"]) or "<root>",
            "type": error["type"],
            "message": error["msg"],
        }
        # NaN and Infinity are not JSON values; report them as their repr rather
        # than emitting a payload that a strict JSON parser will reject.
        given = error.get("input")
        if isinstance(given, str | int | bool | type(None)):
            record["given"] = given
        elif isinstance(given, float):
            record["given"] = given if given == given and abs(given) != float("inf") else repr(given)
        else:
            record["given"] = repr(given)
        trimmed.append(record)
    return trimmed


def build_server(ledger: Ledger | None = None) -> MCPServer:
    """Construct the server. Separate from running it so tests can call tools directly."""
    book = ledger if ledger is not None else Ledger.with_sample_data()
    server = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Issue customer refunds. Confirm the amount with the customer before calling "
            "trigger_refund; refunds are not reversible from this interface."
        ),
        extensions=[StrictParamsExtension()],
    )

    @server.tool(
        name="trigger_refund",
        title="Trigger a refund",
        description=(
            "Issue a refund against a customer's account. The reason is written to the "
            "audit log verbatim, so it should say why the refund is warranted."
        ),
    )
    def trigger_refund(customer_id: CustomerId, amount: RefundAmount, reason: RefundReason) -> dict[str, Any]:
        """The annotations are the published inputSchema; see validation.py."""
        try:
            refund = book.refund(customer_id, amount, reason)
        except RefundRejected as exc:
            # Deliberately a ToolError, not an MCPError: the arguments were valid,
            # the ledger said no, and the model can act on that (pick a smaller
            # amount, confirm the id) if it is told what happened.
            raise ToolError(str(exc)) from exc

        return {
            "refund_id": refund.refund_id,
            "customer_id": refund.customer_id,
            "amount": str(refund.amount),
            "reason": refund.reason,
            "remaining_refundable": str(refund.remaining_refundable),
            "status": "issued",
        }

    return server
