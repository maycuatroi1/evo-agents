"""The plugin's source of its project's knowledge graph, the contribution ``history`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): the messages of the group chats whose label holds a part of the project, as the hub
makes them into items through the API of core plugins (``core_plugin_api.chat_history_items``; the label rule of chats
and what each item is stay in the hub, ``evo_agents.hub.server.chat_graph``). The source gives nothing while the
project does not grant the plugin ``chats:read``.
"""

from __future__ import annotations

from evo_agents.hub.server import core_plugin_api as hub


def plugin() -> hub.Plugin:
    return hub.plugin(__name__)


async def read(conn, project: str) -> hub.KgSourceAnswer:
    """The answer of the source for ``project``."""
    return await hub.chat_history_items(conn, plugin(), project)


async def kg_source(params: dict, context: hub.Context) -> hub.KgSourceAnswer:
    """The contribution ``history``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``)."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    async with hub.transaction(context) as conn:
        return await read(conn, context.project or params["project"])


__all__ = ["kg_source", "read"]
