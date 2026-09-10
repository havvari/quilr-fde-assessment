"""A toy in-memory ledger, standing in for a payments backend.

It exists to give `trigger_refund` failures that are *not* schema failures --
a well-formed customer id that nobody has, a refund larger than what was
charged -- because those are the cases that motivate the error-mapping split
described in the README.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Customer:
    customer_id: str
    name: str
    # Money as Decimal, never float. A float ledger is a rounding bug waiting for
    # a large enough transaction volume.
    charged: Decimal
    refunded: Decimal = Decimal("0")

    @property
    def refundable(self) -> Decimal:
        return self.charged - self.refunded


@dataclass
class Refund:
    refund_id: str
    customer_id: str
    amount: Decimal
    reason: str
    remaining_refundable: Decimal


class RefundRejected(Exception):
    """A business-rule failure: the request was well-formed, the world said no."""


@dataclass
class Ledger:
    customers: dict[str, Customer] = field(default_factory=dict)

    @classmethod
    def with_sample_data(cls) -> Ledger:
        return cls(
            customers={
                c.customer_id: c
                for c in (
                    Customer("CUST-00042", "Ada Lovelace", Decimal("250.00")),
                    Customer("CUST-01337", "Grace Hopper", Decimal("89.99")),
                    Customer("CUST-90210", "Alan Turing", Decimal("1200.00"), refunded=Decimal("1200.00")),
                )
            }
        )

    def refund(self, customer_id: str, amount: float, reason: str) -> Refund:
        customer = self.customers.get(customer_id)
        if customer is None:
            raise RefundRejected(
                f"No customer {customer_id} exists. Check the id and try again, or list the account first."
            )

        # str(float) round-trips the shortest repr, which is the least-surprising
        # bridge from the JSON number we were handed to the Decimal we keep.
        requested = Decimal(str(amount))
        if requested > customer.refundable:
            raise RefundRejected(
                f"Refund of {requested} exceeds the {customer.refundable} still refundable "
                f"on {customer_id} (charged {customer.charged}, already refunded {customer.refunded})."
            )

        customer.refunded += requested
        refund = Refund(
            refund_id=f"RF-{uuid.uuid4().hex[:12].upper()}",
            customer_id=customer_id,
            amount=requested,
            reason=reason,
            remaining_refundable=customer.refundable,
        )
        logger.info("issued refund %s for %s amount=%s", refund.refund_id, customer_id, requested)
        return refund
