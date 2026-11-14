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
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from evo_agents.kg import settings as kg_settings
from evo_agents.kg import snapshot_text
from evo_agents.kg.policy import audience_below, audience_join
from evo_agents.kg.store_filters import LIVE, label_columns, stored_audience, within
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
    # what the reads of provenance and status give (``StoreRows``); empty for a snapshot of an assembled graph
    units: MappingProxyType = MappingProxyType({})  # unit id: its row (UNIT_KEYS, then its label_aud)
    where: MappingProxyType = MappingProxyType({})  # element id: its (unit, span) rows
    derivations: MappingProxyType = MappingProxyType({})  # element id: its (units, needs) rows
    bodies: MappingProxyType = MappingProxyType({})  # node id: the body of its own full-text row and that row's aud
    summary: MappingProxyType = MappingProxyType({})  # ``Store.summary`` of the build
    aliases: MappingProxyType = MappingProxyType({})  # an alias: the node that holds it (``node_alias``)
    asked: tuple[tuple, ...] = ()  # the rows of the build's asked index (``StoreRows.asked``)
    asked_weight: float | None = None  # the weight of an asked match the build recorded

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
            aliases=MappingProxyType({alias: ids[i] for alias, i in aliased.items()}),
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
        return cls.from_stored(store_rows(store, b), b, _vectors_of(store))

    @classmethod
    def from_stored(cls, rows: StoreRows, build: int = 0, vectors: snapshot_text.Vectors | None = None) -> Snapshot:
        """The snapshot of rows a store gave (``store_rows``) and something kept, such as the hub's tables; its
        vectors are ``vectors``, else those of the rows."""
        text = snapshot_text.TextIndex.build(sorted(rows.keyed), rows.code, folded=True)
        vectors = vectors if vectors is not None else rows_vectors(rows)
        snap = cls.from_rows(rows.content_hash, list(rows.nodes), list(rows.edges), text, vectors, build, rows.top)
        where: dict[str, list] = defaultdict(list)
        for elem, unit, span in rows.wheres:
            where[elem].append((unit, span))
        derived: dict[str, list] = defaultdict(list)
        for elem, units, needs in rows.derivations:
            derived[elem].append((units, needs))
        bodies = {key: (row[4], row[5] if len(row) > 5 else None) for key, row in rows.keyed}
        return replace(
            snap,
            units=MappingProxyType({r[0]: tuple(r) for r in rows.units}),
            where=MappingProxyType({k: tuple(v) for k, v in where.items()}),
            derivations=MappingProxyType({k: tuple(v) for k, v in derived.items()}),
            bodies=MappingProxyType(bodies),
            summary=MappingProxyType(dict(rows.summary)),
            asked=tuple(rows.asked),
            asked_weight=rows.asked_weight,
        )

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
    units: tuple[tuple, ...] = ()  # live units: UNIT_KEYS, then label_aud
    wheres: tuple[tuple, ...] = ()  # live where-provenance: (element, unit, span), by element, unit, span
    derivations: tuple[tuple, ...] = ()  # live derivations: (element, units JSON, needs JSON), by element, deriv id
    summary: dict = field(default_factory=dict)  # ``Store.summary``
    vectors: tuple[tuple, ...] = ()  # the rows of node_vec: (key, node id, kind, aud, vector bytes), by key
    vector_info: dict | None = None  # ``semantic.Info`` of them as a dict; None without vectors
    # the rows of the asked index (node id, level, location, audience, prior, words), by ``asked_order``, and the
    # weight of an asked match the build recorded (``Store.write_asked``); empty and None without them
    asked: tuple[tuple, ...] = ()
    asked_weight: float | None = None


UNIT_KEYS = ("unit", "item", "anchor", "rev", "hash", "source", "span")


