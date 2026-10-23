"""The problems of a project in its knowledge graph (plan problem-oriented-hub, criterion a5): a source the hub reads
beside the sources of the core plugins (``evo_agents.hub.kg_sources``), for a project that has at least one problem.

Its id is SOURCE, in the ``plugin.`` namespace that is the hub's, so a build keeps its items as it keeps a plugin's and
removes them once it gives none; an underscore, which no plugin id holds (``evo_agents.plugins.manifest.PLUGIN_ID``),
keeps it apart from any plugin's. It answers two items, each a Document of the graph with the project's default label:

- ``plugin.hub_problems:doc:problems``, titled Problems: a fragment per problem keyed by its code (``P-3``), titled
  with its number and never its code, whose text is its statement, who is hurt, when it happens, how they cope today,
  its status (with the reason of a closed one), its rank and its metric, so search finds a problem by what it says.
  The fragment ``defines`` the Problem node ``problem:P-3``, which holds the code alone, as a claim's node does
  (``evo_agents.core_plugins.canvas.kg``).
- ``plugin.hub_problems:doc:goals``, titled Goals: a fragment per goal whose newest revision answers a problem, keyed by
  its code (``G-1``), with its statement and status. The fragment ``defines`` the Goal node ``goal:G-1``, which
  ``addresses`` the problem.

A plan whose body names a problem ``addresses`` it (``evo_agents.kg.pipeline.stages``), and so does a claim that links
one (``evo_agents.core_plugins.canvas.kg``). The identifier PROBLEM_PATTERN links a mention of a problem's code in any
source of the project to its node. A project without a problem gets no item, so its graph builds as before.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, problems, tables
from evo_agents.kg.ids import utc_text

SOURCE = "plugin.hub_problems"
DOC = "doc"  # the kind in an item's id: plugin.hub_problems:doc:problems
PROBLEM = "Problem"
GOAL = "Goal"
ADDRESSES = "addresses"  # a plan, a claim or a goal to the problem it answers
PROBLEM_PATTERN = r"(?<![\w-])P-[1-9][0-9]{0,8}(?![\w-])"  # a problem's code inside text (problems.code)


def problem_node_id(code: str) -> str:
    """The id of a problem's node, the one the link stage gives a mention of its code: ``problem:P-3``."""
    return f"{PROBLEM.lower()}:{code}"


def goal_node_id(code: str) -> str:
    """The id of a goal's node: ``goal:G-1``."""
    return f"{GOAL.lower()}:{code}"


def identifiers() -> list[dict]:
    return [{"kind": PROBLEM, "pattern": PROBLEM_PATTERN}]


def _page(base_url: str | None, project: str, rest: str) -> dict:
    if not base_url:
        return {}
    return {"uri": f"{base_url.rstrip('/')}/p/{quote(project, safe='')}/{rest}"}


def _problem_text(row) -> str:
    lines = [
        row.statement,
        "",
        f"Who is hurt: {row.who}",
        f"When: {row.when}",
        f"How they cope today: {row.workaround}",
        f"Status: {problems.status_text(row._mapping)}. Rank {row.rank}.",
        f"Metric: {problems.metric_text(row.metric)}.",
    ]
    return "\n".join(lines)


async def _problems(conn: AsyncConnection, project_id: int) -> list:
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(
            p.c.id,
            p.c.number,
            r.c.statement,
            r.c.who,
            r.c.when,
            r.c.workaround,
            r.c.metric,
            r.c.rank,
            r.c.status,
            r.c.close_reason,
            r.c.created_at,
        )
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id)
        .order_by(p.c.number)
    )
    return (await conn.execute(query)).all()


async def _goals(conn: AsyncConnection, project_id: int) -> list:
    """The goals of the project whose newest revision answers a problem, with that problem's number."""
    g, r, p = tables.goals, tables.goal_revisions, tables.problems
    query = (
        select(g.c.number, r.c.statement, r.c.status, r.c.close_reason, r.c.created_at, p.c.number.label("problem"))
        .join_from(g, r, and_(r.c.goal_id == g.c.id, r.c.revision == g.c.revision))
        .join(p, p.c.id == r.c.problem_id)
        .where(g.c.project_id == project_id)
        .order_by(g.c.number)
    )
    return (await conn.execute(query)).all()


def _problems_item(rows: list, label: dict, page: dict) -> dict:
    item_id = f"{SOURCE}:{DOC}:problems"
    fragments, nodes, edges = [], [], []
    for row in rows:
        code = problems.code(row.number)
        node = problem_node_id(code)
        text = _problem_text(row)
        fragments.append({"anchor": code, "title": f"Problem {row.number}", "level": 1, "text": text})
        nodes.append({"id": node, "kind": PROBLEM, "name": code, "props": {"code": code}, "anchor": code})
        edges.append({"src": f"{item_id}#{code}", "rel": "defines", "dst": node, "anchor": code})
    return {
        "id": item_id,
        "kind": "doc",
        "title": "Problems",
        "rev_time": utc_text(max(row.created_at for row in rows)),
        "fragments": fragments,
        "props": {"problems": len(rows), "graph": {"nodes": nodes, "edges": edges}},
        "label": label,
        **page,
    }


def _goals_item(rows: list, label: dict, page: dict) -> dict:
    item_id = f"{SOURCE}:{DOC}:goals"
    fragments, nodes, edges = [], [], []
    for row in rows:
        code, node = goals.code(row.number), goal_node_id(goals.code(row.number))
        status = row.status + (f" ({row.close_reason})" if row.close_reason else "")
        text = f"{row.statement}\n\nStatus: {status}."
        fragments.append({"anchor": code, "title": f"Goal {row.number}", "level": 1, "text": text})
        nodes.append({"id": node, "kind": GOAL, "name": code, "props": {"code": code}, "anchor": code})
        edges.append({"src": f"{item_id}#{code}", "rel": "defines", "dst": node, "anchor": code})
        target = problem_node_id(problems.code(row.problem))
        edges.append({"src": node, "rel": ADDRESSES, "dst": target, "anchor": code})
    return {
        "id": item_id,
        "kind": "doc",
        "title": "Goals",
        "rev_time": utc_text(max(row.created_at for row in rows)),
        "fragments": fragments,
        "props": {"goals": len(rows), "graph": {"nodes": nodes, "edges": edges}},
        "label": label,
        **page,
    }


async def read(conn: AsyncConnection, project_id: int, project: str, base_url: str | None = None) -> dict:
    """The answer of the source, as a plugin's source answers (``kg_sources.checked``): no item for a project without
    a problem; each item links its page on the hub's web when the hub knows its public URL (``base_url``)."""
    found = await _problems(conn, project_id)
    if not found:
        return {"items": [], "identifiers": []}
    label = await conn.scalar(select(tables.projects.c.default_label).where(tables.projects.c.id == project_id))
    label = dict(label or {})
    items = [_problems_item(found, label, _page(base_url, project, "problems"))]
    answering = await _goals(conn, project_id)
    if answering:
        items.append(_goals_item(answering, label, _page(base_url, project, "goals")))
    return {"items": items, "identifiers": identifiers()}


__all__ = [
    "ADDRESSES",
    "SOURCE",
    "goal_node_id",
    "identifiers",
    "problem_node_id",
    "read",
]
