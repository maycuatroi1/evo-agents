"""The SQL of a project's forge secrets (``evo_agents.hub.server.forge.service.secrets`` says what they are): the ones
the hub may open for a project."""

from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import FORGE_KIND

FORGE_ROLE = "admin"  # the role on a project a forge secret's owner holds, when they write it and when it is used


def _usable(project_id: int):
    """The forge secrets bound to ``project_id``, live, not past their end, of an owner who holds FORGE_ROLE on it."""
    s, b, g = tables.secrets, tables.secret_bindings, tables.grants
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    admin = exists().where(g.c.user_id == s.c.owner_id, g.c.project_id == project_id, g.c.role == FORGE_ROLE)
    return select(
        s.c.id,
        s.c.owner_id,
        s.c.name,
        s.c.url_prefix,
        s.c.sealed,
        s.c.nonce,
        s.c.key_id,
        s.c.expires_at,
        s.c.updated_at,
    ).where(
        s.c.kind == FORGE_KIND,
        s.c.deleted_at.is_(None),
        s.c.expires_at > func.now(),
        bound,
        admin,
    )


async def usable_secrets(conn: AsyncConnection, project_id: int) -> list:
    """The forge secrets of project ``project_id`` the hub may open, sealed, each with its url_prefix and its end."""
    return list(await conn.execute(_usable(project_id)))
