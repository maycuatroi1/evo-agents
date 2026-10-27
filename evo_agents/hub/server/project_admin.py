"""A self-managed project: what a hub admin turns on, and what the admins of that project then do themselves (plan
company-ops-on-hub, criteria a4 and a5; ``evo_agents.hub.self_managed`` holds the state).

For hub admins:

- GET /v1/admin/projects/{project}: ``self_managed``, ``trusted_owners``, the ladder and every grant of the project;
- PUT /v1/admin/projects/{project}/self-managed: turn the state on with the GitHub owners they trust (at least one), or
  off. Turning it off keeps the plugins installed and the grants given; the project's admins can no longer change them.
  Audited as project.self_managed.

For the admins of a self-managed project (anyone else gets 403, the 404 of ``project_access`` for a project they do not
see, and the agent of a run never):

- GET /v1/projects/{project}/admin: the same view as a hub admin's;
- PUT and DELETE /v1/projects/{project}/admin/grants/{login}: give, change and take back a grant, never a role or a max
  level above their own, nor touch a grant above their max level (``hub admin grant|revoke`` do it for any project);
- POST /v1/projects/{project}/admin/invites: a one-time link that joins the project, with the same limits;
- POST /v1/projects/{project}/plugins/install-github and POST .../plugins/{name}/remove: install, upgrade and remove an
  isolated plugin from the release of a repository of a trusted owner. The release's signer must be a workflow of that
  same repository (``sigstore:OWNER/REPO/.github/workflows/FILE.yml``), the hub trusts it for that release alone, and
  the plugin belongs to this project (``plugins.project_id``): no other project sees or enables it, and a name the hub
  or another project holds is refused. A plugin of any other owner still needs a hub admin.

Each is audited as the same action a hub admin's route writes, in the project (grant.put, grant.delete, invite.create,
plugin.install, plugin.upgrade, plugin.remove).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.self_managed import ProjectScope, SelfManaged, normalized, own_signer, state_of
from evo_agents.hub.server import admin as grants
from evo_agents.hub.server import audit, invites
from evo_agents.hub.server.admin import LOGIN_NAME, MISSING, Grant, GrantRequest, Login, ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.invites import InviteCreated, InviteRequest
from evo_agents.hub.server.market_install import (
    NOTHING_INSTALLED,
    TAG,
    GitHubInstallIn,
    Installing,
    MarketInstalled,
    install_github,
)
from evo_agents.hub.server.plugin_grants import check_admin
from evo_agents.hub.server.plugin_publishers import SIGNER
from evo_agents.hub.server.plugin_store import Plugin, find, from_image, plugin, set_removed, version_rows
from evo_agents.hub.server.project_plugins import PluginName
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import AdminUser, CurrentUser, Principal, admin
from evo_agents.plugins.market import REPO

OWNER = r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$"
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 502, 503)}

admin_router = APIRouter(
    prefix="/v1/admin/projects",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)
router = APIRouter(prefix="/v1/projects", tags=["project-admin"], responses={401: {"model": ErrorBody}})


class ProjectGrant(BaseModel):
    login: str
    role: Literal["reader", "writer", "admin"]
    max_level: str
    granted_by: str | None = Field(description="the login of whoever gave or last changed the grant")
    granted_at: datetime


class ProjectAdminView(BaseModel):
    name: str
    self_managed: bool = Field(description="whether the project's admins install plugins and give grants themselves")
    trusted_owners: list[str] = Field(description="the GitHub owners whose releases they install plugins from")
    levels: list[str] = Field(description="the project's label ladder, lowest first")
    grants: list[ProjectGrant] = Field(description="every grant of the project, by login")


class SelfManagedIn(BaseModel):
    enabled: bool
    trusted_owners: list[Annotated[str, Field(pattern=OWNER)]] = Field(
        default_factory=list,
        max_length=50,
        description="GitHub owners (users or organisations); at least one to turn on",
    )


class ProjectInstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no trust_publisher: the hub keeps no trust of a project's install

    repo: Annotated[str, Field(pattern=f"^{REPO.pattern}$", description="OWNER/REPO of a trusted owner")]
    tag: Annotated[str | None, Field(pattern=TAG)] = Field(
        None, description="the release's tag; the latest if left out"
    )
    signer: Annotated[str | None, Field(pattern=SIGNER)] = Field(
        None, description="a workflow of the repo that signs its releases; its ci.yml when left out"
    )


class ProjectInviteIn(BaseModel):
    role: Literal["reader", "writer", "admin"]
    max_level: str = Field(min_length=1, max_length=100)
    for_login: Annotated[str | None, Field(pattern=LOGIN_NAME)] = None
    days: int = Field(invites.DEFAULT_DAYS, ge=1, le=invites.MAX_DAYS)


# Reading


async def view_of(conn: AsyncConnection, project: str) -> ProjectAdminView:
    """The view of ``project``; 404 when the hub has none."""
    project_id, ladder = await grants.project_ladder(conn, project)
    state = await state_of(conn, project_id)
    g, u, by = tables.grants, tables.users, tables.users.alias("granter")
    rows = await conn.execute(
        select(u.c.login, g.c.role, g.c.max_level, by.c.login.label("granted_by"), g.c.granted_at)
        .join_from(g, u, u.c.id == g.c.user_id)
        .outerjoin(by, by.c.id == g.c.granted_by)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return ProjectAdminView(
        name=project,
        self_managed=state.enabled,
        trusted_owners=list(state.owners),
        levels=list(ladder),
        grants=[ProjectGrant(**row._asdict()) for row in rows],
    )


@admin_router.get("/{project}", response_model=ProjectAdminView, responses=MISSING)
async def show_project(request: Request, project: ProjectName) -> ProjectAdminView:
    """The project's self-managed state, trusted owners, ladder and grants."""
    async with request.app.state.engine.begin() as conn:
        return await view_of(conn, project)


