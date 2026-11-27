"""The SQL of the quota of a Z.ai provider account (``evo_agents.hub.server.accounts.service.quota`` says how the hub
reads it): the live Z.ai accounts with their sealed keys, and the usage kept as an account's last, with the cooldown
it sets or ends."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.quota import ZAI_PROVIDER
from evo_agents.hub.server.accounts.models.quota import ZAI_REASON
from evo_agents.hub.server.accounts.repository.choice import cool


def _rows(*where):
    s, p = tables.secrets, tables.provider_accounts
    return (
        select(s.c.id, s.c.owner_id, s.c.profile_id, s.c.name, s.c.sealed, s.c.nonce, s.c.key_id, p.c.base_url)
        .join_from(s, p, p.c.secret_id == s.c.id)
        .where(s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None), p.c.provider == ZAI_PROVIDER, *where)
        .order_by(s.c.id)
    )


async def zai_account(conn: AsyncConnection, secret_id: int):
    """The row of the live Z.ai account ``secret_id`` with its sealed key and base URL; None when there is none."""
    return (await conn.execute(_rows(tables.secrets.c.id == secret_id))).one_or_none()


async def zai_ids(conn: AsyncConnection) -> list[int]:
    """The secret ids of every live Z.ai account."""
    return list((await conn.execute(_rows().with_only_columns(tables.secrets.c.id))).scalars())


async def keep(conn: AsyncConnection, secret_id: int, usage: dict) -> None:
    """Keep ``usage`` as the account's last, and cool it down or end that cooldown, as ``service.quota`` says."""
    a = tables.accounts
    values = {"last_usage": usage, "last_usage_at": func.now()}
    statement = insert(a).values(secret_id=secret_id, **values)
    await conn.execute(statement.on_conflict_do_update(index_elements=[a.c.secret_id], set_=values))
    resets_at = usage.get("resets_at")
    if usage["status"] == "rejected" and resets_at:
        until = datetime.fromisoformat(resets_at)
        if until > datetime.now(UTC):
            await cool(conn, secret_id, until, f"{ZAI_REASON}: {usage.get('rate_limit_type')}")
        return
    await conn.execute(
        update(a)
        .where(a.c.secret_id == secret_id, a.c.cooldown_reason.startswith(ZAI_REASON))
        .values(cooldown_until=None, cooldown_reason=None, updated_at=func.now())
    )
