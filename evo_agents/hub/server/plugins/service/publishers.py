"""The publishers whose signed plugin bundles the hub accepts (plan plugin-market, criterion a6): those the operator
lists in EVO_HUB_PLUGIN_PUBLISHERS, and the signers hub admins trusted from the web or the CLI as they installed from a
market or a GitHub release (``plugin_publishers``, 0154). The hub accepts the union of both
(``service.store.trusted_publishers``). EVO_HUB_PLUGIN_PUBLISHERS_LOCKED=true refuses a write (409, with the reason),
and a core publisher (EVO_HUB_PLUGIN_CORE_PUBLISHERS) takes no signer from here: its signers run Python in the hub's
processes, so only the operator names them. Writes are audited as plugin.publisher.add and plugin.publisher.remove,
naming ``publisher:<id> signer:<signer>``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plugins.models.publishers import CORE, LOCKED, PluginPublisher, TrustedSigner
from evo_agents.hub.server.plugins.repository import publishers as repository
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.market import merge_publishers, signer_identity, signer_publisher


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
    if not await repository.add_signer(conn, publisher, signer, user.user_id, note):
        return False
    target = f"publisher:{publisher} signer:{signer}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PLUGIN_PUBLISHER_ADD, target=target
    )
    return True


async def remove(conn: AsyncConnection, user: Principal, entry_id: int) -> None:
    """Stop trusting signer ``entry_id``, with its audit row; 404 when there is no such live row."""
    row = await repository.remove_signer(conn, entry_id, user.user_id)
    if row is None:
        raise HTTPException(404, f"no trusted signer {entry_id}: see `evo-agents hub plugin publisher list`")
    target = f"publisher:{row.publisher} signer:{row.signer}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PLUGIN_PUBLISHER_REMOVE, target=target
    )


async def list_publishers(conn: AsyncConnection, config: HubConfig) -> list[PluginPublisher]:
    rows = await repository.trusted_rows(conn)
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


async def all_publishers(engine: AsyncEngine, config: HubConfig) -> list[PluginPublisher]:
    """``list_publishers``, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await list_publishers(conn, config)


async def trust_and_list(
    engine: AsyncEngine, user: Principal, config: HubConfig, publisher: str, signer: str, note: str | None
) -> list[PluginPublisher]:
    """``trust`` ``signer`` for ``publisher``, then the publishers, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        await trust(conn, user, config, publisher, signer, note)
        return await list_publishers(conn, config)


async def remove_and_list(
    engine: AsyncEngine, user: Principal, config: HubConfig, entry_id: int
) -> list[PluginPublisher]:
    """``remove`` signer ``entry_id``, then the publishers, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        await remove(conn, user, entry_id)
        return await list_publishers(conn, config)
