"""The browser identities of a project: /v1/projects/{project}/browser-identities (plan agent-browser, criterion a2).

A browser identity is a Chromium profile kept logged in to some sites, which runs of the project use on the one worker
that holds it (``evo_agents.hub.tables_browser``). A member's identity is theirs: they create it with the writer role
and only they see, use and delete it. A project's identity is the admins' to create and delete, and every writer may
attach it to a run. Its worker is the caller's own worker, not revoked, or a shared one, serves the project, and has
the feature browser: its daemon starts Chromium (``evo_agents.worker.browser``).

These routes deal in metadata alone: no answer, log line or audit row holds a cookie, a profile, a key or a hash of
one. The profiles themselves are sealed and kept by ``evo_agents.hub.server.browser_profiles``; deleting an identity
takes its keys from the row and records its blobs for deletion. Every write is an audit row (``audit.BROWSER_*``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from evo_agents.hub import browser_login, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.browser_profiles import locked, retire_profiles
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.worker_models import WORKER_NAME

log = logging.getLogger(__name__)

MAX_DOMAINS = 50
MAX_IDENTITIES = 200  # live identities of one project, its members' included
DOMAIN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
OWNERS = ("member", "project")
STATUSES = ("active", "needs_login", "disabled")
IdentityName = Annotated[
    str,
    Field(pattern=f"^{SECRET_NAME.pattern}$", description="unique among its owner's live identities in the project"),
]
Domain = Annotated[
    str, Field(max_length=253, pattern=DOMAIN, description="a host name in lower case, such as github.com")
]
IdentityId = Annotated[int, Path(ge=1, description="the identity's id (`evo-agents hub browser identity list`)")]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

router = APIRouter(prefix="/v1/projects", tags=["browser"], responses={401: {"model": ErrorBody}})


# Models


class BrowserIdentityCreate(BaseModel):
    name: IdentityName
    domains: list[Domain] = Field(
        min_length=1,
        max_length=MAX_DOMAINS,
        description="the sites whose cookies it keeps, each with its subdomains; the others are dropped at every save",
    )
    worker: str = Field(
        pattern=WORKER_NAME,
        description="the worker that holds it: your own, or a shared one, serving the project; its profile opens there "
        "alone",
    )
    owner: Literal[OWNERS] = Field(
        "member", description="member: yours alone (the writer role); project: the admins', usable by every writer"
    )
    login_url: str | None = Field(
        None,
        max_length=browser_login.MAX_URL_CHARS,
        description="the page a login session opens, an http or https URL; null: the home page of the first domain",
    )


class BrowserIdentity(BaseModel):
    id: int
    project: str
    name: str
    owner: Literal[OWNERS]
    owner_login: str | None = Field(description="the member who owns it; null for the project's")
    domains: list[str]
    login_url: str | None = Field(description="the page its login session opens; null: its first domain's home page")
    worker: str
    worker_owner: str = Field(description="the login of the worker's owner")
    status: Literal[STATUSES]
    version: int = Field(description="profiles saved so far; 0 until the first login is saved")
    size: int | None = Field(description="bytes of the current profile, sealed; null at version 0")
    chromium_version: str | None = Field(description="the Chromium that saved the current profile")
    held_by_run: int | None = Field(description="the run that holds it now")
    last_saved_at: datetime | None
    last_saved_by: str | None
    last_used_at: datetime | None
    created_at: datetime
    can_manage: bool = Field(description="whether you may delete it: its member, or an admin for the project's")


# Reading


def identity_listing(project_id: int):
    """The live identities of the project, joined to the names they show, ordered by owner then name."""
    b, w, u = tables.browser_identities, tables.workers, tables.users
    owner, saver, worker_owner = aliased(u), aliased(u), aliased(u)
    return (
        select(
            b,
            owner.c.login.label("owner_login"),
            saver.c.login.label("saver_login"),
            w.c.name.label("worker_name"),
            worker_owner.c.login.label("worker_owner_login"),
        )
        .join(w, w.c.id == b.c.worker_id)
        .join(worker_owner, worker_owner.c.id == w.c.owner_id)
        .outerjoin(owner, owner.c.id == b.c.owner_id)
        .outerjoin(saver, saver.c.id == b.c.last_saved_by)
        .where(b.c.project_id == project_id, b.c.deleted_at.is_(None))
        .order_by(b.c.owner_kind, b.c.name, b.c.id)
    )


def identity_can_manage(row, user: Principal, access: ProjectAccess) -> bool:
    if row.owner_kind == "member":
        return row.owner_id == user.user_id
    return has_role(access.role, "admin")


def identity_view(row, project: str, user: Principal, access: ProjectAccess) -> BrowserIdentity:
    return BrowserIdentity(
        id=row.id,
        project=project,
        name=row.name,
        owner=row.owner_kind,
        owner_login=row.owner_login,
        domains=list(row.domains),
        login_url=row.login_url,
        worker=row.worker_name,
        worker_owner=row.worker_owner_login,
        status=row.status,
        version=row.version,
        size=row.size,
        chromium_version=row.chromium_version,
        held_by_run=row.held_by_run,
        last_saved_at=row.last_saved_at,
        last_saved_by=row.saver_login,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        can_manage=identity_can_manage(row, user, access),
    )


@router.get("/{project}/browser-identities", response_model=list[BrowserIdentity], responses=REFUSALS)
async def list_browser_identities(
    request: Request, response: Response, project: ProjectName, user: CurrentUser
) -> list[BrowserIdentity]:
    """The project's identities and your own in it, never another member's; metadata only."""
    b = tables.browser_identities
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        mine = or_(b.c.owner_kind == "project", b.c.owner_id == user.user_id)
        rows = (await conn.execute(identity_listing(access.project_id).where(mine))).all()
    response.headers.update(NO_STORE)
    return [identity_view(row, project, user, access) for row in rows]


# Writing


async def identity_worker(conn: AsyncConnection, user: Principal, access: ProjectAccess, name: str) -> int:
    """The id of worker ``name``: the caller's own, not revoked, or a shared one of another member, serving the
    project, whose last heartbeat listed the feature browser; the caller's own when two share the name. 422
    otherwise."""
    w, wp = tables.workers, tables.worker_projects
    usable = select(w.c.id, w.c.owner_id, w.c.features).where(
        w.c.revoked_at.is_(None),
        func.lower(w.c.name) == name.lower(),
        or_(w.c.owner_id == user.user_id, w.c.shared.is_(True)),
        select(wp.c.worker_id).where(wp.c.worker_id == w.c.id, wp.c.project_id == access.project_id).exists(),
    )
    rows = (await conn.execute(usable)).all()
    own = [row for row in rows if row.owner_id == user.user_id]
    if len(own or rows) != 1:
        why = "is shared by more than one member" if rows else "is neither your worker, not revoked, nor a shared one"
        raise HTTPException(
            422,
            f"worker {name} {why} that serves project {access.name}: a browser identity is held by your own worker or "
            "a shared one that serves its project (GET /v1/workers)",
        )
    found = (own or rows)[0]
    if runs.BROWSER not in (found.features or ()):
        raise HTTPException(
            422,
            f"worker {name} does not report the feature browser, so it cannot open a browser identity: run "
            "`evo-agents worker browser install` on it and restart its daemon",
        )
    return found.id


def _may_create(access: ProjectAccess, owner: str) -> None:
    needed = "admin" if owner == "project" else "writer"
    if not has_role(access.role, needed):
        held = access.role or "no grant"
        whose = "the project's" if owner == "project" else "your own"
        raise HTTPException(
            403, f"creating {whose} browser identity in project {access.name} needs the {needed} role; you hold {held}"
        )


async def _taken(conn: AsyncConnection, user: Principal, access: ProjectAccess, body: BrowserIdentityCreate) -> None:
    """409 when the owner has a live identity of that name in the project, or the project has MAX_IDENTITIES."""
    b = tables.browser_identities
    live = (b.c.project_id == access.project_id, b.c.deleted_at.is_(None))
    owned = b.c.owner_kind == "project" if body.owner == "project" else b.c.owner_id == user.user_id
    same = select(b.c.id).where(*live, owned, b.c.owner_kind == body.owner, b.c.name == body.name)
    if (await conn.execute(same)).first() is not None:
        whose = "the project" if body.owner == "project" else "you"
        raise HTTPException(409, f"{whose} have a browser identity named {body.name} in project {access.name} already")
    count = (await conn.execute(select(func.count()).select_from(b).where(*live))).scalar_one()
    if count >= MAX_IDENTITIES:
        raise HTTPException(409, f"project {access.name} has {count} browser identities, the most it may have")


def _target(project: str, identity_id: int) -> str:
    return f"{project} browser-identity:{identity_id}"


async def identity_record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, identity_id: int):
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=_target(access.name, identity_id),
        project_id=access.project_id,
    )


@router.post("/{project}/browser-identities", status_code=201, response_model=BrowserIdentity, responses=REFUSALS)
async def create_browser_identity(
    request: Request, response: Response, project: ProjectName, body: BrowserIdentityCreate, user: CurrentUser
) -> BrowserIdentity:
    """Create an identity with no profile yet (version 0): your own with the writer role, or the project's as an
    admin. It is logged in later, on its worker."""
    b = tables.browser_identities
    domains = list(dict.fromkeys(body.domains))
    if body.login_url is not None and not browser_login.valid_url(body.login_url):
        raise HTTPException(422, "login_url is an http or https URL with a host; nothing was created")
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _may_create(access, body.owner)
        worker_id = await identity_worker(conn, user, access, body.worker)
        await _taken(conn, user, access, body)
        row = {
            "project_id": access.project_id,
            "owner_kind": body.owner,
            "owner_id": user.user_id if body.owner == "member" else None,
            "name": body.name,
            "domains": domains,
            "login_url": body.login_url,
            "worker_id": worker_id,
            "created_by": user.user_id,
        }
        try:
            identity_id = (await conn.execute(insert(b).values(row).returning(b.c.id))).scalar_one()
        except IntegrityError:  # another request created one of that name meanwhile
            raise HTTPException(
                409, f"a browser identity named {body.name} was just created in project {project}"
            ) from None
        await identity_record(conn, user, access, audit.BROWSER_IDENTITY_CREATE, identity_id)
        created = (await conn.execute(identity_listing(access.project_id).where(b.c.id == identity_id))).one()
    log.info(
        "browser identity created",
        extra={"project": project, "login": user.login, "browser_identity": identity_id, "owner": body.owner},
    )
    response.headers.update(NO_STORE)
    return identity_view(created, project, user, access)


@router.delete(
    "/{project}/browser-identities/{identity_id}", status_code=204, response_class=Response, responses=REFUSALS
)
async def delete_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> Response:
    """Delete an identity: its member, or an admin for the project's. Its keys go from the hub and its profiles from
    the blob store; 409 while a run holds it."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = await locked(conn, identity_id)
        visible = row is not None and row.project_id == access.project_id and row.deleted_at is None
        if not visible or (row.owner_kind == "member" and row.owner_id != user.user_id):
            raise HTTPException(404, f"project {project} has no browser identity {identity_id} of yours or its own")
        if not identity_can_manage(row, user, access):
            held = access.role or "no grant"
            raise HTTPException(
                403, f"deleting a browser identity of project {project} needs the admin role on it; you hold {held}"
            )
        if row.held_by_run is not None:
            raise HTTPException(409, f"run {row.held_by_run} holds the browser identity; delete it after the run ends")
        await retire_profiles(conn, row)
        await identity_record(conn, user, access, audit.BROWSER_IDENTITY_DELETE, identity_id)
    log.info(
        "browser identity deleted", extra={"project": project, "login": user.login, "browser_identity": identity_id}
    )
    return Response(status_code=204)
