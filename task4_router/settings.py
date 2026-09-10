"""Router configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from task4_router.limiter import DEFAULT_LIMIT, DEFAULT_WINDOW_SECONDS
from task4_router.providers import ProviderSpec

DEFAULT_DATABASE = "rate_limit.db"


@dataclass(frozen=True)
class RouterConfig:
    primary: ProviderSpec
    secondary: ProviderSpec
    database_path: str = DEFAULT_DATABASE
    token_limit: int = DEFAULT_LIMIT
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    # Characters per token. A crude estimate, and deliberately so -- it is only
    # ever a reservation, corrected the moment real usage is known.
    chars_per_token: float = 4.0
    # Floor on the reservation, so a one-word prompt still claims something and
    # a flood of tiny requests cannot slip past the window unaccounted.
    minimum_reservation: int = 16

    @classmethod
    def from_env(cls) -> RouterConfig:
        base = os.environ.get("PROVIDER_BASE_URL", "http://127.0.0.1:8100/v1/chat/completions")
        timeout = float(os.environ.get("PRIMARY_TIMEOUT_SECONDS", "3.0"))
        return cls(
            primary=ProviderSpec(
                name=os.environ.get("PRIMARY_PROVIDER", "primary"),
                url=os.environ.get("PRIMARY_URL", base),
                model=os.environ.get("PRIMARY_MODEL", "mock-primary"),
                timeout_seconds=timeout,
            ),
            secondary=ProviderSpec(
                name=os.environ.get("SECONDARY_PROVIDER", "secondary"),
                url=os.environ.get("SECONDARY_URL", base),
                model=os.environ.get("SECONDARY_MODEL", "mock-secondary"),
                timeout_seconds=float(os.environ.get("SECONDARY_TIMEOUT_SECONDS", str(timeout * 2))),
            ),
            database_path=os.environ.get("RATE_LIMIT_DB", DEFAULT_DATABASE),
            token_limit=int(os.environ.get("TOKEN_LIMIT_PER_MINUTE", DEFAULT_LIMIT)),
        )
