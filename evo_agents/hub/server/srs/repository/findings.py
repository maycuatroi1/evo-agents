"""The findings of a document as srs_findings holds them, and the runs that review a version (``service.findings``
checks and decides; this reads and writes the rows)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.server.srs.repository.store import DocumentRow

OPEN = "open"


@dataclass(frozen=True)
class FindingRow:
    """A finding with the number of the version it was found on, its item's key and code, and its settler's login."""

    id: int
    version: int
    item: str | None
    item_code: str | None
    kind: str
    level: str
    sources: list[dict]
    summary: str
    status: str
    resolution: str | None
    resolution_sources: list[dict]
    settled_by: str | None
    settled_at: datetime | None
    chat_link: str | None
    run_id: int | None
    created_at: datetime
    options: list[str] = field(default_factory=list)


def _query(document_id: int):
    s, u = tables.srs, tables.users
    f, v, i = s.findings, s.versions, s.items
    return (
        select(
            f.c.id,
            v.c.number.label("version"),
            i.c.key.label("item"),
            i.c.code.label("item_code"),
            f.c.kind,
            f.c.level,
            f.c.sources,
            f.c.summary,
            f.c.status,
            f.c.resolution,
            f.c.resolution_sources,
            u.c.login.label("settled_by"),
            f.c.settled_at,
            f.c.chat_link,
            f.c.run_id,
            f.c.created_at,
            f.c.options,
        )
        .select_from(
            f.join(v, v.c.id == f.c.version_id)
            .outerjoin(i, i.c.id == f.c.item_id)
            .outerjoin(u, u.c.id == f.c.settled_by)
        )
        .where(f.c.document_id == document_id)
    )


async def findings(conn: AsyncConnection, doc: DocumentRow, status: str | None = None) -> list[FindingRow]:
    """The findings of ``doc``, oldest first; those in ``status`` only when given."""
    f = tables.srs.findings
    query = _query(doc.id)
    if status is not None:
        query = query.where(f.c.status == status)
    return [FindingRow(**row._mapping) for row in await conn.execute(query.order_by(f.c.id))]


async def finding(conn: AsyncConnection, doc: DocumentRow, finding_id: int) -> FindingRow:
    """Finding ``finding_id`` of ``doc``; 404 when the document has none such."""
    row = (await conn.execute(_query(doc.id).where(tables.srs.findings.c.id == finding_id))).first()
    if row is None:
        raise SrsError(404, f"document {doc.slug} has no finding {finding_id}")
    return FindingRow(**row._mapping)


async def code_holders(conn: AsyncConnection, project_id: int, keys: list[str]) -> list[tuple[str, str, dict]]:
    """(code key, slug, label) of the document whose item holds each code of ``keys`` in the project."""
    s = tables.srs
    c, i, d = s.codes, s.items, s.documents
    query = (
        select(c.c.code_key, d.c.slug, d.c.label)
        .join_from(c, i, i.c.id == c.c.item_id)
        .join(d, d.c.id == i.c.document_id)
        .where(c.c.project_id == project_id, c.c.code_key.in_(keys))
    )
    return [(row.code_key, row.slug, row.label) for row in await conn.execute(query)]


async def held_code_keys(conn: AsyncConnection, project_id: int, keys: list[str]) -> set[str]:
    """The keys of ``keys`` some item of the project holds as a code or an alias."""
    c = tables.srs.codes
    query = select(c.c.code_key).where(c.c.project_id == project_id, c.c.code_key.in_(keys))
    return set((await conn.execute(query)).scalars())


async def insert_finding(conn: AsyncConnection, values: dict) -> int:
    """Write a finding with ``values``; its id."""
    f = tables.srs.findings
    return (await conn.execute(insert(f).values(values).returning(f.c.id))).scalar_one()


async def settle(conn: AsyncConnection, finding_id: int, values: dict) -> None:
    """Keep how finding ``finding_id`` was settled (``values``), settled now."""
    f = tables.srs.findings
    await conn.execute(update(f).where(f.c.id == finding_id).values({**values, "settled_at": func.now()}))


async def reopen(conn: AsyncConnection, finding_id: int, hours: int) -> bool:
    """Open finding ``finding_id`` again when it was settled at most ``hours`` ago; whether it was."""
    f = tables.srs.findings
    recent = and_(f.c.id == finding_id, f.c.settled_at > func.now() - timedelta(hours=hours))
    values = {"status": OPEN, "resolution": None, "resolution_sources": [], "settled_by": None, "settled_at": None}
    done = await conn.execute(update(f).where(recent).values(values))
    return bool(done.rowcount)


async def version_review_run(conn: AsyncConnection, version_id: int):
    """@ba's review of the round of version ``version_id`` (its id and state), or None when the hub handed none."""
    v, r = tables.srs.versions, tables.runs
    query = select(r.c.id, r.c.state).join_from(v, r, r.c.id == v.c.review_run_id).where(v.c.id == version_id)
    return (await conn.execute(query)).first()


async def run(conn: AsyncConnection, run_id: int | None):
    """Run ``run_id`` (its id and state), or None."""
    if run_id is None:
        return None
    r = tables.runs
    return (await conn.execute(select(r.c.id, r.c.state).where(r.c.id == run_id))).first()


__all__ = [
    "FindingRow",
    "code_holders",
    "finding",
    "findings",
    "held_code_keys",
    "insert_finding",
    "reopen",
    "run",
    "settle",
    "version_review_run",
]
