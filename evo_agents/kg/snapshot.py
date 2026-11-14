"""An immutable in-RAM snapshot of one build of a project's knowledge graph: what the hub serves a build from without
a SQLite file per request.

A snapshot holds every live node and edge of the build, whatever its label, and answers a reader through a view
(``Snapshot.view``) that filters by the reader's audience and ceiling at read time, as the store's reads do
(``evo_agents.kg.store_filters``): a node or edge whose audience is not within the reader's is never read, and
``edges_of`` keeps only the edges at most the view's ceiling (level, location). The view reads as a ``Store`` does
(``nodes``, ``node``, ``edges_of``, ``edges_into``, ``count_into``, ``walk_edges``), so ``search.with_mentions``,
``search.through_entities`` and the walk of ``evo_agents.kg.ppr`` run on it unchanged.

Nodes are numbered in the order of their ids. Edges are numbered in the order (rel, src, dst, edge id) and partitioned
by (relation, status); each partition holds two CSR indexes over the node numbers, one by source and one by
target (``Partition``), so the edges of a node with given relations and statuses are a slice of an array, in the order
``Store.edges_of`` reads them. Each array is an ``array('I')`` of the standard library: no numpy.

``from_graph`` builds a snapshot from an assembled ``Graph`` with the content hash the store gives the build of it
(``evo_agents.kg.content``); ``from_store`` reads one from a build of a store file, for the bench. The text index
(``evo_agents.kg.snapshot_text``) is built with it."""

from __future__ import annotations

import json
import sqlite3
import struct
from array import array
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType

from evo_agents.kg import settings as kg_settings
from evo_agents.kg import snapshot_text
from evo_agents.kg.policy import audience_below, audience_join
from evo_agents.kg.store_filters import LIVE, label_columns, stored_audience
from evo_agents.kg.store_schema import EDGE_COLUMNS, NODE_COLUMNS


def _node_dict(row) -> dict:
    """A node as ``Store.nodes`` reads it, from a row of NODE_COLUMNS."""
    return {
        "id": row[0],
        "kind": row[1],
        "name": row[2],
        "props": json.loads(row[3] or "{}"),
        "status": row[4],
        "conf": row[5],
        "label": (row[6], row[7], row[8], stored_audience(row[10])),
        "aliases": json.loads(row[9] or "[]"),
    }


def _edge_dict(row) -> dict:
    """An edge as ``Store.edges_of`` reads it, from a row of EDGE_COLUMNS."""
    return {
        "id": row[0],
        "src": row[1],
        "rel": row[2],
        "dst": row[3],
        "props": json.loads(row[4] or "{}"),
        "status": row[5],
        "conf": row[6],
        "label": (row[7], row[8], row[9], stored_audience(row[10])),
    }


def label_pair(label) -> tuple[int, int]:
    """The level and location of a label."""
    return int(label[0]), int(label[1])


def _label_row(label) -> tuple:
    columns = label_columns(label)
    return columns["label_lvl"], columns["label_loc"], columns["label_int"], columns.get("label_aud")


def node_row(nid: str, n: dict) -> tuple:
    """The NODE_COLUMNS row the store writes for node ``n`` of a graph."""
    lvl, loc, integrity, aud = _label_row(n["label"])
    props = json.dumps(n["props"], ensure_ascii=False, sort_keys=True)
    return (
        nid,
        n["kind"],
        n["name"],
        props,
        n["status"],
        n["conf"],
        lvl,
        loc,
        integrity,
        json.dumps(n.get("aliases", [])),
        aud,
    )


def edge_row(eid: str, e: dict) -> tuple:
    """The EDGE_COLUMNS row the store writes for edge ``e`` of a graph."""
    lvl, loc, integrity, aud = _label_row(e["label"])
    props = json.dumps(e["props"], ensure_ascii=False, sort_keys=True)
    return (eid, e["src"], e["rel"], e["dst"], props, e["status"], e["conf"], lvl, loc, integrity, aud)


@dataclass(frozen=True)
class Partition:
    """The edges of one (relation, status), as CSR: the edges out of node i are ``out_edges[out_start[i]:out_start[i +
    1]]`` by (dst, edge id), those into it ``in_edges[in_start[i]:in_start[i + 1]]`` by (src, edge id); each an edge
    number of the snapshot."""

    rel: str
    status: str
    out_start: array
    out_edges: array
    in_start: array
    in_edges: array

    def out_of(self, i: int) -> array:
        return self.out_edges[self.out_start[i] : self.out_start[i + 1]]

    def into(self, i: int) -> array:
        return self.in_edges[self.in_start[i] : self.in_start[i + 1]]

    def __len__(self) -> int:
        return len(self.out_edges)