def asked_order(rows) -> tuple[tuple, ...]:
    """Asked rows in one order whatever read them: by node, then by the JSON of the rest (an audience may be None)."""
    return tuple(sorted((tuple(r) for r in rows), key=lambda r: (r[0], json.dumps(r[1:]))))


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
    units = db.execute(
        f"SELECT unit_id, item_id, anchor, rev, hash, source, span, label_aud FROM units WHERE {LIVE} ORDER BY unit_id",
        {"b": b},
    ).fetchall()
    wheres = db.execute(
        f"SELECT elem_id, unit_id, span FROM where_prov WHERE {LIVE} ORDER BY elem_id, unit_id, span", {"b": b}
    ).fetchall()
    derivations = db.execute(
        f"SELECT elem_id, units, needs FROM derivations WHERE {LIVE} ORDER BY elem_id, deriv_id", {"b": b}
    ).fetchall()
    vectors, info = _vector_rows(store)
    asked: list = []
    if store.has_asked():
        asked = db.execute(
            "SELECT r.node_id, r.lvl, r.loc, r.aud, r.prior, a.asked FROM node_asked_rows r"
            " JOIN node_asked a ON a.rowid = r.id"
        ).fetchall()
    return StoreRows(
        row[0] if row else "",
        tuple(map(tuple, nodes)),
        tuple(map(tuple, edges)),
        tuple(sorted(keyed)),
        code,
        top,
        tuple(map(tuple, units)),
        tuple(map(tuple, wheres)),
        tuple(map(tuple, derivations)),
        store.summary() if store.latest_ready() == b else {},
        vectors,
        info,
        asked_order(asked),
        store.recorded_asked_weight(),
    )


def _vector_rows(store) -> tuple[tuple[tuple, ...], dict | None]:
    """The rows of a store file's node_vec by key and what they are (``semantic.Info``), or nothing."""
    from dataclasses import asdict

    from evo_agents.kg import semantic

    found = semantic.info(store.db)
    if found is None:
        return (), None
    rows = store.db.execute("SELECT key, node_id, kind, aud, vec FROM node_vec ORDER BY key").fetchall()
    return tuple(map(tuple, rows)), asdict(found)


