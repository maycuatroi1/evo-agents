"""The plugin's source of its project's knowledge graph, the contribution ``contacts`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): one document of kind ``doc`` for each contact, which says three things and no more:
its login, its source and its stage. No touchpoint, no note, no time of a touchpoint and no link reaches the graph.
Each document carries the label of its row.
"""

from __future__ import annotations

from evo_agents.core_plugins.crm import store
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.kg.ids import utc_text

KIND = "contact"  # the kind in an item's id: plugin.crm:contact:<login>


def contact_item(source: str, row: store.Contact) -> dict:
    """The kg/1 item of a contact: login, source and stage."""
    return {
        "id": f"{source}:{KIND}:{row.login}",
        "kind": "doc",
        "title": row.login,
        "rev_time": utc_text(row.stage_at),
        "fragments": [
            {"anchor": "_preamble", "level": 0, "text": f"{row.login}: source {row.source}, stage {row.stage}"}
        ],
        "props": {"login": row.login, "source": row.source, "stage": row.stage},
        "label": row.label,
    }


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``contacts``: ``params`` names the source and the project."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    async with context.engine.begin() as conn:
        rows = await store.contacts(conn, context.project_id, limit=None)
    return {"items": [contact_item(params["source"], row) for row in rows], "identifiers": []}


__all__ = ["contact_item", "kg_source"]
