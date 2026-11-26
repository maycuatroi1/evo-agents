"""The rows deciding a version and keeping comments: a version's state, its reviews, a document's archiving, and the
comments of a document (``service.reviews`` checks and decides; this reads and writes the rows)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.server.srs.repository.store import DocumentRow, VersionRow

WITHDRAWN = "withdrawn"
LAST_STATES = ("superseded", WITHDRAWN)  # a version in one never changes state again


async def submit_version(conn: AsyncConnection, version_id: int) -> None:
    """Put version ``version_id`` in review, its round one more, with no review run of @ba yet."""
    v = tables.srs.versions
    changed = {"state": "in_review", "round": v.c.round + 1, "submitted_at": func.now(), "review_run_id": None}
    await conn.execute(update(v).where(v.c.id == version_id).values(changed))


async def approve_version(conn: AsyncConnection, doc: DocumentRow, held: VersionRow) -> None:
    """Approve ``held``, supersede every older version of ``doc`` not in a last state, and make it ``doc``'s approved
    one."""
    s = tables.srs
    v, d = s.versions, s.documents
    await conn.execute(update(v).where(v.c.id == held.id).values(state="approved", decided_at=func.now()))
    older = and_(v.c.document_id == doc.id, v.c.number < held.number, v.c.state.not_in(LAST_STATES))
    await conn.execute(update(v).where(older).values(state="superseded"))
    await conn.execute(update(d).where(d.c.id == doc.id).values(approved_version=held.number))


async def back_to_draft(conn: AsyncConnection, version_id: int) -> None:
    v = tables.srs.versions
    await conn.execute(update(v).where(v.c.id == version_id).values(state="draft"))


async def withdraw_version(conn: AsyncConnection, version_id: int) -> None:
    v = tables.srs.versions
    await conn.execute(update(v).where(v.c.id == version_id).values(state=WITHDRAWN, decided_at=func.now()))


async def insert_review(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.srs.reviews).values(values))


async def set_archived(conn: AsyncConnection, document_id: int, archived: bool) -> None:
    d = tables.srs.documents
    await conn.execute(update(d).where(d.c.id == document_id).values(archived_at=func.now() if archived else None))


# Comments


@dataclass(frozen=True)
class CommentRow:
    id: int
    version: int | None
    item: str | None
    item_code: str | None
    parent_id: int | None
    author: str
    run_id: int | None
    body: str
    created_at: datetime
    resolved_by: str | None
    resolved_at: datetime | None
    field: str | None = None
    quote: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    start_offset: int | None = None
    ask_run_id: int | None = None
    author_id: int | None = None


def _comments_query(document_id: int):
    s, u = tables.srs, tables.users
    c, v, i = s.comments, s.versions, s.items
    author, resolver = u.alias("author"), u.alias("resolver")
    return (
        select(
            c.c.id,
            v.c.number.label("version"),
            i.c.key.label("item"),
            i.c.code.label("item_code"),
            c.c.parent_id,
            author.c.login.label("author"),
            c.c.run_id,
            c.c.body,
            c.c.created_at,
            resolver.c.login.label("resolved_by"),
            c.c.resolved_at,
            c.c.field,
            c.c.quote,
            c.c.prefix,
            c.c.suffix,
            c.c.start_offset,
            c.c.ask_run_id,
            c.c.author_id,
        )
        .select_from(
            c.join(author, author.c.id == c.c.author_id)
            .outerjoin(v, v.c.id == c.c.version_id)
            .outerjoin(i, i.c.id == c.c.item_id)
            .outerjoin(resolver, resolver.c.id == c.c.resolved_by)
        )
        .where(c.c.document_id == document_id)
    )


async def comments(conn: AsyncConnection, doc: DocumentRow) -> list[CommentRow]:
    """The comments of ``doc``, oldest first."""
    query = _comments_query(doc.id).order_by(tables.srs.comments.c.id)
    return [CommentRow(**row._mapping) for row in await conn.execute(query)]


async def comment_row(conn: AsyncConnection, doc: DocumentRow, comment_id: int) -> CommentRow | None:
    """Comment ``comment_id`` of ``doc``, or None."""
    row = (await conn.execute(_comments_query(doc.id).where(tables.srs.comments.c.id == comment_id))).first()
    return CommentRow(**row._mapping) if row is not None else None


async def thread_root(conn: AsyncConnection, doc: DocumentRow, parent_id: int) -> int | None:
    """The root of the thread comment ``parent_id`` of ``doc`` is in, or None when ``doc`` has no such comment."""
    c = tables.srs.comments
    held = select(c.c.id, c.c.parent_id).where(c.c.id == parent_id, c.c.document_id == doc.id)
    row = (await conn.execute(held)).first()
    return None if row is None else row.parent_id or row.id


async def insert_comment(conn: AsyncConnection, doc: DocumentRow, values: dict) -> CommentRow:
    """Write a comment with ``values``; it as read back."""
    c = tables.srs.comments
    comment_id = (await conn.execute(insert(c).values(values).returning(c.c.id))).scalar_one()
    return CommentRow(**(await conn.execute(_comments_query(doc.id).where(c.c.id == comment_id))).one()._mapping)


async def resolver(conn: AsyncConnection, comment_id: int) -> int | None:
    """Who closed the thread of comment ``comment_id``, or None."""
    c = tables.srs.comments
    return await conn.scalar(select(c.c.resolved_by).where(c.c.id == comment_id))


async def set_ask_run(conn: AsyncConnection, comment_id: int, run_id: int) -> None:
    """Keep run ``run_id`` as the one asked to answer comment ``comment_id``."""
    c = tables.srs.comments
    await conn.execute(update(c).where(c.c.id == comment_id).values(ask_run_id=run_id))


async def resolve_thread(
    conn: AsyncConnection, doc: DocumentRow, comment_id: int, user_id: int, reopen: bool
) -> tuple[bool, CommentRow]:
    """Close the thread of comment ``comment_id`` of ``doc`` as ``user_id``, or open it again (``reopen``): whether a
    row changed, and the thread's root after; 404 when ``doc`` has no such comment."""
    c = tables.srs.comments
    held = and_(c.c.id == comment_id, c.c.document_id == doc.id)
    above = (await conn.execute(select(c.c.parent_id).where(held))).scalar()
    if above is not None:  # an answer: its thread is its root's
        comment_id = above
        held = and_(c.c.id == comment_id, c.c.document_id == doc.id)
    if reopen:
        change = update(c).where(held, c.c.resolved_at.is_not(None)).values(resolved_by=None, resolved_at=None)
    else:
        values = {"resolved_by": user_id, "resolved_at": func.now()}
        change = update(c).where(held, c.c.resolved_at.is_(None)).values(values)
    done = await conn.execute(change)
    row = await comment_row(conn, doc, comment_id)
    if row is None:
        raise SrsError(404, f"document {doc.slug} has no comment {comment_id}")
    return bool(done.rowcount), row


__all__ = [
    "LAST_STATES",
    "WITHDRAWN",
    "CommentRow",
    "approve_version",
    "back_to_draft",
    "comment_row",
    "comments",
    "insert_comment",
    "insert_review",
    "resolve_thread",
    "resolver",
    "set_ask_run",
    "set_archived",
    "submit_version",
    "thread_root",
    "withdraw_version",
]
