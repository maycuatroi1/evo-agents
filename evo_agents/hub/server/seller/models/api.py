"""The shapes of the seller's API for buying hubs under /v1/seller-api (``evo_agents.hub.server.seller.service.api``),
and its constants."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Header, Path
from pydantic import BaseModel, Field

from evo_agents.hub import tables_seller
from evo_agents.hub.seller_client import HUB_ID_HEADER
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import PRINTABLE
from evo_agents.plugins.manifest import PLUGIN_ID

MAX_OPEN_PER_BUYER = 5
MAX_OPEN = 500
ORDER_LOCK = 0x5E11E4  # pg_advisory_xact_lock: one order number at a time
EMAIL = r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$"
VERSION = r"^[0-9A-Za-z.+-]{1,64}$"
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422, 429, 503)}
UNKNOWN_ORDER = "this seller has no such order, or the secret is not its"
HubHeader = Annotated[str, Header(alias=HUB_ID_HEADER, pattern=f"^{tables_seller.HUB_ID}$")]
AuthHeader = Annotated[str | None, Header(alias="Authorization")]
BundlePlugin = Annotated[str, Path(pattern=PLUGIN_ID)]


class SellerListing(BaseModel):
    plugin: str
    publisher: str
    usd_per_hub_month: float
    version: str | None = Field(None, description="its newest version for sale")


class SellerApiInfo(BaseModel):
    seller: str
    key_id: str
    public_key: str
    listings: list[SellerListing]


class Contact(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=tables_seller.MAX_CONTACT_CHARS, pattern=PRINTABLE)]
    email: Annotated[str, Field(min_length=3, max_length=tables_seller.MAX_CONTACT_CHARS, pattern=EMAIL)]


class OrderIn(BaseModel):
    plugin: Annotated[str, Field(pattern=PLUGIN_ID)]
    hub: Annotated[str, Field(pattern=f"^{tables_seller.HUB_ID}$", description="the buying hub's stable id")]
    hub_url: Annotated[str | None, Field(max_length=tables_seller.MAX_URL_CHARS, pattern=PRINTABLE)] = None
    months: Annotated[int, Field(ge=1, le=tables_seller.MAX_MONTHS)]
    contact: Contact


class OrderPlaced(BaseModel):
    number: str
    secret: str = Field(description="what the buying hub reads the order with; shown once, kept only hashed here")
    plugin: str
    publisher: str
    months: int
    usd_per_month: float
    usd_total: float
    instructions: str = Field(description="how to pay it")


class OrderState(BaseModel):
    number: str
    plugin: str
    state: str
    months: int
    usd_total: float
    instructions: str | None = Field(None, description="how to pay it, while it is open")
    license: str | None = Field(None, description="the licence's token, once paid")
    valid_from: str | None = None
    valid_until: str | None = None


class SellerBundle(BaseModel):
    plugin: str
    version: str
    sha256: str
    size: int
    signature: dict
    bundle: str = Field(description="the bundle's bytes, in base64")
