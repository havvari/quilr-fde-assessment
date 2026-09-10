"""Input contract for `trigger_refund`.

The constraints live in `Annotated` aliases so they have exactly one definition
and are used twice: once as the tool function's parameter annotations, which is
what MCP publishes as the tool's `inputSchema`, and once in `RefundRequest`,
which the strict-params extension validates against before the tool body runs.
Two copies of "amount must be > 0" would eventually disagree.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# The brief writes customer ids as CUST-XXXXX. It does not say what X is.
# Assumption: five decimal digits, uppercase prefix, no surrounding whitespace.
# Documented in the README as an ambiguity resolved by literal reading.
CUSTOMER_ID_PATTERN = r"^CUST-\d{5}$"

MIN_REASON_LENGTH = 10


def _reason_must_have_substance(value: str) -> str:
    """Enforce the length floor *after* stripping, and return the stripped text.

    `Field(min_length=10)` alone accepts ten spaces. A reason is meant to be an
    audit trail entry a human will read later, so whitespace does not count
    toward it. The stripped value is what gets stored, so the ledger never holds
    a reason whose leading newlines change how it renders in a report.
    """
    stripped = value.strip()
    if len(stripped) < MIN_REASON_LENGTH:
        raise ValueError(
            f"reason must contain at least {MIN_REASON_LENGTH} non-whitespace-padded characters; "
            f"got {len(stripped)} after stripping"
        )
    return stripped


CustomerId = Annotated[
    str,
    Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer identifier in the form CUST-00000 (literally five digits).",
    ),
]

RefundAmount = Annotated[
    float,
    Field(
        gt=0,
        # JSON has no NaN or Infinity literals, but Python's json module parses
        # the bare tokens `NaN` and `Infinity` by default, so a hand-rolled
        # client can put either on the wire. `gt=0` does not reject NaN (every
        # comparison against NaN is False, so pydantic's own check is what saves
        # us) and does not reject +inf at all. This flag rejects both.
        allow_inf_nan=False,
        description="Refund amount in account currency. Strictly positive, finite.",
    ),
]

RefundReason = Annotated[
    str,
    # min_length is declared so the published inputSchema advertises the floor to
    # clients; the validator enforces the stricter post-strip rule the schema
    # cannot express.
    Field(
        min_length=MIN_REASON_LENGTH,
        description=(
            f"Why the refund is being issued. At least {MIN_REASON_LENGTH} characters "
            "after leading and trailing whitespace is removed."
        ),
    ),
    AfterValidator(_reason_must_have_substance),
]


class RefundRequest(BaseModel):
    """The same contract as the tool signature, in a form we can validate eagerly.

    `extra="forbid"` so an unknown argument is a validation failure rather than a
    silently ignored one. A caller that sends `custmer_id` has made a mistake, and
    telling it so beats accepting the call with a missing field.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    amount: RefundAmount
    reason: RefundReason
