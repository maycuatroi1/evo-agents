"""The routes of notifications. Each passes the request's parts to the service.

``router``: a member reads their own notifications: GET /v1/me/notifications, GET /v1/me/notifications/count and POST
/v1/me/notifications/read (``service.reads``). ``worker_router``: POST /v1/worker/runs/{id}/notices takes a notice
from the worker holding a plan run (``service.notices``). ``admin_router``: PUT and DELETE
/v1/admin/users/{login}/channels/{kind}, a hub admin's link of a member's channel (``service.admin_channels``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from evo_agents.hub import runs
from evo_agents.hub.server import route_autonomy
from evo_agents.hub.server.admin import MISSING, PROJECT_NAME, Login
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications.models import (
    MAX_LIST,
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    ChannelKind,
    ChannelLink,
    ChannelLinked,
    ChannelUnlinked,
    NoticeIn,
    Notification,
    NotificationCount,
    NotificationList,
    ReadRequest,
    ReadResult,
)
from evo_agents.hub.server.notifications.service import admin_channels, notices, reads
from evo_agents.hub.server.ops.repository import views as ops_views
from evo_agents.hub.server.runs.models import MAX_ID, REFUSALS, RunId
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin

router = APIRouter(prefix="/v1/me", tags=["notifications"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
admin_router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(admin)], responses={401: {"model": ErrorBody}}
)


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
    return await reads.notification_list(
        request.app.state.engine,
        user.user_id,
        unread=unread,
        kind=kind,
        project=project,
        run=run,
        q=q,
        handler=handler,
        limit=limit,
        offset=offset,
    )


@router.get("/notifications/count", response_model=NotificationCount)
async def count_notifications(request: Request, user: CurrentUser) -> NotificationCount:
    """How many of the caller's notifications are unread, and how many decisions wait for their answer."""
    return await reads.notification_count(request.app.state.engine, request.app.state.cache, user.user_id)


@router.post("/notifications/read", response_model=ReadResult, responses={422: {"model": ErrorBody}})
async def read_notifications(request: Request, body: ReadRequest, user: CurrentUser) -> ReadResult:
    """Mark the caller's notifications read: those named, or all of them."""
    return await reads.mark_read(request.app.state.engine, user, body)


@worker_router.post(
    "/runs/{run_id}/notices",
    status_code=201,
    response_model=Notification,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def send_notice(request: Request, run_id: RunId, body: NoticeIn, user: CurrentUser) -> Notification:
    """Notify the owner of a plan run this worker holds: a push or merge into a default branch, say."""
    return await notices.send_notice(request.app.state.engine, user, run_id, body)


@admin_router.put(
    "/users/{login}/channels/{kind}",
    response_model=ChannelLinked,
    responses={**MISSING, 422: {"model": ErrorBody}},
    dependencies=[route_autonomy.K12],
)
async def link_channel(
    request: Request, body: ChannelLink, login: Login, kind: ChannelKind, user: AdminUser
) -> ChannelLinked:
    """Link the channel of ``kind`` of member ``login`` with ``config``, as a hub admin, bound to no session of theirs,
    in place of the one they had."""
    return await admin_channels.link(request.app.state.engine, user, login, kind, body)


@admin_router.delete(
    "/users/{login}/channels/{kind}",
    response_model=ChannelUnlinked,
    responses={**MISSING},
    dependencies=[route_autonomy.K12],
)
async def unlink_channel(request: Request, login: Login, kind: ChannelKind, user: AdminUser) -> ChannelUnlinked:
    """Unlink the channel of ``kind`` of member ``login``, whoever linked it and whatever its session."""
    return await admin_channels.unlink(request.app.state.engine, user, login, kind)
