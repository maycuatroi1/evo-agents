"""The publishers whose signed plugin bundles the hub accepts (plan plugin-market, criterion a6): those the operator
lists in EVO_HUB_PLUGIN_PUBLISHERS, and the signers hub admins trusted from the web or the CLI as they installed from a
market or a GitHub release (``plugin_publishers``, 0154). The hub accepts the union of both
(``plugin_store.trusted_publishers``).

- GET /v1/plugin-publishers: every publisher, the operator's first in their order, each with what signs for it
  (``identities``, the operator's and the trusted ones), whether its plugins may be core plugins, and the signers hub
  admins trusted for it (``trusted``), with who and when;
- POST /v1/plugin-publishers: trust a signer for a publisher;
- DELETE /v1/plugin-publishers/{id}: stop trusting one; the row stays with who removed it and when.

All three are for hub admins. EVO_HUB_PLUGIN_PUBLISHERS_LOCKED=true refuses a write (409, with the reason), and a
core publisher (EVO_HUB_PLUGIN_CORE_PUBLISHERS) takes no signer from here: its signers run Python in the hub's
processes, so only the operator names them. Writes are audited as plugin.publisher.add and plugin.publisher.remove,
naming ``publisher:<id> signer:<signer>``. ``evo-agents hub plugin publisher list|add|remove`` is the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import AdminUser, Principal, admin
from evo_agents.plugins.market import merge_publishers, signer_identity, signer_publisher
from evo_agents.plugins.signing import PUBLISHER_ID

MAX_NOTE_CHARS = 500  # plugin_publishers.note (0154)
SIGNER = r"^(sigstore|ed25519):\S{1,600}$"
LOCKED = (
    "this hub's operator locked its publishers (EVO_HUB_PLUGIN_PUBLISHERS_LOCKED): the hub trusts only those "
    "EVO_HUB_PLUGIN_PUBLISHERS lists; ask the operator to add {publisher} there"
)
CORE = (
    "{publisher} is a core publisher of this hub, whose plugins run Python in the hub's processes: only the operator "
    "names what signs for it, in EVO_HUB_PLUGIN_PUBLISHERS"
)

router = APIRouter(
    prefix="/v1/plugin-publishers",
    tags=["plugins"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


class TrustedSigner(BaseModel):
    id: int = Field(description="the row to remove with DELETE /v1/plugin-publishers/{id}")
    signer: str = Field(description="sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY, as it was trusted")
    identity: str = Field(description="how it reads among the publisher's identities")
    added_by: str | None
    added_at: datetime
    note: str | None = None


class PluginPublisher(BaseModel):
    id: str
    identities: list[str] = Field(
        description="what signs for it: sigstore:<workflow> for a CI workflow, ed25519:<key id> for a key"
    )
    core: bool = Field(description="whether its plugins may be core plugins, which run in the hub's processes")
    operator: bool = Field(False, description="whether EVO_HUB_PLUGIN_PUBLISHERS lists it")
    trusted: list[TrustedSigner] = Field(
        default_factory=list, description="the signers hub admins trusted for it, besides the operator's"
    )


class PublisherTrustIn(BaseModel):
    publisher: Annotated[str, Field(pattern=f"^{PUBLISHER_ID.pattern}$")]
    signer: Annotated[str, Field(pattern=SIGNER)]
    note: Annotated[str | None, Field(max_length=MAX_NOTE_CHARS)] = None


def check_may_trust(config: HubConfig, publisher: str) -> None:
    """409 when hub admins may not trust a signer for ``publisher`` on this hub."""
    if config.plugin_publishers_locked:
        raise HTTPException(409, LOCKED.format(publisher=publisher))
    if publisher in config.plugin_core_publishers:
        raise HTTPException(409, CORE.format(publisher=publisher))


async def trust(
    conn: AsyncConnection, user: Principal, config: HubConfig, publisher: str, signer: str, note: str | None = None
) -> bool:
    """Trust ``signer`` for ``publisher``, with its audit row; False when the hub trusted it already."""
    check_may_trust(config, publisher)
    try:
        signer_publisher(publisher, signer)
    except ValueError as exc:
        raise HTTPException(422, f"signer {signer} does not read: {exc}") from None
    p = tables.plugin_publishers
    inserted = await conn.execute(
        pg_insert(p)
        .values(publisher=publisher, signer=signer, added_by=user.user_id, note=note)
        .on_conflict_do_nothing()
        .returning(p.c.id)
    )
    if inserted.first() is None:
        return False
    target = f"publisher:{publisher} signer:{signer}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PLUGIN_PUBLISHER_ADD, target=target
    )
    return True


async def list_publishers(conn: AsyncConnection, config: HubConfig) -> list[PluginPublisher]:
    p, users = tables.plugin_publishers, tables.users
    query = (
        select(p, users.c.login)
        .join_from(p, users, users.c.id == p.c.added_by, isouter=True)
        .where(p.c.removed_at.is_(None))
        .order_by(p.c.id)
    )
    rows = (await conn.execute(query)).all()
    merged = merge_publishers(config.plugin_publishers, [(row.publisher, row.signer) for row in rows])
    operator = {item.id for item in config.plugin_publishers}
    found = []
    for item in merged:
        trusted = [
            TrustedSigner(
                id=row.id,
                signer=row.signer,
                identity=signer_identity(row.signer),
                added_by=row.login,
                added_at=row.added_at,
                note=row.note,
            )
            for row in rows
            if row.publisher == item.id
        ]
        found.append(
            PluginPublisher(
                id=item.id,
                identities=item.identities(),
                core=item.id in config.plugin_core_publishers,
                operator=item.id in operator,
                trusted=trusted,
            )
        )
    return found


@router.get("", response_model=list[PluginPublisher])
async def publishers(request: Request) -> list[PluginPublisher]:
    """The publishers this hub trusts: the operator's in the order EVO_HUB_PLUGIN_PUBLISHERS names them, then those
    only hub admins trusted."""
    async with request.app.state.engine.begin() as conn:
        return await list_publishers(conn, request.app.state.config)


@router.post("", response_model=list[PluginPublisher], responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}})
async def add_publisher(request: Request, body: PublisherTrustIn, user: AdminUser) -> list[PluginPublisher]:
    """Trust ``signer`` to sign the plugins of ``publisher``; nothing changes when the hub trusts it already."""
    config = request.app.state.config
    async with request.app.state.engine.begin() as conn:
        await trust(conn, user, config, body.publisher, body.signer, body.note)
        return await list_publishers(conn, config)


@router.delete("/{entry_id}", response_model=list[PluginPublisher], responses={404: {"model": ErrorBody}})
async def remove_publisher(
    request: Request, entry_id: Annotated[int, Path(ge=1)], user: AdminUser
) -> list[PluginPublisher]:
    """Stop trusting a signer hub admins trusted; a plugin it signed stays installed, and installing or upgrading
    it again is refused until something the hub trusts signs it."""
    config = request.app.state.config
    p = tables.plugin_publishers
    async with request.app.state.engine.begin() as conn:
        removed = await conn.execute(
            update(p)
            .where(p.c.id == entry_id, p.c.removed_at.is_(None))
            .values(removed_by=user.user_id, removed_at=func.now())
            .returning(p.c.publisher, p.c.signer)
        )
        row = removed.first()
        if row is None:
            raise HTTPException(404, f"no trusted signer {entry_id}: see `evo-agents hub plugin publisher list`")
        target = f"publisher:{row.publisher} signer:{row.signer}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PLUGIN_PUBLISHER_REMOVE, target=target
        )
        return await list_publishers(conn, config)
