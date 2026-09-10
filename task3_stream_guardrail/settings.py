"""Configuration for the streaming guardrail."""

from __future__ import annotations

import os
from dataclasses import dataclass

from task3_stream_guardrail.redactor import DEFAULT_HOLD


@dataclass(frozen=True)
class GuardrailConfig:
    upstream_url: str = "http://127.0.0.1:8100/v1/chat/completions"
    # The worst-case held-back window. Overridable mainly so a test can shrink it
    # and demonstrate that the constant is load-bearing rather than decorative.
    hold_characters: int = DEFAULT_HOLD
    require_luhn: bool = False
    request_timeout: float = 60.0

    @classmethod
    def from_env(cls) -> GuardrailConfig:
        return cls(
            upstream_url=os.environ.get("UPSTREAM_LLM_URL", cls.upstream_url),
            hold_characters=int(os.environ.get("GUARDRAIL_HOLD", DEFAULT_HOLD)),
            require_luhn=os.environ.get("GUARDRAIL_REQUIRE_LUHN", "").lower() in {"1", "true", "yes"},
            request_timeout=float(os.environ.get("GUARDRAIL_TIMEOUT_SECONDS", "60.0")),
        )
