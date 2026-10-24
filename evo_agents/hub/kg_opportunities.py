"""The opportunities of a project in its knowledge graph (plan opportunity-intake, criterion a1): a source the hub reads
beside the sources of the core plugins and the problems (``evo_agents.hub.kg_sources``), for a project that has at
least one opportunity.

Its id is SOURCE, in the ``plugin.`` namespace that is the hub's, with an underscore no plugin id holds
(``evo_agents.plugins.manifest.PLUGIN_ID``). It answers one item, ``plugin.hub_opportunities:doc:opportunities``,
titled Opportunities, a Document of the graph with the project's default label: a fragment per opportunity keyed by its
code (``O-3``), titled with its number and never its code, whose text is its statement, who benefits, the leverage, the
window, its status (with the reason of a closed one), its rank and its metric, so search finds an opportunity by what it
says. The fragment ``defines`` the Opportunity node ``opportunity:O-3``, which holds the code alone, and the node
``answers`` each Problem node of the same project it answers.

An answer that names a problem of another project reaches neither the text nor an edge: the graph of a project holds
what the project's members read, and a reader may hold no grant on the other project. Those references live in the
hub's table alone (``opportunity_answers``).
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_problems, opportunities, problems, tables
from evo_agents.kg.ids import utc_text

SOURCE = "plugin.hub_opportunities"
DOC = "doc"  # the kind in an item's id: plugin.hub_opportunities:doc:opportunities
OPPORTUNITY = "Opportunity"
ANSWERS = "answers"  # an opportunity to a problem of its own project it answers


def opportunity_node_id(code: str) -> str:
    """The id of an opportunity's node: ``opportunity:O-3``."""
    return f"{OPPORTUNITY.lower()}:{code}"


def _page(base_url: str | None, project: str) -> dict:
    if not base_url:
        return {}
    return {"uri": f"{base_url.rstrip('/')}/p/{quote(project, safe='')}/opportunities"}


async def _opportunities(conn: AsyncConnection, project_id: int) -> list:
    o, r = tables.opportunities, tables.opportunity_revisions
    query = (
        select(
            o.c.id,
            o.c.number,
            r.c.statement,
            r.c.beneficiary,
            r.c.leverage,
            r.c.window_until,
            r.c.window_note,
            r.c.metric,
            r.c.rank,
            r.c.status,
            r.c.close_reason,
            r.c.created_at,
        )
        .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
        .where(o.c.project_id == project_id)
        .order_by(o.c.number)
    )
    return (await conn.execute(query)).all()


async def _answers(conn: AsyncConnection, project_id: int) -> dict[int, list[int]]:
    """The numbers of the problems of the same project each opportunity (by id) answers now, in order."""
    o, a, p = tables.opportunities, tables.opportunity_answers, tables.problems
    query = (
        select(o.c.id, p.c.number)
        .join_from(o, a, and_(a.c.opportunity_id == o.c.id, a.c.revision == o.c.revision))
        .join(p, p.c.id == a.c.problem_id)
        .where(o.c.project_id == project_id, p.c.project_id == project_id)
        .order_by(o.c.id, a.c.position)
    )
    found: dict[int, list[int]] = {}
    for row in await conn.execute(query):
        found.setdefault(row.id, []).append(row.number)
    return found


def _text(row, answers: list[int]) -> str:
    lines = [
        row.statement,
        "",
        f"Who benefits: {row.beneficiary}",
        f"Leverage: {row.leverage}",
        f"Window: {opportunities.window_text({'window_until': row.window_until, 'window_note': row.window_note})}.",
        f"Status: {opportunities.status_text(row._mapping)}. Rank {row.rank}.",
        f"Metric: {problems.metric_text(row.metric)}.",
    ]
    if answers:
        lines.append("Answers: " + ", ".join(problems.code(number) for number in answers) + ".")
    return "\n".join(lines)


def _item(rows: list, answers: dict[int, list[int]], label: dict, page: dict) -> dict:
    item_id = f"{SOURCE}:{DOC}:opportunities"
    fragments, nodes, edges = [], [], []
    for row in rows:
        code = opportunities.code(row.number)
        node = opportunity_node_id(code)
        answered = answers.get(row.id, [])
        fragments.append(
            {"anchor": code, "title": f"Opportunity {row.number}", "level": 1, "text": _text(row, answered)}
        )
        nodes.append({"id": node, "kind": OPPORTUNITY, "name": code, "props": {"code": code}, "anchor": code})
        edges.append({"src": f"{item_id}#{code}", "rel": "defines", "dst": node, "anchor": code})
        for number in answered:
            target = kg_problems.problem_node_id(problems.code(number))
            edges.append({"src": node, "rel": ANSWERS, "dst": target, "anchor": code})
    return {
        "id": item_id,
        "kind": "doc",
        "title": "Opportunities",
        "rev_time": utc_text(max(row.created_at for row in rows)),
        "fragments": fragments,
        "props": {"opportunities": len(rows), "graph": {"nodes": nodes, "edges": edges}},
        "label": label,
        **page,
    }


async def read(conn: AsyncConnection, project_id: int, project: str, base_url: str | None = None) -> dict:
    """The answer of the source, as a plugin's source answers (``kg_sources.checked``): no item for a project without
    an opportunity."""
    found = await _opportunities(conn, project_id)
    if not found:
        return {"items": [], "identifiers": []}
    label = await conn.scalar(select(tables.projects.c.default_label).where(tables.projects.c.id == project_id))
    item = _item(found, await _answers(conn, project_id), dict(label or {}), _page(base_url, project))
    return {"items": [item], "identifiers": []}


__all__ = ["ANSWERS", "SOURCE", "opportunity_node_id", "read"]
