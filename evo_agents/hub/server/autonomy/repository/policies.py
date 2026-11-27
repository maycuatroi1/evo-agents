"""Every query of a project's autonomy policy, table autonomy_policies (schema 0079), and of the repos the project
lists, which the policy names."""

from __future__ import annotations

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy, tables


def _revisions():
    a, u = tables.autonomy_policies, tables.users
    return select(a.c.revision, a.c.body, a.c.note, u.c.login.label("created_by"), a.c.created_at).join_from(
        a, u, u.c.id == a.c.created_by
    )


async def newest(conn: AsyncConnection, project_id: int):
    """The newest revision of the project's policy (revision, body, note, created_by, created_at); None without one."""
    a = tables.autonomy_policies
    query = _revisions().where(a.c.project_id == project_id).order_by(a.c.revision.desc()).limit(1)
    return (await conn.execute(query)).first()


async def revision(conn: AsyncConnection, project_id: int, number: int):
    """Revision ``number`` of the project's policy, as ``newest`` reads one; None when it has no such revision."""
    a = tables.autonomy_policies
    query = _revisions().where(a.c.project_id == project_id, a.c.revision == number)
    return (await conn.execute(query)).first()


async def revisions(conn: AsyncConnection, project_id: int):
    """Every revision of the project's policy, as ``newest`` reads one, the oldest first."""
    a = tables.autonomy_policies
    query = _revisions().where(a.c.project_id == project_id).order_by(a.c.revision)
    return (await conn.execute(query)).all()


async def body_at(conn: AsyncConnection, project_id: int, revision: int) -> dict | None:
    """What revision ``revision`` of the project's policy sets: {} for revision 0, None when there is no such one."""
    if revision == 0:
        return {}
    a = tables.autonomy_policies
    query = select(a.c.body).where(a.c.project_id == project_id, a.c.revision == revision)
    return (await conn.execute(query)).scalar_one_or_none()


async def project_repos(conn: AsyncConnection, project_id: int) -> dict[str, autonomy.RepoFacts]:
    """The repos the project lists, with their origin and default branch."""
    r = tables.project_repos
    query = select(r.c.name, r.c.origin, r.c.default_branch).where(r.c.project_id == project_id)
    return {row.name: autonomy.RepoFacts(row.origin, row.default_branch) for row in await conn.execute(query)}


async def lock(conn: AsyncConnection, project_id: int) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the project's policy."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"autonomy_policy:{project_id}", 0))))


async def add(conn: AsyncConnection, values: dict) -> None:
    """Insert the revision ``values`` names (project_id, revision, body, note, created_by)."""
    await conn.execute(insert(tables.autonomy_policies).values(values))
