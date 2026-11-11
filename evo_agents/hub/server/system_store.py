"""Agent systems in the database (``evo_agents.hub.tables_systems``): a system as the hub holds it, its revisions, what
of the project its spec names, and the version a system pins for its entry agent, for the routes of
``agent_systems`` and for the chat runs (``chat_runs``).

A spec is checked (``system_spec.check`` and ``check_refs``) and pinned (``system_spec.pin``) before it is written; the
store writes it whole as the next revision, with its revision row and the profile that is its entry agent, in the
caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_spec, tables


@dataclass(frozen=True)
class System:
    id: int
    slug: str
    name: str
    spec: dict
    revision: int
    entry_profile_id: int | None
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    is_default: bool = False  # the project's default system (0237)
    ops_paused_at: datetime | None = None  # its ops hold their batches since then (0237)
    ops_paused_reason: str | None = None


@dataclass(frozen=True)
class Refs:
    """What of the project a spec may name: its profiles by handle, the names of its named graphs, and the profiles
    that are the entry agent of a system, by handle, with the slug of that system."""

    profiles: dict[str, system_spec.ProfileRef]
    ids: dict[str, int]  # the id of each profile, by handle
    graphs: set[str]
    entries: dict[str, str]

    def entries_but(self, slug: str) -> dict[str, str]:
        return {handle: held for handle, held in self.entries.items() if held != slug}


def _select():
    s, u1, u2 = tables.agent_systems, tables.users.alias("creator"), tables.users.alias("changer")
    return select(
        s.c.id,
        s.c.slug,
        s.c.name,
        s.c.spec,
        s.c.revision,
        s.c.entry_profile_id,
        select(u1.c.login).where(u1.c.id == s.c.created_by).scalar_subquery().label("created_by"),
        select(u2.c.login).where(u2.c.id == s.c.updated_by).scalar_subquery().label("updated_by"),
        s.c.created_at,
        s.c.updated_at,
        s.c.is_default,
        s.c.ops_paused_at,
        s.c.ops_paused_reason,
    )


async def listed(conn: AsyncConnection, project_id: int) -> list[System]:
    s = tables.agent_systems
    rows = await conn.execute(_select().where(s.c.project_id == project_id).order_by(s.c.slug))
    return [System(**row._mapping) for row in rows]


async def one(conn: AsyncConnection, project_id: int, slug: str, *, lock: bool = False) -> System | None:
    s = tables.agent_systems
    query = _select().where(s.c.project_id == project_id, s.c.slug == slug)
    row = (await conn.execute(query.with_for_update(of=s) if lock else query)).one_or_none()
    return None if row is None else System(**row._mapping)


async def default_of(conn: AsyncConnection, project_id: int, *, lock: bool = False) -> System | None:
    """The project's default system, or None when it has none."""
    s = tables.agent_systems
    query = _select().where(s.c.project_id == project_id, s.c.is_default)
    row = (await conn.execute(query.with_for_update(of=s) if lock else query)).one_or_none()
    return None if row is None else System(**row._mapping)


async def set_default(conn: AsyncConnection, project_id: int, system_id: int) -> None:
    """Make system ``system_id`` the project's default, and no other; the caller locked the project's default."""
    s = tables.agent_systems
    await conn.execute(update(s).values(is_default=False).where(s.c.project_id == project_id, s.c.is_default))
    await conn.execute(update(s).values(is_default=True).where(s.c.id == system_id))


async def set_paused(conn: AsyncConnection, system_id: int, reason: str | None) -> None:
    """Pause the ops of system ``system_id`` for ``reason``, or resume them when it is None."""
    s = tables.agent_systems
    paused_at = None if reason is None else func.coalesce(s.c.ops_paused_at, func.now())
    await conn.execute(update(s).values(ops_paused_at=paused_at, ops_paused_reason=reason).where(s.c.id == system_id))


async def revision_spec(conn: AsyncConnection, system_id: int, revision: int) -> dict | None:
    r = tables.agent_system_revisions
    query = select(r.c.spec).where(r.c.system_id == system_id, r.c.revision == revision)
    return (await conn.execute(query)).scalar_one_or_none()


async def revisions(conn: AsyncConnection, system_id: int) -> list[dict]:
    """Every revision of a system, the newest first: ``{revision, actor, created_at}``."""
    r, u = tables.agent_system_revisions, tables.users
    query = (
        select(r.c.revision, u.c.login.label("actor"), r.c.created_at)
        .join_from(r, u, u.c.id == r.c.actor_id, isouter=True)
        .where(r.c.system_id == system_id)
        .order_by(r.c.revision.desc())
    )
    return [dict(row._mapping) for row in await conn.execute(query)]


async def refs(conn: AsyncConnection, project_id: int) -> Refs:
    ap, g, s = tables.agent_profiles, tables.kg_named_graphs, tables.agent_systems
    rows = (
        await conn.execute(
            select(ap.c.id, ap.c.handle, ap.c.version, ap.c.deleted_at).where(ap.c.project_id == project_id)
        )
    ).all()
    graphs = set((await conn.execute(select(g.c.name).where(g.c.project_id == project_id))).scalars())
    entries = await conn.execute(
        select(ap.c.handle, s.c.slug)
        .join_from(s, ap, ap.c.id == s.c.entry_profile_id)
        .where(s.c.project_id == project_id)
    )
    return Refs(
        profiles={r.handle: system_spec.ProfileRef(r.version, r.deleted_at is not None) for r in rows},
        ids={r.handle: r.id for r in rows},
        graphs=graphs,
        entries={r.handle: r.slug for r in entries},
    )


async def write(
    conn: AsyncConnection,
    project_id: int,
    slug: str,
    spec: dict,
    entry_profile_id: int | None,
    user_id: int,
    held: System | None,
) -> int:
    """Store ``spec`` as the next revision of system ``slug`` (revision 1 when ``held`` is None); the revision."""
    s, r = tables.agent_systems, tables.agent_system_revisions
    values = {"name": spec["name"], "spec": spec, "entry_profile_id": entry_profile_id, "updated_by": user_id}
    if held is None:
        revision = 1
        statement = insert(s).values(project_id=project_id, slug=slug, created_by=user_id, revision=1, **values)
        system_id = (await conn.execute(statement.returning(s.c.id))).scalar_one()
    else:
        system_id, revision = held.id, held.revision + 1
        await conn.execute(
            update(s).values(**values, revision=revision, updated_at=func.now()).where(s.c.id == held.id)
        )
    await conn.execute(insert(r).values(system_id=system_id, revision=revision, spec=spec, actor_id=user_id))
    return revision


async def remove(conn: AsyncConnection, system_id: int) -> None:
    await conn.execute(delete(tables.agent_systems).where(tables.agent_systems.c.id == system_id))


async def entry_pin(conn: AsyncConnection, profile_id: int) -> tuple[str, int] | None:
    """The system whose entry agent is profile ``profile_id`` and the version of the profile it pins: (slug, version);
    None when the profile is the entry agent of no system, or when that system pins no version of it."""
    s, ap = tables.agent_systems, tables.agent_profiles
    query = (
        select(s.c.slug, s.c.spec, ap.c.handle)
        .join_from(s, ap, ap.c.id == s.c.entry_profile_id)
        .where(s.c.entry_profile_id == profile_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    for pin in system_spec.pins(row.spec):
        if pin["profile"] == row.handle and pin["kind"] == "agent" and pin["version"]:
            return row.slug, int(pin["version"])
    return None
