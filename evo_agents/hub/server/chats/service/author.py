"""The chat of an author run (``evo_agents.hub.author``): the agent's messages and its owner's, the wait for a reply,
and its end. ``docs/workers.md`` (Author runs) is the protocol.

POST /v1/worker/runs/{id}/chat takes the last message of a turn of the agent of an author run the worker holds (404
for any other run but a chat run, whose message is its reply in the thread of its group chat, ``chats.service.runs``),
at most ``author.MAX_CHAT_BYTES`` of UTF-8, and keeps it as a ``system`` event of the hub's own whose body holds
``"chat": "agent"``, numbered in the run's log as every event is. The worker posts it before it reports ``waiting``. The
owner's messages are the run's ``user_message`` events, which POST .../runs/{id}/messages writes (``run_events``): so
the chat is in the order of the log, the agent's messages between the owner's. The owner's answer to a decision of the
run (``decisions``), a ``user_message`` event that names the decision, is no message of the chat: the decision shows it.

GET /v1/projects/{p}/runs/{id}/chat (a reader of the run, ``runs.readable_run``) returns the chat of an author run: its
messages and those of the runs it resumes and that resume it (a parked run and the run a reply queued, through
``resume_of_run_id``), oldest first, at most ``author.MAX_CHAT_MESSAGES`` of the latest; the run of them that takes
the next message (the latest), its state, whose turn it is (``author.chat_status``) and the plan it wrote or revises.
404 for a run of another kind, which has no chat. The chat lives as long as the run's log: the daily prune deletes the
events of runs that ended EVO_HUB_RUN_LOG_DAYS ago.

When the worker reports ``waiting`` for an author run (``runs.report_state``), the hub sends its owner the notice
``author_waiting`` (``notify_waiting``) with the agent's last message, which the web shows and Telegram sends as its
label rule lets it: a restricted project's message holds its name, the kind of thing that waits and the link alone
(``minimal_message`` of the core plugin telegram's ``model``). The notices of the run its owner has not read yet are
read then: one waits at a time. A run that moves to waiting with a decision of its own still open sends no notice: the
decision's notification told its owner already, and Home lists the run through the decision.

POST /v1/projects/{p}/runs/{id}/finish is the owner's end of the chat (another member 403, a run of another kind 409):
a parked author run ends ``done`` at once; for one a worker holds, the hub records the ask (``finish_requested_at``),
the next heartbeat says ``finish``, and the worker ends the run ``done`` once the agent's turn is over, never ``failed``
or ``cancelled``. A queued run, whose agent has not started, is cancelled instead (409), and a run that ended takes no
end (409). It is audited (run.finish), once per run.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import author, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.author import WAITING_TITLE, ChatMessage, RunChat, chat_link
from evo_agents.hub.server.chats.repository import author as store
from evo_agents.hub.server.run_state import move_run, write_event
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service.audits import audit_run
from evo_agents.hub.server.runs.service.controls import owned_run
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.runs.service.views import readable_run, run_view
from evo_agents.hub.server.security import Principal

# The worker's side


async def post_chat(engine: AsyncEngine, user: Principal, run_id: int, text: str) -> int:
    """Keep ``text``, the last message of a turn of the agent of run ``run_id`` the worker ``user`` holds, as a message
    of its chat (of a chat run, its reply in its thread); its event's seq."""
    async with UnitOfWork(engine) as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RUN_KINDS)
        if row.kind == "chat":  # its reply in the thread of its group chat
            # it reads runs through this module's package
            from evo_agents.hub.server.chats.service.runs import agent_reply

            return await agent_reply(conn, run_id, text)
        if row.kind != "author":
            raise HTTPException(404, f"run {run_id} is not an author run: only an author run has a chat")
        return await write_event(conn, run_id, {"text": text, "chat": author.CHAT_AGENT})


async def notify_waiting(conn: AsyncConnection, run_id: int) -> int | None:
    """Send the owner of author run ``run_id``, which has just moved to waiting, the notice author_waiting with the
    agent's last message of the chat, and read the run's earlier ones the owner has not read; its id. None, and no
    notice, while a decision of the run is open: the owner has its notification already."""
    from evo_agents.hub.server.notifications.service.notifier import (
        NOTIFIER,  # it reads runs through the routes that import this module
    )

    found = await store.waiting_run(conn, run_id)
    said = await store.last_said(conn, run_id)
    await store.read_waiting(conn, found.dispatched_by, run_id)
    if await store.decision_open(conn, run_id):
        return None
    details = {"plan_id": found.plan_id, "plan_revision": found.plan_revision} if found.plan_id else None
    return await NOTIFIER.notify(
        conn,
        user_id=found.dispatched_by,
        kind="notice",
        notice_kind="author_waiting",
        project_id=found.project_id,
        run_id=run_id,
        title=WAITING_TITLE.format(id=run_id),
        body=said,
        details=details,
        link=chat_link(found.name, run_id),
    )


# The owner's side


async def read_chat(engine: AsyncEngine, user: Principal, project: str, run_id: int, sink: str | None) -> RunChat:
    """The chat of author run ``run_id`` of ``project`` as ``user`` reads it through ``sink``."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        kind = await store.kind_of(conn, run_id)
        if kind != "author":
            raise HTTPException(404, f"run {run_id} is a {kind} run: only an author run has a chat")
        rows = await store.messages(conn, run_id, author.MAX_CHAT_MESSAGES + 1)
        latest = await store.latest(conn, run_id)
        more = len(rows) > author.MAX_CHAT_MESSAGES
        shown = [
            ChatMessage(**{**row._mapping, "attachments": row.attachments or []})
            for row in reversed(rows[: author.MAX_CHAT_MESSAGES])
        ]
        return RunChat(
            run_id=latest.id,
            state=latest.state,
            status=author.chat_status(latest.state),
            owner=latest.login,
            plan_id=latest.plan_id,
            plan_revision=latest.plan_revision,
            messages=shown,
            more=more,
        )


async def finish(engine: AsyncEngine, user: Principal, project: str, run_id: int) -> tuple[Run, bool]:
    """End the chat of author run ``run_id`` of ``project`` that ``user`` dispatched: the run as it reads now, and
    whether this asked it (False when it was asked already: nothing changed, nothing is audited)."""
    async with UnitOfWork(engine) as conn:
        access, row = await owned_run(conn, user, project, run_id, "end the chat of")
        state, kind = row[2], row[11]
        if kind not in ("author", "browser"):
            article = "an" if kind[:1] in "aeiou" else "a"
            raise HTTPException(
                409,
                f"run {run_id} is {article} {kind} run: only an author run has a chat to end, and a login session "
                "its Save; cancel it instead",
            )
        if state in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is {state}: its chat is over")
        if state == "queued":
            raise HTTPException(
                409, f"run {run_id} is queued: its agent has not started, so it has no chat to end; cancel it instead"
            )
        if state == "parked":
            reason = f"{user.login} ended the chat"
            await move_run(conn, run_id, "parked", "done", "owner", reason=reason, token_id=user.token_id)
        elif not await store.ask_finish(conn, run_id):
            return await run_view(conn, run_id), False
        await audit_run(conn, user, access, audit.RUN_FINISH, f"{project}/author run:{run_id}")
        return await run_view(conn, run_id), True
