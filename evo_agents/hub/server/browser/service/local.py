"""What a login on the worker's own machine, outside any run, may do: open an identity (creating it, held by this
worker, when it has none), and save its profile back as the next version (see ``routes.local``)."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.browser.models.leases import BrowserProfileSaved
from evo_agents.hub.server.browser.models.local import LocalLogin, LocalLoginAsk
from evo_agents.hub.server.browser.repository import identities as identities_repository
from evo_agents.hub.server.browser.repository.logins import open_session
from evo_agents.hub.server.browser.service.identities import (
    agent_profile_id,
    check_create,
    insert_identity,
    may_create,
)
from evo_agents.hub.server.browser.service.leases import lease_sealer, opened_profile, save_archive
from evo_agents.hub.server.browser.service.shapes import BrowserIdentityCreate
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal


def _need(access: ProjectAccess, owner: str) -> None:
    """403 unless the member holds the role the identity of ``owner`` asks of them to log it in."""
    needed = "admin" if owner == "agent" else "writer"
    if not has_role(access.role, needed):
        held = access.role or "no grant"
        raise Forbidden(
            f"logging in a browser identity of project {access.name} needs the {needed} role; the worker's owner holds {held}",
        )


async def _existing(conn: AsyncConnection, access: ProjectAccess, user: Principal, body: LocalLoginAsk):
    profile_id = (
        await agent_profile_id(conn, access, body.agent) if body.owner == "agent" and body.agent is not None else None
    )
    return await identities_repository.named(
        conn, access.project_id, body.owner, body.name, user_id=user.user_id, profile_id=profile_id
    )


def _refuse(row, worker_id: int, session: int | None) -> None:
    if row.worker_id != worker_id:
        raise Conflict(
            f"browser identity {row.name} is held by another worker: log it in there, or Move it to this one on the "
            "Browser identities page",
        )
    if row.held_by_run is not None:
        raise Conflict(f"run {row.held_by_run} holds the browser identity; log in after the run ends")
    if row.status == "disabled":
        raise Conflict(f"browser identity {row.name} is disabled: enable it before it is logged in")
    if session is not None:
        raise Conflict(f"login session {session} of this browser identity has not ended")


async def _create(conn: AsyncConnection, user: Principal, access: ProjectAccess, body: LocalLoginAsk, worker):
    """Create the identity ``body`` names, held by ``worker``; its row."""
    if not body.domains:
        raise Unprocessable(
            f"project {body.project} has no browser identity {body.name} of that owner: say its sites "
            "(--domain) to create it; nothing was created",
        )
    create = BrowserIdentityCreate(
        name=body.name,
        domains=body.domains,
        worker=worker.name,
        owner=body.owner,
        agent=body.agent,
        login_url=body.login_url,
    )
    check_create(create)
    may_create(access, body.owner)
    identity_id = await insert_identity(conn, user, access, create, worker.id)
    row = await identities_repository.row(conn, identity_id)
    assert row is not None  # inserted just now
    return row


async def _opened(conn: AsyncConnection, user: Principal, body: LocalLoginAsk):
    """(row, created) of the identity ``body`` names, the lease audited."""
    worker = await worker_of(conn, user)
    access = await project_access(conn, user, body.project)
    if (body.owner == "agent") != (body.agent is not None):
        raise Unprocessable("agent names the agent profile of an identity with owner agent, and only of one")
    row = await _existing(conn, access, user, body)
    created = row is None
    if row is None:
        row = await _create(conn, user, access, body, worker)
    else:
        _need(access, body.owner)
        _refuse(row, worker.id, await open_session(conn, row.id))
    target = f"{body.project} browser-identity:{row.id} version={row.version} local"
    await Auditor.of(conn, user, access).record(audit.BROWSER_IDENTITY_LEASE, target)
    return row, created


async def open_local_login(request: Request, user: Principal, body: LocalLoginAsk) -> LocalLogin:
    """The identity, with its profile, for a login on this worker's own machine; created, held by this worker, when it
    does not exist yet."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        row, created = await _opened(conn, user, body)
    keys = None if row.sha256 is None else (blob_store(request), lease_sealer(request))
    status, version, profile = row.status, row.version, None
    if keys is not None:
        opened = await opened_profile(engine, *keys, row.id)
        if opened:
            version, profile = opened
    return LocalLogin(
        identity_id=row.id,
        project=body.project,
        created=created,
        name=row.name,
        domains=list(row.domains),
        status=status,
        version=version,
        chromium_version=row.chromium_version,
        profile=profile,
        login_url=row.login_url or browser_login.default_url(row.domains),
    )


async def save_local_profile(
    request: Request, user: Principal, identity_id: int, base_version: int, chromium_version: str | None
) -> BrowserProfileSaved:
    """Seal the profile a login on the worker's machine leaves as the identity's next version."""
    state = request.app.state
    archive = await request.body()
    async with state.engine.begin() as conn:
        worker = await worker_of(conn, user)
        row = await identities_repository.row(conn, identity_id, lock=True)
        if row is None or row.deleted_at is not None or row.worker_id != worker.id:
            raise NotFound(f"this worker holds no browser identity {identity_id}")
        mine = row.owner_kind == "project" or row.owner_id == user.user_id or row.owner_kind == "agent"
        project = await identities_repository.project_name(conn, row.project_id)
        access = await project_access(conn, user, project)
        if not mine:
            raise NotFound(f"this worker holds no browser identity {identity_id}")
        _need(access, row.owner_kind)
        store, sealer = blob_store(request), lease_sealer(request)
    version = await save_archive(
        state.engine,
        store,
        sealer,
        archive,
        identity_id=identity_id,
        base_version=base_version,
        chromium_version=chromium_version,
        actor_id=user.user_id,
        token_id=user.token_id,
        login_worker_id=worker.id,
        unheld=True,
    )
    return BrowserProfileSaved(identity_id=identity_id, version=version)
