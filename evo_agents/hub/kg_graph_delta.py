"""What a build changed of a branch's graph, read back for the snapshot the api holds (plan kg-sound-fast-optimal,
criterion a3): the notice a build sends (``kg_graph_rows.change_note``) names the rows it touched, and the coordinator
(``kg_snapshots.SnapshotCoordinator``) reads only those rows from the tables (``read``) and applies them to the
snapshot it holds (``applied``, ``Snapshot.apply_changes``), instead of reading every row again.

The rows are read in one transaction under REPEATABLE READ, with the head: when the head is no longer the build the
notice names, a later build wrote meanwhile, and ``read`` answers None so the caller reads the rows whole."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import (
    BATCH,
    CODE,
    MAIN,
    TEXT,
    assembled_rows,
    read_columns,
    read_table_rows,
    row_pairs,
)
from evo_agents.kg.snapshot import Snapshot, StoreRows
from evo_agents.kg.snapshot_delta import Delta


@dataclass(frozen=True)
class Note:
    """What a notice says a build changed (``kg_graph_rows.change_note``)."""

    base: str  # the content hash of the rows before it
    content_hash: str  # the build's
    nodes: tuple[str, ...]
    edges: tuple[str, ...]
    text: tuple[tuple[str, str, str | None], ...]  # (kind, key, digest of the row now or None)
    provenance: tuple[str, ...]  # the elements whose provenance rows changed
    asked: bool
    review: bool


def note_of(changes: str | None) -> Note | None:
    """The ``Note`` of a notice's ``changes``; None without one or for one this cannot read."""
    if not changes:
        return None
    try:
        raw = json.loads(zlib.decompress(base64.b85decode(changes)))
        return Note(
            str(raw["base"]),
            str(raw["hash"]),
            tuple(raw["nodes"]),
            tuple(raw["edges"]),
            tuple((kind, key, digest) for kind, key, digest in raw["text"]),
            tuple(raw["provenance"]),
            bool(raw["asked"]),
            bool(raw["review"]),
        )
    except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
        return None


@dataclass(frozen=True)
class Read:
    """The rows ``read`` read of what a build changed, to be put together in a thread (``applied``)."""

    note: Note
    build_id: int | None
    head: object
    rows: dict[str, dict[str, tuple]]


async def read(conn: AsyncConnection, project_id: int, note: Note) -> Read | None:
    """The rows of the main branch ``note`` names, as they are now; None when the head is another build's. Runs its
    own transaction on ``conn`` under REPEATABLE READ, so the rows are those of the head it read."""
    conn = await conn.execution_options(isolation_level="REPEATABLE READ")
    async with conn.begin():
        heads = tables.kg_graph_heads
        head = (
            await conn.execute(
                select(
                    heads.c.build_id,
                    heads.c.content_hash,
                    heads.c.top_label,
                    heads.c.top_audience,
                    heads.c.summary,
                    heads.c.vectors,
                    heads.c.asked_weight,
                ).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
            )
        ).one_or_none()
        if head is None or head.content_hash != note.content_hash:
            return None
        nodes, edges = tables.kg_nodes, tables.kg_edges
        found: dict[str, dict[str, tuple]] = {"kg_embeddings": {}}
        found["kg_nodes"] = await _by(conn, project_id, "kg_nodes", nodes.c.node_id, note.nodes)
        found["kg_edges"] = await _by(conn, project_id, "kg_edges", edges.c.edge_id, note.edges)
        for end in (edges.c.src, edges.c.dst):
            found["kg_edges"].update(await _by(conn, project_id, "kg_edges", end, note.nodes))
        keys = [f"{kind}\x1f{key}\x1f{digest}" for kind, key, digest in note.text if digest]
        found["kg_text_rows"] = await _by(conn, project_id, "kg_text_rows", tables.kg_text_rows.c.row_key, keys)
        elems = tables.kg_provenance.c.elem_id
        found["kg_provenance"] = await _by(conn, project_id, "kg_provenance", elems, note.provenance)
        for name, whole in (("kg_asked", note.asked), ("kg_review_rows", note.review)):
            found[name] = await read_table_rows(conn, name, project_id, MAIN) if whole else {}
    return Read(note, head.build_id, head, found)


async def _by(conn: AsyncConnection, project_id: int, name: str, column, values) -> dict[str, tuple]:
    """The rows of table ``name`` of the main branch whose ``column`` is one of ``values``, by key, as
    ``kg_graph_rows.read_table_rows`` reads them."""
    table = getattr(tables, name)
    columns = read_columns(name)
    values = list(values)
    out: dict[str, tuple] = {}
    for i in range(0, len(values), BATCH):
        query = select(*columns).where(
            table.c.project_id == project_id, table.c.branch_id == MAIN, column.in_(values[i : i + BATCH])
        )
        out.update(row_pairs(name, (await conn.execute(query.order_by(columns[0]))).all()))
    return out


def delta_of(found: Read) -> Delta:
    """The ``Delta`` of the rows ``read`` read."""
    note = found.note
    rows: StoreRows = assembled_rows(found.head, found.rows)
    summary = dict(rows.summary)
    if found.build_id is not None:  # as ``kg_snapshots.hub_build`` names it
        summary["build_id"] = found.build_id
    provenance: dict[str, tuple[list, list, list]] = {elem: ([], [], []) for elem in note.provenance}
    for unit in rows.units:
        provenance.setdefault(unit[0], ([], [], []))[0].append(unit)
    for where in rows.wheres:
        provenance.setdefault(where[0], ([], [], []))[1].append(where)
    for derived in rows.derivations:
        provenance.setdefault(derived[0], ([], [], []))[2].append(derived)
    return Delta(
        base=note.base,
        content_hash=rows.content_hash,
        top=rows.top,
        summary=summary,
        build=found.build_id or 0,
        node_ids=note.nodes,
        nodes=rows.nodes,
        edge_ids=note.edges,
        edges=rows.edges,
        text_keys=tuple(key for kind, key, _ in note.text if kind == TEXT),
        keyed=rows.keyed,
        code_ids=tuple(key for kind, key, _ in note.text if kind == CODE),
        code=rows.code,
        provenance={elem: (tuple(u), tuple(w), tuple(d)) for elem, (u, w, d) in provenance.items()},
        asked=rows.asked if note.asked else None,
        asked_weight=rows.asked_weight,
        clusters=rows.clusters if note.review else None,
        review=rows.review if note.review else None,
    )


def applied(snap: Snapshot, found: Read) -> Snapshot:
    """``snap`` with the rows ``read`` read applied (``Snapshot.apply_changes``); raises ValueError
    (``snapshot_delta.DeltaError``) when they do not apply to it. Runs in a thread."""
    return snap.apply_changes(delta_of(found))
