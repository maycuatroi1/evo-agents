"""How a seller's hub is paid for an order, and how a month's revenue splits (plan plugin-market, criterion a7).

No payment gateway runs yet: ``ManualTransfer`` hands the buyer the seller's bank-transfer instructions with the order
number as the reference, and a seller admin marks the order paid with the invoice number once the money arrived. A
gateway plugs in later as another ``PaymentProvider``: ``create`` returns what the buyer is shown and what the order
keeps (``market_orders.payment``), ``status`` what the provider knows of it then, which a job could poll and mark the
order paid with. The seller's routes (``evo_agents.hub.server.seller``) reach the provider through ``provider``.

``split`` divides the revenue of a publisher's plugins: PUBLISHER_PERCENT to the publisher, the rest to evo-agents, in
cents, the publisher's share rounded down, so the two always add up to the revenue.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

PUBLISHER_PERCENT = 80
CENT = Decimal("0.01")
DEFAULT_INSTRUCTIONS = "Transfer the total to the seller's bank account, with the order number as the reference."


@dataclass(frozen=True)
class Payment:
    """What a provider made of an order: its name, the reference the buyer pays with, what the buyer is told, and what
    the order keeps besides."""

    provider: str
    reference: str
    instructions: str
    details: dict = field(default_factory=dict)

    def stored(self) -> dict:
        return {"provider": self.provider, "reference": self.reference, **self.details}


class PaymentProvider(Protocol):
    """How a seller is paid: ``create`` when a buying hub orders, ``status`` whenever the seller asks (``open``,
    ``paid`` or ``failed``); a provider that cannot tell answers ``open``, and a seller admin decides."""

    name: str

    def create(self, number: str, usd_total: Decimal, buyer: dict) -> Payment: ...

    def status(self, payment: dict) -> str: ...


@dataclass(frozen=True)
class ManualTransfer:
    """A bank transfer the seller checks by hand: the instructions are the operator's (EVO_HUB_SELLER_PAYMENT_
    INSTRUCTIONS), the reference the order number."""

    instructions: str = DEFAULT_INSTRUCTIONS
    name: str = "manual"

    def create(self, number: str, usd_total: Decimal, buyer: dict) -> Payment:
        text = f"{self.instructions}\nAmount: {usd_total:.2f} USD. Reference: {number}."
        return Payment(self.name, number, text)

    def status(self, payment: dict) -> str:
        return "open"


def provider(instructions: str | None) -> PaymentProvider:
    """The provider of a seller's hub: a bank transfer, with ``instructions`` when the operator gave some."""
    return ManualTransfer(instructions or DEFAULT_INSTRUCTIONS)


def usd(value) -> Decimal:
    """``value`` as an amount in USD, to the cent."""
    return Decimal(str(value)).quantize(CENT)


def split(gross) -> tuple[Decimal, Decimal]:
    """The publisher's share of revenue ``gross`` and evo-agents', in USD to the cent, adding up to ``gross``."""
    total = usd(gross)
    publisher = (total * PUBLISHER_PERCENT / 100).quantize(CENT, rounding=ROUND_DOWN)
    return publisher, total - publisher
