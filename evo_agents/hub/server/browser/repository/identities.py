"""The rows of browser_identities as a project's routes read and write them: the listing with the names it shows, the
workers and agent profiles an identity may name, the names taken, and the writes of a create, a default and a
status."""

from __future__ import annotations

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from evo_agents.hub import tables


def listing(project_id: int):
    """The live identities of the project, joined to the names they show, ordered by owner then name."""
    b, w, u, ap = tables.browser_identities, tables.workers, tables.users, tables.agent_profiles
    owner, saver, worker_owner = aliased(u), aliased(u), aliased(u)
    return (
        select(
            b,
            owner.c.login.label("owner_login"),
            saver.c.login.label("saver_login"),
            w.c.name.label("worker_name"),
            worker_owner.c.login.label("worker_owner_login"),
            ap.c.handle.label("agent_handle"),
        )
        .join(w, w.c.id == b.c.worker_id)
        .join(worker_owner, worker_owner.c.id == w.c.owner_id)
        .outerjoin(owner, owner.c.id == b.c.owner_id)
        .outerjoin(ap, ap.c.id == b.c.profile_id)
        .outerjoin(saver, saver.c.id == b.c.last_saved_by)
        .where(b.c.project_id == project_id, b.c.deleted_at.is_(None))
        .order_by(b.c.owner_kind, b.c.name, b.c.id)
    )


async def listed(conn: AsyncConnection, project_id: int, user_id: int, *, agents: bool) -> list:
    """The project's identities and the member's own in it, with its agents' when ``agents``, as ``listing`` reads
    them."""
    b = tables.browser_identities
    mine = or_(b.c.owner_kind == "project", b.c.owner_id == user_id)
    if agents:
        mine = or_(mine, b.c.owner_kind == "agent")
    return list((await conn.execute(listing(project_id).where(mine))).all())


async def listed_one(conn: AsyncConnection, project_id: int, identity_id: int):
    """Identity ``identity_id`` of the project as ``listing`` reads it."""
    b = tables.browser_identities
    return (await conn.execute(listing(project_id).where(b.c.id == identity_id))).one()


async def usable_workers(conn: AsyncConnection, user_id: int, project_id: int, name: str) -> list:
    """The workers named ``name``, not revoked, the member's own or shared, that serve the project: their id, owner
    and features."""
    w, wp = tables.workers, tables.worker_projects
    usable = select(w.c.id, w.c.owner_id, w.c.features).where(
        w.c.revoked_at.is_(None),
        func.lower(w.c.name) == name.lower(),
        or_(w.c.owner_id == user_id, w.c.shared.is_(True)),
        select(wp.c.worker_id).where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id).exists(),
    )
    return list((await conn.execute(usable)).all())


async def agent_profile(conn: AsyncConnection, project_id: int, handle: str):
    """Agent profile ``handle`` of the project, its row locked: its id, whether it is the default and when it was
    deleted; None for none."""
    ap = tables.agent_profiles
    query = (
        select(ap.c.id, ap.c.is_default, ap.c.deleted_at)
        .where(ap.c.project_id == project_id, ap.c.handle == handle)
        .with_for_update(of=ap)
    )
    return (await conn.execute(query)).one_or_none()


async def has_default(conn: AsyncConnection, profile_id: int) -> bool:
    """Whether agent profile ``profile_id`` has a live identity it hands its runs."""
    b = tables.browser_identities
    live = select(b.c.id).where(b.c.profile_id == profile_id, b.c.profile_default, b.c.deleted_at.is_(None))
    return (await conn.execute(live)).first() is not None


async def name_taken(
    conn: AsyncConnection, project_id: int, owner: str, name: str, *, user_id: int, profile_id: int | None
) -> bool:
    """Whether the owner (the member ``user_id``, the project, or agent profile ``profile_id``) has a live identity
    named ``name`` in the project."""
    b = tables.browser_identities
    if owner == "agent":
        owned = b.c.profile_id == profile_id
    else:
        owned = b.c.owner_kind == "project" if owner == "project" else b.c.owner_id == user_id
    same = select(b.c.id).where(
        b.c.project_id == project_id, b.c.deleted_at.is_(None), owned, b.c.owner_kind == owner, b.c.name == name
    )
    return (await conn.execute(same)).first() is not None


async def live_count(conn: AsyncConnection, project_id: int) -> int:
    """How many live identities the project has."""
    b = tables.browser_identities
    live = select(func.count()).select_from(b).where(b.c.project_id == project_id, b.c.deleted_at.is_(None))
    return (await conn.execute(live)).scalar_one()


async def inserted(conn: AsyncConnection, row: dict) -> int:
    """Insert identity ``row``; its id. IntegrityError when another of that name was created meanwhile."""
    b = tables.browser_identities
    return (await conn.execute(insert(b).values(row).returning(b.c.id))).scalar_one()


async def make_default(conn: AsyncConnection, profile_id: int, identity_id: int) -> None:
    """Make identity ``identity_id`` the one agent profile ``profile_id`` hands its runs, in place of the one that
    was."""
    b = tables.browser_identities
    off = update(b).values(profile_default=False, updated_at=func.now())
    await conn.execute(off.where(b.c.profile_id == profile_id, b.c.id != identity_id, b.c.profile_default))
    await conn.execute(update(b).values(profile_default=True, updated_at=func.now()).where(b.c.id == identity_id))


async def set_status(conn: AsyncConnection, identity_id: int, status: str) -> None:
    b = tables.browser_identities
    await conn.execute(update(b).values(status=status, updated_at=func.now()).where(b.c.id == identity_id))


async def of_profile(conn: AsyncConnection, profile_id: int) -> list[int]:
    """The ids of the live identities of agent profile ``profile_id``."""
    b = tables.browser_identities
    query = select(b.c.id).where(b.c.profile_id == profile_id, b.c.deleted_at.is_(None))
    return list((await conn.execute(query)).scalars().all())


async def row(conn: AsyncConnection, identity_id: int, *, lock: bool = False):
    """The row of identity ``identity_id``, locked with ``lock``; None for none."""
    b = tables.browser_identities
    query = select(b).where(b.c.id == identity_id)
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()


async def named(conn: AsyncConnection, project_id: int, owner: str, name: str, *, user_id: int, profile_id: int | None):
    """The live identity ``name`` of the owner (the member ``user_id``, the project, or agent profile
    ``profile_id``) in the project, its row locked; None for none."""
    b = tables.browser_identities
    query = select(b).where(
        b.c.project_id == project_id,
        b.c.deleted_at.is_(None),
        b.c.owner_kind == owner,
        b.c.name == name,
    )
    if owner == "member":
        query = query.where(b.c.owner_id == user_id)
    elif owner == "agent":
        query = query.where(b.c.profile_id == profile_id)
    return (await conn.execute(query.with_for_update())).one_or_none()


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()
