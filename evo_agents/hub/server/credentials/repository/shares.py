"""The queries of the shares of secrets and accounts (``service.shares``): the owner's secret, its bound projects,
its live shares, and the rows of credential_shares and credential_share_projects a share writes."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.share_views import CredentialShare, share_of, share_query

NOW = func.now()  # what terms_ack_at of a share whose owner acknowledged the terms warning takes


async def own_secret(conn: AsyncConnection, owner_id: int, name: str, *, lock: bool = False):
    """(id, kind, env_var) of the live secret ``name`` of ``owner_id``, locked with ``lock``; None for none."""
    s = tables.secrets
    query = select(s.c.id, s.c.kind, s.c.env_var).where(
        s.c.owner_id == owner_id, s.c.name == name, s.c.deleted_at.is_(None)
    )
    return (await conn.execute(query.with_for_update() if lock else query)).first()


async def share(conn: AsyncConnection, share_id: int) -> CredentialShare:
    cs = tables.credential_shares
    return share_of((await conn.execute(share_query().where(cs.c.id == share_id))).one())


async def shares_of_secret(conn: AsyncConnection, secret_id: int) -> list[CredentialShare]:
    """The shares of secret ``secret_id``, revoked ones too, newest first."""
    cs = tables.credential_shares
    return [share_of(row) for row in (await conn.execute(share_query().where(cs.c.secret_id == secret_id))).all()]


async def bound_projects(conn: AsyncConnection, secret_id: int) -> dict[str, int]:
    """Name -> id of the projects the secret is bound to now."""
    b, p = tables.secret_bindings, tables.projects
    bound = select(p.c.name, p.c.id).distinct().join_from(b, p, p.c.id == b.c.project_id)
    return {row.name: row.id for row in await conn.execute(bound.where(b.c.secret_id == secret_id))}


async def live_grantees(conn: AsyncConnection, secret_id: int) -> list[tuple]:
    """(grantee_kind, grantee_user_id, grantee_unit_id) of each live share of the secret."""
    cs = tables.credential_shares
    live = select(cs.c.grantee_kind, cs.c.grantee_user_id, cs.c.grantee_unit_id).where(
        cs.c.secret_id == secret_id, cs.c.revoked_at.is_(None)
    )
    return [tuple(row) for row in (await conn.execute(live)).all()]


async def insert_share(conn: AsyncConnection, values: dict, project_ids: list[int]) -> int:
    """A new share of ``values`` for ``project_ids``; its id."""
    cs, csp = tables.credential_shares, tables.credential_share_projects
    made = insert(cs).values(**values).returning(cs.c.id)
    share_id = (await conn.execute(made)).scalar_one()
    await conn.execute(insert(csp), [{"share_id": share_id, "project_id": project_id} for project_id in project_ids])
    return share_id


async def share_to_revoke(conn: AsyncConnection, share_id: int):
    """(secret_id, revoked_at, owner_id) of share ``share_id``, its row locked; None for none."""
    cs, s = tables.credential_shares, tables.secrets
    found = select(cs.c.secret_id, cs.c.revoked_at, s.c.owner_id).join_from(cs, s, s.c.id == cs.c.secret_id)
    return (await conn.execute(found.where(cs.c.id == share_id).with_for_update(of=cs))).first()


async def revoke(conn: AsyncConnection, share_id: int, actor_id: int) -> None:
    cs = tables.credential_shares
    await conn.execute(update(cs).values(revoked_at=func.now(), revoked_by=actor_id).where(cs.c.id == share_id))
