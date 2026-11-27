"""The queries of workers on ``tables.workers`` and ``tables.worker_projects``: what a Worker shows of each, who
sees one another member shares, the worker a caller owns, its name, its settings, and its revocation."""

from __future__ import annotations

from typing import Any

import psycopg
from sqlalchemy import BigInteger, Text, exists, func, insert, literal, or_, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import browser_install, update_now
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.runs.service.worker_fit import unit_admits, writes
from evo_agents.hub.server.workers.repository.placement import all_labels, pool_name
from evo_agents.hub.server.workers.repository.placement import columns as placement_columns

SETTINGS = ("slots", "labels", "taints", "name", "plugins", "version_locked", "shared")  # PATCH's, but projects


def listed(*where):
    """The workers that match ``where``, newest first, with what a Worker shows of each and the database's now."""
    w, users, wp, projects = tables.workers, tables.users, tables.worker_projects, tables.projects
    names = (
        select(projects.c.name)
        .join_from(wp, projects, projects.c.id == wp.c.project_id)
        .where(wp.c.worker_id == w.c.id)
        .order_by(projects.c.name)
    )
    held_runs = (
        select(func.count())
        .select_from(tables.runs)
        .where(tables.runs.c.worker_id == w.c.id, tables.runs.c.state.in_(runs.HELD_STATES))
        .scalar_subquery()
    )
    return (
        select(
            w.c.id,
            w.c.name,
            users.c.login.label("owner"),
            w.c.hostname,
            w.c.os,
            w.c.arch,
            w.c.agent_version,
            w.c.agent_commit,
            w.c.slots,
            all_labels(w).label("labels"),
            pool_name(w).label("pool"),
            w.c.plugins,
            w.c.version_locked,
            w.c.shared,
            w.c.self_update,
            w.c.self_update_since,
            w.c.kg_sync,
            w.c.browser,
            *update_now.update_columns(),
            *browser_install.columns(),
            *update_now.nightly_columns(),
            func.array(names.scalar_subquery(), type_=ARRAY(Text)).label("projects"),
            w.c.runtimes,
            w.c.checkouts,
            w.c.free_slots,
            w.c.health,
            w.c.allow_web_terminal,
            w.c.dispatch_from,
            held_runs.label("held_runs"),
            w.c.features,
            w.c.created_at,
            w.c.last_heartbeat_at,
            w.c.drained_at,
            w.c.revoked_at,
            func.now().label("now"),
            *placement_columns(w),
        )
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(*where)
        .order_by(w.c.created_at.desc(), w.c.id.desc())
    )


async def rows(conn: AsyncConnection, *where) -> list:
    return list(await conn.execute(listed(*where)))


async def paged(conn: AsyncConnection, page: Paging, *where) -> list:
    return list(await page.rows(conn, listed(*where)))


def shared_with(user_id: int):
    """Whether the worker of the row is one another member shares with ``user_id``: shared, serving a project on which
    both its owner and ``user_id`` hold writer now, and, when narrowed to units, one ``user_id`` is seated in
    (``worker_fit.unit_admits``; ``docs/workers.md``, Shared workers)."""
    w, wp = tables.workers, tables.worker_projects
    both = exists().where(
        wp.c.worker_id == w.c.id, writes(user_id, wp.c.project_id), writes(w.c.owner_id, wp.c.project_id)
    )
    return (w.c.owner_id != user_id) & w.c.shared & both & unit_admits(w.c.id, user_id)


def visible_to(user_id: int, *, revoked: bool) -> list:
    """The conditions of the workers member ``user_id`` lists: their own, and the live ones shared with them; with
    ``revoked`` false, none that is revoked."""
    w = tables.workers
    where = [or_(w.c.owner_id == user_id, shared_with(user_id) & w.c.revoked_at.is_(None))]
    return where if revoked else [*where, w.c.revoked_at.is_(None)]


def live(revoked: bool) -> list:
    """The conditions of every worker a hub admin lists: with ``revoked`` false, none that is revoked."""
    return [] if revoked else [tables.workers.c.revoked_at.is_(None)]


def by_id(worker_id: int):
    return tables.workers.c.id == worker_id


async def is_shared_with(conn: AsyncConnection, worker_id: int, user_id: int) -> bool:
    """Whether worker ``worker_id`` is live and shared with ``user_id`` by another member (``shared_with``)."""
    w = tables.workers
    shared = select(exists().where(w.c.id == worker_id, w.c.revoked_at.is_(None), shared_with(user_id)))
    return (await conn.execute(shared)).scalar_one()


async def granted_names(conn: AsyncConnection, user_id: int) -> set[str]:
    """The names of the projects on which ``user_id`` holds a grant."""
    p, g = tables.projects, tables.grants
    names = select(p.c.name).join_from(g, p, p.c.id == g.c.project_id).where(g.c.user_id == user_id)
    return set((await conn.execute(names)).scalars())


