"""The query of the sync of the model gateway (``service.sync``): the live provider accounts, with their sealed keys."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND


async def provider_accounts(engine: AsyncEngine) -> list:
    """Every live provider account, its key sealed, with its catalog provider, base URL, API format, pause and end."""
    s, p, a = tables.secrets, tables.provider_accounts, tables.accounts
    statement = (
        select(s.c.id, s.c.owner_id, s.c.profile_id, s.c.name, s.c.sealed, s.c.nonce, s.c.key_id, s.c.expires_at)
        .add_columns(p.c.provider, p.c.base_url, p.c.api_format, a.c.paused_at, func.now().label("now"))
        .join_from(s, p, p.c.secret_id == s.c.id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None))
        .order_by(s.c.id)
    )
    async with engine.connect() as conn:
        return list((await conn.execute(statement)).all())
