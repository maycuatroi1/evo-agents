"""What logging a browser identity in allows: a login session queued on its worker (or, for a Move, on another), an
identity switched off and on, and its owner told it waits for a new login (``notify_needs_login``)."""

from __future__ import annotations

import logging

import psycopg
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.server import audit
from evo_agents.hub.server.agents.service import placement as agent_placement
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.browser.models.identities import BrowserIdentity
from evo_agents.hub.server.browser.models.logins import BrowserLogin
from evo_agents.hub.server.browser.repository import identities as identities_repository
from evo_agents.hub.server.browser.repository import logins as repository
from evo_agents.hub.server.browser.repository.profiles import locked
from evo_agents.hub.server.browser.service.identities import (
    identity_can_manage,
    identity_record,
    identity_worker,
    shown,
)
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import notify_queued
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service import dispatch
from evo_agents.hub.server.runs.service.views import run_view
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


async def _visible(conn: AsyncConnection, user: Principal, access: ProjectAccess, identity_id: int):
    """The identity, locked, when ``user`` may use it: a live one of the project, the member's own or the project's;
    404 otherwise."""
    row = await locked(conn, identity_id)
    if (
        row is None
        or row.project_id != access.project_id
        or row.deleted_at is not None
        or (row.owner_kind == "member" and row.owner_id != user.user_id)
    ):
        raise NotFound(f"project {access.name} has no browser identity {identity_id} of yours or its own")
    return row


def _manager(row, user: Principal, access: ProjectAccess, what: str) -> None:
    if not identity_can_manage(row, user, access):
        held = access.role or "no grant"
        raise Forbidden(
            f"{what} a browser identity of project {access.name} needs the admin role on it; you hold {held}"
        )


async def _loggable(conn: AsyncConnection, user: Principal, access: ProjectAccess, identity_id: int, move: bool):
    """The identity, locked, when ``user`` may queue its login session now; 409 while a run or a session holds it."""
    row = await _visible(conn, user, access, identity_id)
    if move or row.owner_kind == "agent":
        _manager(row, user, access, "moving" if move else "logging in")
    if row.status == "disabled":
        raise Conflict(f"browser identity {row.name} is disabled: enable it before it is logged in")
    if row.held_by_run is not None:
        raise Conflict(f"run {row.held_by_run} holds the browser identity; log in after the run ends")
    waiting = await repository.open_session(conn, identity_id)
    if waiting is not None:
        raise Conflict(f"login session {waiting} of this browser identity has not ended")
    return row


async def _pinned(conn: AsyncConnection, user: Principal, access: ProjectAccess, row, worker_id: int, project: str):
    """The worker the session is pinned to and the agent's home; 409 when it is no node of the home's pool, or
    cannot take the session."""
    pinned = await dispatch.pinnable(conn, user, access, worker_id)
    home = await (
        agent_placement.home_of(conn, row.profile_id)
        if row.owner_kind == "agent"
        else agent_placement.default_home(conn, access.project_id)
    )
    if home is not None and pinned.pool != home.pool:
        raise Conflict(
            f"the agent whose runs these are runs in pool mode, on the nodes of pool {home.pool}, and worker "
            f"{pinned.name} is not one: move the browser identity to a node of the pool (worker); nothing was "
            "dispatched",
        )
    problems = dispatch.unfit(pinned, project, browser_login.KIND, [], "any", browser=True)
    if problems:
        raise Conflict(
            f"worker {pinned.name} cannot take this login session: {'; '.join(problems)}; nothing was dispatched",
        )
    return pinned, home


async def queue_login(
    conn: AsyncConnection, user: Principal, project: str, identity_id: int, body: BrowserLogin
) -> Run:
    """Queue a login session of the identity, pinned to its worker or, for a Move, to ``body.worker``."""
    access = await project_access(conn, user, project)
    dispatch.check_dispatcher(access)
    await dispatch.check_seat(conn, user)
    move = body.worker is not None
    row = await _loggable(conn, user, access, identity_id, move)
    worker_id = await identity_worker(conn, user, access, body.worker) if body.worker is not None else row.worker_id
    pinned, home = await _pinned(conn, user, access, row, worker_id, project)
    title = browser_login.title_of(row.name, move=move and worker_id != row.worker_id, worker=pinned.name)
    values = {
        "kind": browser_login.KIND,
        "project_id": access.project_id,
        "title": title,
        "dispatched_by": user.user_id,
        "dispatched_via": dispatch_credential(user.kind),
        "pinned_worker_id": pinned.id,
        **(
            {"profile_id": row.profile_id, "profile_version": await repository.profile_version(conn, row.profile_id)}
            if home is not None and row.owner_kind == "agent"
            else {}
        ),
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
            raise Unprocessable("the login session could not be queued; nothing was dispatched") from None
        raise
    await notify_queued(conn, run_id)
    target = f"{project} browser-identity:{identity_id} run:{run_id}"
    await Auditor.of(conn, user, access).record(audit.BROWSER_IDENTITY_LOGIN, target)
    view = await run_view(conn, run_id)
    log.info(
        "browser login session queued",
        extra={"project": project, "browser_identity": identity_id, "run_id": run_id, "worker_id": pinned.id},
    )
    return view


async def set_status(
    conn: AsyncConnection, user: Principal, project: str, identity_id: int, *, disable: bool
) -> BrowserIdentity:
    """Disable the identity, or enable a disabled one: active when it has a saved profile, else needs_login."""
    access = await project_access(conn, user, project)
    row = await _visible(conn, user, access, identity_id)
    _manager(row, user, access, "disabling" if disable else "enabling")
    if disable:
        if row.held_by_run is not None:
            raise Conflict(f"run {row.held_by_run} holds the browser identity; disable it after the run")
        status = "disabled"
    else:
        if row.status != "disabled":
            raise Conflict(f"browser identity {row.name} is not disabled")
        status = "active" if row.sha256 is not None else "needs_login"
    await identities_repository.set_status(conn, identity_id, status)
    action = audit.BROWSER_IDENTITY_DISABLE if disable else audit.BROWSER_IDENTITY_ENABLE
    await identity_record(conn, user, access, action, identity_id)
    return await shown(conn, user, access, project, identity_id)


async def notify_needs_login(conn: AsyncConnection, identity, project: str) -> None:
    """Tell the owner of ``identity`` (a row of browser_identities; the creator for a project's) it needs a new login,
    in the Inbox and on Telegram, with a link to the page that logs it in."""
    user_id = identity.owner_id or identity.created_by
    if user_id is None:
        return
    await NOTIFIER.notify(
        conn,
        user_id=user_id,
        kind="notice",
        notice_kind=browser_login.NEEDS_LOGIN_NOTICE,
        title=browser_login.needs_login_title(identity.name),
        project_id=identity.project_id,
        details={"browser_identity": identity.id, "name": identity.name},
        link=f"/p/{project}/browser?login={identity.id}",
    )
