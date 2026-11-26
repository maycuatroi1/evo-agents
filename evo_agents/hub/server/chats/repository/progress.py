"""The queries of the progress of a chat's runs on their way (``chats.service.progress``): the runs, their worker's
notes since their last move, their inboxes and what their sessions last wrote in their threads.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs, runs, tables
from evo_agents.hub.db import one_of

RECENT_EVENTS = 200  # the events of a run its step is read from: its last move and its worker's notes since
MAX_RUNS = 50  # the runs of one chat on their way whose progress shows
INBOX_HEAD_CHARS = 200  # what is read of a message of the inbox: its first line names the chat message (inbox_text)


def _on_their_way(chat_id: int):
    """The chat runs of chat ``chat_id`` on their way, oldest first: their project, agent, worker (the one that holds
    it, else the one it is pinned to) and the thread of the message that started each."""
    r, p, ap, cm = tables.runs, tables.projects, tables.agent_profiles, tables.chat_messages
    held, pinned = tables.workers.alias("held"), tables.workers.alias("pinned")
    return (
        select(
            r.c.id,
            r.c.state,
            r.c.branch,
            r.c.chat_message_id,
            r.c.queued_at,
            r.c.leased_at,
            r.c.started_at,
            p.c.name.label("project"),
            ap.c.handle,
            func.coalesce(held.c.name, pinned.c.name).label("worker"),
            func.coalesce(cm.c.thread_root_id, cm.c.id).label("root_id"),
        )
        .select_from(
            r.join(p, p.c.id == r.c.project_id)
            .join(ap, ap.c.id == r.c.profile_id)
            .join(cm, cm.c.id == r.c.chat_message_id)
            .outerjoin(held, held.c.id == r.c.worker_id)
            .outerjoin(pinned, pinned.c.id == r.c.pinned_worker_id)
        )
        .where(r.c.chat_id == chat_id, r.c.kind == "chat", r.c.state.in_(runs.MESSAGE_STATES))
        .order_by(r.c.id)
        .limit(MAX_RUNS)
    )


async def on_their_way(conn: AsyncConnection, chat_id: int) -> list:
    """The rows of ``_on_their_way``."""
    return list((await conn.execute(_on_their_way(chat_id))).all())


async def notes(conn: AsyncConnection, ids: list[int]) -> dict[int, tuple[list[dict], datetime | None]]:
    """(the notes of its worker since its last move that name a step, oldest first, when that move was; None when its
    last RECENT_EVENTS events hold no move) of each run of ``ids``, which ``progress_step`` reads its step from."""
    e, r = tables.run_events, tables.runs
    recent = (
        select(
            e.c.run_id,
            e.c.at,
            e.c.kind,
            e.c.body["setup"].astext.label("setup"),
            e.c.body["repo"].astext.label("repo"),
        )
        .join_from(e, r, r.c.id == e.c.run_id)
        .where(one_of(e.c.run_id, ids), e.c.seq > r.c.event_seq - RECENT_EVENTS, e.c.kind.in_(("state", "system")))
        .order_by(e.c.run_id, e.c.seq)
    )
    notes: dict[int, list[dict]] = defaultdict(list)
    moved: dict[int, datetime] = {}
    for row in await conn.execute(recent):
        if row.kind == "state":  # the notes before the run's last move are of a step it left
            notes[row.run_id] = []
            moved[row.run_id] = row.at
        elif row.setup is not None:
            notes[row.run_id].append({chat_runs.SETUP_FIELD: row.setup, "repo": row.repo, "at": row.at})
    return {run_id: (notes.get(run_id, []), moved.get(run_id)) for run_id in ids}


async def inbox(conn: AsyncConnection, ids: list[int]) -> dict[int, list[tuple[int, datetime | None]]]:
    """The chat messages that wait, or waited, in the inbox of each run of ``ids``: (message id, when the worker
    handed it to the agent or None), oldest first."""
    i = tables.run_inbox
    query = (
        select(i.c.run_id, func.left(i.c.body, INBOX_HEAD_CHARS).label("head"), i.c.delivered_at)
        .where(one_of(i.c.run_id, ids), i.c.decision_id.is_(None))
        .order_by(i.c.id)
    )
    found: dict[int, list[tuple[int, datetime | None]]] = defaultdict(list)
    for row in await conn.execute(query):
        message_id = chat_runs.inbox_message(row.head)
        if message_id is not None:
            found[row.run_id].append((message_id, row.delivered_at))
    return found


async def answered(conn: AsyncConnection, chat_id: int, sessions: list) -> dict[str, tuple[int, datetime]]:
    """The latest message a run of each session of ``sessions`` (its runs on their way) wrote in its thread: (its id,
    when), by the session's branch."""
    cm, by = tables.chat_messages, tables.runs.alias("by")
    branches = sorted({run.branch for run in sessions if run.branch})
    if not branches:
        return {}
    query = (
        select(by.c.branch, func.max(cm.c.id).label("last_id"), func.max(cm.c.created_at).label("last_at"))
        .join_from(cm, by, by.c.id == cm.c.run_id)
        .where(
            cm.c.chat_id == chat_id,
            one_of(cm.c.thread_root_id, sorted({run.root_id for run in sessions})),
            by.c.chat_id == chat_id,
            by.c.kind == "chat",
            one_of(by.c.branch, branches),
        )
        .group_by(by.c.branch)
    )
    return {row.branch: (row.last_id, row.last_at) for row in await conn.execute(query)}
