"""The tables of paid plugins (0159), which ``evo_agents.hub.tables`` adds to its metadata as ``tables.hub_identity``,
``tables.market_listings``, ``tables.market_bundles``, ``tables.market_orders``, ``tables.market_payouts`` and
``tables.market_purchases``.

``hub_identity`` holds the hub's stable id, ``hub_`` and 32 hex digits, made the first time the hub needs it
(``evo_agents.hub.server.market_licenses.hub_id``): a seller's licence names it.

A seller's side (the feature seller, ``evo_agents.hub.server.seller``): a listing (``market_listings``) is a plugin a
publisher sells, by the publisher's id and the signer of its bundles, owned by the member who listed it, at a price in
USD per hub per month; ``draft`` until its first bundle, then ``listed``, or ``withdrawn``. A bundle
(``market_bundles``) is one version of it, its bytes in the blob store under their SHA-256 and its signature here. An
order (``market_orders``) is what a buying hub ordered: ``ORD-YYYY-NNNN``, counted per year; the hub's id, address and
contact; the months, the price per month and the total; the SHA-256 of the secret the buyer reads it with; what the
payment provider keeps of it (``payment``, ``evo_agents.hub.payments``); and ``open`` until a seller admin marks it
``paid`` with an invoice number, which issues its licence, or ``cancelled``. A payout (``market_payouts``) records a
publisher's share of a month's revenue paid out, by whom.

A buyer's side (``evo_agents.hub.server.market_licenses``): a purchase (``market_purchases``) is an order the hub placed
with a seller, with the order's secret sealed with the hub's key (``{ciphertext, nonce, key_id}``), the state the seller
last answered, and the licence received once it checked against the keys the market's index lists for the seller.

The CHECKs on the shapes and states are the migration's alone.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB

HUB_ID = r"hub_[0-9a-f]{32}"
ORDER_NUMBER = r"ORD-[0-9]{4}-[0-9]{4,}"
LISTING_STATES = ("draft", "listed", "withdrawn")
ORDER_STATES = ("open", "paid", "cancelled")
MAX_MONTHS = 36
MAX_CONTACT_CHARS = 200
MAX_URL_CHARS = 500
MAX_INVOICE_CHARS = 100
MAX_NOTE_CHARS = 500
MAX_ERROR_CHARS = 2000


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _usd(name: str, digits: int = 12) -> Column:
    return Column(name, Numeric(digits, 2), nullable=False)


def _user(table: str, column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], ["users.id"], ondelete="RESTRICT", name=f"{table}_{column}_fkey")


def _licence() -> list[Column]:
    return [
        Column("license_id", Text),  # lic_ and 16 hex digits
        Column("valid_from", Date),
        Column("valid_until", Date),  # the first day it no longer covers
        Column("key_id", Text),  # the seller's key that signed it
        Column("token", Text),  # evo-license/1
    ]


def seller_tables(metadata: MetaData) -> tuple[Table, ...]:
    """hub_identity, market_listings, market_bundles, market_orders, market_payouts and market_purchases (0159)."""
    identity = Table(
        "hub_identity",
        metadata,
        Column("singleton", Boolean, nullable=False, server_default=true()),
        Column("hub_id", Text, nullable=False),
        _stamp("created_at"),
        PrimaryKeyConstraint("singleton", name="hub_identity_pkey"),
    )
    listings = Table(
        "market_listings",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("plugin", Text, nullable=False),
        Column("publisher", Text, nullable=False),
        Column("signer", Text, nullable=False),  # sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY
        Column("owner_id", BigInteger, nullable=False),
        _usd("usd_per_hub_month", 10),
        Column("state", Text, nullable=False, server_default="draft"),
        _stamp("created_at"),
        _stamp("updated_at"),
        _user("market_listings", "owner_id"),
        PrimaryKeyConstraint("id", name="market_listings_pkey"),
        UniqueConstraint("plugin", name="market_listings_plugin_key"),
    )
    bundles = Table(
        "market_bundles",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("listing_id", BigInteger, nullable=False),
        Column("version", Text, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("signature", JSONB, nullable=False),
        Column("signed_by", Text, nullable=False),
        Column("uploaded_by", BigInteger, nullable=False),
        _stamp("uploaded_at"),
        ForeignKeyConstraint(
            ["listing_id"], ["market_listings.id"], ondelete="CASCADE", name="market_bundles_listing_id_fkey"
        ),
        _user("market_bundles", "uploaded_by"),
        PrimaryKeyConstraint("id", name="market_bundles_pkey"),
        UniqueConstraint("listing_id", "version", name="market_bundles_version_key"),
    )
    orders = Table(
        "market_orders",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("number", Text, nullable=False),
        Column("listing_id", BigInteger, nullable=False),
        Column("buyer_hub", Text, nullable=False),
        Column("buyer_url", Text),
        Column("buyer_name", Text, nullable=False),
        Column("buyer_email", Text, nullable=False),
        Column("months", Integer, nullable=False),
        _usd("usd_per_month", 10),
        _usd("usd_total"),
        Column("secret_hash", Text, nullable=False),
        Column("state", Text, nullable=False, server_default="open"),
        Column("payment", JSONB, nullable=False, server_default="{}"),
        _stamp("created_at"),
        Column("invoice", Text),
        Column("closed_by", BigInteger),
        _when("closed_at"),
        *_licence(),
        _when("issued_at"),
        ForeignKeyConstraint(
            ["listing_id"], ["market_listings.id"], ondelete="RESTRICT", name="market_orders_listing_id_fkey"
        ),
        _user("market_orders", "closed_by"),
        PrimaryKeyConstraint("id", name="market_orders_pkey"),
        UniqueConstraint("number", name="market_orders_number_key"),
        UniqueConstraint("license_id", name="market_orders_license_id_key"),
        Index("market_orders_hub_idx", "buyer_hub", "listing_id"),
    )
    payouts = Table(
        "market_payouts",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("month", Date, nullable=False),  # its first day
        Column("publisher", Text, nullable=False),
        _usd("gross_usd"),
        _usd("publisher_usd"),
        _usd("platform_usd"),
        Column("paid_by", BigInteger, nullable=False),
        _stamp("paid_at"),
        Column("note", Text),
        _user("market_payouts", "paid_by"),
        PrimaryKeyConstraint("id", name="market_payouts_pkey"),
        UniqueConstraint("month", "publisher", name="market_payouts_month_key"),
    )
    purchases = Table(
        "market_purchases",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("source_id", BigInteger),
        Column("seller", Text, nullable=False),  # its id in the market's index
        Column("seller_url", Text, nullable=False),
        Column("plugin", Text, nullable=False),
        Column("publisher", Text, nullable=False),
        Column("signer", Text, nullable=False),
        Column("number", Text, nullable=False),
        Column("months", Integer, nullable=False),
        _usd("usd_total"),
        Column("secret", JSONB, nullable=False),  # sealed
        Column("state", Text, nullable=False, server_default="open"),
        Column("placed_by", BigInteger, nullable=False),
        _stamp("placed_at"),
        _when("checked_at"),
        Column("last_error", Text),
        *_licence(),
        _when("received_at"),
        _when("reminded_at"),
        ForeignKeyConstraint(
            ["source_id"], ["market_sources.id"], ondelete="SET NULL", name="market_purchases_source_id_fkey"
        ),
        _user("market_purchases", "placed_by"),
        PrimaryKeyConstraint("id", name="market_purchases_pkey"),
        UniqueConstraint("seller_url", "number", name="market_purchases_number_key"),
        Index("market_purchases_plugin_idx", "plugin", "valid_until"),
    )
    return identity, listings, bundles, orders, payouts, purchases
