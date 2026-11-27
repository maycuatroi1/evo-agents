"""The shapes of buying a paid plugin and of its licences (``evo_agents.hub.server.market.service.licenses``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from evo_agents.hub import tables_seller
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.seller.models.api import Contact

NOTICE = "market_license_expiring"  # evo_agents.hub.runs.NOTICE_KINDS
ORDER_NUMBER = re.compile(tables_seller.ORDER_NUMBER)
LAPSED = "licence lapsed"  # how plugins.disabled_reason of a plugin turned off by its licence starts
MARKET_PAGE = "/admin/market"
NO_SEALER = (
    "buying a plugin needs EVO_HUB_SECRETS_KEY: the hub seals the order's secret with it; an operator sets it "
    "(`evo-agents hub secrets-key`)"
)
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 502, 503)}


class HubIdentity(BaseModel):
    hub: str = Field(description="the hub's stable id, which a seller's licence names")
    url: str | None = Field(None, description="EVO_HUB_PUBLIC_URL, which an order carries")


class BuyIn(BaseModel):
    months: Annotated[int, Field(ge=1, le=tables_seller.MAX_MONTHS)]
    contact: Contact
    trust_publisher: bool = Field(
        False, description="trust the signer for the publisher from now on, when the hub does not yet"
    )


class Purchase(BaseModel):
    id: int
    seller: str
    seller_url: str
    plugin: str
    publisher: str
    number: str
    months: int
    usd_total: float
    state: str = Field(description="open until the seller marks it paid, paid, or cancelled")
    placed_by: str | None = None
    placed_at: datetime
    checked_at: datetime | None = None
    last_error: str | None = None
    license_id: str | None = None
    valid_from: date | None = None
    valid_until: date | None = Field(None, description="the first day the licence no longer covers")
    license_state: str | None = Field(None, description="pending, active, grace or lapsed, today")


class Bought(BaseModel):
    purchase: Purchase
    instructions: str = Field(description="how to pay the order, as the seller says")
    trusted_publisher: bool


class Purchases(BaseModel):
    hub: str
    purchases: list[Purchase]


class Checked(BaseModel):
    checked: int = Field(description="the open orders the hub asked their sellers about")
    received: list[str] = Field(description="the licences received")
    installed: list[str] = Field(description="the plugins installed or turned on again")
    reminded: list[str]
    lapsed: list[str] = Field(description="the plugins turned off")
    errors: list[str]


@dataclass(frozen=True)
class Placer:
    """The admin who ordered a plugin, as whom the hub installs it from the worker."""

    user_id: int
    login: str
    token_id: int | None = None
    admin: bool = True
