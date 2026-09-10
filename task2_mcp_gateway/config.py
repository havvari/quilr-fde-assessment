"""Gateway configuration, read from the environment once at import.

Kept in a frozen dataclass rather than read ad hoc so that tests construct one
explicitly instead of mutating `os.environ` and racing each other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Static token -> role table. Production would verify a signed JWT instead; see
# the README's "Production gaps". Deliberately not a secret worth protecting --
# these are fixtures, and naming them `tok_` makes that obvious in a log.
DEFAULT_TOKENS: dict[str, str] = {
    "tok_admin": "admin",
    "tok_viewer": "viewer",
}

# The prefix that marks a tool as privileged. A prefix is a weak contract -- see
# the README -- but it is the one the brief specifies.
ADMIN_TOOL_PREFIX = "admin_"

ADMIN_ROLE = "admin"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GatewayConfig:
    downstream_url: str = "http://127.0.0.1:8200/mcp"
    # The gateway's *own* credential for the downstream server. The client's
    # bearer token is never forwarded; see the README.
    downstream_token: str | None = None
    tokens: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TOKENS))
    # Off by default: the brief asks for tools/list to be forwarded transparently.
    # On, a viewer never sees admin_ tools at all. Argued in the README.
    filter_tool_list: bool = False
    request_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> GatewayConfig:
        return cls(
            downstream_url=os.environ.get("DOWNSTREAM_MCP_URL", cls.downstream_url),
            downstream_token=os.environ.get("DOWNSTREAM_MCP_TOKEN"),
            filter_tool_list=_env_flag("FILTER_TOOL_LIST"),
            request_timeout=float(os.environ.get("GATEWAY_TIMEOUT_SECONDS", "10.0")),
        )
