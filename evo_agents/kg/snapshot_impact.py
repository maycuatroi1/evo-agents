"""kg_impact on an in-RAM snapshot (``evo_agents.kg.snapshot``): what depends on a set of nodes, walked back along the
visible edges into them hop by hop, as ``kg serve`` walks a store (``evo_agents.kg.serve_impact``), reading the
in-edges of a node from the CSR partitions of the relations and statuses the walk follows.

A seed is a node id, or ``repo:path`` of a visible file or document. Each node is reached once, through its strongest
edge (status, then relation by name, then target), and ``rels`` lists every relation it has to that edge's target;
a reached node with more than HUB_DEGREE edges is marked ``hub`` and not expanded; a seed's in-edges are read up to
IMPACT_SEED_EDGES and a seed with more is reported in ``cut``; the documents that mention a reached node or a seed
come back in ``docs`` with their ``mentions``. The lines of a diff are not read here."""

from __future__ import annotations

from evo_agents.kg.impact import Impact, seed_coverage
from evo_agents.kg.serve_impact import IMPACT_MIN_RANK, IMPACT_SEED_EDGES, IMPACT_STATUSES, WALKED_RELS
from evo_agents.kg.serve_walk import HUB_DEGREE, STATUS_RANK
from evo_agents.kg.snapshot_search import SnapshotSearch, node_json


def shows(reader: SnapshotSearch, node: dict) -> bool:
    return reader.visible(node["label"])


def seeds_of(reader: SnapshotSearch, ids: list[str], paths: list[str], found: Impact) -> None:
    """Put in ``found.seeds`` the visible nodes of ``ids`` and the one visible file or document of each
    ``repo:path``; the others go to ``missing`` and ``unmatched``."""
    view = reader.view
    nodes = view.nodes(ids)
    for nid in ids:
        node = nodes.get(nid)
        if node is not None and shows(reader, node):
            found.seeds[nid] = node
        else:
            found.missing += 1
    for path in paths:
        repo, _, rest = path.partition(":")
        matches = [
            n
            for n in view.nodes(view.snap.ids).values()
            if n["kind"] in ("File", "Document")
            and n["props"].get("repo") == repo
            and n["props"].get("path") == rest
            and shows(reader, n)
        ]
        if len(matches) == 1:
            found.seeds[matches[0]["id"]] = matches[0]
        elif matches:
            found.ambiguous.append({"path": path, "candidates": [n["id"] for n in matches]})
        else:
            found.unmatched.append(path)


def _seed_edges(reader: SnapshotSearch, found: Impact, nid: str) -> list[dict]:
    """The edges into a seed of the relations and statuses impact follows, up to IMPACT_SEED_EDGES."""
    query = (nid, found.build, WALKED_RELS, IMPACT_STATUSES, reader.clearance)
    edges = reader.view.edges_into(*query, limit=IMPACT_SEED_EDGES)
    total = reader.view.count_into(*query)
    if total > len(edges):
        found.cut.append({"seed": nid, "read": len(edges), "total": total})
    return edges


def edges_back(reader: SnapshotSearch, found: Impact, nid: str, hop: int) -> list[dict]:
    """The visible edges into ``nid`` impact follows, mentions included, never weaker than resolved; a reached node
    with more than HUB_DEGREE edges is marked a hub and gives none."""
    if hop == 0:
        edges = _seed_edges(reader, found, nid)
    else:
        edges = reader.view.edges_of(nid, found.build, limit=HUB_DEGREE + 1)
        if len(edges) > HUB_DEGREE:
            found.reached[nid]["hub"] = True
            return []
    return [
        e
        for e in edges
        if e["dst"] == nid
        and e["src"] != nid
        and e["rel"] in WALKED_RELS
        and STATUS_RANK.get(e["status"], 0) >= IMPACT_MIN_RANK
        and reader.seen(e)
    ]


def walk_back(reader: SnapshotSearch, found: Impact) -> None:
    """Walk back from the seeds: whoever has an edge into a reached node depends on it (``ImpactTools._walk_back``)."""
    frontier = sorted(found.seeds)
    for hop in range(found.depth + 1):
        step: dict[str, tuple] = {}
        links: dict[tuple[str, str], set[str]] = {}
        for nid in frontier:
            for e in edges_back(reader, found, nid, hop):
                if e["rel"] == "mentions":
                    found.mentions.setdefault(e["src"], []).append(e)
                elif hop < found.depth and e["src"] not in found.seeds and e["src"] not in found.reached:
                    links.setdefault((e["src"], e["dst"]), set()).add(e["rel"])
                    key = (-STATUS_RANK[e["status"]], e["rel"], e["dst"])
                    if e["src"] not in step or key < step[e["src"]][0]:
                        step[e["src"]] = (key, e)
        nodes = reader.view.nodes(step)
        frontier = []
        for other in sorted(step):
            node = nodes.get(other)
            if node is not None and shows(reader, node):
                e = step[other][1]
                rels = sorted(links[(other, e["dst"])])
                found.reached[other] = {"hop": hop + 1, "node": node, "edge": e, "rels": rels, "hub": False}
                frontier.append(other)


def impact(
    reader: SnapshotSearch,
    ids: list[str] | None = None,
    paths: list[str] | None = None,
    depth: int = 2,
    limit: int = 20,
) -> Impact:
    """What a change to ``ids`` and ``paths`` reaches within ``depth`` hops, as kg_impact finds it."""
    found = Impact(build=reader.view.snap.build, depth=max(1, min(int(depth), 3)), limit=limit)
    seeds_of(reader, list(ids or []), list(paths or []), found)
    walk_back(reader, found)
    found.docs = {
        d: n
        for d, n in reader.view.nodes(found.mentions).items()
        if shows(reader, n) and d not in found.seeds and d not in found.reached
    }
    found.coverage = seed_coverage(found.seeds.values())
    return found


def impact_data(found: Impact) -> dict:
    """The structured result of kg_impact (``ImpactTools._impact_data``) without the text's ``left_out``."""
    return {
        "build": found.build,
        "depth": found.depth,
        "seeds": [node_json(found.seeds[s]) for s in sorted(found.seeds)],
        "coverage": found.coverage,
        "impacted": [
            {
                "hop": r["hop"],
                "node": node_json(r["node"]),
                "rel": r["edge"]["rel"],
                "rels": r["rels"],
                "status": r["edge"]["status"],
                "to": r["edge"]["dst"],
                "hub": r["hub"],
            }
            for r in found.reached.values()
        ],
        "documents": [
            {
                "node": node_json(found.docs[d]),
                "mentions": [
                    {"node": e["dst"], "status": e["status"]} for e in sorted(found.mentions[d], key=lambda e: e["dst"])
                ],
            }
            for d in sorted(found.docs)
        ],
        "cut": found.cut,
        "ambiguous": found.ambiguous,
        "unmatched": found.unmatched,
        "not_found": found.missing,
        "summary": f"{len(found.reached)} dependents, {len(found.docs)} documents",
    }
