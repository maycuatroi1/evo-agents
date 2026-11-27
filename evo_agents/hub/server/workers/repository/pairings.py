"""The queries of pairings on ``tables.worker_pairings``: how many a member holds, a new one under a free selector,
one's state, the unused one a code's selector finds, its wrong tries and its use."""

from __future__ import annotations

from sqlalchemy import BigInteger, any_, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.workers.models.codes import PAIRING_KEPT, PAIRING_TTL, SELECTOR_LENGTH
from evo_agents.hub.server.workers.models.workers import PairingRequest


async def lock_member(conn: AsyncConnection, user_id: int) -> None:
    """Lock the member's row: one pairing of a member at a time, so two of them cannot both pass the count."""
    users = tables.users
    await conn.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())


async def prune(conn: AsyncConnection) -> None:
    """Delete the unused pairings whose expiry is PAIRING_KEPT behind."""
    wp = tables.worker_pairings
    await conn.execute(delete(wp).where(wp.c.used_at.is_(None), wp.c.expires_at < func.now() - PAIRING_KEPT))


async def live_count(conn: AsyncConnection, owner_id: int) -> int:
    """The pairings of ``owner_id`` neither used nor expired. A locked code holds its selector until it is pruned, so
    it counts as live until it expires: a member cannot take more selectors by locking codes with wrong tries."""
    wp = tables.worker_pairings
    live = (
        select(func.count())
        .select_from(wp)
        .where(wp.c.owner_id == owner_id, wp.c.used_at.is_(None), wp.c.expires_at > func.now())
    )
    return (await conn.execute(live)).scalar_one()


async def insert(
    conn: AsyncConnection, code: str, digest: str, owner_id: int, body: PairingRequest, project_ids, labels
):
    """(id, expires_at) of a new pairing of ``code``, kept as ``digest``; None when an unused pairing holds its
    selector already."""
    wp = tables.worker_pairings
    statement = pg_insert(wp).values(
        code_selector=code[:SELECTOR_LENGTH],
        code_hash=digest,
        owner_id=owner_id,
        name=body.name,
        projects=project_ids,
        slots=body.slots,
        labels=labels,
        allow_web_terminal=body.allow_web_terminal,
        expires_at=func.now() + PAIRING_TTL,
    )
    statement = statement.on_conflict_do_nothing(
        index_elements=[wp.c.code_selector], index_where=wp.c.used_at.is_(None)
    ).returning(wp.c.id, wp.c.expires_at)
    return (await conn.execute(statement)).one_or_none()


async def state(conn: AsyncConnection, pairing_id: int):
    """What GET /v1/workers/pairings/{id} shows of pairing ``pairing_id``, with whether it expired; None for none."""
    wp = tables.worker_pairings
    statement = select(
        wp.c.owner_id,
        wp.c.name,
        wp.c.projects,
        wp.c.slots,
        wp.c.labels,
        wp.c.allow_web_terminal,
        wp.c.attempts,
        wp.c.created_at,
        wp.c.expires_at,
        wp.c.used_at,
        wp.c.worker_id,
        (wp.c.expires_at <= func.now()).label("expired"),
    ).where(wp.c.id == pairing_id)
    return (await conn.execute(statement)).one_or_none()


async def project_names(conn: AsyncConnection, project_ids: list[int]) -> list[str]:
    """The names of the projects ``project_ids``, in their order."""
    projects = tables.projects
    ids = literal(list(project_ids), ARRAY(BigInteger))
    names = select(projects.c.name).where(projects.c.id == any_(ids)).order_by(func.array_position(ids, projects.c.id))
    return list((await conn.execute(names)).scalars())


async def find(conn: AsyncConnection, selector: str):
    """The unused pairing that holds ``selector``, its row locked, and whether it has not expired (``live``)."""
    wp = tables.worker_pairings
    statement = (
        select(
            wp.c.id,
            wp.c.code_hash,
            wp.c.owner_id,
            wp.c.name,
            wp.c.projects,
            wp.c.slots,
            wp.c.labels,
            wp.c.allow_web_terminal,
            wp.c.attempts,
            (wp.c.expires_at > func.now()).label("live"),
        )
        .where(wp.c.code_selector == selector, wp.c.used_at.is_(None))
        .with_for_update()
    )
    return (await conn.execute(statement)).one_or_none()


async def count_wrong_try(conn: AsyncConnection, pairing_id: int) -> None:
    wp = tables.worker_pairings
    await conn.execute(update(wp).values(attempts=wp.c.attempts + 1).where(wp.c.id == pairing_id))


async def mark_used(conn: AsyncConnection, pairing_id: int, worker_id: int) -> None:
    wp = tables.worker_pairings
    await conn.execute(update(wp).values(used_at=func.now(), worker_id=worker_id).where(wp.c.id == pairing_id))
