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

from fastapi import APIRouter, Depends, Request, Response

from evo_agents.hub.server import admin as grants
from evo_agents.hub.server.admin import MISSING, Grant, GrantRequest
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.base.models import Login, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.invites import InviteCreated
from evo_agents.hub.server.market.models.install import MarketInstalled
from evo_agents.hub.server.market.service.install import Installing, install_github
from evo_agents.hub.server.plugins.models.store import Plugin, PluginName
from evo_agents.hub.server.project_registry.models.project_admin import OWNER as OWNER
from evo_agents.hub.server.project_registry.models.project_admin import (
    ProjectAdminView,
    ProjectInstallIn,
    ProjectInviteIn,
    SelfManagedIn,
)
from evo_agents.hub.server.project_registry.models.project_admin import ProjectGrant as ProjectGrant
from evo_agents.hub.server.project_registry.service import project_admin as service
from evo_agents.hub.server.project_registry.service.project_admin import view_of as view_of
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 502, 503)}

admin_router = APIRouter(
    prefix="/v1/admin/projects",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)
router = APIRouter(prefix="/v1/projects", tags=["project-admin"], responses={401: {"model": ErrorBody}})


@admin_router.get("/{project}", response_model=ProjectAdminView, responses=MISSING)
async def show_project(request: Request, project: ProjectName) -> ProjectAdminView:
    """The project's self-managed state, trusted owners, ladder and grants."""
    return await service.show_project(request.app.state.engine, project)


@admin_router.put(
    "/{project}/self-managed", response_model=ProjectAdminView, responses={**MISSING, 422: {"model": ErrorBody}}
)
async def set_self_managed(
    request: Request, project: ProjectName, body: SelfManagedIn, user: AdminUser
) -> ProjectAdminView:
    """Turn the project's self-managed state on with the GitHub owners hub admins trust, or off."""
    return await service.set_self_managed(request.app.state.engine, user, project, body)


@router.get("/{project}/admin", response_model=ProjectAdminView, responses=REFUSALS)
async def show_own_project(request: Request, project: ProjectName, user: CurrentUser) -> ProjectAdminView:
    """The self-managed state, trusted owners, ladder and grants of a project, for its admins and hub admins."""
    return await service.show_own_project(request.app.state.engine, user, project)


@router.put(
    "/{project}/admin/grants/{login}",
    response_model=Grant,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def put_grant(
    request: Request, project: ProjectName, login: Login, body: GrantRequest, user: CurrentUser
) -> Grant:
    """Give ``login`` a role on the project, or change the one it has, up to the admin's own role and max level."""
    given, taken = await service.put_grant(request.app.state.engine, user, project, login, body)
    await grants.revoke_taken(request, taken)
    return given


@router.delete("/{project}/admin/grants/{login}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_grant(request: Request, project: ProjectName, login: Login, user: CurrentUser) -> Response:
    """Take back the grant of ``login`` on the project, if it is not above the admin's own role and max level."""
    taken = await service.delete_grant(request.app.state.engine, user, project, login)
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
    state = request.app.state
    created = await service.make_invite(state.engine, state.config, user, project, body)
    response.headers.update(NO_STORE)
    return created


@router.post("/{project}/plugins/install-github", response_model=MarketInstalled, responses=REFUSALS)
async def install_for_project(
    request: Request, project: ProjectName, body: ProjectInstallIn, user: CurrentUser
) -> MarketInstalled:
    """Install, or upgrade, the plugin of the release of a trusted owner's repository, for this project alone."""
    asked, scope = await service.install_asked(request.app.state.engine, user, project, body)
    transport = getattr(request.app.state, "market_transport", None)
    return await install_github(Installing.of(request), transport, user, asked, scope)


@router.post("/{project}/plugins/{name}/remove", response_model=Plugin, responses=REFUSALS)
async def remove_for_project(request: Request, project: ProjectName, name: PluginName, user: CurrentUser) -> Plugin:
    """Remove the installed version of a plugin this project installed; every version stays in the store."""
    state = request.app.state
    return await service.remove(state.engine, state.hub_version, user, project, name)
