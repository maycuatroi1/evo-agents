"""How far the agents' runs that took messages of a group chat are, as the thread shows it under those messages
(``docs/hub.md``, Group chats): by rule, from each run's state and its worker's notes (``chat_runs.progress_step``),
with no model asked anything and no chat message, notice or Telegram message written for it.

A chat run on its way (``runs.MESSAGE_STATES``) shows under the message that started it until a run of its session
writes in the thread after that message, and under each message that waits in its inbox, read from the inbox's own
rows (``chat_runs.inbox_message``), so a message the run before left untaken shows the run that took it, until a run
of the session writes after the worker handed it to the agent. Such a message is at step inbox while the agent works
on before it takes it (``chat_runs.taken_step``). A reader sees the progress of a message whose label they clear, of a
run they may read (``runs.service.views.readable_run``: a grant on its project, and its default label through the
project's hub sink); for anyone else, the chat reads as before: the mention's run and the reactions.

A run's step comes from its last RECENT_EVENTS events: its last move, and the notes of its worker after it that name
``chat_runs.SETUP_FIELD``, already through ``credentials.scrub`` on the worker as every event is. The hub wakes the
chat's streams (``chat_store.wake``) when a chat run moves, when its worker sends such a note and when its worker takes
its inbox; a stream asked for progress (``chat_events``) sends it at once, then each time it changed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs, runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.chat_access import label_reader
from evo_agents.hub.server.chat_views import ChatProgress, message_rows
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.service.views import sees_unplanned
from evo_agents.hub.server.security import Principal

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


async def _readable(conn: AsyncConnection, user: Principal, projects: set[str]) -> set[str]:
    """The projects of ``projects`` whose chat runs ``user`` may read: those of no plan (``readable_run``)."""
    found = set()
    for name in sorted(projects):
        try:
            access = await project_access(conn, user, name)
        except HTTPException:
            continue
        if access.role is not None and sees_unplanned(access, None):
            found.add(name)
    return found


async def _notes(conn: AsyncConnection, ids: list[int]) -> dict[int, tuple[list[dict], datetime | None]]:
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


def _since(run, step_note: dict | None, moved_at: datetime | None) -> datetime:
    """When the step of ``run`` began: the note that started it, else the run's last move."""
    if step_note is not None:
        return step_note["at"]
    by_state = {"queued": run.queued_at, "leased": run.leased_at}.get(run.state, run.started_at)
    return moved_at or by_state or run.leased_at or run.queued_at


async def _inbox(conn: AsyncConnection, ids: list[int]) -> dict[int, list[tuple[int, datetime | None]]]:
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


async def _answered(conn: AsyncConnection, chat_id: int, sessions: list) -> dict[str, tuple[int, datetime]]:
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


async def chat_progress(conn: AsyncConnection, user: Principal, chat_id: int) -> list[ChatProgress]:
    """The progress of every message of chat ``chat_id`` whose run is on its way, as ``user`` may read it (see the
    module's docstring), by message then run."""
    found = (await conn.execute(_on_their_way(chat_id))).all()
    if not found:
        return []
    readable = await _readable(conn, user, {run.project for run in found})
    shown = [run for run in found if run.project in readable]
    if not shown:
        return []
    ids = [run.id for run in shown]
    notes, inbox, answered = await _notes(conn, ids), await _inbox(conn, ids), await _answered(conn, chat_id, shown)
    wanted = {run.chat_message_id for run in shown} | {m for run in shown for m, _ in inbox.get(run.id, [])}
    rows = await message_rows(conn, chat_id, sorted(wanted))
    reader = await label_reader(conn, user.user_id, [row.label for row in rows])
    messages = {row.id: row for row in rows if reader.sees(row.label)}
    progress = []
    for run in shown:
        since_move, moved_at = notes[run.id]
        step, repo, note = chat_runs.progress_step(run.state, since_move)
        since = _since(run, note, moved_at)
        last_id, last_at = answered.get(run.branch, (None, None))
        base = {"run_id": run.id, "handle": run.handle, "worker": run.worker, "repo": repo}
        trigger = messages.get(run.chat_message_id)
        if trigger is not None and (last_id is None or last_id < trigger.id):
            progress.append(ChatProgress(message_id=trigger.id, step=step, since=since, **base))
        for message_id, delivered_at in inbox.get(run.id, []):
            message = messages.get(message_id)
            if message is None or message_id == run.chat_message_id:
                continue
            if delivered_at is not None and last_at is not None and last_at > delivered_at:
                continue  # its session answered once the agent had it
            taken = chat_runs.taken_step(step, run.state, waits=delivered_at is None)
            at = max(since, message.created_at) if taken == "inbox" else since
            item = {**base, "repo": repo if taken == "opening" else None}
            progress.append(ChatProgress(message_id=message_id, step=taken, since=at, **item))
    return sorted(progress, key=lambda item: (item.message_id, item.run_id))


def by_message(progress: list[ChatProgress]) -> dict[int, list[ChatProgress]]:
    """``progress`` (``chat_progress``) by message id, each message's in run order."""
    grouped: dict[int, list[ChatProgress]] = defaultdict(list)
    for item in progress:
        grouped[item.message_id].append(item)
    return dict(grouped)
