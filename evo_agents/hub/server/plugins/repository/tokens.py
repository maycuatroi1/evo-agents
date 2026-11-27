"""The queries of the plugin tokens (table plugin_tokens, schema 0036): only their SHA-256 is kept."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def revoke_tokens(conn: AsyncConnection, plugin_id: int) -> None:
    """Revoke every live token of plugin ``plugin_id``: when it is removed or turned off, and before a new one."""
    pt = tables.plugin_tokens
    await conn.execute(
        update(pt).where(pt.c.plugin_id == plugin_id, pt.c.revoked_at.is_(None)).values(revoked_at=func.now())
    )


async def add_token(conn: AsyncConnection, plugin_id: int, token_hash: str) -> None:
    await conn.execute(insert(tables.plugin_tokens).values(plugin_id=plugin_id, token_hash=token_hash))


async def plugin_of_hash(conn: AsyncConnection, token_hash: str) -> int | None:
    """The plugin id of the live token whose hash is ``token_hash``, its plugin installed and not turned off."""
    pt, p = tables.plugin_tokens, tables.plugins
    query = (
        select(pt.c.plugin_id)
        .join(p, p.c.id == pt.c.plugin_id)
        .where(
            pt.c.token_hash == token_hash,
            pt.c.revoked_at.is_(None),
            p.c.installed_version.is_not(None),
            p.c.disabled_reason.is_(None),
        )
    )
    return (await conn.execute(query)).scalar_one_or_none()
