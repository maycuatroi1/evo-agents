"""Which features of the hub are on (criterion a11 of plan hub-agent-plugins; ``evo_agents.hub.features``).

- GET /v1/features, for anyone signed in: each feature core declares, the layers it may be set on and whether it is
  on for the hub, then the values of the layer of each project that sets something, among the projects the caller
  holds a grant on (every one for a hub admin). The web reads it to leave out of its menus what is off: a feature is
  on in a project when the project's values say so, else as the hub has it.
- GET and POST /v1/admin/features, GET .../versions and POST .../rollback: the hub's own layer, for hub admins.
- The same under /v1/admin/projects/{project}/features: a project's layer, for hub admins.

A write turns features on (true) or off (false) and unsets others, which then follow the layer below
(``hub_settings.write_layer``); a feature turned on or off for the whole hub only (chats, market) has no project
layer (422). The routes and the /mcp tools of a feature that is off answer 404 (``hub_settings.FeatureGate``,
``hub_settings.require_tool``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.hub_admin.models.features import READ_REFUSALS as READ_REFUSALS
from evo_agents.hub.server.hub_admin.models.features import WRITE_REFUSALS as WRITE_REFUSALS
from evo_agents.hub.server.hub_admin.models.features import Features as Features
from evo_agents.hub.server.hub_admin.models.features import FeatureState as FeatureState
from evo_agents.hub.server.hub_admin.models.features import ProjectFeatures as ProjectFeatures
from evo_agents.hub.server.hub_admin.service import features as service
from evo_agents.hub.server.hub_admin.service.features import HUB as HUB
from evo_agents.hub.server.hub_settings import SettingsLayer, SettingsRollback, SettingsWrite
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin

router = APIRouter(prefix="/v1", tags=["features"], responses={401: {"model": ErrorBody}})
admin_router = APIRouter(
    prefix="/v1/admin",
    tags=["features"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


@router.get("/features", response_model=Features)
async def list_features(request: Request, user: CurrentUser) -> Features:
    """The features of the hub, whether each is on for the hub, and what each project's layer sets."""
    return await service.list_features(request.app.state.engine, user)


# The hub's own layer


@admin_router.get("/features", response_model=SettingsLayer)
async def hub_features(request: Request) -> SettingsLayer:
    """The current version of the hub's own layer of features."""
    return await service.hub_layer(request.app.state.engine)


@admin_router.get("/features/versions", response_model=list[SettingsLayer])
async def hub_feature_versions(request: Request) -> list[SettingsLayer]:
    """Every version of the hub's own layer of features, the newest first."""
    return await service.hub_versions(request.app.state.engine)


@admin_router.post("/features", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def write_hub_features(request: Request, body: SettingsWrite, user: AdminUser) -> SettingsLayer:
    """Write the next version of the hub's own layer: the features ``values`` turns on or off, those ``unset``
    clears."""
    return await service.write_hub(request.app.state.engine, user, body)


@admin_router.post("/features/rollback", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def rollback_hub_features(request: Request, body: SettingsRollback, user: AdminUser) -> SettingsLayer:
    """Copy an older version of the hub's own layer of features as its next version."""
    return await service.rollback_hub(request.app.state.engine, user, body)


# A project's layer


@admin_router.get("/projects/{project}/features", response_model=SettingsLayer, responses=READ_REFUSALS)
async def project_features(request: Request, project: ProjectName) -> SettingsLayer:
    """The current version of the project's layer of features."""
    return await service.project_layer(request.app.state.engine, project)


@admin_router.get("/projects/{project}/features/versions", response_model=list[SettingsLayer], responses=READ_REFUSALS)
async def project_feature_versions(request: Request, project: ProjectName) -> list[SettingsLayer]:
    """Every version of the project's layer of features, the newest first."""
    return await service.project_versions(request.app.state.engine, project)


@admin_router.post("/projects/{project}/features", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def write_project_features(
    request: Request, project: ProjectName, body: SettingsWrite, user: AdminUser
) -> SettingsLayer:
    """Write the next version of the project's layer: the features ``values`` turns on or off for the project, those
    ``unset`` clears, which then follow the hub's own layer."""
    return await service.write_project(request.app.state.engine, user, project, body)


@admin_router.post("/projects/{project}/features/rollback", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def rollback_project_features(
    request: Request, project: ProjectName, body: SettingsRollback, user: AdminUser
) -> SettingsLayer:
    """Copy an older version of the project's layer of features as its next version."""
    return await service.rollback_project(request.app.state.engine, user, project, body)
