"""Task 1, input validation and error mapping.

Two distinct outcomes are asserted, and the distinction is the design:

* Malformed arguments -> a top-level JSON-RPC error, code -32602. The tool never
  runs; the *host* sees the failure and the model does not.
* Well-formed arguments the ledger declines -> a normal result carrying
  `isError: true`. The model sees the text and can act on it.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from mcp_types import INVALID_PARAMS

from task1_mcp_server.ledger import Ledger, RefundRejected
from task1_mcp_server.validation import RefundRequest
from tests.stdio_harness import StdioServer, call_tool, run_session

VALID = {"customer_id": "CUST-00042", "amount": 10.0, "reason": "duplicate charge on invoice 8812"}


# --------------------------------------------------------------------------
# Malformed input -> -32602, over the real wire.
# --------------------------------------------------------------------------

MALFORMED: list[tuple[str, dict[str, Any], str]] = [
    ("id too short", {**VALID, "customer_id": "CUST-1234"}, "customer_id"),
    ("id too long", {**VALID, "customer_id": "CUST-123456"}, "customer_id"),
    ("id lowercase prefix", {**VALID, "customer_id": "cust-12345"}, "customer_id"),
    ("id non-digit body", {**VALID, "customer_id": "CUST-ABCDE"}, "customer_id"),
    ("id empty", {**VALID, "customer_id": ""}, "customer_id"),
    ("id null", {**VALID, "customer_id": None}, "customer_id"),
    ("id padded with spaces", {**VALID, "customer_id": " CUST-00042 "}, "customer_id"),
    ("id with trailing newline", {**VALID, "customer_id": "CUST-00042\n"}, "customer_id"),
    ("id missing", {k: v for k, v in VALID.items() if k != "customer_id"}, "customer_id"),
    ("amount zero", {**VALID, "amount": 0}, "amount"),
    ("amount negative", {**VALID, "amount": -5}, "amount"),
    ("amount tiny negative", {**VALID, "amount": -0.0001}, "amount"),
    ("amount null", {**VALID, "amount": None}, "amount"),
    ("amount non-numeric string", {**VALID, "amount": "ten"}, "amount"),
    ("amount missing", {k: v for k, v in VALID.items() if k != "amount"}, "amount"),
    ("reason 9 chars", {**VALID, "reason": "too short"}, "reason"),
    ("reason 10 spaces", {**VALID, "reason": " " * 10}, "reason"),
    ("reason padded but thin", {**VALID, "reason": "   short    "}, "reason"),
    ("reason empty", {**VALID, "reason": ""}, "reason"),
    ("reason null", {**VALID, "reason": None}, "reason"),
    ("reason missing", {k: v for k, v in VALID.items() if k != "reason"}, "reason"),
    ("unknown argument", {**VALID, "aproved_by": "me"}, "aproved_by"),
]


@pytest.fixture(scope="module")
def malformed_session() -> Any:
    requests = [
        call_tool(index, "trigger_refund", args) for index, (_, args, _) in enumerate(MALFORMED, start=2)
    ]
    return run_session(requests)


@pytest.mark.parametrize(("label", "arguments", "field"), MALFORMED, ids=[c[0] for c in MALFORMED])
def test_malformed_arguments_are_invalid_params(
    malformed_session: Any, label: str, arguments: dict[str, Any], field: str
) -> None:
    request_id = next(i for i, case in enumerate(MALFORMED, start=2) if case[0] == label)
    frame = malformed_session.by_id(request_id)

    assert "error" in frame, f"{label} was accepted as a result, not rejected: {frame}"
    assert frame["error"]["code"] == INVALID_PARAMS == -32602
    assert "result" not in frame

    reported = {entry["field"] for entry in frame["error"]["data"]["errors"]}
    assert field in reported, f"{label}: expected {field} to be blamed, got {reported}"


def test_ten_spaces_and_ten_letters_are_treated_differently() -> None:
    """The whitespace rule, stated as the pair of cases that motivates it."""
    session = run_session(
        [
            call_tool(2, "trigger_refund", {**VALID, "reason": " " * 10}),
            call_tool(3, "trigger_refund", {**VALID, "reason": "chargeback"}),
        ]
    )
    assert session.by_id(2)["error"]["code"] == INVALID_PARAMS
    assert session.by_id(3)["result"]["isError"] is False


def test_nan_and_infinity_on_the_wire_are_rejected() -> None:
    """JSON has no NaN, but Python's json module emits and accepts the bare tokens.

    A hand-rolled client can therefore put `NaN` or `Infinity` on the wire, and
    `gt: 0` alone does not stop either -- every comparison with NaN is False, and
    +Infinity is genuinely greater than zero. `allow_inf_nan=False` is what does.
    """
    with StdioServer() as server:
        server.handshake()
        for request_id, token in ((2, "NaN"), (3, "Infinity"), (4, "-Infinity")):
            server.send_raw(
                f'{{"jsonrpc":"2.0","id":{request_id},"method":"tools/call","params":'
                f'{{"name":"trigger_refund","arguments":{{"customer_id":"CUST-00042",'
                f'"amount":{token},"reason":"duplicate charge on invoice"}}}}}}\n'
            )
        for _ in range(3):
            server.read_response()
        session = server.close()

    for request_id in (2, 3, 4):
        frame = session.by_id(request_id)
        assert frame["error"]["code"] == INVALID_PARAMS
        assert frame["error"]["data"]["errors"][0]["field"] == "amount"


def test_invalid_params_error_names_the_offending_value() -> None:
    session = run_session([call_tool(2, "trigger_refund", {**VALID, "customer_id": "CUST-ABCDE"})])
    detail = session.by_id(2)["error"]["data"]["errors"][0]
    assert detail["field"] == "customer_id"
    assert detail["given"] == "CUST-ABCDE"
    assert "CUST" in detail["message"]


def test_error_frames_echo_the_request_id() -> None:
    session = run_session([call_tool(4242, "trigger_refund", {**VALID, "amount": -1})])
    assert session.by_id(4242)["error"]["code"] == INVALID_PARAMS


# --------------------------------------------------------------------------
# Well-formed input the ledger declines -> isError, not a protocol error.
# --------------------------------------------------------------------------


def test_unknown_but_well_formed_customer_is_a_tool_error() -> None:
    session = run_session([call_tool(2, "trigger_refund", {**VALID, "customer_id": "CUST-00000"})])
    frame = session.by_id(2)

    assert "error" not in frame, "a missing customer is not a protocol violation"
    assert frame["result"]["isError"] is True
    text = frame["result"]["content"][0]["text"]
    assert "CUST-00000" in text
    # The message has to be worth reading: the model is the audience.
    assert "No customer" in text


def test_refund_larger_than_the_remaining_balance_is_a_tool_error() -> None:
    session = run_session([call_tool(2, "trigger_refund", {**VALID, "amount": 999_999.0})])
    frame = session.by_id(2)
    assert frame["result"]["isError"] is True
    assert "exceeds" in frame["result"]["content"][0]["text"]


def test_successful_refund_returns_structured_content() -> None:
    session = run_session([call_tool(2, "trigger_refund", VALID)])
    result = session.by_id(2)["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["customer_id"] == "CUST-00042"
    assert structured["status"] == "issued"
    assert structured["refund_id"].startswith("RF-")


def test_published_input_schema_advertises_the_constraints() -> None:
    session = run_session([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    tools = {tool["name"]: tool for tool in session.by_id(2)["result"]["tools"]}
    schema = tools["trigger_refund"]["inputSchema"]

    assert schema["properties"]["customer_id"]["pattern"] == r"^CUST-\d{5}$"
    assert schema["properties"]["amount"]["exclusiveMinimum"] == 0
    assert schema["properties"]["reason"]["minLength"] == 10
    assert set(schema["required"]) == {"customer_id", "amount", "reason"}


# --------------------------------------------------------------------------
# Unit-level checks that do not need a subprocess.
# --------------------------------------------------------------------------


def test_reason_is_stored_stripped() -> None:
    request = RefundRequest.model_validate({**VALID, "reason": "  duplicate charge  "})
    assert request.reason == "duplicate charge"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), math.inf, -math.inf])
def test_amount_rejects_non_positive_and_non_finite(bad: float) -> None:
    with pytest.raises(ValueError):
        RefundRequest.model_validate({**VALID, "amount": bad})


def test_ledger_decrements_the_refundable_balance() -> None:
    ledger = Ledger.with_sample_data()
    first = ledger.refund("CUST-01337", 40.0, "partial refund agreed")
    assert first.remaining_refundable == ledger.customers["CUST-01337"].refundable
    with pytest.raises(RefundRejected):
        ledger.refund("CUST-01337", 60.0, "second refund too large")
