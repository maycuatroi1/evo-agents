"""The queries of the chat of an author run (``chats.service.author``): the chain of runs one chat spans, its
messages, and what the notice author_waiting reads."""

from __future__ import annotations

from sqlalchemy import and_, case, exists, func, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, tables


def _chain(run_id: int):
    """The ids of the runs of the chat of run ``run_id``: it, the runs it resumes (back through resume_of_run_id), and
    the runs that resume it; two recursive CTEs, UNION so a cycle ends."""
    r = tables.runs
    back = select(r.c.id, r.c.resume_of_run_id).where(r.c.id == run_id).cte("back", recursive=True)
    earlier = r.alias("earlier")
    back = back.union(
        select(earlier.c.id, earlier.c.resume_of_run_id).join_from(
            earlier, back, earlier.c.id == back.c.resume_of_run_id
        )
    )
    ahead = select(r.c.id).where(r.c.id == run_id).cte("ahead", recursive=True)
    later = r.alias("later")
    ahead = ahead.union(select(later.c.id).join_from(later, ahead, later.c.resume_of_run_id == ahead.c.id))
    return union(select(back.c.id), select(ahead.c.id)).subquery("chain")


def _is_owner():
    """An event that is a message of the owner's in the chat: a ``user_message`` that answers no decision."""
    e = tables.run_events
    return and_(e.c.kind == "user_message", ~e.c.body.has_key("decision_id"))


def _is_agent():
    e = tables.run_events
    return and_(e.c.kind == "system", e.c.body["chat"].astext == author.CHAT_AGENT)


def _messages(run_id: int, limit: int):
    """The latest ``limit`` messages of the chat of run ``run_id``, newest first."""
    e = tables.run_events
    chain = _chain(run_id)
    owner = _is_owner()
    return (
        select(
            e.c.run_id,
            e.c.seq,
            case((owner, "owner"), else_="agent").label("author"),
            case((owner, e.c.body["from"].astext)).label("login"),
            e.c.body["text"].astext.label("text"),
            e.c.at,
            e.c.body["attachments"].label("attachments"),
        )
        .where(e.c.run_id.in_(select(chain.c.id)), or_(owner, _is_agent()))
        .order_by(e.c.run_id.desc(), e.c.seq.desc())
        .limit(limit)
    )


def _latest(run_id: int):
    """The run of the chat of run ``run_id`` that takes the next message, with its state, owner and plan."""
    r, u = tables.runs, tables.users
    chain = _chain(run_id)
    return (
        select(r.c.id, r.c.state, u.c.login, r.c.plan_id, r.c.plan_revision)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id.in_(select(chain.c.id)))
        .order_by(r.c.id.desc())
        .limit(1)
    )


async def messages(conn: AsyncConnection, run_id: int, limit: int) -> list:
    """The latest ``limit`` messages of the chat of run ``run_id``, newest first."""
    return list((await conn.execute(_messages(run_id, limit))).all())


async def latest(conn: AsyncConnection, run_id: int):
    """The run of the chat of run ``run_id`` that takes the next message (``_latest``)."""
    return (await conn.execute(_latest(run_id))).one()


async def kind_of(conn: AsyncConnection, run_id: int) -> str:
    return (await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == run_id))).scalar_one()


async def waiting_run(conn: AsyncConnection, run_id: int):
    """The owner, project (id and name) and plan of run ``run_id``."""
    r, p = tables.runs, tables.projects
    query = (
        select(r.c.dispatched_by, r.c.project_id, p.c.name, r.c.plan_id, r.c.plan_revision)
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one()


async def last_said(conn: AsyncConnection, run_id: int) -> str | None:
    """The agent's last message of the chat of run ``run_id``; None before its first."""
    e = tables.run_events
    last = select(e.c.body["text"].astext).where(e.c.run_id == run_id, _is_agent()).order_by(e.c.seq.desc()).limit(1)
    return (await conn.execute(last)).scalar_one_or_none()


async def read_waiting(conn: AsyncConnection, user_id: int, run_id: int) -> None:
    """Read the notices author_waiting of run ``run_id`` person ``user_id`` has not read."""
    n = tables.notifications
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(
            n.c.user_id == user_id,
            n.c.run_id == run_id,
            n.c.notice_kind == "author_waiting",
            n.c.read_at.is_(None),
        )
    )


async def decision_open(conn: AsyncConnection, run_id: int) -> bool:
    """Whether a decision of run ``run_id`` is open."""
    d = tables.decisions
    return (await conn.execute(select(exists().where(d.c.run_id == run_id, d.c.state == "open")))).scalar_one()


async def ask_finish(conn: AsyncConnection, run_id: int) -> bool:
    """Record that the owner asked run ``run_id`` to finish, unless they did already: True when this recorded it."""
    # as runs.service.controls.ask_once writes it, which this layer may not import: it reaches the chats' service
    r = tables.runs
    ask = (
        update(r)
        .values(finish_requested_at=func.now())
        .where(r.c.id == run_id, r.c.finish_requested_at.is_(None))
        .returning(r.c.id)
    )
    return (await conn.execute(ask)).one_or_none() is not None
