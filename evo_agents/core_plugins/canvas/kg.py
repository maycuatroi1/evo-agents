"""The plugin's source of its project's knowledge graph, the contribution ``canvases`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): every canvas of the project that is not archived, with its claims that are not.

A canvas is the item ``plugin.canvas:canvas:<slug>`` of kind ``doc``, a Document of the graph, with the product's name,
the canvas's label and the time it or a claim of it last changed. Each cell that holds a claim is a fragment keyed by
the cell (``problem``), a Section, and each claim a fragment under its cell keyed by its code (``CL-3``): its text,
status, confidence, test card, signal keys and problems, so search finds a claim by what it says. Its use cases are
edges only: named in its text, a code with no approved version would become a node of its own.

A claim is a Claim node with the id a mention of its code gets (``claim:CL-3``), holding its code alone, as a use case
of the plugin srs does: a mention of the code in a document of a lower label shows the node at that label, so its text
and status stay in its fragment, which keeps the canvas's label. Declared edges, each on the claim's fragment:

- the fragment ``defines`` the claim;
- each use case the claim links ``implements`` it, while the project enables srs: the node
  ``evo_agents.hub.tables_srs.kg_node_id`` names, which the plugin srs declares from the version approved last, as
  the plugin tasks links a task to it. A use case of no approved version is no node, and the edge is dropped as
  dangling. A task implements its use case and a plan step its task, so kg_path from a claim goes along implements to
  its use cases, their tasks and the steps of those;
- the claim is ``measured_by`` each signal key it links and the key its test card's metric names: a SignalKey node
  ``signalkey:<key>`` the claim's fragment declares with its key alone;
- the claim ``addresses`` each problem of the project it links (plan problem-oriented-hub, criterion a5): the node
  ``problem:P-3`` the hub's source of problems declares (``evo_agents.hub.kg_problems``), and the edge is dropped as
  dangling while no such node is.

The identifiers are the pattern of a claim's code, so a mention of ``CL-3`` in a chat, a plan or a document links to
the claim's node. A canvas links its page on the hub's web (``uri``) when the hub knows its public URL.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import links, store
from evo_agents.core_plugins.canvas.content import FEEDBACK
from evo_agents.hub.kg_problems import ADDRESSES, problem_node_id
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.tables_canvas import CELLS
from evo_agents.hub.tables_srs import kg_node_id
from evo_agents.kg.ids import utc_text

CANVAS = "canvas"  # the kind in an item's id: plugin.canvas:canvas:<slug>
CLAIM = "Claim"
SIGNAL_KEY = "SignalKey"
CODE_PATTERN = r"(?<![\w-])CL-[1-9][0-9]{0,8}(?![\w-])"  # a claim's code inside text (content.code)


def claim_node_id(code: str) -> str:
    """The id of a claim's node, the one the link stage gives a mention of its code: ``claim:CL-3``."""
    return f"{CLAIM.lower()}:{code}"


def signal_key_node_id(key: str) -> str:
    """The id of a signal key's node: ``signalkey:kb.search.zero_result_rate``."""
    return f"{SIGNAL_KEY.lower()}:{key}"


def identifiers() -> list[dict]:
    return [{"kind": CLAIM, "pattern": CODE_PATTERN}]


def canvas_url(base_url: str, project: str, slug: str) -> str:
    """A canvas's page on the hub's web (web/src/app/(app)/p/[project]/x/canvas/canvases/[slug])."""
    return f"{base_url.rstrip('/')}/p/{quote(project, safe='')}/x/canvas/canvases/{quote(slug, safe='')}"


def _cell_title(cell: str) -> str:
    return cell.replace("_", " ").capitalize()


def _title(row: store.ClaimRow) -> str:
    """A claim's heading: its number and cell, never its text nor its code. The link stage reads a code in a heading as
    the section defining it, so a heading that held the claim's text would define each code the text names, and one
    that held ``CL-12`` would define ``CL-1`` as well; the fragment declares that it defines its claim instead."""
    return f"Claim {row.number} in {_cell_title(row.cell)}"


def _card_line(card: dict | None) -> str | None:
    if not card:
        return None
    return "Test card: " + "; ".join(f"{name.replace('_', ' ')} {value}" for name, value in card.items())


