"""Milestone 0: the shared primitives and the mock provider's failure modes."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from common.errors import ErrorType, GatewayError, sanitize
from common.jsonrpc import UNAUTHORIZED_TOOL_CALL, error_response, is_notification
from mock_provider.llm import _SPLIT_SCRIPT, PII_TEXT
from mock_provider.llm import app as llm_app
from mock_provider.mcp_downstream import CALL_LOG
from mock_provider.mcp_downstream import app as downstream_app


@pytest.fixture
def llm() -> TestClient:
    return TestClient(llm_app)


@pytest.fixture
def downstream() -> TestClient:
    CALL_LOG.reset()
    return TestClient(downstream_app)


def test_error_response_echoes_id_including_null() -> None:
    assert error_response(7, UNAUTHORIZED_TOOL_CALL, "nope")["id"] == 7
    assert "id" in error_response(None, UNAUTHORIZED_TOOL_CALL, "nope")
    assert "data" not in error_response(1, -32602, "bad")["error"]
    assert error_response(1, -32602, "bad", {"k": 1})["error"]["data"] == {"k": 1}


def test_notification_is_absent_id_not_null_id() -> None:
    assert is_notification({"jsonrpc": "2.0", "method": "x"})
    assert not is_notification({"jsonrpc": "2.0", "method": "x", "id": None})


def test_gateway_error_never_leaks_internal_detail() -> None:
    secret = "https://internal.provider/v1 shard-7 offline"
    payload = json.dumps(GatewayError(ErrorType.UPSTREAM_TIMEOUT, detail=secret).to_response())
    assert "internal.provider" not in payload
    assert "shard-7" not in payload
    assert json.loads(payload)["error"]["type"] == "upstream_timeout"


def test_sanitize_buckets_unknown_exceptions_as_internal_error() -> None:
    err = sanitize(ValueError("Traceback: /srv/secret.py line 3"))
    assert err.error_type is ErrorType.INTERNAL_ERROR
    assert "secret" not in json.dumps(err.to_response())
    assert err.status_code == 500


def test_split_pii_script_reassembles_and_cuts_inside_every_pattern() -> None:
    assert "".join(_SPLIT_SCRIPT) == PII_TEXT
    # The point of the fixture: no single chunk contains a whole pattern, so a
    # per-chunk regex sees nothing at all.
    for chunk in _SPLIT_SCRIPT:
        assert "@example.com" not in chunk or "ada.lovelace@example.com" not in chunk
    assert not any("123-45-6789" in c for c in _SPLIT_SCRIPT)
    assert not any("4111-1111-1111-1111" in c for c in _SPLIT_SCRIPT)


def test_llm_rate_limited_mode(llm: TestClient) -> None:
    r = llm.post("/v1/chat/completions", json={"mode": "rate_limited"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "30"


def test_llm_mode_header_overrides_body(llm: TestClient) -> None:
    r = llm.post("/v1/chat/completions", json={"mode": "stream"}, headers={"X-Mock-Mode": "rate_limited"})
    assert r.status_code == 429


def test_llm_stream_mode_emits_sse_and_usage(llm: TestClient) -> None:
    r = llm.post("/v1/chat/completions", json={"mode": "stream"})
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [line[len("data: ") :] for line in r.text.splitlines() if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    assert json.loads(frames[-2])["usage"]["total_tokens"] == 150


def test_downstream_lists_admin_tools_and_spies_on_auth(downstream: TestClient) -> None:
    r = downstream.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer tok_viewer"},
    )
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "admin_reset_key" in names
    assert CALL_LOG.count == 1
    assert CALL_LOG.entries[0]["authorization"] == "Bearer tok_viewer"


def test_downstream_returns_no_body_for_notifications(downstream: TestClient) -> None:
    r = downstream.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 204
    assert r.text == ""
