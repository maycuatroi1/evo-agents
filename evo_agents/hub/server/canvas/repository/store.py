"""The SQL of canvases, claims, revs and evidence: the canvases of a project (``canvases_of``), one canvas
(``canvas_of``) with its row lock, which every write of the canvas and its claims takes, its claims, one claim
(``claim_of``), the revs of a claim and the evidence of claims, and the writes of a canvas itself. Who may read what
is the service's (``service.reads``): these answer None where it answers a 404.
"""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.canvas.models.content import Content
from evo_agents.hub.server.canvas.models.rows import CanvasRow, ClaimRow, EvidenceRow, Provenance, RevRow
from evo_agents.hub.tables_canvas import CELLS

# Canvases


def _canvas_query():
    c, users = tables.canvas.canvases, tables.users
    return select(
        c.c.id,
        c.c.slug,
        c.c.name,
        c.c.description,
        c.c.label,
        users.c.login.label("created_by"),
        c.c.created_at,
        c.c.updated_at,
        c.c.archived_at,
    ).select_from(c.join(users, users.c.id == c.c.created_by))


def _canvases_query(project_id: int):
    return _canvas_query().where(tables.canvas.canvases.c.project_id == project_id)


async def canvases_of(conn: AsyncConnection, project_id: int, *, with_archived: bool = False) -> list[CanvasRow]:
    """The canvases of project ``project_id``, by slug, whatever their label; the archived ones too with
    ``with_archived``."""
    c = tables.canvas.canvases
    query = _canvases_query(project_id).order_by(c.c.slug)
    if not with_archived:
        query = query.where(c.c.archived_at.is_(None))
    return [CanvasRow(**row._mapping) for row in await conn.execute(query)]


async def project_canvases(conn: AsyncConnection, project_id: int) -> list[CanvasRow]:
    """The canvases of project ``project_id`` not archived, by slug, whatever their label: for the hub's own jobs,
    which no member's grant bounds (the value check)."""
    c = tables.canvas.canvases
    query = _canvases_query(project_id).where(c.c.archived_at.is_(None)).order_by(c.c.slug)
    return [CanvasRow(**row._mapping) for row in await conn.execute(query)]


async def lock_canvas(conn: AsyncConnection, canvas_id: int) -> CanvasRow | None:
    """Take the row lock of canvas ``canvas_id``, which every write of it takes, and read it again; None when it is
    gone or archived since."""
    c = tables.canvas.canvases
    await conn.execute(select(c.c.id).where(c.c.id == canvas_id).with_for_update())
    row = (await conn.execute(_canvas_query().where(c.c.id == canvas_id))).first()
    return None if row is None or row.archived_at is not None else CanvasRow(**row._mapping)


async def canvas_of(conn: AsyncConnection, project_id: int, slug: str, *, lock: bool = False) -> CanvasRow | None:
    """Canvas ``slug`` of project ``project_id``; None when there is none. ``lock`` takes its row lock first, which
    every write of the canvas and its claims takes, so they go one after the other."""
    c = tables.canvas.canvases
    if lock:
        held = select(c.c.id).where(c.c.project_id == project_id, c.c.slug == slug).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_canvases_query(project_id).where(c.c.slug == slug))).first()
    return None if row is None else CanvasRow(**row._mapping)


async def slug_taken(conn: AsyncConnection, project_id: int, slug: str) -> bool:
    c = tables.canvas.canvases
    return await conn.scalar(select(c.c.id).where(c.c.project_id == project_id, c.c.slug == slug)) is not None


async def insert_canvas(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.canvas.canvases).values(**values))


async def set_archived(conn: AsyncConnection, canvas_id: int, user_id: int | None, archived: bool) -> None:
    """Archive canvas ``canvas_id`` by ``user_id``, or unarchive it."""
    c = tables.canvas.canvases
    values = (
        {"archived_at": func.now(), "archived_by": user_id} if archived else {"archived_at": None, "archived_by": None}
    )
    await conn.execute(update(c).where(c.c.id == canvas_id).values(**values, updated_at=func.now()))


async def touch(conn: AsyncConnection, canvas_id: int) -> None:
    c = tables.canvas.canvases
    await conn.execute(update(c).where(c.c.id == canvas_id).values(updated_at=func.now()))


async def counts(conn: AsyncConnection, canvas_ids: list[int]) -> dict[int, dict[str, int]]:
    """Per canvas, how many of its claims not archived hold each status."""
    k = tables.canvas.claims
    query = (
        select(k.c.canvas_id, k.c.status, func.count())
        .where(k.c.canvas_id.in_(canvas_ids), k.c.archived_at.is_(None))
        .group_by(k.c.canvas_id, k.c.status)
    )
    found: dict[int, dict[str, int]] = {canvas_id: {} for canvas_id in canvas_ids}
    for canvas_id, status, count in await conn.execute(query):
        found[canvas_id][status] = count
    return found


# Claims


def _claims_query():
    k, users = tables.canvas.claims, tables.users
    return select(
        k.c.id,
        k.c.canvas_id,
        k.c.number,
        k.c.cell,
        k.c.position,
        k.c.text,
        k.c.status,
        k.c.confidence,
        k.c.test_card,
        k.c.rev,
        k.c.label,
        users.c.login.label("created_by"),
        k.c.created_at,
        k.c.updated_at,
        k.c.archived_at,
        k.c.use_cases,
        k.c.signal_keys,
        k.c.problems,
        k.c.opportunities,
        k.c.delivery,
    ).select_from(k.join(users, users.c.id == k.c.created_by))