def _claim_text(row: store.ClaimRow) -> str:
    lines = [row.text, "", f"Status: {row.status}. Confidence: {row.confidence}."]
    card = _card_line(row.test_card)
    if card:
        lines.append(card)
    if row.signal_keys:
        lines.append(f"Signal keys: {', '.join(row.signal_keys)}.")
    if row.problems:
        lines.append(f"Problems: {', '.join(row.problems)}.")
    return "\n".join(lines)


def measured_by(row: store.ClaimRow) -> list[str]:
    """The signal keys that measure a claim: those it links, and the one its test card's metric names."""
    metric = (row.test_card or {}).get("metric")
    keys = set(row.signal_keys) | ({metric} if metric and metric != FEEDBACK else set())
    return sorted(keys)


def _fragments(canvas: store.CanvasRow, claims: list[store.ClaimRow]) -> list[dict]:
    found = [{"anchor": "_preamble", "level": 0, "text": canvas.description}] if canvas.description.strip() else []
    for cell in CELLS:
        held = [row for row in claims if row.cell == cell]
        if not held:
            continue
        found.append({"anchor": cell, "title": _cell_title(cell), "level": 1, "text": _cell_title(cell)})
        found += [
            {"anchor": row.code, "title": _title(row), "level": 2, "parent": cell, "text": _claim_text(row)}
            for row in held
        ]
    return found


def _graph(item_id: str, claims: list[store.ClaimRow], with_srs: bool) -> dict:
    """The nodes and edges a canvas declares (see the module)."""
    nodes, edges = [], []
    for row in claims:
        claim, anchor = claim_node_id(row.code), row.code
        nodes.append({"id": claim, "kind": CLAIM, "name": row.code, "props": {"code": row.code}, "anchor": anchor})
        edges.append({"src": f"{item_id}#{anchor}", "rel": "defines", "dst": claim, "anchor": anchor})
        for use_case in row.use_cases if with_srs else ():
            target = kg_node_id("use_case", use_case)
            if target is not None:
                edges.append({"src": target, "rel": "implements", "dst": claim, "anchor": anchor})
        for key in measured_by(row):
            node = signal_key_node_id(key)
            nodes.append({"id": node, "kind": SIGNAL_KEY, "name": key, "props": {"key": key}, "anchor": anchor})
            edges.append({"src": claim, "rel": "measured_by", "dst": node, "anchor": anchor})
        for problem in row.problems:
            edges.append({"src": claim, "rel": ADDRESSES, "dst": problem_node_id(problem), "anchor": anchor})
    return {"nodes": nodes, "edges": edges}


def canvas_item(
    source: str,
    canvas: store.CanvasRow,
    claims: list[store.ClaimRow],
    with_srs: bool,
    page: tuple[str, str] | None = None,
) -> dict:
    """The kg/1 item of a canvas with its claims not archived, as the hub finishes it (see the module). With ``page``
    (the hub's public URL and the project's name), its ``uri`` is the canvas's page on the hub's web."""
    item_id = f"{source}:{CANVAS}:{canvas.slug}"
    changed = max([canvas.updated_at, *(row.updated_at for row in claims)])
    item = {
        "id": item_id,
        "kind": "doc",
        "title": canvas.name,
        "rev_time": utc_text(changed),
        "fragments": _fragments(canvas, claims),
        "props": {"canvas": canvas.slug, "claims": len(claims), "graph": _graph(item_id, claims, with_srs)},
        "label": canvas.label,
    }
    if page is not None:
        item["uri"] = canvas_url(page[0], page[1], canvas.slug)
    return item


async def read(conn: AsyncConnection, source: str, project_id: int, project: str, base_url: str | None = None) -> dict:
    """The answer of the source: an item per canvas not archived, and the identifiers; each canvas links its page on
    the hub's web when the hub knows its public URL (``base_url``)."""
    with_srs = await links.srs_enabled(conn, project_id)
    page = (base_url, project) if base_url else None
    items = []
    for canvas in await store.project_canvases(conn, project_id):
        claims = await store.claims(conn, canvas.id)
        items.append(canvas_item(source, canvas, claims, with_srs, page))
    return {"items": items, "identifiers": identifiers()}


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``canvases``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``);
    the hub's config, when the call carries it, gives the public URL its canvases' pages are at."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    base_url = getattr(context.config, "public_url", None)
    base_url = base_url if isinstance(base_url, str) else None
    async with context.engine.begin() as conn:
        return await read(conn, params["source"], context.project_id, context.project or params["project"], base_url)


__all__ = ["canvas_item", "canvas_url", "claim_node_id", "identifiers", "kg_source", "measured_by", "read"]
