"""The rows of the reviews of use cases (srs_use_case_reviews), and what ``service.use_case_reviews`` reads with them:
the authors of a document's versions and the approval event of a version."""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.srs.repository.store import DocumentRow


async def authors_between(conn: AsyncConnection, doc: DocumentRow, since: int, number: int) -> set[int]:
    """The authors of the versions of ``doc`` after ``since``, up to ``number``."""
    v = tables.srs.versions
    query = select(v.c.author_id).where(v.c.document_id == doc.id, v.c.number > since, v.c.number <= number)
    return set((await conn.execute(query)).scalars())


async def approved_before(conn: AsyncConnection, document_id: int, number: int) -> str | None:
    """The ``previous`` of the newest event ``srs.version.approved`` of version ``number`` of the document, as
    text; None when the event is not there."""
    e = tables.hub_events
    query = (
        select(e.c.payload["previous"].astext)
        .where(
            e.c.kind == "srs.version.approved",
            e.c.payload["document_id"].astext == str(document_id),
            e.c.payload["version"].astext == str(number),
        )
        .order_by(e.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).scalar()


async def reviews(conn: AsyncConnection, doc: DocumentRow, number: int) -> list:
    """The reviews of use cases (with the use case's key, the reviewer's login and the version's number) recorded on
    the versions after the one approved last, up to ``number``, oldest first."""
    s = tables.srs
    r, v, i, u = s.use_case_reviews, s.versions, s.items, tables.users
    query = (
        select(
            i.c.key,
            u.c.login.label("reviewer"),
            r.c.reviewer_id,
            r.c.role,
            r.c.decision,
            r.c.body,
            v.c.number.label("version"),
            r.c.created_at,
        )
        .select_from(
            r.join(v, v.c.id == r.c.version_id).join(i, i.c.id == r.c.item_id).join(u, u.c.id == r.c.reviewer_id)
        )
        .where(v.c.document_id == doc.id, v.c.number > (doc.approved_version or 0), v.c.number <= number)
        .order_by(r.c.id)
    )
    return list(await conn.execute(query))


async def key_of(conn: AsyncConnection, item_id: int) -> str:
    """The key of item ``item_id``."""
    i = tables.srs.items
    return (await conn.execute(select(i.c.key).where(i.c.id == item_id))).scalar_one()


async def insert_review(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.srs.use_case_reviews).values(values))


__all__ = ["approved_before", "authors_between", "insert_review", "key_of", "reviews"]
