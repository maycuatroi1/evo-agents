"""Reading the canvases of a project: the canvases a member sees (``canvases``), one canvas (``canvas``), its claims,
one claim (``claim``), the revs of a claim and the evidence of claims; and who may do what (``reader``, ``writer``,
``admin``).

A canvas carries a label (the project's default, or one an admin gave it when it was made), and every row of it
carries the same; a canvas is read under that label through the sink X-Evo-Sink names, else the project's hub sink, as
plans are read (``ProjectAccess.visible``). A canvas the member cannot see answers the 404 of one that does not exist.
A piece of evidence is read under its own label, so one above what the member may read is counted, never shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.canvas.content import CanvasError, Content, code
from evo_agents.hub.server.canvas.rules import Evidence
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_canvas import CELLS

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
MAX_CLAIMS = 500  # of a canvas, archived ones included
MAX_EVIDENCE = 200  # of a claim, withdrawn ones included


@dataclass(frozen=True)
class CanvasRow:
    id: int
    slug: str
    name: str
    description: str
    label: dict
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True)
class ClaimRow:
    """A claim as it is now."""

    id: int
    canvas_id: int
    number: int
    cell: str
    position: int
    text: str
    status: str
    confidence: str
    test_card: dict | None
    rev: int
    label: dict
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    use_cases: list[str] = field(default_factory=list)  # the codes of the use cases it links
    signal_keys: list[str] = field(default_factory=list)  # the signal keys it links
    problems: list[str] = field(default_factory=list)  # the codes of the problems of the project it answers
    opportunities: list[str] = field(default_factory=list)  # the codes of the opportunities of the project it links
    delivery: str = "code"  # code or action (tables_canvas.DELIVERIES)

    @property
    def code(self) -> str:
        return code(self.number)

    def content(self) -> Content:
        return Content(
            cell=self.cell,
            position=self.position,
            text=self.text,
            status=self.status,
            confidence=self.confidence,
            test_card=self.test_card,
            archived=self.archived_at is not None,
            use_cases=tuple(self.use_cases),
            signal_keys=tuple(self.signal_keys),
            problems=tuple(self.problems),
            opportunities=tuple(self.opportunities),
            delivery=self.delivery,
        )


@dataclass(frozen=True)
class Provenance:
    """Who wrote a rev or a piece of evidence: a person, an agent or the hub; the member it was written for (their
    login), the run with its runtime and model, and the chat message that asked for it."""

    actor: str
    login: str | None
    run_id: int | None
    runtime: str | None
    model: str | None
    chat_message_id: int | None


@dataclass(frozen=True)
class RevRow:
    rev: int
    content: Content
    change: str
    undo_of: int | None
    provenance: Provenance
    reason: str
    created_at: datetime
    baseline_id: int | None = None  # the baseline whose decision wrote it


@dataclass(frozen=True)
class EvidenceRow:
    id: int
    claim_id: int
    kind: str
    direction: str
    summary: str
    url: str | None
    observed_on: date | None
    synthetic: bool
    label: dict
    provenance: Provenance
    created_at: datetime
    confirmed_by: str | None
    confirmed_at: datetime | None
    withdrawn_at: datetime | None
    withdrawn_by: str | None
    withdrawn_reason: str | None
    basis: dict | None = None  # what a piece the hub wrote rests on: a check's figures, or the item judged
    rests_on_run: bool = False  # the hub wrote it from signals the agent of a run pushed

    def facts(self) -> Evidence:
        """What the rule of validated reads of it."""
        return Evidence(
            id=self.id,
            kind=self.kind,
            direction=self.direction,
            synthetic=self.synthetic,
            actor=self.provenance.actor,
            confirmed=self.confirmed_by is not None,
            withdrawn=self.withdrawn_at is not None,
            rests_on_run=self.rests_on_run,
        )


# Who may do what


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise CanvasError(403, f"reading the canvases of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "writer"):
        raise CanvasError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def admin(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "admin"):
        raise CanvasError(403, f"{what} in project {access.name} needs the admin role; you are {access.role}")


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def visible(access: ProjectAccess, label, sink: str | None) -> bool:
    through = sink_of(access, sink)
    return through is not None and access.visible(label, through)


def missing(access: ProjectAccess, slug: str) -> CanvasError:
    return CanvasError(
        404, f"no canvas {slug} in project {access.name} that you can see: see `evo-agents hub canvas list`"
    )


def writable(access: ProjectAccess, canvas: CanvasRow) -> CanvasRow:
    """``canvas`` when it takes writes; 409 when it is archived."""
    if canvas.archived_at is not None:
        raise CanvasError(
            409,
            f"canvas {canvas.slug} of project {access.name} is archived and takes no change: an admin unarchives it",
        )
    return canvas


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


async def canvases(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, *, with_archived: bool = False
) -> list[CanvasRow]:
    """The canvases of the project the member sees, by slug; the archived ones too with ``with_archived``."""
    reader(access)
    c = tables.canvas.canvases
    query = _canvases_query(access.project_id).order_by(c.c.slug)
    if not with_archived:
        query = query.where(c.c.archived_at.is_(None))
    rows = [CanvasRow(**row._mapping) for row in await conn.execute(query)]
    return [row for row in rows if visible(access, row.label, sink)]


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


async def canvas(
    conn: AsyncConnection, access: ProjectAccess, slug: str, sink: str | None, *, lock: bool = False
) -> CanvasRow:
    """Canvas ``slug`` as the member sees it; 404 when there is none or they cannot see it. ``lock`` takes its row
    lock first, which every write of the canvas and its claims takes, so they go one after the other."""
    reader(access)
    c = tables.canvas.canvases
    if lock:
        held = select(c.c.id).where(c.c.project_id == access.project_id, c.c.slug == slug).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_canvases_query(access.project_id).where(c.c.slug == slug))).first()
    if row is None or not visible(access, row.label, sink):
        raise missing(access, slug)
    return CanvasRow(**row._mapping)


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


async def claim(
    conn: AsyncConnection, access: ProjectAccess, canvas: CanvasRow, number: int, *, lock: bool = False
) -> ClaimRow:
    """Claim ``number`` of ``canvas``; 404 when the canvas has none. ``lock`` takes its row lock."""
    k = tables.canvas.claims
    query = _claims_query().where(k.c.project_id == access.project_id, k.c.number == number)
    if lock:
        query = query.with_for_update(of=k)
    row = (await conn.execute(query)).first()
    if row is None or row.canvas_id != canvas.id:
        raise CanvasError(404, f"no claim {code(number)} in canvas {canvas.slug} of project {access.name}")
    return ClaimRow(**row._mapping)


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
