"""The part of a project's main graph a draft branch's operations reach (plan kg-centralized-on-hub, step 7), read by
id from the hub's tables: the rows ``kg_branches`` applies the operations to, diffs, checks against the ontology rules
and merges.

Reading main's whole rows for each apply, diff, check and merge (``kg_graph_rows.read_graph``) held every full-text,
provenance and vector row of the graph in the api, twice, and a check built two snapshots of it: on the graph of
evo-agents (127k nodes, 524k edges) one apply took the api past its memory limit and the kernel killed it. An operation
reads and changes only the nodes it names and the edges at them (``branch_ops.touched``), so the rows read are:

- the nodes the operations name, with their full-text rows (a passage's row too) and their code;
- every edge at one of them, and the node at its other end;
- with an acyclic relation in the ontology, the edges of the acyclic relations reachable from those nodes, so a cycle a
  new or moved edge closes lies within them;
- with a node kind that has keys, every node of the kinds the operations name or make, so a duplicate key does too.

A row outside them is the same on main and on the branch, so a diff, the rule violations a branch adds, a conflict
with main and what a merge writes are those of the whole graph. The violations listed are the ones within reach."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import (
    BATCH,
    GRAPH_TABLES,
    MAIN,
    assembled_rows,
    read_columns,
    read_text_of,
    row_pairs,
)
from evo_agents.kg import branch_ops
from evo_agents.kg.ontology_rules import Rules
from evo_agents.kg.snapshot import StoreRows
from evo_agents.kg.snapshot_delta import Delta


@dataclass(frozen=True)
class Reach:
    """Main's rows within a branch's reach, and its full-text and code rows as the table keeps them, by row key: what a
    merge compares the branch's with (``kg_graph_rows.write_changes``)."""

    rows: StoreRows
    text: dict[str, tuple]


def named_ids(ops: list[dict]) -> list[str]:
    """The node ids ``ops`` (with or without their ``before``) read or change, sorted."""
    found: set[str] = set()
    for op in ops:
        nodes, _ = branch_ops.touched({k: v for k, v in op.items() if k != "before"})
        found.update(nodes)
    return sorted(found)


async def _head(conn: AsyncConnection, project_id: int):
    heads = tables.kg_graph_heads
    query = select(
        heads.c.content_hash,
        heads.c.top_label,
        heads.c.top_audience,
        heads.c.summary,
        heads.c.vectors,
        heads.c.asked_weight,
    ).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
    return (await conn.execute(query)).one_or_none()


async def _rows(conn: AsyncConnection, project_id: int, name: str, column, values) -> dict[str, tuple]:
    """The rows of table ``name`` of main whose ``column`` is one of ``values``, by key, as
    ``kg_graph_rows.read_table_rows`` reads them."""
    table = getattr(tables, name)
    columns = read_columns(name)
    wanted = sorted(set(values))
    out: dict[str, tuple] = {}
    for i in range(0, len(wanted), BATCH):
        query = select(*columns).where(
            table.c.project_id == project_id, table.c.branch_id == MAIN, column.in_(wanted[i : i + BATCH])
        )
        out.update(row_pairs(name, (await conn.execute(query)).all()))
    return out


async def _acyclic(conn: AsyncConnection, project_id: int, named: list[str], rules: Rules) -> dict[str, tuple]:
    """The edges of main's acyclic relations whose source is one of ``named`` or reachable from one along them."""
    rels = sorted(name for name, rule in rules.relations.items() if rule.acyclic)
    if not rels or not named:
        return {}
    e = tables.kg_edges
    scope = (e.c.project_id == project_id, e.c.branch_id == MAIN, e.c.rel.in_(rels))
    succ: dict[str, list[str]] = defaultdict(list)
    for src, dst in await conn.execute(select(e.c.src, e.c.dst).where(*scope)):
        succ[src].append(dst)
    seen, todo = set(named), list(named)
    while todo:
        for nxt in succ.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    found: dict[str, tuple] = {}
    for row_id, row in (await _rows(conn, project_id, "kg_edges", e.c.src, seen & succ.keys())).items():
        if row[2] in rels:
            found[row_id] = row
    return found


