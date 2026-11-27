"""The seller's listings (``evo_agents.hub.server.seller.service.keys`` says what each route does): what the hub
sells, added by any member and changed by its owner or a hub admin, and the signed bundles uploaded to it."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blobs import blob_store, put_bytes
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.seller.models.seller import (
    NOTHING_UPLOADED,
    BundleIn,
    Listing,
    ListingBundle,
    ListingChange,
    ListingIn,
    Listings,
)
from evo_agents.hub.server.seller.repository import listings as repository
from evo_agents.hub.server.seller.service.shared import record, usd
from evo_agents.plugins.bundle import MAX_BUNDLE, BundleError, read_bundle
from evo_agents.plugins.market import signer_publisher
from evo_agents.plugins.signing import SignatureError, SignatureUnavailable, verify

log = logging.getLogger(__name__)


async def listing_row(conn: AsyncConnection, plugin: str, *, lock: bool = False):
    row = await repository.listing(conn, plugin, lock=lock)
    if row is None:
        raise NotFound(f"this hub lists no plugin {plugin} for sale: see `evo-agents hub market listing list`")
    return row


async def listings(conn: AsyncConnection, plugin: str | None = None) -> list[Listing]:
    """The listings (``plugin``'s alone, when given), with their versions and the licences that cover today."""
    rows, bundles = await repository.listings(conn, datetime.now(UTC).date(), plugin)
    return [
        Listing(
            plugin=row.plugin,
            publisher=row.publisher,
            signer=row.signer,
            owner=row.owner,
            usd_per_hub_month=usd(row.usd_per_hub_month),
            state=row.state,
            bundles=[
                ListingBundle(
                    version=found.version,
                    sha256=found.sha256,
                    size=found.size,
                    signed_by=found.signed_by,
                    uploaded_by=found.uploader,
                    uploaded_at=found.uploaded_at,
                )
                for found in bundles.get(row.id, [])
            ],
            active_licenses=row.active_licenses,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def all_listings(engine: AsyncEngine) -> Listings:
    """Every listing, as ``listings`` reads them, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return Listings(listings=await listings(conn))


def _may_change(row, user: Principal) -> None:
    if not user.admin and row.owner_id != user.user_id:
        raise Forbidden(f"only the member who listed {row.plugin} or a hub admin changes its listing")


def _check_signer(publisher: str, signer: str) -> None:
    try:
        signer_publisher(publisher, signer)
    except ValueError as exc:
        raise Unprocessable(f"signer {signer} does not read: {exc}") from None


async def add_listing(engine: AsyncEngine, body: ListingIn, user: Principal) -> Listing:
    """List ``body.plugin`` for sale, owned by ``user``, as a draft; 409 when it is listed already."""
    async with UnitOfWork(engine) as conn:
        _check_signer(body.publisher, body.signer)
        added = await repository.add_listing(
            conn,
            plugin=body.plugin,
            publisher=body.publisher,
            signer=body.signer,
            owner_id=user.user_id,
            usd_per_hub_month=body.usd_per_hub_month,
        )
        if not added:
            raise Conflict(f"{body.plugin} is listed already: change it with PATCH /v1/seller/listings/...")
        target = f"listing:{body.plugin} usd={body.usd_per_hub_month} state=draft"
        await record(conn, user, audit.MARKET_LISTING_ADD, target)
        return (await listings(conn, body.plugin))[0]


async def change_listing(engine: AsyncEngine, plugin: str, body: ListingChange, user: Principal) -> Listing:
    """Change the listing of ``plugin`` as ``body`` says, for its owner or a hub admin."""
    async with UnitOfWork(engine) as conn:
        row = await listing_row(conn, plugin, lock=True)
        _may_change(row, user)
        values = {}
        if body.usd_per_hub_month is not None:
            values["usd_per_hub_month"] = body.usd_per_hub_month
        if body.signer is not None:
            _check_signer(row.publisher, body.signer)
            values["signer"] = body.signer
        if body.state is not None:
            if body.state == "listed" and not await repository.bundle_count(conn, row.id):
                raise Conflict(f"{plugin} has no bundle yet: upload one, which lists it")
            values["state"] = body.state
        if values:
            await repository.change_listing(conn, row.id, **values)
            price = values.get("usd_per_hub_month", row.usd_per_hub_month)
            target = f"listing:{plugin} usd={price} state={values.get('state', row.state)}"
            await record(conn, user, audit.MARKET_LISTING_CHANGE, target)
        return (await listings(conn, plugin))[0]


def _check_bundle(row, data: bytes, signature: dict, sigstore):
    """The manifest of bundle ``data`` and who signed it: the listing's plugin, by its publisher, isolated, signed by
    its signer; 422 (or 503 when the signature cannot be checked here) otherwise."""
    try:
        manifest = read_bundle(data).manifest
    except BundleError as exc:
        raise Unprocessable(f"refused (malformed): {exc}; {NOTHING_UPLOADED}") from None
    if manifest.id != row.plugin or manifest.publisher != row.publisher:
        raise Unprocessable(
            f"the bundle is {manifest.id} of {manifest.publisher}, not {row.plugin} of {row.publisher}; "
            f"{NOTHING_UPLOADED}",
        )
    if manifest.tier != "isolated":
        raise Unprocessable(f"a paid plugin is isolated: {manifest.id} is a core plugin; {NOTHING_UPLOADED}")
    try:
        signed = verify(data, signature, row.publisher, (signer_publisher(row.publisher, row.signer),), sigstore)
    except SignatureError as exc:
        raise Unprocessable(f"refused ({exc.reason}): {exc}; {NOTHING_UPLOADED}") from None
    except SignatureUnavailable as exc:
        raise HTTPException(
            503, f"the signature cannot be checked on this hub now: {exc}; {NOTHING_UPLOADED}"
        ) from None
    return manifest, signed


async def upload_bundle(request: Request, plugin: str, body: BundleIn, user: Principal) -> Listing:
    """Check bundle ``body`` against the listing of ``plugin``, keep it and add it as a version; its first lists the
    plugin, and a version uploaded again with another bundle is a 409."""
    state = request.app.state
    try:
        data = base64.b64decode(body.bundle, validate=True)
    except (binascii.Error, ValueError):
        raise Unprocessable(f"bundle is the bundle's bytes in base64; {NOTHING_UPLOADED}") from None
    if len(data) > MAX_BUNDLE:
        raise Unprocessable(f"a plugin bundle is at most {MAX_BUNDLE // (1024 * 1024)} MiB; {NOTHING_UPLOADED}")
    async with state.engine.begin() as conn:
        row = await listing_row(conn, plugin)
        _may_change(row, user)
    manifest, signed = await asyncio.to_thread(_check_bundle, row, data, body.signature, state.plugin_sigstore)
    sha256 = hashlib.sha256(data).hexdigest()
    await asyncio.to_thread(put_bytes, blob_store(request), sha256, data)
    async with state.engine.begin() as conn:
        row = await listing_row(conn, plugin, lock=True)
        same = await repository.bundle_sha256(conn, row.id, manifest.version)
        if same is not None:
            if same != sha256:
                raise Conflict(
                    f"version {manifest.version} of {plugin} is uploaded with another bundle; a version never changes, "
                    "upload the change as a new version",
                )
            return (await listings(conn, plugin))[0]
        await repository.add_bundle(
            conn,
            sha256,
            len(data),
            user.user_id,
            listing_id=row.id,
            version=manifest.version,
            signature=body.signature,
            signed_by=signed.signed_by,
        )
        if row.state == "draft":
            await repository.change_listing(conn, row.id, state="listed")
        await record(conn, user, audit.MARKET_BUNDLE_UPLOAD, f"listing:{plugin}@{manifest.version}")
        result = (await listings(conn, plugin))[0]
    log.info("paid plugin bundle uploaded", extra={"plugin": plugin, "version": manifest.version})
    return result
