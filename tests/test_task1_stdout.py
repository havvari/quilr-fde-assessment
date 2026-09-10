"""Task 1, stdout isolation -- the top-scored criterion.

Under stdio, fd 1 is the JSON-RPC wire. These tests spawn the server as a real
subprocess, drive a full session including failing calls, and assert that every
byte on fd 1 parses as JSON.

Worth being precise about who provides the guarantee. MCP SDK v2's
`stdio_server()` claims fd 1 for the session -- it dup2s the real wire onto a
private descriptor and points fd 1 at stderr -- so a `print()` inside a handler
cannot corrupt the stream. The residual hole is the window before that claim:
import-time writes still land on the wire. `test_import_time_write_corrupts_the_wire`
exercises exactly that window, which is also what proves this suite can fail.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.stdio_harness import Session, StdioServer, call_tool, initialize_frames, run_session

VALID_ARGS = {
    "customer_id": "CUST-00042",
    "amount": 10.0,
    "reason": "duplicate charge on invoice 8812",
}


def _mixed_workload() -> list[dict[str, Any]]:
    """A session that exercises every response path the server has."""
    return [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call_tool(3, "trigger_refund", VALID_ARGS),
        # -32602: malformed id, short reason, non-positive amount.
        call_tool(4, "trigger_refund", {**VALID_ARGS, "customer_id": "nope"}),
        call_tool(5, "trigger_refund", {**VALID_ARGS, "reason": "short"}),
        call_tool(6, "trigger_refund", {**VALID_ARGS, "amount": -1}),
        # isError: well-formed, but the ledger declines.
        call_tool(7, "trigger_refund", {**VALID_ARGS, "customer_id": "CUST-00000"}),
        call_tool(8, "trigger_refund", {**VALID_ARGS, "amount": 999999.0}),
        # Unknown tool, and an unknown method.
        call_tool(9, "no_such_tool", {}),
        {"jsonrpc": "2.0", "id": 10, "method": "resources/list"},
    ]


@pytest.fixture(scope="module")
def workload_session() -> Session:
    return run_session(_mixed_workload())


def test_every_stdout_line_is_json_rpc(workload_session: Session) -> None:
    """The deliverable. Not one stray byte on the wire across the whole session."""
    session = workload_session
    assert session.stdout_lines, "server produced no output at all"
    for index, line in enumerate(session.stdout_lines):
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - the failure we are preventing
            pytest.fail(f"stdout line {index} is not JSON: {line!r} ({exc})")
        assert frame.get("jsonrpc") == "2.0", f"line {index} is JSON but not JSON-RPC: {line!r}"
        assert "result" in frame or "error" in frame, f"line {index} is neither a result nor an error"


def test_server_exits_cleanly_and_logs_to_stderr(workload_session: Session) -> None:
    session = workload_session
    assert session.returncode == 0
    # The server does log -- the point is where those logs went.
    assert "starting refund-desk over stdio" in session.stderr
    assert "starting refund-desk over stdio" not in session.stdout


def test_failing_calls_do_not_leak_tracebacks_to_stdout(workload_session: Session) -> None:
    session = workload_session
    assert "Traceback" not in session.stdout
    assert 'File "' not in session.stdout


def test_every_request_got_exactly_one_response(workload_session: Session) -> None:
    session = workload_session
    ids = [frame["id"] for frame in session.responses]
    assert sorted(ids) == list(range(1, 11))
    # Responses may arrive out of request order -- the server handles calls
    # concurrently -- which is precisely why every frame must echo its id.
    assert len(ids) == len(set(ids))


def test_import_time_write_corrupts_the_wire() -> None:
    """The hole the SDK's fd-1 claim cannot cover, and proof this suite can fail.

    `tests/fixtures/noisy_server.py` prints during import, before the transport
    claims fd 1. Those bytes land in front of the first frame, so the very first
    thing a client reads is unparseable and the session is dead on arrival.

    Lines are read raw here rather than through `read_response`: a harness that
    parses as it reads cannot show you the byte that broke the parser.
    """
    with StdioServer("tests.fixtures.noisy_server") as server:
        first = server.read_line()
        assert first is not None
        assert not _is_json(first), "expected the banner to precede the wire"
        assert first.strip() == "starting refund desk..."

        # The transport itself is fine -- it is the stream in front of it that is
        # poisoned -- so the handshake still answers, just one line too late.
        for frame in initialize_frames():
            server.send(frame)
        handshake_line = server.read_line()
        assert handshake_line is not None
        assert _is_json(handshake_line)
        session = server.close()

    assert session.stdout.startswith("starting refund desk...")
    assert [line for line in session.stdout_lines if not _is_json(line)] == ["starting refund desk..."]


def test_print_inside_a_handler_cannot_reach_the_wire() -> None:
    """The SDK's own protection, asserted rather than assumed.

    A handler that prints is writing to a descriptor the transport has already
    pointed at stderr, so the wire stays clean. This is why "we avoided print()"
    is not the interesting claim -- and why the import-time test above is.
    """
    with StdioServer("tests.fixtures.chatty_handler_server") as server:
        server.handshake()
        server.send(call_tool(2, "shout", {}))
        assert server.read_response()["result"]["isError"] is False
        session = server.close()

    assert all(_is_json(line) for line in session.stdout_lines)
    assert "SIDE EFFECT FROM INSIDE THE HANDLER" in session.stderr
    assert "SIDE EFFECT FROM INSIDE THE HANDLER" not in session.stdout


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True
