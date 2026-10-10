"""The chat of an author run (``evo_agents.hub.author``): the agent's messages and its owner's, the wait for a reply,
and its end. ``docs/workers.md`` (Author runs) is the protocol.

POST /v1/worker/runs/{id}/chat takes the last message of a turn of the agent of an author run the worker holds (404
for any other run), at most ``author.MAX_CHAT_BYTES`` of UTF-8, and keeps it as a ``system`` event of the hub's own
whose body holds ``"chat": "agent"``, numbered in the run's log as every event is. The worker posts it before it
reports ``waiting``. The owner's messages are the run's ``user_message`` events, which POST .../runs/{id}/messages
writes (``run_events``): so the chat is in the order of the log, the agent's messages between the owner's.

GET /v1/projects/{p}/runs/{id}/chat (a reader of the run, ``runs.readable_run``) returns the chat of an author run: its
messages and those of the runs it resumes and that resume it (a parked run and the run a reply queued, through
``resume_of_run_id``), oldest first, at most ``author.MAX_CHAT_MESSAGES`` of the latest; the run of them that takes
the next message (the latest), its state, whose turn it is (``author.chat_status``) and the plan it wrote or revises.
404 for a run of another kind, which has no chat. The chat lives as long as the run's log: the daily prune deletes the
events of runs that ended EVO_HUB_RUN_LOG_DAYS ago.

When the worker reports ``waiting`` for an author run (``runs.report_state``), the hub sends its owner the notice
``author_waiting`` (``notify_waiting``) with the agent's last message, which the web shows and Telegram sends as its
label rule lets it: a restricted project's message holds its name, the kind of thing that waits and the link alone
(``evo_agents.hub.telegram.minimal_message``). The notices of the run its owner has not read yet are read then: one
waits at a time.

POST /v1/projects/{p}/runs/{id}/finish is the owner's end of the chat (another member 403, a run of another kind 409):
a parked author run ends ``done`` at once; for one a worker holds, the hub records the ask (``finish_requested_at``),
the next heartbeat says ``finish``, and the worker ends the run ``done`` once the agent's turn is over, never ``failed``
or ``cancelled``. A queued run, whose agent has not started, is cancelled instead (409), and a run that ended takes no
end (409). It is audited (run.finish), once per run.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, case, func, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_state import move_run, write_event
from evo_agents.hub.server.runs.models import REFUSALS, Run, RunId
from evo_agents.hub.server.runs.service.audits import audit_run
from evo_agents.hub.server.runs.service.controls import ask_once, owned_run
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.runs.service.views import readable_run, run_view
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

WAITING_TITLE = "Author run #{id} waits for your reply"


def chat_link(project: str, run_id: int) -> str:
    """The web page of an author run's chat, as its notice links to it."""
    return f"/p/{project}/runs/{run_id}?tab=chat"


# Models


class ChatPost(BaseModel):
    text: str = Field(
        min_length=1, max_length=author.MAX_CHAT_BYTES, description="the agent's last message of a turn, at most 16 KiB"
    )

    @model_validator(mode="after")
    def _bounded(self):
        if not self.text.strip():
            raise ValueError("a message of the chat needs some text")
        if len(self.text.encode()) > author.MAX_CHAT_BYTES:
            raise ValueError(f"a message of the chat is at most {author.MAX_CHAT_BYTES} bytes of UTF-8")
        return self


class ChatPosted(BaseModel):
    run_id: int
    seq: int = Field(description="its event in the run's log")


class ChatMessage(BaseModel):
    run_id: int = Field(description="the run whose log holds it: the run read, or one it resumes or that resumes it")
    seq: int = Field(description="its event in that run's log")
    author: Literal["agent", "owner"]
    login: str | None = Field(description="the owner's login for the owner's message; null for the agent's")
    text: str
    at: datetime


class RunChat(BaseModel):
    run_id: int = Field(description="the run of the chat that takes the next message: the latest that resumes it")
    state: Literal[runs.RUN_STATES] = Field(description="that run's state")
    status: Literal[author.CHAT_STATUSES] = Field(
        description="working: the agent works on its turn; waiting: it waits for your reply (waiting or parked); "
        "ended: the run ended"
    )
    owner: str = Field(description="the login of the run's owner, the one member who replies")
    plan_id: str | None = Field(description="the plan the run wrote or revises; null before its first put")
    plan_revision: int | None = Field(description="that plan's revision the run last wrote, or was dispatched on")
    messages: list[ChatMessage] = Field(description="oldest first")
    more: bool = Field(
        description=f"older messages were left out: the chat shows its {author.MAX_CHAT_MESSAGES} latest"
    )


