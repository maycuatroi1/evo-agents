"""The routes a member reads their notifications with, out of ``notifications``, the outbox and its channels.

A member reads their own notifications: GET /v1/me/notifications (open decisions and proposals first, then newest
first; filtered by unread, kind, project, run and words of the title or body, a page at a time), GET /v1/me/notifications/count (the unread ones, and
the decisions and the proposals still waiting for an answer, for the bell, which every page of the web asks every 10
seconds: the hub's cache keeps it COUNT_TTL seconds and drops it when a transaction that wrote a notification, a
decision, a proposal or a grant commits, ``cache.watch``), and POST /v1/me/notifications/read (by ids,
or all), audited as notification.read with the ids, never the text. A notification of a project the member no longer
holds a grant on is not shown. Answering a decision reads its notification too (``decisions``). A notification of kind
proposal is a tier 2 proposal of the Curator in the member's Inbox (``proposals``), and answering it reads it too.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, exists, false, func, or_, select, update

from evo_agents.hub import cache, runs, tables
from evo_agents.hub.server import audit, ops_views
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import Notification, notification_rows, notifications_of, shown
from evo_agents.hub.server.outputs.service import views as output_views
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

MAX_LIST = 200
COUNT_TTL = 60.0  # seconds the cache keeps a member's count; a write of what it is read from drops it before
MAX_OFFSET = 100_000
MAX_READ_IDS = 500
MAX_QUERY_CHARS = 200  # the words GET /v1/me/notifications searches the titles and bodies for

cache.watch("notifications", tables.notifications, tables.decisions, tables.proposals, tables.grants)

router = APIRouter(prefix="/v1/me", tags=["notifications"], responses={401: {"model": ErrorBody}})


def _visible(user_id: int):
    """The notifications of member ``user_id`` that are of no project, or of a project they hold a grant on."""
    stored, grants = tables.notifications, tables.grants
    granted = exists().where(grants.c.user_id == stored.c.user_id, grants.c.project_id == stored.c.project_id)
    return and_(stored.c.user_id == user_id, or_(stored.c.project_id.is_(None), granted))


def _open_decision():
    """A notification of a decision that still waits for its answer."""
    return and_(tables.notifications.c.kind == "decision", tables.decisions.c.state == "open")


def _open_proposal():
    """A notification of a proposal that still waits for its answer."""
    return and_(tables.notifications.c.kind == "proposal", tables.proposals.c.state == "open")


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches it anywhere, its own wildcards taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _listed(
    user_id: int,
    unread: bool,
    kind: str | None,
    project: str | None,
    run_id: int | None = None,
    text: str | None = None,
    handler: str | None = None,
) -> list:
    """The conditions of the list's filters, over ``_shown``: ``text`` matches anywhere in the title or the body."""
    stored = tables.notifications
    found = [_visible(user_id)]
    if unread:
        found.append(stored.c.read_at.is_(None))
    if kind is not None:
        found.append(stored.c.kind == kind)
    if project is not None:
        found.append(tables.projects.c.name == project)
    if run_id is not None:
        found.append(stored.c.run_id == run_id)
    if text:
        pattern = _like(text)
        found.append(or_(stored.c.title.ilike(pattern), stored.c.body.ilike(pattern)))
    if handler is not None:
        found.append(ops_views.held() if handler == "ops" else ~ops_views.held())
    return found


def _counts(user_id: int):
    """How many of the member's notifications are unread, and how many are of a decision or a proposal still open."""
    stored = tables.notifications
    return (
        select(
            func.count().filter(stored.c.read_at.is_(None)).label("unread"),
            func.count().filter(_open_decision()).label("open_decisions"),
            func.count().filter(_open_proposal()).label("open_proposals"),
        )
        .select_from(shown())
        .where(_visible(user_id))
    )


def _mark_read(user_id: int, ids: list[int] | None):
    """Mark the member's unread notifications read, those of ``ids`` or all of them when it is None; their ids."""
    stored = tables.notifications
    marked = update(stored).values(read_at=func.now()).where(_visible(user_id), stored.c.read_at.is_(None))
    if ids is not None:
        marked = marked.where(stored.c.id.in_(ids))
    return marked.returning(stored.c.id)


