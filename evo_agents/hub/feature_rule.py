"""Whether a feature of the hub is on in a project (``evo_agents.hub.features.enabled``), as SQL: for the queries of the
hub's api and its job worker that leave out the projects a feature is off for (the Curator's night shift, the
plugins' extension points that canvas, srs and tasks run on: ``evo_agents.hub.plugin_calls.enablements``).

Outside ``evo_agents.hub.server``, so the job worker uses it too.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import false, func, null, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import features, tables

FEATURES = "features"  # hub_settings.kind of the features' layers (tables_settings.KINDS)


def feature_on(feature_id: str, project_id):
    """Whether feature ``feature_id`` is on in the project whose id is ``project_id`` (a column or a value), as a
    boolean SQL expression: the current version of the project's layer when it sets the feature, else the hub's
    own, else the feature's default."""
    feature = features.BY_ID[feature_id]
    s = tables.hub_settings.c

    def layer(where):
        newest = select(s["values"][feature_id].as_boolean()).where(s.kind == FEATURES, where)
        return newest.order_by(s.version.desc()).limit(1).scalar_subquery()

    own = layer(s.project_id == project_id) if feature.per_project() else null()
    return func.coalesce(own, layer(s.project_id.is_(None)), true() if feature.default else false())


async def on_since(conn: AsyncConnection, feature_id: str, project_id: int) -> datetime | None:
    """Since when feature ``feature_id`` is on in project ``project_id`` without a break, as the layer that decides
    it has it (the project's while it sets the feature, else the hub's): the time of the first version of the run of
    versions that end at the current one and turn it on. None while it is off, or on by default only."""
    s = tables.hub_settings.c
    for where in (s.project_id == project_id, s.project_id.is_(None)):
        query = (
            select(s.changed_at, s["values"].label("plain")).where(s.kind == FEATURES, where).order_by(s.version.desc())
        )
        rows = (await conn.execute(query)).all()
        if not rows or feature_id not in rows[0].plain:
            continue
        since = None
        for row in rows:
            if row.plain.get(feature_id) is not True:
                break
            since = row.changed_at
        return since
    return None
