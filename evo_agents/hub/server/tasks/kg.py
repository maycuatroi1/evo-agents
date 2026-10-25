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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.tasks import store
from evo_agents.hub.server.tasks.settings import Settings, settings, srs_enabled
from evo_agents.hub.tables_srs import kg_node_id
from evo_agents.kg.ids import utc_text

TICKET = "ticket"  # the kind of a task's item, and the kind in its id: plugin.tasks:ticket:T-42


async def _links(conn: AsyncConnection, task_ids: list[int], with_srs: bool) -> dict[int, list]:
    """The links of the tasks to SRS items (while srs is enabled) and to plan steps, by task, in the order made."""
    k, i = tables.tasks.links, tables.srs.items
    kinds = ["srs_item", "plan_step"] if with_srs else ["plan_step"]
    query = (
        select(k.c.task_id, k.c.kind, k.c.plan_id, k.c.step_id, i.c.kind.label("srs_kind"), i.c.code)
        .select_from(k.outerjoin(i, i.c.id == k.c.srs_item_id))
        .where(k.c.task_id.in_(task_ids or [0]), k.c.kind.in_(kinds))
        .order_by(k.c.task_id, k.c.id)
    )
    found: dict[int, list] = {}
    for row in await conn.execute(query):
        found.setdefault(row.task_id, []).append(row)
    return found


def _edges(item_id: str, links: list) -> list[dict]:
    edges = []
    for link in links:
        if link.kind == "srs_item":
            target = kg_node_id(link.srs_kind, link.code)
            edge = {"src": item_id, "rel": "implements", "dst": target} if target else None
        else:
            edge = {"src": f"plan:{link.plan_id}/step:{link.step_id}", "rel": "implements", "dst": item_id}
        if edge is not None and edge not in edges:
            edges.append(edge)
    return edges


def ticket_item(source: str, config: Settings, row: store.TaskRow, people: store.People | None, links: list) -> dict:
    """The kg/1 item of a task, as the hub finishes it (see the module)."""
    ref = config.ref(row.number)
    item_id = f"{source}:{TICKET}:{ref}"
    props = {
        "number": ref,
        "type": row.type,
        "status": row.status,
        "status_group": config.group(row.status),
        "priority": row.priority,
        "sprint": row.sprint,
        "assignees": sorted(people.assignees) if people else [],
        "due_on": row.due_on.isoformat() if row.due_on else None,
    }
    item = {
        "id": item_id,
        "kind": TICKET,
        "title": f"{ref} {row.title}",
        "rev_time": utc_text(row.updated_at),
        "props": {**{key: value for key, value in props.items() if value}, "graph": {"edges": _edges(item_id, links)}},
        "label": row.label,
    }
    if row.description_md.strip():
        item["body"] = {"format": "markdown", "text": row.description_md}
    return item


async def read(conn: AsyncConnection, source: str, project_id: int, project: str) -> dict:
    """The answer of the source: an item per task of the project, and no identifiers."""
    config = await settings(conn, project_id, project)
    rows = await store.all_tasks(conn, project_id)
    ids = [row.id for row in rows]
    people = await store.people(conn, ids)
    links = await _links(conn, ids, await srs_enabled(conn, project_id))
    items = [ticket_item(source, config, row, people.get(row.id), links.get(row.id, [])) for row in rows]
    return {"items": items, "identifiers": []}


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``backlog``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``)."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    async with context.engine.begin() as conn:
        return await read(conn, params["source"], context.project_id, context.project or params["project"])


__all__ = ["kg_source", "read", "ticket_item"]
