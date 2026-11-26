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
chat's streams (``chats.repository.store.wake``) when a chat run moves, when its worker sends such a note and when its
worker takes its inbox; a stream asked for progress (``chats.routes.events``) sends it at once, then each time it
changed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs
from evo_agents.hub.server.chats.models.views import ChatProgress
from evo_agents.hub.server.chats.repository import progress as store
from evo_agents.hub.server.chats.repository.views import message_rows
from evo_agents.hub.server.chats.service.access import label_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.service.views import sees_unplanned
from evo_agents.hub.server.security import Principal


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


def _since(run, step_note: dict | None, moved_at: datetime | None) -> datetime:
    """When the step of ``run`` began: the note that started it, else the run's last move."""
    if step_note is not None:
        return step_note["at"]
    by_state = {"queued": run.queued_at, "leased": run.leased_at}.get(run.state, run.started_at)
    return moved_at or by_state or run.leased_at or run.queued_at


async def chat_progress(conn: AsyncConnection, user: Principal, chat_id: int) -> list[ChatProgress]:
    """The progress of every message of chat ``chat_id`` whose run is on its way, as ``user`` may read it (see the
    module's docstring), by message then run."""
    found = await store.on_their_way(conn, chat_id)
    if not found:
        return []
    readable = await _readable(conn, user, {run.project for run in found})
    shown = [run for run in found if run.project in readable]
    if not shown:
        return []
    ids = [run.id for run in shown]
    notes = await store.notes(conn, ids)
    inbox = await store.inbox(conn, ids)
    answered = await store.answered(conn, chat_id, shown)
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
    """``progress`` (``chats.service.progress``) by message id, each message's in run order."""
    grouped: dict[int, list[ChatProgress]] = defaultdict(list)
    for item in progress:
        grouped[item.message_id].append(item)
    return dict(grouped)
