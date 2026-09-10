"""Task 2, the MCP security gateway.

The load-bearing assertion is not "the client got -32001" -- it is
"the downstream was never contacted". A gateway that forwards a privileged call
and then discards the response has already leaked the call. `CALL_LOG` on the
mock downstream is what makes that observable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from common.jsonrpc import INVALID_REQUEST, PARSE_ERROR, UNAUTHORIZED_TOOL_CALL
from mock_provider.mcp_downstream import CALL_LOG
from mock_provider.mcp_downstream import app as downstream_app
from task2_mcp_gateway.app import create_app
from task2_mcp_gateway.auth import Principal
from task2_mcp_gateway.config import GatewayConfig
from task2_mcp_gateway.policy import Allow, Deny, decide

ADMIN = {"Authorization": "Bearer tok_admin"}
VIEWER = {"Authorization": "Bearer tok_viewer"}
DOWNSTREAM_URL = "http://downstream.test/mcp"
DOWNSTREAM_TOKEN = "gateway-owned-downstream-secret"


def _gateway(**overrides: Any) -> Iterator[TestClient]:
    """A gateway wired to the mock downstream in-process, no sockets involved."""
    config = GatewayConfig(
        downstream_url=DOWNSTREAM_URL,
        downstream_token=DOWNSTREAM_TOKEN,
        **overrides,
    )
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=downstream_app))
    CALL_LOG.reset()
    with TestClient(create_app(config, client)) as test_client:
        yield test_client


@pytest.fixture
def gateway() -> Iterator[TestClient]:
    yield from _gateway()


@pytest.fixture
def filtering_gateway() -> Iterator[TestClient]:
    yield from _gateway(filter_tool_list=True)


def call(name: str, request_id: int | str | None = 1, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


# --------------------------------------------------------------------------
# The core requirement.
# --------------------------------------------------------------------------


def test_viewer_calling_an_admin_tool_is_denied_without_touching_downstream(gateway: TestClient) -> None:
    response = gateway.post("/mcp", json=call("admin_reset_key", 7, tenant="acme"), headers=VIEWER)

    assert response.status_code == 200, "a JSON-RPC error rides on HTTP 200"
    body = response.json()
    assert body["error"]["code"] == UNAUTHORIZED_TOOL_CALL == -32001
    assert body["error"]["message"] == "Unauthorized Tool Call"
    assert body["id"] == 7, "the client cannot correlate an error that drops the id"
    assert "result" not in body

    # The assertion this whole task is about.
    assert CALL_LOG.count == 0, f"downstream was contacted: {CALL_LOG.entries}"


def test_admin_calling_an_admin_tool_is_forwarded(gateway: TestClient) -> None:
    response = gateway.post("/mcp", json=call("admin_reset_key", 7, tenant="acme"), headers=ADMIN)

    body = response.json()
    assert "error" not in body
    assert body["id"] == 7
    assert "admin_reset_key" in body["result"]["content"][0]["text"]
    assert CALL_LOG.count == 1
    assert CALL_LOG.entries[0]["tool"] == "admin_reset_key"


def test_viewer_calling_a_normal_tool_is_forwarded(gateway: TestClient) -> None:
    response = gateway.post("/mcp", json=call("get_order_status", 3, order_id="A-1"), headers=VIEWER)

    assert response.json()["result"]["isError"] is False
    assert CALL_LOG.count == 1


def test_tools_list_is_forwarded_transparently_for_every_role(gateway: TestClient) -> None:
    response = gateway.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=VIEWER)

    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    # Transparent by default: the brief asks for tools/list to be forwarded
    # unchanged, so a viewer sees admin tools listed even though calling one fails.
    assert "admin_reset_key" in names
    assert CALL_LOG.count == 1


# --------------------------------------------------------------------------
# The client's credential must not reach the downstream.
# --------------------------------------------------------------------------


def test_client_bearer_token_is_not_forwarded_downstream(gateway: TestClient) -> None:
    gateway.post("/mcp", json=call("get_order_status", 3, order_id="A-1"), headers=ADMIN)

    seen = CALL_LOG.entries[0]["authorization"]
    assert seen == f"Bearer {DOWNSTREAM_TOKEN}", "the gateway must present its own credential"
    assert "tok_admin" not in (seen or "")


# --------------------------------------------------------------------------
# Authentication failures.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        ("missing header", {}),
        ("wrong scheme", {"Authorization": "Basic dG9rOnBhc3M="}),
        ("empty credential", {"Authorization": "Bearer "}),
        ("unknown token", {"Authorization": "Bearer tok_nobody"}),
        ("bare token", {"Authorization": "tok_admin"}),
    ],
)
def test_unauthenticated_requests_get_http_401(
    gateway: TestClient, label: str, headers: dict[str, str]
) -> None:
    response = gateway.post("/mcp", json=call("get_order_status", 1, order_id="A-1"), headers=headers)

    # 401, not a JSON-RPC error: there is no authenticated session in which a
    # -32001 would mean anything. See the README.
    assert response.status_code == 401, label
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert CALL_LOG.count == 0


def test_lowercase_bearer_scheme_is_accepted(gateway: TestClient) -> None:
    response = gateway.post(
        "/mcp",
        json=call("get_order_status", 1, order_id="A-1"),
        headers={"Authorization": "bearer tok_viewer"},
    )
    assert response.status_code == 200


def test_rejected_token_is_not_echoed_in_the_response(gateway: TestClient) -> None:
    response = gateway.post("/mcp", json=call("x", 1), headers={"Authorization": "Bearer hunter2"})
    assert "hunter2" not in response.text


# --------------------------------------------------------------------------
# Wire-format edge cases.
# --------------------------------------------------------------------------


def test_notification_gets_no_response_body(gateway: TestClient) -> None:
    response = gateway.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=VIEWER,
    )
    assert response.status_code == 204
    assert response.content == b""


def test_denied_notification_gets_no_body_but_is_still_blocked(gateway: TestClient) -> None:
    """A refusal with no `id` cannot be reported. It must still be a refusal."""
    response = gateway.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}},
        headers=VIEWER,
    )
    assert response.status_code == 204
    assert response.content == b""
    assert CALL_LOG.count == 0, "silence is not permission"


def test_null_id_is_a_request_not_a_notification(gateway: TestClient) -> None:
    """`{"id": null}` is a request whose id is null; absent `id` is a notification."""
    response = gateway.post("/mcp", json=call("admin_reset_key", None), headers=VIEWER)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == UNAUTHORIZED_TOOL_CALL


def test_batch_requests_are_rejected_explicitly(gateway: TestClient) -> None:
    response = gateway.post(
        "/mcp",
        json=[call("get_order_status", 1), call("admin_reset_key", 2)],
        headers=VIEWER,
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_REQUEST
    assert CALL_LOG.count == 0


def test_malformed_json_is_a_parse_error(gateway: TestClient) -> None:
    response = gateway.post(
        "/mcp",
        content=b'{"jsonrpc": "2.0", "method": ',
        headers={**VIEWER, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == PARSE_ERROR
    assert CALL_LOG.count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {}}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 42}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ""}},
    ],
)
def test_tools_call_that_names_no_tool_is_denied_not_forwarded(
    gateway: TestClient, payload: dict[str, Any]
) -> None:
    """A request the gateway cannot classify is a request it must not forward."""
    response = gateway.post("/mcp", json=payload, headers=VIEWER)

    assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
    assert CALL_LOG.count == 0


def test_non_object_payload_is_invalid_request(gateway: TestClient) -> None:
    response = gateway.post("/mcp", json="just a string", headers=VIEWER)
    assert response.json()["error"]["code"] == INVALID_REQUEST


def test_unknown_methods_are_forwarded(gateway: TestClient) -> None:
    """Only tools/call is gated; the gateway is not an allowlist of methods."""
    response = gateway.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "initialize"}, headers=VIEWER)
    assert response.json()["result"]["serverInfo"]["name"] == "mock-downstream"
    assert CALL_LOG.count == 1


# --------------------------------------------------------------------------
# The opt-in tool-list filter.
# --------------------------------------------------------------------------


def test_filter_hides_admin_tools_from_a_viewer(filtering_gateway: TestClient) -> None:
    response = filtering_gateway.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=VIEWER
    )
    names = [tool["name"] for tool in response.json()["result"]["tools"]]

    assert names == ["get_order_status", "search_docs"]
    assert not any(name.startswith("admin_") for name in names)
    # And the names are gone from the bytes, not merely from the parsed list.
    assert "admin_reset_key" not in response.text


def test_filter_leaves_admins_with_the_full_list(filtering_gateway: TestClient) -> None:
    response = filtering_gateway.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=ADMIN
    )
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert "admin_reset_key" in names
    assert len(names) == 4


def test_filter_does_not_change_call_authorization(filtering_gateway: TestClient) -> None:
    """Hiding a tool is defence in depth, not the control. The control still runs."""
    response = filtering_gateway.post("/mcp", json=call("admin_reset_key", 9, tenant="a"), headers=VIEWER)
    assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
    assert CALL_LOG.count == 0


# --------------------------------------------------------------------------
# Downstream failure.
# --------------------------------------------------------------------------


def test_downstream_failure_does_not_leak_the_upstream_url() -> None:
    async def explode(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("failed to connect to http://secret-internal-host:8200/mcp")

    config = GatewayConfig(downstream_url="http://secret-internal-host:8200/mcp")
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(explode))
    with TestClient(create_app(config, client)) as gateway:
        response = gateway.post("/mcp", json=call("get_order_status", 1, order_id="A"), headers=VIEWER)

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32603
    assert "secret-internal-host" not in response.text


# --------------------------------------------------------------------------
# The policy on its own, with no HTTP anywhere near it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "tool", "allowed"),
    [
        ("admin", "admin_reset_key", True),
        ("admin", "get_order_status", True),
        ("viewer", "get_order_status", True),
        ("viewer", "admin_reset_key", False),
        ("viewer", "admin_delete_tenant", False),
        # Prefix matching is literal and case-sensitive, which is a weakness worth
        # naming: a downstream tool called `Admin_reset` would not be gated.
        ("viewer", "Admin_reset_key", True),
        ("viewer", "not_admin_tool", True),
    ],
)
def test_policy_decisions(role: str, tool: str, allowed: bool) -> None:
    decision = decide(Principal("tok", role), "tools/call", {"name": tool})
    assert isinstance(decision, Allow if allowed else Deny)


def test_policy_ignores_non_tool_call_methods() -> None:
    for method in ("tools/list", "initialize", "resources/read", "notifications/initialized"):
        assert isinstance(decide(Principal("tok", "viewer"), method, {"name": "admin_reset_key"}), Allow)


def test_deny_payload_explains_itself() -> None:
    decision = decide(Principal("tok", "viewer"), "tools/call", {"name": "admin_reset_key"})
    assert isinstance(decision, Deny)
    assert decision.data is not None
    assert decision.data["tool"] == "admin_reset_key"
    assert decision.data["required_role"] == "admin"
    # The reason is JSON-serialisable; it goes on the wire.
    json.dumps(decision.data)
