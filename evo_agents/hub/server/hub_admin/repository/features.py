"""The SQL of which features of the hub are on (``evo_agents.hub.server.features``): the layers of the projects a
caller may see."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection


async def project_layers(conn: AsyncConnection, query):
    """(name, plain) of the layer of features of each project ``query`` (``hub_settings.project_layers_query``)
    reads."""
    return (await conn.execute(query)).all()
