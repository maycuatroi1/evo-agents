"""Logging a browser identity in (plan agent-browser, criterion a7): POST /v1/projects/{project}/browser-identities/{id}/login
queues a login session (a run of kind browser, ``evo_agents.hub.browser_login``), and .../disable and .../enable switch
an identity off and on. ``notify_needs_login`` tells an identity's owner it waits for a new login.

A login session is pinned to the worker that holds the identity or, for a Move (``worker`` names another one), to that
worker; the identity moves there when the session saves. The dispatcher opens it in the live view of the run and ends it
with Save (``POST .../runs/{id}/finish``) or cancels it.
"""

from __future__ import annotations

import logging

import psycopg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login, runs, tables
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.browser_identities import (
    REFUSALS,
    BrowserIdentity,
    IdentityId,
    identity_can_manage,
    identity_listing,
    identity_record,
    identity_view,
    identity_worker,
)
from evo_agents.hub.server.browser_profiles import locked
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import notify_queued
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service import dispatch
from evo_agents.hub.server.runs.service.audits import audit_run
from evo_agents.hub.server.runs.service.views import run_view
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/projects", tags=["browser"], responses={401: {"model": ErrorBody}})


class BrowserLogin(BaseModel):
    worker: str | None = Field(
        None,
        description="a Move: the name of the worker the identity should live on after the session saves (your own, "
        "or a shared one that serves the project and has the feature browser); null: the worker that holds it",
    )


async def _visible(conn: AsyncConnection, user: Principal, access: ProjectAccess, identity_id: int):
    """The identity, locked, when ``user`` may use it: a live one of the project, the member's own or the project's;
    404 otherwise."""
    row = await locked(conn, identity_id)
    visible = row is not None and row.project_id == access.project_id and row.deleted_at is None
    if not visible or (row.owner_kind == "member" and row.owner_id != user.user_id):
        raise HTTPException(404, f"project {access.name} has no browser identity {identity_id} of yours or its own")
    return row


def _manager(row, user: Principal, access: ProjectAccess, what: str) -> None:
    if not identity_can_manage(row, user, access):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"{what} a browser identity of project {access.name} needs the admin role on it; you hold {held}"
        )


async def _open_session(conn: AsyncConnection, identity_id: int) -> int | None:
    """The id of a login session of the identity that has not ended, if there is one."""
    r = tables.runs
    query = select(r.c.id).where(
        r.c.kind == browser_login.KIND,
        r.c.browser_identities.contains([identity_id]),
        r.c.state.not_in(runs.TERMINAL_STATES),
    )
    return (await conn.execute(query)).scalars().first()