class NotificationList(BaseModel):
    notifications: list[Notification] = Field(description="open decisions and proposals first, then newest first")
    total: int = Field(description="notifications that match the filters")
    limit: int
    offset: int


class NotificationCount(BaseModel):
    unread: int = Field(description="notifications not read yet: the bell's number")
    open_decisions: int = Field(description="decisions that still wait for the member's answer, read or not")
    open_proposals: int = Field(0, description="proposals in the member's Inbox that wait for an answer, read or not")


class ReadRequest(BaseModel):
    ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] | None = Field(
        None, min_length=1, max_length=MAX_READ_IDS, description="the notifications to mark read"
    )
    all: bool = Field(False, description="mark every notification of the caller read")

    @model_validator(mode="after")
    def _one(self):
        if (self.ids is None) == (not self.all):
            raise ValueError("name the notifications to mark read with ids, or set all, not both")
        return self


class ReadResult(BaseModel):
    read: int = Field(description="notifications this request marked read")
    unread: int = Field(description="notifications left unread")


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    request: Request,
    user: CurrentUser,
    unread: Annotated[bool, Query(description="only the notifications not read yet")] = False,
    kind: Annotated[
        Literal[runs.NOTIFICATION_KINDS] | None,  # pyright: ignore[reportInvalidTypeForm]
        Query(),
    ] = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME, description="only this project's")] = None,
    run: Annotated[int | None, Query(ge=1, le=MAX_ID, description="only this run's")] = None,
    q: Annotated[
        str | None, Query(max_length=MAX_QUERY_CHARS, description="only those whose title or body holds these words")
    ] = None,
    handler: Annotated[
        Literal[ops_views.HANDLERS] | None,  # pyright: ignore[reportInvalidTypeForm]
        Query(description="ops: only those the Orchestrator of a default system holds; person: only the others"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> NotificationList:
    """The caller's notifications, open decisions and proposals first, then newest first."""
    found = _listed(user.user_id, unread, kind, project, run, (q or "").strip() or None, handler)
    waiting = or_(_open_decision(), _open_proposal())
    order = (func.coalesce(waiting, false()).desc(), tables.notifications.c.id.desc())
    listed = notification_rows().where(*found).order_by(*order).limit(limit).offset(offset)
    total = select(func.count()).select_from(shown()).where(*found)
    async with request.app.state.engine.begin() as conn:
        page = notifications_of((await conn.execute(listed)).all())
        matching = (await conn.execute(total)).scalar_one()
        carried = await output_views.by_notification(conn, page)
        holding = await ops_views.by_notification(conn, [item.id for item in page])
    page = [item.model_copy(update={"outputs": carried[item.id]}) if item.id in carried else item for item in page]
    page = [item.model_copy(update={"ops": holding[item.id]}) if item.id in holding else item for item in page]
    return NotificationList(notifications=page, total=matching, limit=limit, offset=offset)


@router.get("/notifications/count", response_model=NotificationCount)
async def count_notifications(request: Request, user: CurrentUser) -> NotificationCount:
    """How many of the caller's notifications are unread, and how many decisions wait for their answer."""

    async def load() -> dict:
        async with request.app.state.engine.begin() as conn:
            return dict((await conn.execute(_counts(user.user_id))).one()._mapping)

    key = f"notifications:count:{user.user_id}"
    return NotificationCount(**await request.app.state.cache.fetch(cache.HUB, key, COUNT_TTL, load))


@router.post("/notifications/read", response_model=ReadResult, responses={422: {"model": ErrorBody}})
async def read_notifications(request: Request, body: ReadRequest, user: CurrentUser) -> ReadResult:
    """Mark the caller's notifications read: those named, or all of them."""
    ids = None if body.all else list(dict.fromkeys(body.ids or []))
    async with request.app.state.engine.begin() as conn:
        marked = sorted((await conn.execute(_mark_read(user.user_id, ids))).scalars().all())
        if marked:
            target = "notifications:all" if body.all else "notifications:" + ",".join(map(str, marked))
            if body.all:
                target += f" count={len(marked)}"
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.NOTIFICATION_READ, target=target
            )
        unread = (await conn.execute(_counts(user.user_id))).one().unread
    log.info("notifications read", extra={"login": user.login, "read": len(marked)})
    return ReadResult(read=len(marked), unread=unread)
