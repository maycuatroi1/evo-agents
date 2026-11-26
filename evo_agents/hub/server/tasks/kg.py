"""The plugin's source of its project's knowledge graph, the contribution ``backlog`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): every task of the project.

A task is the item ``plugin.tasks:ticket:<T-42>`` of kind ``ticket``, a Ticket node: its number and title, its
description as its text (whose mentions of codes, plans and files the link stage links as in any document), its label,
the time it changed last, and as props its number, type, status, the status's group, priority, sprint, assignees and
due date. It declares the edges of its links: it ``implements`` each coded item of the plugin srs it links to while the
project enables srs (the node ``evo_agents.hub.tables_srs.kg_node_id`` names, which the plugin srs declares from the
version approved last), and each plan step it links to ``implements`` it (``plan:<plan>/step:<step>``, the node of the
step in the plan's own item). An edge to a node no source gives, a use case of no approved version or a plan no
machine read, is dropped as dangling. A run and a pull request are no nodes of the graph; their links are left out.
It adds no identifiers: a task's number is no code a document names.
"""

from __future__ import annotations

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.tasks.service.backlog import read


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``backlog``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``)."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    async with UnitOfWork(context.engine) as conn:
        return await read(conn, params["source"], context.project_id, context.project or params["project"])


__all__ = ["kg_source"]
