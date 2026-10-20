"""The plugin's source of its project's knowledge graph, the contribution ``history`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): the messages of the group chats whose label holds a part of the project, as the hub
makes them into items (``evo_agents.hub.server.chat_graph``, which holds the label rule of chats and says what each
item is). The source gives nothing while the project does not grant the plugin ``chats:read``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server import chat_graph
from evo_agents.hub.server.chat_graph import join, message_item, message_label

PLUGIN = "chats"


async def read(conn: AsyncConnection, source: str, project_id: int, project: str) -> dict:
    """The answer of the source for ``project``."""
    return await chat_graph.read(conn, PLUGIN, source, project_id, project)


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``history``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``)."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    async with context.engine.begin() as conn:
        return await read(conn, params["source"], context.project_id, context.project or params["project"])


__all__ = ["join", "kg_source", "message_item", "message_label", "read"]