async def claims(conn: AsyncConnection, canvas_id: int, *, with_archived: bool = False) -> list[ClaimRow]:
    """The claims of a canvas, by cell, place in the cell and number; the archived ones too with ``with_archived``."""
    k = tables.canvas.claims
    query = _claims_query().where(k.c.canvas_id == canvas_id)
    if not with_archived:
        query = query.where(k.c.archived_at.is_(None))
    rows = [ClaimRow(**row._mapping) for row in await conn.execute(query)]
    order = {cell: index for index, cell in enumerate(CELLS)}
    return sorted(rows, key=lambda row: (order.get(row.cell, len(order)), row.position, row.number))


async def claim_of(conn: AsyncConnection, project_id: int, number: int, *, lock: bool = False) -> ClaimRow | None:
    """Claim ``number`` of project ``project_id``, in any of its canvases; None when it has none. ``lock`` takes its
    row lock."""
    k = tables.canvas.claims
    query = _claims_query().where(k.c.project_id == project_id, k.c.number == number)
    if lock:
        query = query.with_for_update(of=k)
    row = (await conn.execute(query)).first()
    return None if row is None else ClaimRow(**row._mapping)


async def claim_anywhere(conn: AsyncConnection, project_id: int, number: int) -> tuple[int, int] | None:
    """(claim id, canvas id) of the claim of the project numbered ``number``, in any canvas; None when none is."""
    k = tables.canvas.claims
    row = (
        await conn.execute(select(k.c.id, k.c.canvas_id).where(k.c.project_id == project_id, k.c.number == number))
    ).first()
    return None if row is None else (row.id, row.canvas_id)


# Revs and evidence


def _provenance(table, users):
    return (
        table.c.actor,
        users.c.login,
        table.c.run_id,
        table.c.runtime,
        table.c.model,
        table.c.chat_message_id,
    )


def provenance_of(row) -> Provenance:
    return Provenance(row.actor, row.login, row.run_id, row.runtime, row.model, row.chat_message_id)


async def revs(conn: AsyncConnection, claim_id: int, *, upto: int | None = None, since: int = 1) -> list[RevRow]:
    """The revs of a claim from ``since`` to ``upto`` (its last when None), oldest first."""
    r, users = tables.canvas.revs, tables.users
    query = (
        select(
            r.c.rev,
            r.c.content,
            r.c.change,
            r.c.undo_of,
            *_provenance(r, users),
            r.c.reason,
            r.c.created_at,
            r.c.baseline_id,
        )
        .select_from(r.outerjoin(users, users.c.id == r.c.user_id))
        .where(r.c.claim_id == claim_id, r.c.rev >= since)
        .order_by(r.c.rev)
    )
    if upto is not None:
        query = query.where(r.c.rev <= upto)
    return [
        RevRow(
            rev=row.rev,
            content=Content.from_json(row.content),
            change=row.change,
            undo_of=row.undo_of,
            provenance=provenance_of(row),
            reason=row.reason,
            created_at=row.created_at,
            baseline_id=row.baseline_id,
        )
        for row in await conn.execute(query)
    ]


async def rev(conn: AsyncConnection, claim_id: int, number: int) -> RevRow | None:
    found = await revs(conn, claim_id, upto=number, since=number)
    return found[0] if found else None


async def evidence(conn: AsyncConnection, claim_ids: list[int]) -> dict[int, list[EvidenceRow]]:
    """The evidence of each claim, oldest first, withdrawn pieces included."""
    e, users = tables.canvas.evidence, tables.users
    confirmer, withdrawer = users.alias("confirmer"), users.alias("withdrawer")
    query = (
        select(
            e.c.id,
            e.c.claim_id,
            e.c.kind,
            e.c.direction,
            e.c.summary,
            e.c.url,
            e.c.observed_on,
            e.c.synthetic,
            e.c.label,
            *_provenance(e, users),
            e.c.created_at,
            confirmer.c.login.label("confirmed_by"),
            e.c.confirmed_at,
            e.c.withdrawn_at,
            withdrawer.c.login.label("withdrawn_by"),
            e.c.withdrawn_reason,
            e.c.basis,
            e.c.rests_on_run,
        )
        .select_from(
            e.outerjoin(users, users.c.id == e.c.user_id)
            .outerjoin(confirmer, confirmer.c.id == e.c.confirmed_by)
            .outerjoin(withdrawer, withdrawer.c.id == e.c.withdrawn_by)
        )
        .where(e.c.claim_id.in_(claim_ids))
        .order_by(e.c.id)
    )
    found: dict[int, list[EvidenceRow]] = {claim_id: [] for claim_id in claim_ids}
    for row in await conn.execute(query):
        values = dict(row._mapping)
        provenance = provenance_of(row)
        for name in ("actor", "login", "run_id", "runtime", "model", "chat_message_id"):
            values.pop(name)
        found[row.claim_id].append(EvidenceRow(**values, provenance=provenance))
    return found
