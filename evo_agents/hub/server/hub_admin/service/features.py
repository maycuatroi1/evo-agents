"""Which features of the hub are on (``evo_agents.hub.server.features``): the features with the layers of the projects
a caller may see, and the hub's own layer and a project's, read, written and rolled back in a transaction each."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import features
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.hub_admin.models.features import Features, FeatureState, ProjectFeatures
from evo_agents.hub.server.hub_admin.repository import features as repository
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

HUB = Place(FEATURES)


async def list_features(engine: AsyncEngine, user) -> Features:
    """The features of the hub, whether each is on for the hub, and what each project's layer sets."""
    async with UnitOfWork(engine) as conn:
        instance, _ = await feature_values(conn)
        rows = await repository.project_layers(conn, project_layers_query(user))
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


def check(layer: str):
    """The check of a write to a layer of features on ``layer``."""

    def problems(sets: dict, after: dict) -> list[str]:
        return features.problems(sets, layer)

    return problems


async def hub_layer(engine: AsyncEngine) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await read_layer(conn, HUB)


async def hub_versions(engine: AsyncEngine) -> list[SettingsLayer]:
    async with UnitOfWork(engine) as conn:
        return await layer_versions(conn, HUB)


async def write_hub(engine: AsyncEngine, user, body: SettingsWrite) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await write_layer(conn, user, HUB, body, check(features.INSTANCE))


async def rollback_hub(engine: AsyncEngine, user, body: SettingsRollback) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await rollback_layer(conn, user, HUB, body)


async def project_layer(engine: AsyncEngine, project: str) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await read_layer(conn, await project_place(conn, FEATURES, project))


async def project_versions(engine: AsyncEngine, project: str) -> list[SettingsLayer]:
    async with UnitOfWork(engine) as conn:
        return await layer_versions(conn, await project_place(conn, FEATURES, project))


async def write_project(engine: AsyncEngine, user, project: str, body: SettingsWrite) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        place = await project_place(conn, FEATURES, project)
        return await write_layer(conn, user, place, body, check(features.PROJECT))


async def rollback_project(engine: AsyncEngine, user, project: str, body: SettingsRollback) -> SettingsLayer:
    async with UnitOfWork(engine) as conn:
        return await rollback_layer(conn, user, await project_place(conn, FEATURES, project), body)
