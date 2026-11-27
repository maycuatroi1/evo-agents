"""Selling plugins from this hub (plan plugin-market, criterion a7): the listings, orders, licences, revenue and payouts
of a seller's hub. The feature ``seller`` (``evo_agents.hub.features``), off by default, owns these routes.

- GET /v1/seller: the seller's id, the id and public key of the key that signs its licences (what a market's index
  lists under ``sellers[]``), and whether the hub can sell: EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY set (this module);
- GET|POST /v1/seller/listings, PATCH /v1/seller/listings/{plugin}: what the hub sells. Any member signed in with
  GitHub lists a plugin of a publisher, by the signer of its bundles, at a price in USD per hub per month, and owns
  the listing; its owner or a hub admin changes it. A listing is a draft until its first bundle;
- POST /v1/seller/listings/{plugin}/bundles: its owner or a hub admin uploads a signed version, checked against the
  listing's publisher and signer and kept in the blob store; it is never on a public release, and the seller serves it
  only for a licence of the hub that asks (``evo_agents.hub.server.seller.service.api``). Both are
  ``evo_agents.hub.server.seller.service.listings``;
- GET /v1/seller/orders, POST /v1/seller/orders/{number}/paid and /cancel (hub admins): the orders buying hubs placed;
  marking one paid with the invoice number issues its licence (``evo_agents.plugins.license``), signed with
  EVO_HUB_SELLER_KEY, from the end of the hub's last licence of the plugin when that has not lapsed, else today. An
  order left open ORDER_TTL is cancelled (``expire_orders``) the next time an order is read or placed
  (``evo_agents.hub.server.seller.service.orders``);
- GET /v1/seller/revenue?month=YYYY-MM and POST /v1/seller/payouts (hub admins): the month's revenue of each
  publisher, the orders paid that month in UTC, split PUBLISHER_PERCENT to the publisher and the rest to evo-agents
  (``evo_agents.hub.payments``), and a publisher's share marked paid out by hand
  (``evo_agents.hub.server.seller.service.revenue``).

Every action on money or a licence is audited (``audit.MARKET_*``); the key and the licence tokens never are, nor are
they logged. With EVO_HUB_SELLER_SIGNAL_PROJECT set the hub writes market.active_licenses and
market.revenue_usd_month to that project, both keys registered there, as each order is paid and daily (the job
hub.market_signals, ``evo_agents.hub.server.seller.service.signals``). ``evo-agents hub market orders``, ``paid``,
``revenue`` and ``payout`` are the client.
"""

from __future__ import annotations

import base64

from fastapi import HTTPException

from evo_agents.hub import payments
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.seller.models.seller import NOT_SELLING, SellerInfo
from evo_agents.plugins import license as lic
from evo_agents.plugins.signing import key_id


def selling(config: HubConfig) -> bool:
    return bool(config.seller_id and config.seller_key)


def seller_key(config: HubConfig) -> tuple[str, str]:
    """(key id, public key) of the key that signs the licences; 503 when the hub does not sell."""
    if not selling(config):
        raise HTTPException(503, NOT_SELLING)
    public = lic.public_key_of(config.seller_key)  # pyright: ignore[reportArgumentType]
    return key_id(base64.b64decode(public)), public


def own_keys(config: HubConfig) -> dict[str, bytes]:
    """The seller's own key, as ``evo_agents.plugins.license.read_license`` takes the keys of an index."""
    found, public = seller_key(config)
    return {found: base64.b64decode(public)}


def seller_info(config: HubConfig) -> SellerInfo:
    """The seller's id and public key, and whether the hub can sell."""
    found, public = seller_key(config) if selling(config) else (None, None)
    return SellerInfo(
        seller=config.seller_id,
        key_id=found,
        public_key=public,
        selling=selling(config),
        payment_instructions=payments.provider(config.seller_payment_instructions).instructions,  # pyright: ignore[reportAttributeAccessIssue]
        signal_project=config.seller_signal_project,
    )
