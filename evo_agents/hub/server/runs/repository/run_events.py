"""The SQL of a run's log, the owner's messages to its agent, and the log and diff its worker uploads
(``evo_agents.hub.server.run_events``)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import DateTime, and_, bindparam, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.runs.models.run_events import MAX_INBOX


async def live_worker(conn: AsyncConnection, token_id):
    """(id, name) of the live worker of token ``token_id``; None for none."""
    w = tables.workers
    live = select(w.c.id, w.c.name).where(w.c.token_id == token_id, w.c.revoked_at.is_(None))
    return (await conn.execute(live)).one_or_none()


async def locked_run(conn: AsyncConnection, run_id: int):
    """(worker_id, state, event_seq, events_acked, project) of run ``run_id``, its row locked; None for none."""
    r, p = tables.runs, tables.projects
    claimed = (
        select(r.c.worker_id, r.c.state, r.c.event_seq, r.c.events_acked, p.c.name.label("project"))
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    return (await conn.execute(claimed)).one_or_none()


async def add_events(conn: AsyncConnection, run_id: int, rows: list[dict]) -> None:
    """Insert the worker's event ``rows`` of run ``run_id`` and count them on the run; a time after the hub's now is
    taken as now."""
    e, r = tables.run_events, tables.runs
    stored_at = func.least(bindparam("seen_at", type_=DateTime(timezone=True)), func.now())
    await conn.execute(insert(e).values(at=stored_at), rows)
    count = update(r).values(event_seq=r.c.event_seq + len(rows), events_acked=r.c.events_acked + len(rows))
    await conn.execute(count.where(r.c.id == run_id))


def deliver(run_id: int, ack: int):
    """Mark the messages of run ``run_id`` up to ``ack`` delivered, and the decisions they answer, which reached the
    agent with them: one statement, the update of the inbox a data-modifying CTE of the update of decisions."""
    i, d = tables.run_inbox, tables.decisions
    taken = (
        update(i)
        .values(delivered_at=func.now())
        .where(i.c.run_id == run_id, i.c.id <= ack, i.c.delivered_at.is_(None))
        .returning(i.c.decision_id)
        .cte("taken")
    )
    return (
        update(d)
        .values(delivered_at=func.now())
        .where(d.c.id.in_(select(taken.c.decision_id)), d.c.state == "answered", d.c.delivered_at.is_(None))
    )


def undelivered(run_id: int):
    i, u = tables.run_inbox, tables.users
    return (
        select(i.c.id, i.c.body.label("text"), u.c.login.label("sent_by"), i.c.created_at, i.c.decision_id)
        .join_from(i, u, u.c.id == i.c.sent_by)
        .where(i.c.run_id == run_id, i.c.delivered_at.is_(None))
        .order_by(i.c.id)
        .limit(MAX_INBOX)
    )


async def mark_delivered(conn: AsyncConnection, run_id: int, ack: int) -> None:
    await conn.execute(deliver(run_id, ack))


async def waiting_messages(conn: AsyncConnection, run_id: int) -> list:
    return list((await conn.execute(undelivered(run_id))).all())


async def upload_kinds(conn: AsyncConnection, upload_ids: list[str]) -> list[str]:
    """The kinds of the blob uploads ``upload_ids``."""
    uploads = tables.blob_uploads
    query = select(uploads.c.kind).where(uploads.c.upload_id.in_([UUID(upload_id) for upload_id in upload_ids]))
    return list((await conn.execute(query)).scalars().all())


async def record_blobs(conn: AsyncConnection, run_id: int, found: dict[str, str]):
    """Record the log and diff ``found`` (by column) on run ``run_id``, keeping the ones it had for the others; the
    row of both."""
    r = tables.runs
    record = (
        update(r)
        .values(
            log_sha256=func.coalesce(found.get("log_sha256"), r.c.log_sha256),
            diff_sha256=func.coalesce(found.get("diff_sha256"), r.c.diff_sha256),
        )
        .where(r.c.id == run_id)
        .returning(r.c.log_sha256, r.c.diff_sha256)
    )
    return (await conn.execute(record)).one()


async def events_after(conn: AsyncConnection, run_id: int, after: int, kinds: list[str], limit: int) -> list:
    """The events of run ``run_id`` after seq ``after``, of ``kinds`` (any kind when empty), in seq order."""
    e = tables.run_events
    query = select(e.c.seq, e.c.at, e.c.kind, e.c.body, e.c.truncated).where(e.c.run_id == run_id, e.c.seq > after)
    if kinds:
        query = query.where(e.c.kind.in_(kinds))
    return list((await conn.execute(query.order_by(e.c.seq).limit(limit))).all())


async def progress(conn: AsyncConnection, run_id: int):
    """(state, event_seq) of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.state, r.c.event_seq).where(r.c.id == run_id))).one()


async def chat_ended(conn: AsyncConnection, run_id: int) -> bool:
    """Whether the owner ended the chat of author run ``run_id``."""
    r = tables.runs
    query = select(r.c.finish_requested_at.is_not(None)).where(r.c.id == run_id)
    return bool((await conn.execute(query)).scalar_one())


async def add_user_message(conn: AsyncConnection, run_id: int, seq: int, body: dict) -> None:
    values = {"run_id": run_id, "seq": seq, "kind": "user_message", "body": body}
    await conn.execute(insert(tables.run_events).values(**values))


async def add_to_inbox(conn: AsyncConnection, run_id: int, sent_by: int, text: str):
    """Leave ``text`` in the inbox of run ``run_id``; (id, created_at) of the message."""
    inbox = tables.run_inbox
    left = insert(inbox).values(run_id=run_id, sent_by=sent_by, body=text).returning(inbox.c.id, inbox.c.created_at)
    return (await conn.execute(left)).one()


async def diff_of(conn: AsyncConnection, run_id: int):
    """(diff_sha256, size) of run ``run_id``'s diff, both None when it has none."""
    r, b = tables.runs, tables.blobs
    query = (
        select(r.c.diff_sha256, b.c.size)
        .select_from(r.outerjoin(b, and_(b.c.project_id == r.c.project_id, b.c.sha256 == r.c.diff_sha256)))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one()