# The chain of runs one chat spans


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
    return tables.run_events.c.kind == "user_message"


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


async def _kind(conn: AsyncConnection, run_id: int) -> str:
    return (await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == run_id))).scalar_one()


# The worker's side


@worker_router.post(
    "/runs/{run_id}/chat",
    status_code=201,
    response_model=ChatPosted,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def post_chat(request: Request, run_id: RunId, body: ChatPost, user: CurrentUser) -> ChatPosted:
    """Keep the last message of a turn of the agent of an author run this worker holds as a message of its chat."""
    async with request.app.state.engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RUN_KINDS)
        if row.kind != "author":
            raise HTTPException(404, f"run {run_id} is not an author run: only an author run has a chat")
        seq = await write_event(conn, run_id, {"text": body.text, "chat": author.CHAT_AGENT})
    log.info("chat message of the agent", extra={"run_id": run_id, "seq": seq})
    return ChatPosted(run_id=run_id, seq=seq)


async def notify_waiting(conn: AsyncConnection, run_id: int) -> int:
    """Send the owner of author run ``run_id``, which has just moved to waiting, the notice author_waiting with the
    agent's last message of the chat, and read the run's earlier ones the owner has not read; its id."""
    from evo_agents.hub.server import notifications  # it reads runs through the routes that import this module

    r, p, e, n = tables.runs, tables.projects, tables.run_events, tables.notifications
    query = (
        select(r.c.dispatched_by, r.c.project_id, p.c.name, r.c.plan_id, r.c.plan_revision)
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
    )
    found = (await conn.execute(query)).one()
    last = select(e.c.body["text"].astext).where(e.c.run_id == run_id, _is_agent()).order_by(e.c.seq.desc()).limit(1)
    said = (await conn.execute(last)).scalar_one_or_none()
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(
            n.c.user_id == found.dispatched_by,
            n.c.run_id == run_id,
            n.c.notice_kind == "author_waiting",
            n.c.read_at.is_(None),
        )
    )
    details = {"plan_id": found.plan_id, "plan_revision": found.plan_revision} if found.plan_id else None
    return await notifications.notify(
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


@router.get(
    "/{project}/runs/{run_id}/chat",
    response_model=RunChat,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def read_chat(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> RunChat:
    """The chat of an author run: the agent's messages and its owner's, oldest first, and whose turn it is."""
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        kind = await _kind(conn, run_id)
        if kind != "author":
            raise HTTPException(404, f"run {run_id} is a {kind} run: only an author run has a chat")
        rows = (await conn.execute(_messages(run_id, author.MAX_CHAT_MESSAGES + 1))).all()
        latest = (await conn.execute(_latest(run_id))).one()
    more = len(rows) > author.MAX_CHAT_MESSAGES
    shown = [ChatMessage(**row._mapping) for row in reversed(rows[: author.MAX_CHAT_MESSAGES])]
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


@router.post("/{project}/runs/{run_id}/finish", response_model=Run, responses=REFUSALS)
async def finish(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """End the chat of an author run one dispatched: a parked one is done at once, and the worker holding one ends it
    done once its agent's turn is over."""
    async with request.app.state.engine.begin() as conn:
        access, row = await owned_run(conn, user, project, run_id, "end the chat of")
        state, kind = row[2], row[11]
        if kind != "author":
            article = "an" if kind[:1] in "aeiou" else "a"
            raise HTTPException(
                409, f"run {run_id} is {article} {kind} run: only an author run has a chat to end; cancel it instead"
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
        elif not await ask_once(conn, run_id, tables.runs.c.finish_requested_at):
            return await run_view(conn, run_id)  # asked already: nothing changes, nothing is audited
        await audit_run(conn, user, access, audit.RUN_FINISH, f"{project}/author run:{run_id}")
        view = await run_view(conn, run_id)
    log.info("author chat ended", extra={"run_id": run_id, "state": view.state, "login": user.login})
    return view