async def _keyed(
    conn: AsyncConnection, project_id: int, nodes: dict[str, tuple], ops: list[dict], rules: Rules
) -> dict[str, tuple]:
    """Every node of main of a kind with keys that one of ``nodes`` has or an operation's ``node.put`` gives."""
    keyed = {kind for kind, rule in rules.kinds.items() if rule.keys}
    kinds = {row[1] for row in nodes.values()} | {op.get("kind") for op in ops if op.get("op") == "node.put"}
    wanted = sorted(keyed & kinds)
    if not wanted:
        return {}
    return await _rows(conn, project_id, "kg_nodes", tables.kg_nodes.c.kind, wanted)


async def reach(conn: AsyncConnection, project_id: int, ops: list[dict], rules: Rules | None = None) -> Reach | None:
    """Main's rows within the reach of ``ops`` (the module's list), the cycles and keys of ``rules`` included when
    given; None before main's first build."""
    head = await _head(conn, project_id)
    if head is None:
        return None
    named = named_ids(ops)
    plain = [{k: v for k, v in op.items() if k != "before"} for op in ops]
    nodes = await _rows(conn, project_id, "kg_nodes", tables.kg_nodes.c.node_id, named)
    edges: dict[str, tuple] = {}
    for end in (tables.kg_edges.c.src, tables.kg_edges.c.dst):
        edges.update(await _rows(conn, project_id, "kg_edges", end, named))
    if rules:
        edges.update(await _acyclic(conn, project_id, named, rules))
        nodes.update(await _keyed(conn, project_id, nodes, plain, rules))
    ends = {row[i] for row in edges.values() for i in (1, 3)} - nodes.keys()
    nodes.update(await _rows(conn, project_id, "kg_nodes", tables.kg_nodes.c.node_id, ends))
    text = await read_text_of(conn, project_id, named)
    read: dict[str, dict[str, tuple]] = {name: {} for name in GRAPH_TABLES}
    read.update(kg_nodes=dict(sorted(nodes.items())), kg_edges=dict(sorted(edges.items())), kg_text_rows=text)
    return Reach(await asyncio.to_thread(assembled_rows, head, read), text)


async def totals(conn: AsyncConnection, project_id: int) -> tuple[int, int]:
    """How many nodes and edges main's graph holds."""
    counts = []
    for table in (tables.kg_nodes, tables.kg_edges):
        query = select(func.count()).where(table.c.project_id == project_id, table.c.branch_id == MAIN)
        counts.append(int(await conn.scalar(query) or 0))
    return counts[0], counts[1]


def _changed(old: dict, new: dict) -> list[str]:
    return sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))


def delta(main: StoreRows, rows: StoreRows, build: int | None) -> Delta:
    """What a branch's rows ``rows`` change of main's ``main``, both within its reach (``reach``), as the delta main's
    snapshot of that content hash takes to become the branch's (``Snapshot.apply_changes``), naming build ``build``:
    a read of the branch with a kg tool then costs what it changes, not a snapshot of every row."""
    old_nodes, new_nodes = {r[0]: r for r in main.nodes}, {r[0]: r for r in rows.nodes}
    old_edges, new_edges = {r[0]: r for r in main.edges}, {r[0]: r for r in rows.edges}
    node_ids = _changed(old_nodes, new_nodes)
    edge_ids = _changed(old_edges, new_edges)
    at, named = set(node_ids), set(edge_ids)
    text_keys = _changed(dict(main.keyed), dict(rows.keyed))
    code_ids = _changed(main.code, rows.code)
    keyed, code, text = dict(rows.keyed), rows.code, set(text_keys)
    summary = dict(rows.summary) if build is None else {**rows.summary, "build_id": build}
    return Delta(
        base=main.content_hash,
        content_hash=rows.content_hash,
        top=rows.top,
        summary=summary,
        build=build or 0,
        node_ids=tuple(node_ids),
        nodes=tuple(new_nodes[k] for k in node_ids if k in new_nodes),
        edge_ids=tuple(edge_ids),
        edges=tuple(r for k, r in sorted(new_edges.items()) if k in named or r[1] in at or r[3] in at),
        text_keys=tuple(text_keys),
        keyed=tuple((k, keyed[k]) for k in sorted(text) if k in keyed),
        code_ids=tuple(code_ids),
        code={k: code[k] for k in code_ids if k in code},
    )