def _csr(count: int, keys: list[tuple[int, int, int]]) -> tuple[array, array]:
    """(start, edges) of the CSR index of ``keys`` (node number, other end's number, edge number) over ``count``
    nodes, each node's edges by the other end, then the edge."""
    start = array("I", [0] * (count + 1))
    for node, _, _ in keys:
        start[node + 1] += 1
    for i in range(count):
        start[i + 1] += start[i]
    return start, array("I", (edge for _, _, edge in sorted(keys)))


def _partitions(count: int, src: array, dst: array, groups: dict[tuple[str, str], list[int]]) -> MappingProxyType:
    out = {}
    for (rel, status), edges in sorted(groups.items()):
        out_start, out_edges = _csr(count, [(src[e], dst[e], e) for e in edges])
        in_start, in_edges = _csr(count, [(dst[e], src[e], e) for e in edges])
        out[(rel, status)] = Partition(rel, status, out_start, out_edges, in_start, in_edges)
    return MappingProxyType(out)


@dataclass(frozen=True)
class Snapshot:
    """One build of a graph in RAM: its nodes by number (``ids``, ``index``), its edges by number in the order (rel,
    src, dst, edge id), the CSR partitions by (relation, status), the text index, and the build's content hash. Nothing
    of it changes once built; a view reads it for one reader."""

    content_hash: str
    ids: tuple[str, ...]
    index: MappingProxyType
    nodes: tuple[dict, ...]
    edges: tuple[dict, ...]
    src: array
    dst: array
    partitions: MappingProxyType
    names: MappingProxyType  # a node's name, id or alias: the numbers of the nodes it names
    text: snapshot_text.TextIndex
    vectors: snapshot_text.Vectors | None = None
    build: int = 0
    top_label: tuple[int, int] = (0, 0)  # the highest level and location of its labels
    top_audience: frozenset[str] = frozenset()  # the join of its audiences

    @classmethod
    def from_rows(
        cls,
        content_hash: str,
        nodes: list[tuple],
        edges: list[tuple],
        text: snapshot_text.TextIndex,
        vectors: snapshot_text.Vectors | None = None,
        build: int = 0,
        top: tuple[tuple[int, int], frozenset[str]] | None = None,
    ) -> Snapshot:
        """A snapshot of rows as the store holds them (NODE_COLUMNS, EDGE_COLUMNS); an edge whose end is no node is
        kept out of the CSR indexes but read by its id, as a store read of a node's edges never meets it."""
        read = sorted((_node_dict(r) for r in nodes), key=lambda n: n["id"])
        ids = tuple(n["id"] for n in read)
        index = {nid: i for i, nid in enumerate(ids)}
        held = sorted(
            (e for e in map(_edge_dict, edges) if e["src"] in index and e["dst"] in index),
            key=lambda e: (e["rel"], e["src"], e["dst"], e["id"]),
        )
        src = array("I", (index[e["src"]] for e in held))
        dst = array("I", (index[e["dst"]] for e in held))
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for i, e in enumerate(held):
            groups[(e["rel"], e["status"] or "")].append(i)
        names: dict[str, set[int]] = defaultdict(set)
        aliased: dict[str, int] = {}
        for i, n in enumerate(read):
            for name in (n["id"], n["name"]):
                if isinstance(name, str):
                    names[name].add(i)
            for alias in n["aliases"]:
                aliased.setdefault(alias, i)  # the store keeps an alias for the first node by id (node_alias)
        for alias, i in aliased.items():
            names[alias].add(i)
        if top is None:
            labels = [x["label"] for x in (*read, *held)]
            audience: frozenset[str] = frozenset()
            for label in labels:
                audience = audience_join(audience, label[3])
            top = ((max((x[0] for x in labels), default=0), max((x[1] for x in labels), default=0)), audience)
        return cls(
            content_hash=content_hash,
            ids=ids,
            index=MappingProxyType(index),
            nodes=tuple(read),
            edges=tuple(held),
            src=src,
            dst=dst,
            partitions=_partitions(len(ids), src, dst, groups),
            names=MappingProxyType({k: tuple(sorted(v)) for k, v in names.items()}),
            text=text,
            vectors=vectors,
            build=build,
            top_label=top[0],
            top_audience=top[1],
        )

    @classmethod
    def from_graph(cls, graph, build: int = 0) -> Snapshot:
        """The snapshot of an assembled ``Graph``, with the content hash a store gives the build of it."""
        from evo_agents.kg import content
        from evo_agents.kg.store_diff import top_audience_of, top_label_of
        from evo_agents.kg.store_fts import search_rows

        _, keyed = search_rows(graph)
        text = snapshot_text.TextIndex.build(keyed, graph.code)
        return cls.from_rows(
            content.content_hash(graph),
            [node_row(nid, n) for nid, n in graph.nodes.items()],
            [edge_row(eid, e) for eid, e in graph.edges.items()],
            text,
            build=build,
            top=(label_pair(top_label_of(graph)), frozenset(top_audience_of(graph).get("top_audience", ()))),
        )

    @classmethod
    def from_store(cls, store, b: int) -> Snapshot:
        """The snapshot of build ``b`` of a store file: its live rows whatever their label, the rows of its full-text
        tables and its vectors. The full-text tables hold the latest build only, so ``b`` should be that build."""
        rows = store_rows(store, b)
        text = snapshot_text.TextIndex.build(list(rows.keyed), rows.code, folded=True)
        return cls.from_rows(
            rows.content_hash, list(rows.nodes), list(rows.edges), text, _vectors_of(store), build=b, top=rows.top
        )

    @classmethod
    def from_stored(cls, rows: StoreRows, build: int = 0, vectors: snapshot_text.Vectors | None = None) -> Snapshot:
        """The snapshot of rows a store gave (``store_rows``) and something kept, such as the hub's tables."""
        text = snapshot_text.TextIndex.build(sorted(rows.keyed), rows.code, folded=True)
        return cls.from_rows(rows.content_hash, list(rows.nodes), list(rows.edges), text, vectors, build, rows.top)

    def view(
        self,
        reader: frozenset[str] = frozenset(),
        ceiling: tuple[int, int] | None = None,
        tuning: kg_settings.Tuning = kg_settings.DEFAULT,
    ) -> SnapshotView:
        """What a reader with audience ``reader`` reads of the snapshot, edges of ``edges_of`` at most ``ceiling``."""
        return SnapshotView(self, frozenset(reader), ceiling, tuning)

    def rels(self) -> list[str]:
        return sorted({rel for rel, _ in self.partitions})