def rows_vectors(rows: StoreRows) -> snapshot_text.Vectors | None:
    """The vectors of ``rows`` (``StoreRows.vectors``), as ``_vectors_of`` reads them from a file."""
    from evo_agents.kg import semantic

    if not rows.vectors or not rows.vector_info:
        return None
    found = semantic.Info(**rows.vector_info)
    unpack = struct.Struct(f"<{found.dim}e").unpack
    return snapshot_text.Vectors(
        found,
        tuple(r[1] for r in rows.vectors),
        tuple(r[2] for r in rows.vectors),
        tuple(r[3] for r in rows.vectors),
        tuple(array("f", unpack(bytes(r[4]))) for r in rows.vectors),
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
        self._ranker = None
        self.asked_clearance: tuple[int, int] | None = None  # search reads the asked rows within it (``Store``)
        self.asked_weight: float | None = None  # the weight of an asked match in place of the build's (an eval)

    def close(self) -> None:
        """Nothing to close: a view reads RAM."""

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

    def degree(self, node_id: str, b: int = 0) -> int:
        """How many edges touch a node, whatever their relation, status or level, the reader's audience within, an
        edge from the node to itself once (``Store.degree``): what a hub is told by."""
        i = self.snap.index.get(node_id)
        if i is None:
            return 0
        held = {e for p in self.snap.partitions.values() for e in (*p.out_of(i), *p.into(i))}
        return sum(1 for e in held if self.sees(self.snap.edges[e]))

    def degrees(self, ids, b: int, ceiling: tuple[int, int]) -> dict[str, int]:
        """How many edges at most ``ceiling`` touch each of ``ids``, the reader's audience within, an edge from a node
        to itself twice; ids without one are left out (``Store.degrees``)."""
        out: dict[str, int] = {}
        for nid in dict.fromkeys(ids):
            i = self.snap.index.get(nid)
            if i is None:
                continue
            count = sum(
                1
                for p in self.snap.partitions.values()
                for e in (*p.out_of(i), *p.into(i))
                if self.sees(self.snap.edges[e]) and self.under(self.snap.edges[e], ceiling)
            )
            if count:
                out[nid] = count
        return out

    def alias_of(self, alias: str, b: int = 0) -> str | None:
        """The node the reader sees that holds ``alias`` (an id merged into it), or None."""
        nid = self.snap.aliases.get(alias)
        return nid if nid is not None and self.node(nid) is not None else None

    # -- search (``Store.search``, ``search_scored``, ``plain_search``, ``code_files``, ``code_lines``) ----------------

    @property
    def ranker(self):
        if self._ranker is None:
            self._ranker = snapshot_text.Ranker(self)
        return self._ranker

    def search(self, query: str, b: int = 0, limit: int = 50, **kw) -> list[str]:
        return self.ranker.search(query, b, limit, **kw)

    def search_scored(self, query: str, b: int = 0, limit: int = 50, **kw) -> list[tuple[str, float | None]]:
        return self.ranker.search_scored(query, b, limit, **kw)

    def matched(self, ids: list[str], query: str, snippets=frozenset()) -> dict[str, dict]:
        """``Store.matched``'s word counts, without its snippet and passage lines (not made from a snapshot yet)."""
        return self.ranker.matched(ids, query)

    def plain_search(self, query: str, limit: int, kinds: list[str] | None = None) -> list[str]:
        """Ids of the nodes plain BM25 ranks for ``query``, best first, from the rows without an audience."""
        from evo_agents.kg import search as text_search

        fts_query = text_search.plain_query(query)
        if fts_query is None:
            return []
        wanted = {k for k in kinds or text_search.PLAIN_KINDS if isinstance(k, str)}
        text = self.snap.text
        found = text.public.search(
            fts_query, text_search.PLAIN_WEIGHTS[2:], lambda n: text.public_rows[n].kind in wanted
        )
        ranked = sorted(found, key=lambda x: (x[1], text.public_rows[x[0]].node_id))[: limit * 4]
        return list(dict.fromkeys(text.public_rows[n].node_id for n, _ in ranked))[:limit]

    def code_files(self, words: list[str], limit: int | None = None) -> list[tuple[str, int]]:
        return self.ranker.code_files(words) if limit is None else self.ranker.code_files(words, limit)

    def code_lines(self, node_id: str, words: list[str], most: int | None = None) -> list[int]:
        return self.ranker.code_lines(node_id, words) if most is None else self.ranker.code_lines(node_id, words, most)

    def asked_nodes(self, ids) -> set[str]:
        """The nodes of ``ids`` with an asked row under any label (``Store.asked_nodes``)."""
        return set(ids) & {row[0] for row in self.snap.asked}

    def weight_of_asked(self) -> float:
        """``Store.weight_of_asked``: ``asked_weight`` when set, else the build's, else ``search.ASKED_WEIGHT``."""
        from evo_agents.kg import search as text_search

        for weight in (self.asked_weight, self.snap.asked_weight):
            if weight is not None:
                return weight
        return text_search.ASKED_WEIGHT

    # -- the web's counts (``Store.kind_counts``, ``shape_counts``, ``build_row``) -------------------------------------

    def kind_counts(self, b: int = 0) -> list[tuple[str, int, int, str, int]]:
        counts: dict[tuple, int] = defaultdict(int)
        for n in self.snap.nodes:
            if self.sees(n):
                counts[(n["kind"], *n["label"][:3])] += 1
        return [(*key, count) for key, count in counts.items()]

    def shape_counts(self, b: int = 0) -> list[tuple]:
        counts: dict[tuple, int] = defaultdict(int)
        nodes, index = self.snap.nodes, self.snap.index
        for e in self.snap.edges:
            s, d = nodes[index[e["src"]]], nodes[index[e["dst"]]]
            if self.sees(e) and self.sees(s) and self.sees(d):
                counts[(s["kind"], e["rel"], d["kind"], *(tuple(x["label"][:3]) for x in (e, s, d)))] += 1
        return [(*key, count) for key, count in counts.items()]

    def build_row(self, build_id: int = 0) -> dict:
        """What the build's summary keeps of ``Store.build_row``: id, status, finish, content hash, coverage, stats."""
        summary = self.snap.summary
        return {
            "build_id": summary.get("build_id"),
            "status": "ready" if summary.get("ready") else None,
            "finished_at": summary.get("built_at"),
            "content_hash": summary.get("content_hash"),
            "coverage": dict(summary.get("coverage") or {}),
            "stats": dict(summary.get("stats") or {}),
        }

    # -- what locate and plain search read whatever the reader (``Store.file_ids`` and the rest) ----------------------

    def file_ids(self) -> list[str]:
        return [n["id"] for n in self.snap.nodes if n["kind"] == "File"]

    def nodes_between(self, low: str, high: str) -> list[tuple[str, dict]]:
        from bisect import bisect_left

        ids = self.snap.ids
        return [(ids[i], self.snap.nodes[i]["props"]) for i in range(bisect_left(ids, low), bisect_left(ids, high))]

    def changed_by(self, file_id: str, limit: int) -> list[str]:
        i = self.snap.index.get(file_id)
        if i is None:
            return []
        part = [p for (rel, _), p in self.snap.partitions.items() if rel == "changes"]
        return [self.snap.edges[e]["src"] for p in part for e in p.into(i)][:limit]

    def changes_of(self, commits) -> list[tuple[str, str]]:
        part = [p for (rel, _), p in self.snap.partitions.items() if rel == "changes"]
        out = []
        for c in commits:
            i = self.snap.index.get(c)
            if i is not None:
                out += [(c, self.snap.edges[e]["dst"]) for p in part for e in p.out_of(i)]
        return out

    # -- provenance, text and status (``Store.unit``, ``where``, ``derivations``, ``body``, ``summary``, ``seen``) ----

    def unit(self, unit_id: str, b: int = 0) -> dict | None:
        row = self.snap.units.get(unit_id)
        if row is None or not within(self.reader, row[7]):
            return None
        return dict(zip(UNIT_KEYS, row[:7], strict=True))

    def where(self, elem_id: str, b: int = 0) -> list[dict]:
        return [{"unit": unit, "span": span} for unit, span in self.snap.where.get(elem_id, ())]

    def derivations(self, elem_id: str, b: int = 0) -> list[dict]:
        return [{"units": json.loads(u), "needs": json.loads(n)} for u, n in self.snap.derivations.get(elem_id, ())]

    def body(self, node_id: str) -> str | None:
        """The text of node ``node_id``'s own full-text row; a row with an audience only for a reader within it."""
        from evo_agents.kg.search import stored_text

        held = self.snap.bodies.get(node_id)
        if held is None or (held[1] is not None and (not self.reader or not within(self.reader, held[1]))):
            return None
        return stored_text(held[0]) if held[0] else None

    def path_nodes(self, b: int = 0) -> list[dict]:
        """Live File and Document nodes that carry a path, by id."""
        return [
            dict(n)
            for n in self.snap.nodes
            if n["kind"] in ("File", "Document") and self.sees(n) and n["props"].get("path")
        ]

    def file_symbols(self, file_ids, b: int = 0) -> dict[str, list[dict]]:
        """Live Symbol nodes grouped by the file item that defines them (``props.file``), by id."""
        wanted, out = set(file_ids), defaultdict(list)
        for n in self.snap.nodes:
            if n["kind"] == "Symbol" and n["props"].get("file") in wanted and self.sees(n):
                out[n["props"]["file"]].append(dict(n))
        return dict(out)

    def latest_ready(self, before: int | None = None) -> int | None:
        return self.snap.summary.get("build_id", self.snap.build) if before is None else None

    def top_label(self, b: int = 0) -> tuple[int, int]:
        return self.snap.top_label

    def top_audience(self, b: int = 0) -> frozenset[str]:
        return self.snap.top_audience

    def summary(self) -> dict:
        return dict(self.snap.summary) if self.snap.summary else {"ready": False}

    def seen(self, b: int = 0) -> dict:
        """What the snapshot holds within the reader's audience, whatever the level (``Store.seen``)."""
        units = [r for r in self.snap.units.values() if within(self.reader, r[7])]
        items: dict[str, set] = defaultdict(set)
        for r in units:
            items[r[5]].add(r[1])
        return {
            "nodes": sum(1 for n in self.snap.nodes if self.sees(n)),
            "edges": sum(1 for e in self.snap.edges if self.sees(e)),
            "units": len(units),
            "items": {source: len(ids) for source, ids in items.items()},
            "audience": {r[5] for r in self.snap.units.values() if r[7] is not None},
        }

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