@admin_router.put(
    "/{project}/self-managed", response_model=ProjectAdminView, responses={**MISSING, 422: {"model": ErrorBody}}
)
async def set_self_managed(
    request: Request, project: ProjectName, body: SelfManagedIn, user: AdminUser
) -> ProjectAdminView:
    """Turn the project's self-managed state on with the GitHub owners hub admins trust, or off."""
    owners = normalized(body.trusted_owners)
    if body.enabled and not owners:
        raise HTTPException(422, "a self-managed project names at least one trusted GitHub owner")
    if not body.enabled and owners:
        raise HTTPException(422, "turning self-managed off takes no trusted owners")
    p = tables.projects
    async with request.app.state.engine.begin() as conn:
        project_id, _ = await grants.project_ladder(conn, project)
        await conn.execute(
            update(p).where(p.c.id == project_id).values(self_managed=body.enabled, trusted_owners=list(owners))
        )
        target = (
            f"{project} self_managed=true owners={','.join(owners)}"
            if body.enabled
            else f"{project} self_managed=false"
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.PROJECT_SELF_MANAGED,
            target=target,
            project_id=project_id,
        )
        return await view_of(conn, project)


# What a project's admin may do


async def _own_project(
    conn: AsyncConnection, user: Principal, project: str, doing: str
) -> tuple[ProjectAccess, SelfManaged]:
    """The access of ``user`` to a self-managed project they are an admin of; 403 otherwise."""
    access = await project_access(conn, user, project)
    if user.acts_for_run is not None:
        raise HTTPException(403, f"{doing} is for an admin of project {project}, never for the agent of a run")
    check_admin(access, doing)
    state = await state_of(conn, access.project_id)
    if not state.enabled:
        raise HTTPException(
            403,
            f"project {project} is not self-managed, so {doing} is for a hub admin: one turns that on with "
            f"`evo-agents hub admin project self-managed {project} --on --owner OWNER`",
        )
    return access, state


def _rank(access: ProjectAccess, level: str) -> int:
    levels = list(access.rules.levels)
    return levels.index(level) if level in levels else len(levels)


def _within(access: ProjectAccess, role: str, max_level: str, doing: str) -> None:
    """403 when the role or the max level is above what the admin holds themselves."""
    if not has_role(access.role, role):
        raise HTTPException(403, f"{doing}: role {role} is above your own, {access.role}")
    if access.max_level is None or _rank(access, max_level) > _rank(access, access.max_level):
        raise HTTPException(
            403, f"{doing}: level {max_level} is above your own max level, {access.max_level}; nothing was changed"
        )


