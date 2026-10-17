"""Which features of the hub are on (criterion a11 of plan hub-agent-plugins; ``evo_agents.hub.features``).

- GET /v1/features, for anyone signed in: each feature core declares, the layers it may be set on and whether it is
  on for the hub, then the values of the layer of each project that sets something, among the projects the caller
  holds a grant on (every one for a hub admin). The web reads it to leave out of its menus what is off: a feature is
  on in a project when the project's values say so, else as the hub has it.
- GET and POST /v1/admin/features, GET .../versions and POST .../rollback: the hub's own layer, for hub admins.
- The same under /v1/admin/projects/{project}/features: a project's layer, for hub admins.

A write turns features on (true) or off (false) and unsets others, which then follow the layer below
(``hub_settings.write_layer``); a feature turned on or off for the whole hub only (chats, monitor) has no project
layer (422). The routes and the /mcp tools of a feature that is off answer 404 (``hub_settings.FeatureGate``,
``hub_settings.require_tool``).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from evo_agents.hub import features
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.hub_settings import (
    FEATURES,
    Place,
    SettingsLayer,
    SettingsRollback,
    SettingsWrite,
    feature_values,
    layer_versions,
    project_layers_query,
    project_place,
    read_layer,
    rollback_layer,
    write_layer,
)
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin

READ_REFUSALS = {404: {"model": ErrorBody}}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422)}
HUB = Place(FEATURES)

router = APIRouter(prefix="/v1", tags=["features"], responses={401: {"model": ErrorBody}})
admin_router = APIRouter(
    prefix="/v1/admin",
    tags=["features"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)


class FeatureState(BaseModel):
    id: str
    layers: list[Literal[features.LAYERS]] = Field(
        description="where it may be set: instance, the hub's own layer; project, a project's"
    )
    enabled: bool = Field(description="whether it is on for the hub: as the hub's own layer sets it, else on")


class ProjectFeatures(BaseModel):
    project: str
    values: dict[str, bool] = Field(description="the features its layer sets; any other is as the hub has it")


class Features(BaseModel):
    features: list[FeatureState]
    projects: list[ProjectFeatures] = Field(
        description="the projects whose layer sets something, among those the caller may see"
    )


@router.get("/features", response_model=Features)
async def list_features(request: Request, user: CurrentUser) -> Features:
    """The features of the hub, whether each is on for the hub, and what each project's layer sets."""
    async with request.app.state.engine.begin() as conn:
        instance, _ = await feature_values(conn)
        rows = (await conn.execute(project_layers_query(user))).all()
    states = [
        FeatureState(id=feature.id, layers=list(feature.layers), enabled=features.enabled(feature, instance))
        for feature in features.FEATURES
    ]
    projects = []
    for row in sorted(rows, key=lambda row: row.name):
        values = {name: bool(value) for name, value in row.plain.items() if name in features.BY_ID}
        if values:
            projects.append(ProjectFeatures(project=row.name, values=values))
    return Features(features=states, projects=projects)


def _check(layer: str):
    def check(sets: dict, after: dict) -> list[str]:
        return features.problems(sets, layer)

    return check


# The hub's own layer


@admin_router.get("/features", response_model=SettingsLayer)
async def hub_features(request: Request) -> SettingsLayer:
    """The current version of the hub's own layer of features."""
    async with request.app.state.engine.begin() as conn:
        return await read_layer(conn, HUB)


@admin_router.get("/features/versions", response_model=list[SettingsLayer])
async def hub_feature_versions(request: Request) -> list[SettingsLayer]:
    """Every version of the hub's own layer of features, the newest first."""
    async with request.app.state.engine.begin() as conn:
        return await layer_versions(conn, HUB)


@admin_router.post("/features", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def write_hub_features(request: Request, body: SettingsWrite, user: AdminUser) -> SettingsLayer:
    """Write the next version of the hub's own layer: the features ``values`` turns on or off, those ``unset``
    clears."""
    async with request.app.state.engine.begin() as conn:
        return await write_layer(conn, user, HUB, body, _check(features.INSTANCE))


@admin_router.post("/features/rollback", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def rollback_hub_features(request: Request, body: SettingsRollback, user: AdminUser) -> SettingsLayer:
    """Copy an older version of the hub's own layer of features as its next version."""
    async with request.app.state.engine.begin() as conn:
        return await rollback_layer(conn, user, HUB, body)


# A project's layer


@admin_router.get("/projects/{project}/features", response_model=SettingsLayer, responses=READ_REFUSALS)
async def project_features(request: Request, project: ProjectName) -> SettingsLayer:
    """The current version of the project's layer of features."""
    async with request.app.state.engine.begin() as conn:
        return await read_layer(conn, await project_place(conn, FEATURES, project))


@admin_router.get("/projects/{project}/features/versions", response_model=list[SettingsLayer], responses=READ_REFUSALS)
async def project_feature_versions(request: Request, project: ProjectName) -> list[SettingsLayer]:
    """Every version of the project's layer of features, the newest first."""
    async with request.app.state.engine.begin() as conn:
        return await layer_versions(conn, await project_place(conn, FEATURES, project))


@admin_router.post("/projects/{project}/features", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def write_project_features(
    request: Request, project: ProjectName, body: SettingsWrite, user: AdminUser
) -> SettingsLayer:
    """Write the next version of the project's layer: the features ``values`` turns on or off for the project, those
    ``unset`` clears, which then follow the hub's own layer."""
    async with request.app.state.engine.begin() as conn:
        place = await project_place(conn, FEATURES, project)
        return await write_layer(conn, user, place, body, _check(features.PROJECT))


@admin_router.post("/projects/{project}/features/rollback", response_model=SettingsLayer, responses=WRITE_REFUSALS)
async def rollback_project_features(
    request: Request, project: ProjectName, body: SettingsRollback, user: AdminUser
) -> SettingsLayer:
    """Copy an older version of the project's layer of features as its next version."""
    async with request.app.state.engine.begin() as conn:
        return await rollback_layer(conn, user, await project_place(conn, FEATURES, project), body)
