"""The shapes of what the seller's members read and write under /v1/seller
(``evo_agents.hub.server.seller.service``), and its constants."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub import payments, tables_seller
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugins.models.publishers import SIGNER
from evo_agents.hub.server.projects import PRINTABLE
from evo_agents.plugins.bundle import MAX_BUNDLE
from evo_agents.plugins.manifest import PLUGIN_ID

NOTHING_UPLOADED = "nothing was uploaded"
NOT_SELLING = (
    "this hub does not sell plugins yet: its operator sets EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY (`evo-agents hub "
    "market seller-key` makes the key)"
)
PUBLISHER = r"^[a-z0-9][a-z0-9-]{0,62}$"  # evo_agents.plugins.signing.PUBLISHER_ID
MONTH = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
ORDER_NUMBER = Annotated[str, Path(pattern=f"^{tables_seller.ORDER_NUMBER}$", description="ORD-YYYY-NNNN")]
ListingPlugin = Annotated[str, Path(pattern=PLUGIN_ID, description="the listed plugin's id")]
MAX_PRICE = Decimal("99999999.99")  # market_listings.usd_per_hub_month, numeric(10, 2)
SIGNAL_SOURCE = "market"
SIGNAL_INGEST_KEY = "seller"  # the project's ingest key the seller's signals come through, made on first use
ACTIVE_LICENSES = "market.active_licenses"
REVENUE = "market.revenue_usd_month"
ORDER_TTL = timedelta(days=30)  # an order left open this long is cancelled: nobody is paying it
SIGNAL_KEYS = (
    (ACTIVE_LICENSES, "licences", "the licences of this hub's plugins that cover today"),
    (REVENUE, "USD", "the revenue of the orders paid this month, in UTC"),
)
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}


class SellerInfo(BaseModel):
    seller: str | None = Field(description="EVO_HUB_SELLER_ID: the seller's id in the markets that list it")
    key_id: str | None = Field(description="the id of the key that signs the licences, as the market's index lists it")
    public_key: str | None = Field(description="its public key, 32 bytes in base64; the private one is never shown")
    selling: bool = Field(description="whether the hub can sell: the id and the key are both set")
    payment_instructions: str = Field(description="what a buying hub is told to pay an order by")
    signal_project: str | None = Field(description="EVO_HUB_SELLER_SIGNAL_PROJECT")


class ListingBundle(BaseModel):
    version: str
    sha256: str
    size: int
    signed_by: str
    uploaded_by: str | None = None
    uploaded_at: datetime


class Listing(BaseModel):
    plugin: str
    publisher: str
    signer: str
    owner: str | None = None
    usd_per_hub_month: float
    state: Literal["draft", "listed", "withdrawn"]
    bundles: list[ListingBundle] = Field(description="its versions for sale, the newest first")
    active_licenses: int = Field(description="the licences of it that cover today")
    created_at: datetime
    updated_at: datetime


class Listings(BaseModel):
    listings: list[Listing]


class ListingIn(BaseModel):
    plugin: Annotated[str, Field(pattern=PLUGIN_ID)]
    publisher: Annotated[str, Field(pattern=PUBLISHER)]
    signer: Annotated[str, Field(pattern=SIGNER, description="sigstore:OWNER/REPO/.github/workflows/FILE.yml or ...")]
    usd_per_hub_month: Annotated[Decimal, Field(gt=0, le=MAX_PRICE, decimal_places=2)]


class ListingChange(BaseModel):
    usd_per_hub_month: Annotated[Decimal | None, Field(gt=0, le=MAX_PRICE, decimal_places=2)] = None
    signer: Annotated[str | None, Field(pattern=SIGNER)] = None
    state: Literal["listed", "withdrawn"] | None = Field(None, description="listed needs a bundle")


class BundleIn(BaseModel):
    bundle: Annotated[str, Field(min_length=1, max_length=(MAX_BUNDLE * 4) // 3 + 4)] = Field(
        description="the bundle's bytes (tar.gz), in base64"
    )
    signature: dict = Field(description="its signature envelope, as `evo-agents plugin sign` writes it")


class Order(BaseModel):
    number: str
    plugin: str
    publisher: str
    buyer_hub: str
    buyer_url: str | None = None
    buyer_name: str
    buyer_email: str
    months: int
    usd_per_month: float
    usd_total: float
    state: Literal["open", "paid", "cancelled"]
    payment: dict = Field(description="what the payment provider keeps of it: provider and reference")
    created_at: datetime
    invoice: str | None = None
    closed_by: str | None = None
    closed_at: datetime | None = None
    license_id: str | None = None
    valid_from: date | None = None
    valid_until: date | None = Field(None, description="the first day the licence no longer covers")


class Orders(BaseModel):
    orders: list[Order]


class OrderPaidIn(BaseModel):
    invoice: Annotated[str, Field(min_length=1, max_length=tables_seller.MAX_INVOICE_CHARS, pattern=PRINTABLE)]


class Payout(BaseModel):
    publisher_usd: float
    paid_by: str | None = None
    paid_at: datetime
    note: str | None = None


class RevenueLine(BaseModel):
    publisher: str
    orders: int
    gross_usd: float
    publisher_usd: float = Field(description=f"the publisher's {payments.PUBLISHER_PERCENT}%, rounded down to the cent")
    platform_usd: float
    paid_out: Payout | None = Field(None, description="when a hub admin marked the publisher's share paid out")


class Revenue(BaseModel):
    month: str
    publishers: list[RevenueLine]
    gross_usd: float
    publisher_usd: float
    platform_usd: float


class PayoutIn(BaseModel):
    month: Annotated[str, Field(pattern=MONTH)]
    publisher: Annotated[str, Field(pattern=PUBLISHER)]
    note: Annotated[str | None, Field(max_length=tables_seller.MAX_NOTE_CHARS, pattern=PRINTABLE)] = None
