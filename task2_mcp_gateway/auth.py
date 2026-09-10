"""Bearer-token authentication for the gateway.

Authentication only -- who the caller is. Whether that caller may invoke a given
tool is `policy.py`'s job, and keeping the two apart is what lets the policy be
unit-tested without constructing HTTP headers.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.logging import get_logger
from task2_mcp_gateway.config import ADMIN_ROLE

logger = get_logger(__name__)


@dataclass(frozen=True)
class Principal:
    token_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


class AuthenticationError(Exception):
    """No usable credential. Distinct from "authenticated but not entitled"."""


def parse_bearer(header: str | None) -> str:
    """Extract the token from an `Authorization` header.

    The scheme comparison is case-insensitive because RFC 7235 says the scheme is,
    and a client sending `bearer` lowercase is not an attacker, just a client.
    """
    if not header:
        raise AuthenticationError("missing Authorization header")

    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        raise AuthenticationError(f"unsupported authorization scheme {scheme!r}")

    token = credential.strip()
    if not token:
        raise AuthenticationError("empty bearer token")
    return token


def authenticate(header: str | None, tokens: dict[str, str]) -> Principal:
    token = parse_bearer(header)
    role = tokens.get(token)
    if role is None:
        # The token is not echoed into the log. Even a rejected credential is a
        # credential, and rejected-token logs are a routine source of leaks --
        # the sender may simply have pasted the right token at the wrong host.
        logger.warning("rejected an unrecognised bearer token")
        raise AuthenticationError("unrecognised bearer token")
    return Principal(token_id=token, role=role)
