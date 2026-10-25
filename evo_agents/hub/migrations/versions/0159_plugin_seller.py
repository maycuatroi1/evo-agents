"""Paid plugins: the hub's stable id, what a seller lists, sells and pays out, and the licences a buying hub holds.

Revision ID: 0159
Revises: 0158
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_seller``, ``evo_agents.plugins.license`` and
``evo_agents.plugins.market`` as of this revision, never imports. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

hub_identity holds one row at most: the id a hub gives a seller when it buys a plugin, which the licence names, made
the first time the hub needs it (plan plugin-market, criterion a7). A database cloned from another keeps its id.

On a seller's hub (the feature seller), market_listings holds a plugin a publisher signed in with GitHub sells, at a
price in USD per hub per month, draft until its first bundle, then listed or withdrawn; market_bundles each version of
its signed bundle, held in the blob store and never on a public release. market_orders holds what a buying hub
ordered: its number ORD-YYYY-NNNN, the hub's id and contact, the months and the price, the hash of the secret the
buyer reads it with, and its state: open, then paid with the invoice number and the licence it issued (its id, from,
until, the key that signed it and the token), or cancelled; who closed it and when exactly when it is not open.
market_payouts holds the share of a month's revenue a seller admin marked paid out to a publisher.

On a buying hub, market_purchases holds each order it placed with a seller: the seller and plugin as the market's index
named them, the number, the order's secret sealed with the hub's key, its state as the seller last answered and the
licence it received, checked against the index's keys, with when the hub reminded its admins it ends.

The rows of a user are not deleted under them; a listing with orders is not deleted; a purchase outlives the market
source it came from.

Going back drops the six tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None

HUB_ID = "^hub_[0-9a-f]{32}$"  # evo_agents.hub.tables_seller.HUB_ID
PLUGIN = "^[a-z][a-z0-9-]{0,62}$"  # a manifest's id
PUBLISHER = "^[a-z0-9][a-z0-9-]{0,62}$"  # evo_agents.plugins.signing.PUBLISHER_ID
SIGNER = "^(sigstore|ed25519):[^[:space:]]+$"  # what follows ID= in EVO_HUB_PLUGIN_PUBLISHERS
VERSION = "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,40})?$"
SHA256 = "^[0-9a-f]{64}$"
ORDER = "^ORD-[0-9]{4}-[0-9]{4,}$"  # evo_agents.hub.tables_seller.ORDER_NUMBER
LICENSE = "^lic_[0-9a-f]{16}$"  # evo_agents.plugins.license.LICENSE_ID
KEY_ID = "^ed25519:[0-9a-f]{16}$"  # evo_agents.plugins.signing.key_id
LISTING_STATES = ("draft", "listed", "withdrawn")
ORDER_STATES = ("open", "paid", "cancelled")
MAX_MONTHS = 36
MAX_CONTACT_CHARS = 200
MAX_URL_CHARS = 500
MAX_INVOICE_CHARS = 100
MAX_NOTE_CHARS = 500
MAX_ERROR_CHARS = 2000
MAX_TOKEN_CHARS = 4096

col = sa.column
length = sa.func.char_length


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _usd(name: str, digits: int = 12) -> sa.Column:
    return sa.Column(name, sa.Numeric(digits, 2), nullable=False)


def _licence_columns() -> list[sa.Column]:
    return [
        sa.Column("license_id", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("key_id", sa.Text(), nullable=True),
        sa.Column("token", sa.Text(), nullable=True),
    ]


def _licence_check() -> sa.ColumnElement:
    """A licence is there whole or not at all, ends after it starts, and reads as its fields say."""
    held = col("license_id").is_not(None)
    return sa.and_(
        *(col(name).is_(None) != held for name in ("valid_from", "valid_until", "key_id", "token")),
        sa.or_(~held, col("valid_until") > col("valid_from")),
        col("license_id").regexp_match(LICENSE),
        col("key_id").regexp_match(KEY_ID),
        length(col("token")) <= MAX_TOKEN_CHARS,
    )


def _identity() -> None:
    op.create_table(
        "hub_identity",
        sa.Column("singleton", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("hub_id", sa.Text(), nullable=False),
        _stamp("created_at"),
        sa.PrimaryKeyConstraint("singleton", name="hub_identity_pkey"),
        sa.CheckConstraint(sa.and_(col("singleton"), col("hub_id").regexp_match(HUB_ID)), name="hub_identity_check"),
    )


def _listings() -> None:
    op.create_table(
        "market_listings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plugin", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        _usd("usd_per_hub_month", 10),
        sa.Column("state", sa.Text(), server_default="draft", nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key("market_listings", "owner_id", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_listings_pkey"),
        sa.UniqueConstraint("plugin", name="market_listings_plugin_key"),
        sa.CheckConstraint(
            sa.and_(
                col("plugin").regexp_match(PLUGIN),
                col("publisher").regexp_match(PUBLISHER),
                col("signer").regexp_match(SIGNER),
                col("usd_per_hub_month") > 0,
                col("state").in_(LISTING_STATES),
            ),
            name="market_listings_check",
        ),
    )
    op.create_table(
        "market_bundles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("signature", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signed_by", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
        _stamp("uploaded_at"),
        _key("market_bundles", "listing_id", "market_listings", "CASCADE"),
        _key("market_bundles", "uploaded_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_bundles_pkey"),
        sa.UniqueConstraint("listing_id", "version", name="market_bundles_version_key"),
        sa.CheckConstraint(
            sa.and_(col("version").regexp_match(VERSION), col("sha256").regexp_match(SHA256), col("size") > 0),
            name="market_bundles_check",
        ),
    )


def _orders() -> None:
    state = col("state")
    op.create_table(
        "market_orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("buyer_hub", sa.Text(), nullable=False),
        sa.Column("buyer_url", sa.Text(), nullable=True),
        sa.Column("buyer_name", sa.Text(), nullable=False),
        sa.Column("buyer_email", sa.Text(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        _usd("usd_per_month", 10),
        _usd("usd_total"),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        sa.Column("payment", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        _stamp("created_at"),
        sa.Column("invoice", sa.Text(), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        _when("closed_at"),
        *_licence_columns(),
        _when("issued_at"),
        _key("market_orders", "listing_id", "market_listings", "RESTRICT"),
        _key("market_orders", "closed_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_orders_pkey"),
        sa.UniqueConstraint("number", name="market_orders_number_key"),
        sa.UniqueConstraint("license_id", name="market_orders_license_id_key"),
        sa.CheckConstraint(
            sa.and_(
                col("number").regexp_match(ORDER),
                col("buyer_hub").regexp_match(HUB_ID),
                length(col("buyer_url")) <= MAX_URL_CHARS,
                length(col("buyer_name")).between(1, MAX_CONTACT_CHARS),
                length(col("buyer_email")).between(3, MAX_CONTACT_CHARS),
                col("months").between(1, MAX_MONTHS),
                col("usd_per_month") > 0,
                col("usd_total") > 0,
                col("secret_hash").regexp_match(SHA256),
                state.in_(ORDER_STATES),
                (state == "open") == col("closed_at").is_(None),
                (state == "open") == col("closed_by").is_(None),
                (state == "paid") == col("invoice").is_not(None),
                (state == "paid") == col("license_id").is_not(None),
                (state == "paid") == col("issued_at").is_not(None),
                length(col("invoice")).between(1, MAX_INVOICE_CHARS),
                _licence_check(),
            ),
            name="market_orders_check",
        ),
    )
    op.create_index("market_orders_hub_idx", "market_orders", ["buyer_hub", "listing_id"])
    op.create_table(
        "market_payouts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        _usd("gross_usd"),
        _usd("publisher_usd"),
        _usd("platform_usd"),
        sa.Column("paid_by", sa.BigInteger(), nullable=False),
        _stamp("paid_at"),
        sa.Column("note", sa.Text(), nullable=True),
        _key("market_payouts", "paid_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_payouts_pkey"),
        sa.UniqueConstraint("month", "publisher", name="market_payouts_month_key"),
        sa.CheckConstraint(
            sa.and_(
                sa.extract("day", col("month")) == 1,
                col("publisher").regexp_match(PUBLISHER),
                col("publisher_usd") >= 0,
                col("platform_usd") >= 0,
                col("publisher_usd") + col("platform_usd") == col("gross_usd"),
                length(col("note")) <= MAX_NOTE_CHARS,
            ),
            name="market_payouts_check",
        ),
    )


def _purchases() -> None:
    state = col("state")
    op.create_table(
        "market_purchases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("seller", sa.Text(), nullable=False),
        sa.Column("seller_url", sa.Text(), nullable=False),
        sa.Column("plugin", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        _usd("usd_total"),
        sa.Column("secret", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        sa.Column("placed_by", sa.BigInteger(), nullable=False),
        _stamp("placed_at"),
        _when("checked_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_licence_columns(),
        _when("received_at"),
        _when("reminded_at"),
        _key("market_purchases", "source_id", "market_sources", ondelete="SET NULL"),
        _key("market_purchases", "placed_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_purchases_pkey"),
        sa.UniqueConstraint("seller_url", "number", name="market_purchases_number_key"),
        sa.CheckConstraint(
            sa.and_(
                col("plugin").regexp_match(PLUGIN),
                col("publisher").regexp_match(PUBLISHER),
                col("signer").regexp_match(SIGNER),
                col("number").regexp_match(ORDER),
                length(col("seller_url")).between(1, MAX_URL_CHARS),
                col("months").between(1, MAX_MONTHS),
                state.in_(ORDER_STATES),
                (state == "paid") == col("license_id").is_not(None),
                col("license_id").is_(None) == col("received_at").is_(None),
                length(col("last_error")) <= MAX_ERROR_CHARS,
                _licence_check(),
            ),
            name="market_purchases_check",
        ),
    )
    op.create_index("market_purchases_plugin_idx", "market_purchases", ["plugin", "valid_until"])


def upgrade() -> None:
    _identity()
    _listings()
    _orders()
    _purchases()


def downgrade() -> None:
    op.drop_table("market_purchases")
    op.drop_table("market_payouts")
    op.drop_table("market_orders")
    op.drop_table("market_bundles")
    op.drop_table("market_listings")
    op.drop_table("hub_identity")
