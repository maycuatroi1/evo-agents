"""A browser identity logged in on the worker's own machine, outside any run: /v1/worker/browser-identities (plan
agent-browser, criterion a9).

The owner of a machine with a screen runs ``evo-agents worker browser login <identity>`` there: Chromium opens with the
identity's profile, the owner signs in and closes the window, and the command sends the profile back. Both routes
answer the worker's token alone, in the name of the member who owns the worker, who must hold the role the identity
asks (writer for their own or the project's, admin for an agent's) on its project.

POST .../login opens the identity: it names it by project, owner kind and name, and creates it, held by this worker,
when it has none and says its domains. An identity of another worker, held by a run, disabled, or with a login session
waiting is 409. The answer is the lease of a run's identity (``browser_leases.BrowserProfileLease``) with the profile
opened, and the audit row is browser_identity.lease with " local" in its target.

PUT .../{identity}/profile takes the profile back as the next version, as a run's save does (413 past
MAX_ARCHIVE_BYTES, 422 for an archive that is not one, 409 for a save that lost or while a run holds the identity), and
sets the identity active on this worker. The audit row is browser_identity.save.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_archive, browser_login, profiles, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.browser_identities import (
    MAX_DOMAINS,
    OWNERS,
    BrowserIdentityCreate,
    Domain,
    IdentityName,
    _may_create,
    agent_profile_id,
    check_create,
    insert_identity,
)
from evo_agents.hub.server.browser_leases import (
    REFUSALS,
    BrowserProfileLease,
    BrowserProfileSaved,
    ProfileRoute,
    _sealer,
)
from evo_agents.hub.server.browser_logins import _open_session
from evo_agents.hub.server.browser_profiles import (
    MAX_ARCHIVE_BYTES,
    ProfileConflict,
    ProfileTooLarge,
    open_profile,
    save_profile,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.models.browser import CHROMIUM_VERSION
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.sealing import Unsealable
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

TOO_LARGE = f"a browser profile is at most {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB packed; nothing was saved"
LocalId = Annotated[int, Path(ge=1, le=MAX_ID, description="a browser identity this worker logged in")]
router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
upload_router = APIRouter(
    prefix="/v1/worker", tags=["worker protocol"], route_class=ProfileRoute, responses={401: {"model": ErrorBody}}
)


class LocalLoginAsk(BaseModel):
    project: str = Field(description="the project of the identity")
    name: IdentityName
    owner: Literal[OWNERS] = Field("member", description="whose identity: yours, the project's or an agent's")
    agent: str | None = Field(None, pattern=profiles.HANDLE_PATTERN, description="the agent's handle, for owner agent")
    domains: list[Domain] = Field(
        [], max_length=MAX_DOMAINS, description="the sites a new identity keeps; needed to create one, else ignored"
    )
    login_url: str | None = Field(None, max_length=browser_login.MAX_URL_CHARS, description="of a new identity")


class LocalLogin(BrowserProfileLease):
    project: str
    created: bool = Field(description="whether this login created the identity")


def _need(access: ProjectAccess, owner: str) -> None:
    """403 unless the member holds the role the identity of ``owner`` asks of them to log it in."""
    needed = "admin" if owner == "agent" else "writer"
    if not has_role(access.role, needed):
        held = access.role or "no grant"
        raise HTTPException(
            403,
            f"logging in a browser identity of project {access.name} needs the {needed} role; the worker's owner holds {held}",
        )


async def _existing(conn: AsyncConnection, access: ProjectAccess, user, body: LocalLoginAsk):
    b = tables.browser_identities
    query = select(b).where(
        b.c.project_id == access.project_id,
        b.c.deleted_at.is_(None),
        b.c.owner_kind == body.owner,
        b.c.name == body.name,
    )
    if body.owner == "member":
        query = query.where(b.c.owner_id == user.user_id)
    elif body.owner == "agent":
        query = query.where(b.c.profile_id == await agent_profile_id(conn, access, body.agent))
    return (await conn.execute(query.with_for_update())).one_or_none()


@router.post("/browser-identities/login", response_model=LocalLogin, responses=REFUSALS)
async def open_local_login(request: Request, body: LocalLoginAsk, user: CurrentUser) -> LocalLogin:
    """The identity, with its profile, for a login on this worker's own machine; created, held by this worker, when it
    does not exist yet."""
    engine = request.app.state.engine
    created = False
    async with engine.begin() as conn:
        worker = await worker_of(conn, user)
        access = await project_access(conn, user, body.project)
        if (body.owner == "agent") != (body.agent is not None):
            raise HTTPException(422, "agent names the agent profile of an identity with owner agent, and only of one")
        row = await _existing(conn, access, user, body)
        if row is None:
            if not body.domains:
                raise HTTPException(
                    422,
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
            _may_create(access, body.owner)
            identity_id = await insert_identity(conn, user, access, create, worker.id)
            created = True
            b = tables.browser_identities
            row = (await conn.execute(select(b).where(b.c.id == identity_id))).one()
        else:
            _need(access, body.owner)
            _refuse(row, worker.id, await _open_session(conn, row.id))
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.BROWSER_IDENTITY_LEASE,
            target=f"{body.project} browser-identity:{row.id} version={row.version} local",
            project_id=access.project_id,
        )
    keys = None if row.sha256 is None else (blob_store(request), _sealer(request))
    status, version, profile = row.status, row.version, None
    if keys is not None:
        try:
            opened = await open_profile(engine, *keys, row.id)
        except (Unsealable, ProfileConflict) as exc:
            log.warning("browser profile not opened", extra={"browser_identity": row.id, "error": str(exc)})
        else:
            if opened is not None:
                version, archive = opened
                profile = base64.b64encode(archive).decode("ascii")
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


def _refuse(row, worker_id: int, session: int | None) -> None:
    if row.worker_id != worker_id:
        raise HTTPException(
            409,
            f"browser identity {row.name} is held by another worker: log it in there, or Move it to this one on the "
            "Browser identities page",
        )
    if row.held_by_run is not None:
        raise HTTPException(409, f"run {row.held_by_run} holds the browser identity; log in after the run ends")
    if row.status == "disabled":
        raise HTTPException(409, f"browser identity {row.name} is disabled: enable it before it is logged in")
    if session is not None:
        raise HTTPException(409, f"login session {session} of this browser identity has not ended")


@upload_router.put(
    "/browser-identities/{identity_id}/profile",
    response_model=BrowserProfileSaved,
    responses={**REFUSALS, 413: {"model": ErrorBody}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "the archive of the profile, as `evo_agents.hub.browser_archive.pack` makes it",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def save_local_profile(
    request: Request,
    identity_id: LocalId,
    user: CurrentUser,
    base_version: Annotated[int, Query(ge=0, le=MAX_ID, description="the version the login started from")],
    chromium_version: Annotated[
        str | None, Query(pattern=CHROMIUM_VERSION, description="the worker's Chromium, as it says its version")
    ] = None,
) -> BrowserProfileSaved:
    """Seal the profile a login on the worker's machine leaves as the identity's next version."""
    state = request.app.state
    archive = await request.body()
    b = tables.browser_identities
    async with state.engine.begin() as conn:
        worker = await worker_of(conn, user)
        row = (await conn.execute(select(b).where(b.c.id == identity_id).with_for_update())).one_or_none()
        if row is None or row.deleted_at is not None or row.worker_id != worker.id:
            raise HTTPException(404, f"this worker holds no browser identity {identity_id}")
        mine = row.owner_kind == "project" or row.owner_id == user.user_id or row.owner_kind == "agent"
        project = (
            await conn.execute(select(tables.projects.c.name).where(tables.projects.c.id == row.project_id))
        ).scalar_one()
        access = await project_access(conn, user, project)
        if not mine:
            raise HTTPException(404, f"this worker holds no browser identity {identity_id}")
        _need(access, row.owner_kind)
        store, sealer = blob_store(request), _sealer(request)
    try:
        await asyncio.to_thread(browser_archive.check, archive)
    except browser_archive.ProfileArchiveError as exc:
        raise HTTPException(422, f"{exc}; nothing was saved") from None
    try:
        version = await save_profile(
            state.engine,
            store,
            sealer,
            identity_id=identity_id,
            base_version=base_version,
            archive=archive,
            chromium_version=chromium_version,
            actor_id=user.user_id,
            token_id=user.token_id,
            login_worker_id=worker.id,
            unheld=True,
        )
    except ProfileTooLarge:
        raise HTTPException(413, TOO_LARGE) from None
    except ProfileConflict as exc:
        raise HTTPException(409, f"{exc}; nothing was saved") from None
    return BrowserProfileSaved(identity_id=identity_id, version=version)