@dataclass(frozen=True)
class StoreRows:
    """What a snapshot is made of, as a store file holds one build: its content hash, its live node and edge rows
    (NODE_COLUMNS, EDGE_COLUMNS), its full-text rows keyed as ``node_fts_rows`` keeps them (folded), the folded text
    of its code by file node id, and its top label and audience."""

    content_hash: str
    nodes: tuple[tuple, ...]
    edges: tuple[tuple, ...]
    keyed: tuple[tuple[str, tuple], ...]
    code: dict[str, str]
    top: tuple[tuple[int, int], frozenset[str]]


def store_rows(store, b: int) -> StoreRows:
    """The rows of build ``b`` of a store file (``StoreRows``); the full-text tables hold the latest build only."""
    db: sqlite3.Connection = store.db
    nodes = db.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE {LIVE}", {"b": b}).fetchall()
    edges = db.execute(f"SELECT {EDGE_COLUMNS} FROM edges WHERE {LIVE}", {"b": b}).fetchall()
    keyed = []
    for table, aud, extra in (("node_fts", 0, ""), ("node_fts_aud", 1, ", f.aud")):
        keyed += [
            (key, tuple(row))
            for key, *row in db.execute(
                f"SELECT r.node_id, f.node_id, f.kind, f.name, f.path, f.body{extra} FROM node_fts_rows r"
                f" JOIN {table} f ON f.rowid = r.fts_rowid WHERE r.aud = ?",
                (aud,),
            )
        ]
    code = dict(db.execute("SELECT node_id, body FROM code_fts").fetchall())
    row = db.execute("SELECT content_hash FROM builds WHERE build_id = ?", (b,)).fetchone()
    top = (store.top_label(b) or (0, 0), store.top_audience(b))
    return StoreRows(
        row[0] if row else "", tuple(map(tuple, nodes)), tuple(map(tuple, edges)), tuple(sorted(keyed)), code, top
    )


def _vectors_of(store) -> snapshot_text.Vectors | None:
    """The vectors of a store file (``evo_agents.kg.semantic``), None when it has none."""
    from evo_agents.kg import semantic

    found = semantic.info(store.db)
    if found is None:
        return None
    rows = store.db.execute("SELECT node_id, kind, aud, vec FROM node_vec ORDER BY key").fetchall()
    unpack = struct.Struct(f"<{found.dim}e").unpack
    return snapshot_text.Vectors(
        found,
        tuple(r[0] for r in rows),
        tuple(r[1] for r in rows),
        tuple(r[2] for r in rows),
        tuple(array("f", unpack(r[3])) for r in rows),
    )