async def _held(conn: AsyncConnection, project_id: int, login: str):
    g, u = tables.grants, tables.users
    found = await conn.execute(
        select(g.c.role, g.c.max_level)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(
            g.c.project_id == project_id,
            u.c.login.ilike(login.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")),
        )
    )
    return found.first()


async def _not_above(conn: AsyncConnection, access: ProjectAccess, login: str, doing: str) -> None:
    """403 when ``login`` holds a grant above the admin's own: the admin cannot change or take back what they could not
    give."""
    held = await _held(conn, access.project_id, login)
    if held is not None:
        _within(access, held.role, held.max_level, doing)


@router.get("/{project}/admin", response_model=ProjectAdminView, responses=REFUSALS)
async def show_own_project(request: Request, project: ProjectName, user: CurrentUser) -> ProjectAdminView:
    """The self-managed state, trusted owners, ladder and grants of a project, for its admins and hub admins."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not user.admin or access.role is not None:
            check_admin(access, "reading the grants of the project")
        return await view_of(conn, project)


@router.put(
    "/{project}/admin/grants/{login}",
    response_model=Grant,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def put_grant(
    request: Request, project: ProjectName, login: Login, body: GrantRequest, user: CurrentUser
) -> Grant:
    """Give ``login`` a role on the project, or change the one it has, up to the admin's own role and max level."""
    doing = f"giving {login} a grant on project {project}"
    async with request.app.state.engine.begin() as conn:
        access, _ = await _own_project(conn, user, project, doing)
        _within(access, body.role, body.max_level, doing)
        await _not_above(conn, access, login, doing)
        given, taken = await grants.give_grant(conn, user, project, login, body)
    await grants.revoke_taken(request, taken)
    return given


@router.delete("/{project}/admin/grants/{login}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_grant(request: Request, project: ProjectName, login: Login, user: CurrentUser) -> Response:
    """Take back the grant of ``login`` on the project, if it is not above the admin's own role and max level."""
    doing = f"taking back the grant of {login} on project {project}"
    async with request.app.state.engine.begin() as conn:
        access, _ = await _own_project(conn, user, project, doing)
        await _not_above(conn, access, login, doing)
        taken = await grants.take_grant(conn, user, project, login)
    await grants.revoke_taken(request, taken)
    return Response(status_code=204)


@router.post(
    "/{project}/admin/invites",
    status_code=201,
    response_model=InviteCreated,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def make_invite(
    request: Request, response: Response, project: ProjectName, body: ProjectInviteIn, user: CurrentUser
) -> InviteCreated:
    """Make a one-time link that joins the project with a role and max level up to the admin's own."""
    doing = f"making an invite link to project {project}"
    async with request.app.state.engine.begin() as conn:
        access, _ = await _own_project(conn, user, project, doing)
        _within(access, body.role, body.max_level, doing)
        asked = InviteRequest(kind="join", project=project, **body.model_dump())
        invite, token = await invites.make_invite(conn, user, asked)
    response.headers.update(NO_STORE)
    return invites.created(request.app.state.config, invite, token)


# Plugins


def _signer_of(repo: str, signer: str | None) -> str:
    """The signer for ``repo``: a workflow of that same repository, its ci.yml by default; 422 for any other."""
    named = own_signer(repo, signer)
    if named is None:
        raise HTTPException(
            422,
            f"a self-managed project installs plugins signed by a workflow of the repository itself "
            f"(sigstore:{repo}/.github/workflows/FILE.yml), not {signer}; a hub admin trusts any other signer; "
            f"{NOTHING_INSTALLED}",
        )
    return named


@router.post("/{project}/plugins/install-github", response_model=MarketInstalled, responses=REFUSALS)
async def install_for_project(
    request: Request, project: ProjectName, body: ProjectInstallIn, user: CurrentUser
) -> MarketInstalled:
    """Install, or upgrade, the plugin of the release of a trusted owner's repository, for this project alone."""
    doing = f"installing a plugin from {body.repo}"
    async with request.app.state.engine.begin() as conn:
        access, state = await _own_project(conn, user, project, doing)
    if not state.trusts_repo(body.repo):
        owner = body.repo.partition("/")[0]
        raise HTTPException(
            403,
            f"{owner} is not a trusted owner of project {project} ({', '.join(state.owners)}): a hub admin installs "
            f"the plugin, or adds the owner; {NOTHING_INSTALLED}",
        )
    asked = GitHubInstallIn(repo=body.repo, tag=body.tag, signer=_signer_of(body.repo, body.signer))
    transport = getattr(request.app.state, "market_transport", None)
    return await install_github(
        Installing.of(request), transport, user, asked, ProjectScope(access.project_id, access.name)
    )


@router.post("/{project}/plugins/{name}/remove", response_model=Plugin, responses=REFUSALS)
async def remove_for_project(request: Request, project: ProjectName, name: PluginName, user: CurrentUser) -> Plugin:
    """Remove the installed version of a plugin this project installed; every version stays in the store."""
    doing = f"removing plugin {name}"
    async with request.app.state.engine.begin() as conn:
        access, _ = await _own_project(conn, user, project, doing)
        row = await find(conn, name, lock=True)
        if row is None or row.project_id != access.project_id:
            raise HTTPException(404, f"project {project} installed no plugin {name}: see its plugins page")
        if row.installed_version is None:
            raise HTTPException(409, f"plugin {name} is not installed")
        if from_image(await version_rows(conn, row.id)):
            raise HTTPException(409, f"plugin {name} ships in the hub's image")
        await set_removed(conn, user, row, project_id=access.project_id)
        return await plugin(conn, name, request.app.state.hub_version)