async def owner_row(conn: AsyncConnection, worker_id: int, *, lock: bool = False):
    """(owner_id, owner login, name, token_id, drained_at, revoked_at) of worker ``worker_id``, its row locked with
    ``lock``; None for none."""
    w, users = tables.workers, tables.users
    statement = (
        select(w.c.owner_id, users.c.login, w.c.name, w.c.token_id, w.c.drained_at, w.c.revoked_at)
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.id == worker_id)
    )
    if lock:
        statement = statement.with_for_update(of=w)
    return (await conn.execute(statement)).one_or_none()


async def name_taken(conn: AsyncConnection, owner_id: int, name: str, *, besides: int | None = None) -> bool:
    """Whether a worker of ``owner_id`` that is not revoked, other than worker ``besides``, is named ``name`` in any
    case."""
    w = tables.workers
    taken = exists().where(w.c.owner_id == owner_id, func.lower(w.c.name) == func.lower(name), w.c.revoked_at.is_(None))
    if besides is not None:
        taken = taken.where(w.c.id != besides)
    return (await conn.execute(select(taken))).scalar_one()


def _unique_violation(exc: sa_exc.IntegrityError) -> bool:
    return isinstance(exc.orig, psycopg.errors.UniqueViolation)


async def insert_worker(conn: AsyncConnection, values: dict[str, Any], project_ids) -> int | None:
    """A new worker of ``values`` serving ``project_ids``; its id, or None when another worker took its name
    meanwhile."""
    w, wp = tables.workers, tables.worker_projects
    try:
        worker_id = (await conn.execute(insert(w).values(**values).returning(w.c.id))).scalar_one()
    except sa_exc.IntegrityError as exc:
        if not _unique_violation(exc):
            raise
        return None
    projects = literal(list(project_ids), ARRAY(BigInteger))
    await conn.execute(
        insert(wp).from_select(
            ["worker_id", "project_id"], select(literal(worker_id, BigInteger), func.unnest(projects))
        )
    )
    return worker_id


async def set_drained(conn: AsyncConnection, worker_id: int, drain: bool) -> None:
    w = tables.workers
    await conn.execute(update(w).values(drained_at=func.now() if drain else None).where(w.c.id == worker_id))


async def dispatch_from(conn: AsyncConnection, worker_id: int) -> str:
    w = tables.workers
    return (await conn.execute(select(w.c.dispatch_from).where(w.c.id == worker_id))).scalar_one()


async def set_dispatch_from(conn: AsyncConnection, worker_id: int, value: str) -> None:
    w = tables.workers
    await conn.execute(update(w).values(dispatch_from=value).where(w.c.id == worker_id))


async def settings(conn: AsyncConnection, worker_id: int) -> dict[str, Any]:
    """The settings of ``SETTINGS`` the worker has now, lists as lists."""
    w = tables.workers
    row = (await conn.execute(select(*(w.c[name] for name in SETTINGS)).where(w.c.id == worker_id))).one()
    return {
        name: list(value) if name in ("labels", "taints", "plugins") else value for name, value in row._mapping.items()
    }


async def set_settings(conn: AsyncConnection, worker_id: int, columns: dict[str, Any]) -> bool:
    """Write ``columns`` on the worker's row; False when another worker took the name it gives meanwhile."""
    w = tables.workers
    try:
        await conn.execute(update(w).values(**columns).where(w.c.id == worker_id))
    except sa_exc.IntegrityError as exc:
        if not _unique_violation(exc):
            raise
        return False
    return True


async def lock_worker_of_token(
    conn: AsyncConnection, token_id: int, owner_id: int | None = None
) -> tuple[int, str, str] | None:
    """(id, name, owner login) of the live worker whose token is ``token_id``, of ``owner_id`` when given, with its
    row locked; None for any other token. Called before the token's row is changed, so a token revocation locks the
    two rows in the order POST /v1/workers/{id}/revoke does, and neither waits on the other in the opposite order."""
    w, users = tables.workers, tables.users
    statement = (
        select(w.c.id, w.c.name, users.c.login)
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.token_id == token_id, w.c.revoked_at.is_(None))
    )
    if owner_id is not None:
        statement = statement.where(w.c.owner_id == owner_id)
    row = (await conn.execute(statement.with_for_update(of=w))).one_or_none()
    return None if row is None else (row[0], row[1], row[2])


async def revoke(conn: AsyncConnection, worker_id: int, token_id: int) -> None:
    """Mark the worker revoked, and its token unless it is revoked already."""
    w, tokens = tables.workers, tables.tokens
    await conn.execute(update(w).values(revoked_at=func.now()).where(w.c.id == worker_id))
    await conn.execute(
        update(tokens).values(revoked_at=func.now()).where(tokens.c.id == token_id, tokens.c.revoked_at.is_(None))
    )