class SnapshotView:
    """A snapshot as one reader reads it, with the reads of a ``Store`` its callers use; ``b`` is accepted and ignored,
    a view reads its snapshot's one build. A node or edge read is a fresh dict; its props are shared and not to be
    changed."""

    def __init__(self, snap: Snapshot, reader: frozenset[str], ceiling: tuple[int, int] | None, tuning):
        self.snap, self.reader, self.ceiling, self.tuning = snap, reader, ceiling, tuning

    # -- what the reader sees ---------------------------------------------------------------------------------------

    def sees(self, x: dict) -> bool:
        """Whether the row's audience is within the reader's (``store_filters.SEEN``)."""
        return audience_below(x["label"][3], self.reader)

    def under(self, e: dict, ceiling: tuple[int, int] | None) -> bool:
        return ceiling is None or (e["label"][0] <= ceiling[0] and e["label"][1] <= ceiling[1])

    # -- nodes ------------------------------------------------------------------------------------------------------

    def nodes(self, ids, b: int = 0) -> dict[str, dict]:
        out = {}
        for nid in ids:
            i = self.snap.index.get(nid)
            if i is not None and self.sees(self.snap.nodes[i]):
                out[nid] = dict(self.snap.nodes[i])
        return out

    def node(self, node_id: str, b: int = 0) -> dict | None:
        return self.nodes([node_id]).get(node_id)

    def labels(self, ids, b: int = 0) -> dict[str, tuple]:
        return {nid: n["label"] for nid, n in self.nodes(ids).items()}

    def exact(self, query: str, b: int = 0, limit: int = 50, kinds=()) -> list[str]:
        """Live nodes the reader sees whose id, alias or name is ``query``, by id (``Store.exact``)."""
        wanted = set(kinds or ())
        found = sorted(
            self.snap.ids[i]
            for i in self.snap.names.get(query, ())
            if self.sees(self.snap.nodes[i]) and (not wanted or self.snap.nodes[i]["kind"] in wanted)
        )
        return found[:limit]

    def deprecated(self, ids) -> set[str]:
        """The ids of ``ids`` whose node a decision deprecated, whatever the reader (``Store._deprecated``)."""
        out = set()
        for nid in ids:
            i = self.snap.index.get(nid)
            if i is not None and self.snap.nodes[i]["props"].get("deprecated") is not None:
                out.add(nid)
        return out

    # -- edges ------------------------------------------------------------------------------------------------------

    def _numbers(self, node_id: str, direction: str, rels=None, statuses=None) -> list[int]:
        """The edge numbers of a node's edges of ``rels`` and ``statuses`` (all when None), in the order (rel, src,
        dst, edge id) of ``Store.edges_of``."""
        i = self.snap.index.get(node_id)
        if i is None:
            return []
        found: set[int] = set()
        for (rel, status), part in self.snap.partitions.items():
            if (rels is not None and rel not in rels) or (statuses is not None and status not in statuses):
                continue
            if direction in ("out", "both"):
                found.update(part.out_of(i))
            if direction in ("in", "both"):
                found.update(part.into(i))
        return sorted(found)

    def edges_of(self, node_id: str, b: int = 0, direction: str = "both", limit: int = 200) -> list[dict]:
        """The live edges of a node, within ``ceiling`` and the reader's audience, filtered before the limit."""
        out = []
        for e in self._numbers(node_id, direction):
            edge = self.snap.edges[e]
            if self.sees(edge) and self.under(edge, self.ceiling):
                out.append(dict(edge))
                if len(out) >= limit:
                    break
        return out

    def _into(self, node_id: str, rels, statuses, ceiling) -> list[dict]:
        found = self._numbers(node_id, "in", set(rels), set(statuses))
        return [
            self.snap.edges[e]
            for e in found
            if self.sees(self.snap.edges[e]) and self.under(self.snap.edges[e], ceiling)
        ]

    def edges_into(
        self, node_id: str, b: int, rels, statuses, ceiling: tuple[int, int], *, limit: int, offset: int = 0
    ) -> list[dict]:
        """One page of the edges ``count_into`` counts, in the order of ``edges_of``."""
        return [dict(e) for e in self._into(node_id, rels, statuses, ceiling)[offset : offset + limit]]

    def count_into(self, node_id: str, b: int, rels, statuses, ceiling: tuple[int, int]) -> int:
        """How many live edges lead into a node with one of ``rels`` and ``statuses``, under ``ceiling``."""
        return len(self._into(node_id, rels, statuses, ceiling))

    def degree(self, node_id: str) -> int:
        """Every edge of a node, whatever its relation, status or label: what a hub is told by."""
        i = self.snap.index.get(node_id)
        if i is None:
            return 0
        return sum(len(p.out_of(i)) + len(p.into(i)) for p in self.snap.partitions.values())

    def walk_edges(self, ids: list[str], b: int = 0):
        """The edges the walk of ``evo_agents.kg.ppr`` reads for ``ids``: those out of one of them, then those into
        one, the reader's audience within, whatever the ceiling (``ppr.Reader.edges``)."""
        for direction in ("out", "in"):
            for nid in ids:
                for e in self._numbers(nid, direction):
                    edge = self.snap.edges[e]
                    if self.sees(edge):
                        yield edge

    # -- the ranking by meaning ------------------------------------------------------------------------------------

    def vector_info(self):
        return self.snap.vectors.info if self.snap.vectors is not None else None