@router.post(
    "/{project}/browser-identities/{identity_id}/login", status_code=201, response_model=Run, responses=REFUSALS
)
async def login_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, body: BrowserLogin, user: CurrentUser
) -> Run:
    """Queue a login session of an identity: Chromium on its profile, on its worker, for at most 30 minutes, which you
    open in the live view in human mode and end with Save (the profile is sealed, its version raised) or by cancelling
    it (nothing is saved). With ``worker`` it is a Move: the session runs there and the identity moves after Save.
    409 while another run holds the identity or a session of it waits, and for a disabled one."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        dispatch.check_dispatcher(access)
        await dispatch.check_seat(conn, user)
        row = await _visible(conn, user, access, identity_id)
        move = body.worker is not None
        if move or row.owner_kind == "agent":
            _manager(row, user, access, "moving" if move else "logging in")
        if row.status == "disabled":
            raise HTTPException(409, f"browser identity {row.name} is disabled: enable it before it is logged in")
        if row.held_by_run is not None:
            raise HTTPException(409, f"run {row.held_by_run} holds the browser identity; log in after the run ends")
        waiting = await _open_session(conn, identity_id)
        if waiting is not None:
            raise HTTPException(409, f"login session {waiting} of this browser identity has not ended")
        worker_id = await identity_worker(conn, user, access, body.worker) if move else row.worker_id
        pinned = await dispatch.pinnable(conn, user, access, worker_id)
        title = browser_login.title_of(row.name, move=move and worker_id != row.worker_id, worker=pinned.name)
        problems = dispatch.unfit(pinned, project, browser_login.KIND, [], "any", browser=True)
        if problems:
            raise HTTPException(
                409,
                f"worker {pinned.name} cannot take this login session: {'; '.join(problems)}; nothing was dispatched",
            )
        values = {
            "kind": browser_login.KIND,
            "project_id": access.project_id,
            "title": title,
            "dispatched_by": user.user_id,
            "dispatched_via": dispatch_credential(user.kind),
            "pinned_worker_id": pinned.id,
            "requested_runtime": "any",
            "runtime": "any",
            "mode": "headless",
            "approval": "auto",
            "timeout_s": browser_login.TIMEOUT_MINUTES * 60,
            "max_attempts": browser_login.MAX_ATTEMPTS,
            "browser": True,
            "browser_identities": [identity_id],
        }
        try:
            run_id = await dispatch.insert_run(conn, values)
        except IntegrityError as exc:
            if isinstance(exc.orig, psycopg.errors.CheckViolation):
                raise HTTPException(422, "the login session could not be queued; nothing was dispatched") from None
            raise
        await notify_queued(conn, run_id)
        await audit_run(
            conn, user, access, audit.BROWSER_IDENTITY_LOGIN, f"{project} browser-identity:{identity_id} run:{run_id}"
        )
        view = await run_view(conn, run_id)
    log.info(
        "browser login session queued",
        extra={"project": project, "browser_identity": identity_id, "run_id": run_id, "worker_id": pinned.id},
    )
    return view


async def _set_status(
    request: Request, project: str, identity_id: int, user: Principal, *, disable: bool
) -> BrowserIdentity:
    b = tables.browser_identities
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = await _visible(conn, user, access, identity_id)
        _manager(row, user, access, "disabling" if disable else "enabling")
        if disable:
            if row.held_by_run is not None:
                raise HTTPException(409, f"run {row.held_by_run} holds the browser identity; disable it after the run")
            status = "disabled"
        else:
            if row.status != "disabled":
                raise HTTPException(409, f"browser identity {row.name} is not disabled")
            status = "active" if row.sha256 is not None else "needs_login"
        await conn.execute(update(b).values(status=status, updated_at=func.now()).where(b.c.id == identity_id))
        await identity_record(
            conn,
            user,
            access,
            audit.BROWSER_IDENTITY_DISABLE if disable else audit.BROWSER_IDENTITY_ENABLE,
            identity_id,
        )
        found = (await conn.execute(identity_listing(access.project_id).where(b.c.id == identity_id))).one()
    return identity_view(found, project, user, access)


@router.post("/{project}/browser-identities/{identity_id}/disable", response_model=BrowserIdentity, responses=REFUSALS)
async def disable_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> BrowserIdentity:
    """Disable an identity: no run attaches it and its login session is refused until it is enabled; its profile is
    kept. Its member, or an admin for the project's; 409 while a run holds it."""
    return await _set_status(request, project, identity_id, user, disable=True)


@router.post("/{project}/browser-identities/{identity_id}/enable", response_model=BrowserIdentity, responses=REFUSALS)
async def enable_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> BrowserIdentity:
    """Enable a disabled identity: active when it has a saved profile, else needs_login."""
    return await _set_status(request, project, identity_id, user, disable=False)


async def notify_needs_login(conn: AsyncConnection, identity, project: str) -> None:
    """Tell the owner of ``identity`` (a row of browser_identities; the creator for a project's) it needs a new login,
    in the Inbox and on Telegram, with a link to the page that logs it in."""
    user_id = identity.owner_id or identity.created_by
    if user_id is None:
        return
    await notify(
        conn,
        user_id=user_id,
        kind="notice",
        notice_kind=browser_login.NEEDS_LOGIN_NOTICE,
        title=browser_login.needs_login_title(identity.name),
        project_id=identity.project_id,
        details={"browser_identity": identity.id, "name": identity.name},
        link=f"/p/{project}/browser?login={identity.id}",
    )
