"""The project graph store (definition D7): one SQLite file, rebuildable from the log.

Every row carries ``tx_from`` and ``tx_to`` (build ids). A build writes only what changed: rows that
disappeared or changed get ``tx_to``, new versions get ``tx_from``; then the build flips to ``ready``.
Readers pin the latest ready build B and see rows with ``tx_from <= B < tx_to``, so a build in progress
is never visible, and retracting a fact keeps its history queryable until retention deletes the builds older than
its period, but the latest two, and the rows only they read (``history_floor``, ``drop_history``).

Provenance is stored as a circuit: each element has derivations, and a derivation is the AND of its
ownership units and of the elements it needs. An element is alive while one derivation holds (OR).

Keys (PG-Keys): a node of a keyed kind carries ``props.key``, and no two live, non-proposed nodes of that
kind share one. A Symbol's key is ``[repo, path, qualname]``, so two sources reading the same file of the
same repo cannot both put the same function in the graph.

Search (``evo_agents.kg.search``): the full-text index ``node_fts`` and the aliases ``node_alias`` hold the latest
build only. A build rewrites the aliases, and only the rows of ``node_fts`` whose content changed: ``node_fts_rows``
keeps the rowid and digest of each node's row. A node is found by its id, its name or an alias through an index,
then by its words.

Clusters (``evo_agents.kg.clusters``): ``clusters``, ``cluster_members`` and ``cluster_links`` are bitemporal like
the graph and in its content hash, but only when a build has some, so a build without them hashes as before. A file
written before them lacks the tables: it still opens, and ``clusters`` reads None from it.

``cluster_warm`` keeps, per build, the cluster members its clustering started from (the previous build's), and the
build's ``stats["warm"]`` says how many there were (0 for a cold start): a hub build is written into an empty store
that replaces the last one, so the build before it is gone when ``verify_build`` rebuilds it. The table is outside the
content hash, and a build whose ``stats`` lack ``warm`` recorded none.

A build's ``stats`` also hold its ``size`` (live nodes, edges and units) and ``top_label``, the highest level and
the highest location any of its nodes, edges and units carries: a reader whose clearance covers both sees everything,
and need not test each label (``evo_agents.kg.serve``); and ``top_audience``, the join of their audiences, when one has
an audience.

Audience (``evo_agents.kg.policy``): a unit, node or edge with one keeps it in ``label_aud``, a sorted JSON list, and
NULL otherwise. ``reader`` is the audience of whoever reads the store, none until set: every read of units, nodes and
edges, their counts and the full-text search leave out the rows whose audience is not within it, so a hidden element
never fills a limit, a count or a page. The full-text rows of nodes with an audience sit in ``node_fts_aud``, apart from
``node_fts``: they never weigh in the ranking of what a reader without that audience finds. ``counts`` and ``summary``
stay the build's totals; ``seen`` counts what the reader's audience holds.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg import search as text_search
from evo_agents.kg.ids import utc_now
from evo_agents.kg.policy import TOP_AUDIENCE, audience_below, audience_join, audience_of
from evo_agents.kg.protocol import canonical_json, sha256

SCHEMA_VERSION = "4"  # 4: units, nodes and edges gained label_aud, node_fts_aud came; 3: node_fts kind and path
KEYED_KINDS = ("Symbol",)  # kinds whose props.key is unique; keep in step with the nodes_key index

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS builds (
    build_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state_digest TEXT,
    content_hash TEXT,
    coverage TEXT,
    stats TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT NOT NULL, item_id TEXT NOT NULL, anchor TEXT NOT NULL, rev TEXT, hash TEXT, source TEXT,
    span TEXT, label_lvl INTEGER, label_loc INTEGER, label_int TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER, label_aud TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, props TEXT, status TEXT, conf REAL,
    label_lvl INTEGER, label_loc INTEGER, label_int TEXT, aliases TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER, label_aud TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL, props TEXT, status TEXT,
    conf REAL, label_lvl INTEGER, label_loc INTEGER, label_int TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER, label_aud TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS derivations (
    elem_id TEXT NOT NULL, deriv_id TEXT NOT NULL, units TEXT NOT NULL, needs TEXT NOT NULL,
    content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS where_prov (
    elem_id TEXT NOT NULL, unit_id TEXT NOT NULL, span TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS nodes_live ON nodes(node_id) WHERE tx_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS nodes_key ON nodes(kind, json_extract(props, '$.key'))
    WHERE kind = 'Symbol' AND tx_to IS NULL AND status <> 'proposed';
CREATE INDEX IF NOT EXISTS nodes_tx ON nodes(node_id, tx_from);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name, tx_from);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, tx_from);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, tx_from);
CREATE INDEX IF NOT EXISTS edges_id ON edges(edge_id, tx_from);
CREATE INDEX IF NOT EXISTS deriv_elem ON derivations(elem_id, tx_from);
CREATE INDEX IF NOT EXISTS where_elem ON where_prov(elem_id, tx_from);
CREATE INDEX IF NOT EXISTS units_id ON units(unit_id, tx_from);
CREATE INDEX IF NOT EXISTS units_item ON units(item_id, tx_from);
CREATE TABLE IF NOT EXISTS node_alias (alias TEXT PRIMARY KEY, node_id TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    node_id UNINDEXED, kind UNINDEXED, name, path, body, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts_aud USING fts5(
    node_id UNINDEXED, kind UNINDEXED, name, path, body, aud UNINDEXED, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS node_fts_rows (
    node_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL, digest TEXT NOT NULL, aud INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT NOT NULL, level INTEGER NOT NULL, parent TEXT, label TEXT NOT NULL, size INTEGER NOT NULL,
    kinds TEXT NOT NULL, content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS cluster_members (
    node_id TEXT NOT NULL, cluster_id TEXT NOT NULL, sub_id TEXT, content TEXT NOT NULL,
    tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS cluster_links (
    link_id TEXT NOT NULL, level INTEGER NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, weight REAL NOT NULL,
    content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS clusters_id ON clusters(cluster_id, tx_from);
CREATE INDEX IF NOT EXISTS cluster_members_node ON cluster_members(node_id, tx_from);
CREATE INDEX IF NOT EXISTS cluster_members_cluster ON cluster_members(cluster_id, tx_from);
CREATE INDEX IF NOT EXISTS cluster_links_id ON cluster_links(link_id, tx_from);
CREATE TABLE IF NOT EXISTS review_items (
    fingerprint TEXT NOT NULL, rule TEXT NOT NULL, cluster_id TEXT, evidence TEXT NOT NULL, clusters TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL, content TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS review_items_id ON review_items(fingerprint, tx_from);
CREATE TABLE IF NOT EXISTS cluster_warm (
    build_id INTEGER NOT NULL, node_id TEXT NOT NULL, cluster_id TEXT NOT NULL, sub_id TEXT,
    PRIMARY KEY (build_id, node_id)
) STRICT, WITHOUT ROWID;
"""

_NO_WARM = object()  # Store.write: no warm start to record
LIVE = "tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)"
SEEN = "(label_aud IS NULL OR aud_seen(label_aud))"  # the row's audience is within the store's reader
FTS_TABLES = ("node_fts", "node_fts_aud")  # the full-text rows of nodes without an audience, and with one
NODE_COLUMNS = "node_id, kind, name, props, status, conf, label_lvl, label_loc, label_int, aliases, label_aud"
EDGE_COLUMNS = "edge_id, src, rel, dst, props, status, conf, label_lvl, label_loc, label_int, label_aud"
CLUSTER_TABLES = ("clusters", "cluster_members", "cluster_links")  # absent from a file written before clusters came
REVIEW_TABLES = ("review_items",)  # absent from a file written before the review queue came
# the tables whose rows carry tx_from, tx_to
HISTORY_TABLES = ("units", "nodes", "edges", "derivations", "where_prov", *CLUSTER_TABLES, *REVIEW_TABLES)
# Builds retention always keeps: the latest, and the one before it, which a reader that read the latest build id just
# before a build landed may still be reading.
KEEP_BUILDS = 2


def _live(t: str) -> str:
    """LIVE for the rows of table alias ``t``."""
    return f"{t}.tx_from <= :b AND ({t}.tx_to IS NULL OR {t}.tx_to > :b)"


def _owner(t: str) -> str:
    """The node clusters count the node of table alias ``t`` as (``evo_agents.kg.clusters.project``): a Symbol's
    ``props.file``, a Section's ``props.item``, any other node itself."""
    return (
        f"CASE {t}.kind WHEN 'Symbol' THEN json_extract({t}.props, '$.file')"
        f" WHEN 'Section' THEN json_extract({t}.props, '$.item') ELSE {t}.node_id END"
    )


def _seen(t: str) -> str:
    """SEEN for the rows of table alias ``t``."""
    return f"({t}.label_aud IS NULL OR aud_seen({t}.label_aud))"


def edge_id(src: str, rel: str, dst: str) -> str:
    return "e:" + sha256(f"{src}\x1f{rel}\x1f{dst}")[7:23]


def label_columns(label) -> dict:
    """The label columns of a row: ``label_aud`` only when the label names an audience, so a row without one keeps
    the content it had before audiences came."""
    columns = {"label_lvl": label.level, "label_loc": label.location, "label_int": label.integrity}
    return {**columns, "label_aud": json.dumps(sorted(label.audience))} if label.audience else columns


def stored_audience(text: str | None) -> frozenset[str]:
    """The audience a ``label_aud`` holds; the highest for one that cannot be read."""
    if text is None:
        return frozenset()
    try:
        return audience_of(json.loads(text))
    except ValueError:
        return TOP_AUDIENCE


@dataclass
class Graph:
    """An assembled graph, ready to write: plain dicts keyed by id."""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[str, dict] = field(default_factory=dict)
    derivations: dict[str, list[dict]] = field(default_factory=dict)  # elem id -> [{units, needs}]
    where: dict[str, list[list[str]]] = field(default_factory=dict)  # elem id -> [[unit, span]]
    units: dict[str, dict] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)  # node id -> the body of its searchable text
    paths: dict[str, str] = field(default_factory=dict)  # node id -> the path it is found by, when it has one
    clusters: Clusters | None = None  # None: not computed (``evo_agents.kg.clusters``)
    mentions: dict[str, dict] = field(default_factory=dict)  # item id -> its dangling and ambiguous mentions, samples
    review: list[dict] = field(default_factory=list)  # the review items of a build with clusters (``kg.review``)
    # The deprecations a curation item declared (``evo_agents.kg.pipeline.curation``): applied to the props of the
    # nodes and edges they name before the graph is written, never written themselves.
    deprecations: list[dict] = field(default_factory=list)


@dataclass
class Clusters:
    """The clusters of a graph (``evo_agents.kg.clusters``): each cluster's level (1 or 2), parent (a level-2 cluster's
    level-1 cluster), label, size and members by kind; each member's level-1 cluster and level-2 cluster, if any; and
    the weight of the edges between two clusters of a level, keyed (level, a, b) with a < b."""

    clusters: dict[str, dict] = field(default_factory=dict)
    members: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    links: dict[tuple[int, str, str], float] = field(default_factory=dict)


def _content(row: dict) -> str:
    return sha256(canonical_json(row))


def key_conflicts(graph: Graph) -> dict[tuple[str, str], list[str]]:
    """Keys that more than one node of the graph would hold, under the same predicate as the nodes_key
    index. Checked before writing, so a violation names its nodes instead of surfacing as an IntegrityError."""
    holders: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid, n in graph.nodes.items():
        key = (n.get("props") or {}).get("key")
        if n["kind"] in KEYED_KINDS and n["status"] not in (None, "proposed") and key is not None:
            holders[(n["kind"], json.dumps(key, ensure_ascii=False))].append(nid)
    return {k: sorted(ids) for k, ids in sorted(holders.items()) if len(ids) > 1}


class StaleSchema(RuntimeError):
    """A store file written by an older schema. ``kg build`` rebuilds it from the log; readers refuse it."""

    def __init__(self, path: Path, found: str):
        self.path, self.found = path, found
        super().__init__(
            f"{path} has store schema {found}, this code reads {SCHEMA_VERSION}:"
            " run `evo-agents kg build` to rebuild it from the corpus log"
        )


def _schema_version(db: sqlite3.Connection) -> str | None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else None


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        found = _schema_version(self.db)
        if found is not None and found != SCHEMA_VERSION:
            self.db.close()
            raise StaleSchema(path, found)  # before SCHEMA runs: an old file is never half-migrated
        self._reads()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.db.commit()

    @classmethod
    def for_project(cls, project) -> Store:
        return cls(project.root / "graph.sqlite")

    @classmethod
    def open_existing(cls, project) -> Store | None:
        """The project's store for reading; raises StaleSchema on a file from an older schema."""
        path = project.root / "graph.sqlite"
        return cls(path) if path.exists() else None

    @classmethod
    def open_readonly(cls, project) -> Store | None:
        """The project's store for a reader that must stay fast, such as a hook: it never writes, so it never
        waits on a build holding the write lock. None when there is no store; StaleSchema on an older file."""
        path = project.root / "graph.sqlite"
        if not path.exists():
            return None
        return cls.open_file(path)

    @classmethod
    def open_file(cls, path: Path, *, immutable: bool = False) -> Store:
        """The store in the file at ``path``, read-only; StaleSchema on an older file. ``immutable`` is for a file
        nothing ever writes again, such as a graph the hub built: SQLite then takes no lock and makes no -shm file."""
        store = cls.__new__(cls)
        store.path = path
        mode = "mode=ro&immutable=1" if immutable else "mode=ro"
        store.db = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
        found = _schema_version(store.db)
        if found != SCHEMA_VERSION:
            store.db.close()
            raise StaleSchema(path, found or "none")
        store._reads()
        return store

    def _reads(self) -> None:
        """Read as a reader with no audience until ``reader`` is set: rows with one are left out (fail closed)."""
        self.reader: frozenset[str] = frozenset()
        self.db.create_function("aud_seen", 1, lambda text: audience_below(stored_audience(text), self.reader))

    @staticmethod
    def stored_schema(path: Path) -> str | None:
        """The schema version a store file was written with, without migrating it; None if there is none."""
        if not path.exists():
            return None
        db = sqlite3.connect(path)
        try:
            return _schema_version(db)
        finally:
            db.close()

    @classmethod
    def staging(cls, path: Path) -> Store:
        """An empty store next to ``path``, to be filled and then moved over it with ``install``."""
        stage = path.with_name(path.name + ".rebuild")
        for leftover in (stage, Path(f"{stage}-wal"), Path(f"{stage}-shm")):
            leftover.unlink(missing_ok=True)
        return cls(stage)

    def leave_wal(self) -> None:
        """Write the WAL back into the file and leave WAL mode, so the file alone holds the whole store and can be
        moved or copied as one."""
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("PRAGMA journal_mode=DELETE")

    def install(self, target: Path) -> Store:
        """Replace ``target`` with this store's file in one rename and return a store opened there. The
        file leaves WAL mode first, and the old file's WAL and shared memory go, so the new file never
        meets a log written for the old one."""
        self.leave_wal()
        self.db.close()
        for stale in (Path(f"{target}-wal"), Path(f"{target}-shm")):
            stale.unlink(missing_ok=True)
        os.replace(self.path, target)
        return Store(target)

    def close(self) -> None:
        self.db.close()

    # -- builds ---------------------------------------------------------------------------------

    def latest_ready(self, before: int | None = None) -> int | None:
        """The latest ready build, or the latest before build ``before``."""
        if before is not None:
            sql = "SELECT max(build_id) FROM builds WHERE status = 'ready' AND build_id < ?"
            row = self.db.execute(sql, (before,)).fetchone()
            return row[0] if row and row[0] is not None else None
        row = self.db.execute("SELECT max(build_id) FROM builds WHERE status = 'ready'").fetchone()
        return row[0] if row and row[0] is not None else None

    def ready_build_of(self, content_hash: str) -> int | None:
        """The latest ready build whose content hash is ``content_hash``."""
        sql = "SELECT max(build_id) FROM builds WHERE status = 'ready' AND content_hash = ?"
        row = self.db.execute(sql, (content_hash,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def build_row(self, build_id: int) -> dict:
        row = self.db.execute(
            "SELECT build_id, status, started_at, finished_at, state_digest, content_hash, coverage, stats"
            " FROM builds WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        keys = ("build_id", "status", "started_at", "finished_at", "state_digest", "content_hash", "coverage", "stats")
        out = dict(zip(keys, row, strict=True))
        out["coverage"] = json.loads(out["coverage"] or "{}")
        out["stats"] = json.loads(out["stats"] or "{}")
        return out

    def top_label(self, build_id: int) -> tuple[int, int] | None:
        """The highest level and location of build ``build_id``'s labels (its ``stats``); None for a build written
        before builds recorded it."""
        row = self.db.execute(
            "SELECT json_extract(stats, '$.top_label[0]'), json_extract(stats, '$.top_label[1]') FROM builds"
            " WHERE build_id = ?",
            (build_id,),
        ).fetchone()
        return (row[0], row[1]) if row and isinstance(row[0], int) and isinstance(row[1], int) else None

    def top_audience(self, build_id: int) -> frozenset[str]:
        """The join of the audiences of build ``build_id``'s nodes, edges and units (its ``stats``)."""
        sql = "SELECT json_extract(stats, '$.top_audience') FROM builds WHERE build_id = ?"
        found = self.db.execute(sql, (build_id,)).fetchone()
        return stored_audience(found[0] if found else None)

    def write(
        self,
        graph: Graph,
        *,
        state_digest: str,
        coverage: dict,
        stats: dict,
        policy,
        warm: Clusters | None | object = _NO_WARM,
    ) -> int:
        """Write a graph as a new build, diffing against the previous one, in one transaction. ``warm``, when given, is
        the clusters the build's clustering started from, kept in ``cluster_warm`` for ``warm_start``."""
        db = self.db
        with db:
            cur = db.execute(
                "INSERT INTO builds (status, started_at, state_digest) VALUES ('building', ?, ?)",
                (utc_now(), state_digest),
            )
            b = cur.lastrowid
            self._diff(
                "units",
                "unit_id",
                b,
                {uid: {"unit_id": uid, **u} for uid, u in graph.units.items()},
                [
                    "unit_id",
                    "item_id",
                    "anchor",
                    "rev",
                    "hash",
                    "source",
                    "span",
                    "label_lvl",
                    "label_loc",
                    "label_int",
                    "label_aud",
                ],
            )
            self._diff(
                "nodes",
                "node_id",
                b,
                {
                    nid: {
                        "node_id": nid,
                        "kind": n["kind"],
                        "name": n["name"],
                        "props": json.dumps(n["props"], ensure_ascii=False, sort_keys=True),
                        "status": n["status"],
                        "conf": n["conf"],
                        **label_columns(n["label"]),
                        "aliases": json.dumps(n.get("aliases", [])),
                    }
                    for nid, n in graph.nodes.items()
                },
                [
                    "node_id",
                    "kind",
                    "name",
                    "props",
                    "status",
                    "conf",
                    "label_lvl",
                    "label_loc",
                    "label_int",
                    "aliases",
                    "label_aud",
                ],
            )
            self._diff(
                "edges",
                "edge_id",
                b,
                {
                    eid: {
                        "edge_id": eid,
                        "src": e["src"],
                        "rel": e["rel"],
                        "dst": e["dst"],
                        "props": json.dumps(e["props"], ensure_ascii=False, sort_keys=True),
                        "status": e["status"],
                        "conf": e["conf"],
                        **label_columns(e["label"]),
                    }
                    for eid, e in graph.edges.items()
                },
                ["edge_id", "src", "rel", "dst", "props", "status", "conf"]
                + ["label_lvl", "label_loc", "label_int", "label_aud"],
            )
            derivs = {}
            for elem, ds in graph.derivations.items():
                for d in ds:
                    units, needs = json.dumps(sorted(d["units"])), json.dumps(sorted(d["needs"]))
                    did = sha256(f"{elem}\x1f{units}\x1f{needs}")[7:23]
                    derivs[f"{elem}\x1f{did}"] = {"elem_id": elem, "deriv_id": did, "units": units, "needs": needs}
            self._diff("derivations", None, b, derivs, ["elem_id", "deriv_id", "units", "needs"])
            wheres = {}
            for elem, entries in graph.where.items():
                for unit, span in entries:
                    wheres[f"{elem}\x1f{unit}\x1f{span}"] = {"elem_id": elem, "unit_id": unit, "span": span}
            self._diff("where_prov", None, b, wheres, ["elem_id", "unit_id", "span"])
            self._write_clusters(b, graph.clusters or Clusters())
            self._write_review(b, graph.review)
            searched = self._write_search(graph)
            content_hash = self.content_hash(b, searched)
            stats = {**stats, "size": _size(graph), "top_label": _top_label(graph), **_top_audience(graph)}
            if warm is not _NO_WARM:
                stats["warm"] = self._write_warm(b, warm)
            db.execute(
                "UPDATE builds SET status = 'ready', finished_at = ?, content_hash = ?, coverage = ?, stats = ?"
                " WHERE build_id = ?",
                (utc_now(), content_hash, json.dumps(coverage, ensure_ascii=False), json.dumps(stats), b),
            )
        return b

    def _write_warm(self, b: int, warm: Clusters | None) -> int:
        members = sorted((warm.members if warm is not None else {}).items())
        self.db.executemany(
            "INSERT INTO cluster_warm (build_id, node_id, cluster_id, sub_id) VALUES (?, ?, ?, ?)",
            [(b, nid, top, sub) for nid, (top, sub) in members],
        )
        return len(members)

    def warm_start(self, b: int) -> tuple[bool, Clusters | None]:
        """Whether build b recorded the clusters it started from, and those clusters (members only; None for a cold
        start)."""
        stats = self.db.execute("SELECT stats FROM builds WHERE build_id = ?", (b,)).fetchone()
        found = json.loads(stats[0] or "{}") if stats else {}
        if "warm" not in found:
            return False, None
        out = Clusters()
        sql = "SELECT node_id, cluster_id, sub_id FROM cluster_warm WHERE build_id = ? ORDER BY node_id"
        for nid, top, sub in self.db.execute(sql, (b,)):
            out.members[nid] = (top, sub)
        return True, out if out.members else None

    def _write_clusters(self, b: int, found: Clusters) -> None:
        """Bring the cluster tables to ``found``; a graph without clusters closes the previous build's."""
        self._diff(
            "clusters",
            "cluster_id",
            b,
            {
                cid: {
                    "cluster_id": cid,
                    "level": c["level"],
                    "parent": c["parent"],
                    "label": c["label"],
                    "size": c["size"],
                    "kinds": json.dumps(c["kinds"], sort_keys=True),
                }
                for cid, c in found.clusters.items()
            },
            ["cluster_id", "level", "parent", "label", "size", "kinds"],
        )
        self._diff(
            "cluster_members",
            "node_id",
            b,
            {nid: {"node_id": nid, "cluster_id": top, "sub_id": sub} for nid, (top, sub) in found.members.items()},
            ["node_id", "cluster_id", "sub_id"],
        )
        self._diff(
            "cluster_links",
            "link_id",
            b,
            {
                f"{level}\x1f{a}\x1f{c}": {
                    "link_id": f"{level}\x1f{a}\x1f{c}",
                    "level": level,
                    "a": a,
                    "b": c,
                    "weight": w,
                }
                for (level, a, c), w in found.links.items()
            },
            ["link_id", "level", "a", "b", "weight"],
        )

    def _write_review(self, b: int, items: list[dict]) -> None:
        """Bring the review items to ``items``; a graph without them closes the previous build's."""
        self._diff(
            "review_items",
            "fingerprint",
            b,
            {
                item["fingerprint"]: {
                    "fingerprint": item["fingerprint"],
                    "rule": item["rule"],
                    "cluster_id": item["cluster"],
                    "evidence": json.dumps(item["evidence"], ensure_ascii=False),
                    "clusters": json.dumps(item["clusters"]),
                    "action": item["action"],
                    "detail": json.dumps(item["detail"], ensure_ascii=False, sort_keys=True),
                }
                for item in items
            },
            ["fingerprint", "rule", "cluster_id", "evidence", "clusters", "action", "detail"],
        )

    def has_review(self) -> bool:
        """Whether the file has the review table: one written before the review queue came lacks it."""
        sql = "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = ?"
        return self.db.execute(sql, REVIEW_TABLES).fetchone()[0] == 1

    def review_items(self, b: int) -> list[dict]:
        """The review items of build b (``evo_agents.kg.review``), whatever the reader, by fingerprint; none for a
        build without them or a file written before them."""
        if not self.has_review():
            return []
        rows = self.db.execute(
            "SELECT fingerprint, rule, cluster_id, evidence, clusters, action, detail FROM review_items"
            f" WHERE {LIVE} ORDER BY fingerprint",
            {"b": b},
        )
        return [
            {
                "fingerprint": fp,
                "rule": rule,
                "cluster": cluster,
                "evidence": json.loads(evidence),
                "clusters": json.loads(clusters),
                "action": action,
                "detail": json.loads(detail),
            }
            for fp, rule, cluster, evidence, clusters, action, detail in rows
        ]

    def labels(self, ids, b: int) -> dict[str, tuple]:
        """The label of each node of ``ids`` live at build b with an audience within ``reader``."""
        return {nid: n["label"] for nid, n in self.nodes(ids, b).items()}

    def edge_label(self, edge_id: str, b: int) -> tuple | None:
        """The label of edge ``edge_id`` live at build b with an audience within ``reader``; None without one."""
        row = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE edge_id = :id AND {LIVE} AND {SEEN}", {"id": edge_id, "b": b}
        ).fetchone()
        return _edge_row(row)["label"] if row else None

    def has_clusters(self) -> bool:
        """Whether the file has the cluster tables: one written before clusters came lacks them."""
        marks = ", ".join("?" for _ in CLUSTER_TABLES)
        sql = f"SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN ({marks})"
        return self.db.execute(sql, CLUSTER_TABLES).fetchone()[0] == len(CLUSTER_TABLES)

    def clusters(self, b: int) -> Clusters | None:
        """The clusters of build b, whatever the reader; None when the build has none (computed without the extra,
        or written before clusters came)."""
        if not self.has_clusters():
            return None
        out = Clusters()
        for cid, level, parent, label, size, kinds in self.db.execute(
            f"SELECT cluster_id, level, parent, label, size, kinds FROM clusters WHERE {LIVE} ORDER BY cluster_id",
            {"b": b},
        ):
            out.clusters[cid] = {
                "level": level,
                "parent": parent,
                "label": label,
                "size": size,
                "kinds": json.loads(kinds),
            }
        if not out.clusters:
            return None
        for nid, top, sub in self.db.execute(
            f"SELECT node_id, cluster_id, sub_id FROM cluster_members WHERE {LIVE} ORDER BY node_id", {"b": b}
        ):
            out.members[nid] = (top, sub)
        for level, a, c, w in self.db.execute(
            f"SELECT level, a, b, weight FROM cluster_links WHERE {LIVE} ORDER BY link_id", {"b": b}
        ):
            out.links[(level, a, c)] = w
        return out

    def _write_search(self, graph: Graph) -> str:
        """Bring the full-text index to ``graph``'s nodes, deleting and inserting only the rows that changed, and
        replace the aliases; the digest of what the index holds. A row of a node with an audience ends with it."""
        rows = [
            (
                nid,
                n["kind"],
                text_search.index_text(n["name"] or ""),
                text_search.fold(graph.paths.get(nid, "")),
                text_search.index_text(graph.texts.get(nid, "")),
            )
            + ((label_columns(n["label"])["label_aud"],) if n["label"].audience else ())
            for nid, n in sorted(graph.nodes.items())
        ]
        self._update_fts(rows)
        self.db.execute("DELETE FROM node_alias")
        self.db.executemany(
            "INSERT OR IGNORE INTO node_alias (alias, node_id) VALUES (?, ?)",
            sorted((alias, nid) for nid, n in graph.nodes.items() for alias in n.get("aliases", [])),
        )
        return sha256(canonical_json(rows))

    def _update_fts(self, rows: list[tuple]) -> None:
        """Make the full-text tables hold ``rows`` and nothing else, a row with an audience (six columns) in
        ``node_fts_aud``: the rows whose digest ``node_fts_rows`` does not hold go, new and changed ones come in. An
        index whose rows ``node_fts_rows`` does not account for is written again whole."""
        db = self.db
        digests = {row[0]: sha256(canonical_json(row)) for row in rows}
        held = {
            nid: (rowid, d, aud)
            for nid, rowid, d, aud in db.execute("SELECT node_id, fts_rowid, digest, aud FROM node_fts_rows")
        }
        if sum(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in FTS_TABLES) != len(held):
            for table in (*FTS_TABLES, "node_fts_rows"):
                db.execute(f"DELETE FROM {table}")
            held = {}
        gone = [(nid, rowid, aud) for nid, (rowid, d, aud) in held.items() if digests.get(nid) != d]
        for aud, table in enumerate(FTS_TABLES):
            db.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(rowid,) for _, rowid, a in gone if a == aud])
        db.executemany("DELETE FROM node_fts_rows WHERE node_id = ?", [(nid,) for nid, _, _ in gone])
        fresh = [row for row in rows if held.get(row[0], (0, None))[1] != digests[row[0]]]
        for aud, table in enumerate(FTS_TABLES):
            mine = [row for row in fresh if (len(row) > 5) == bool(aud)]
            last = db.execute(f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            start = (last[0] if last else 0) + 1
            columns = "rowid, node_id, kind, name, path, body" + (", aud" if aud else "")
            marks = ", ".join("?" for _ in columns.split(", "))
            db.executemany(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})", [(start + i, *row) for i, row in enumerate(mine)]
            )
            db.executemany(
                "INSERT INTO node_fts_rows VALUES (?, ?, ?, ?)",
                [(row[0], start + i, digests[row[0]], aud) for i, row in enumerate(mine)],
            )

    def _diff(self, table: str, key_col: str | None, b: int, rows: dict[str, dict], cols: list[str]) -> None:
        """Close rows that vanished or changed, insert rows that are new or changed."""
        db = self.db
        if key_col is None:
            key_expr = " || char(31) || ".join(c for c in cols if c in ("elem_id", "deriv_id", "unit_id", "span"))
        else:
            key_expr = key_col
        live = {
            k: (rowid, c)
            for rowid, k, c in db.execute(f"SELECT rowid, {key_expr}, content FROM {table} WHERE tx_to IS NULL")
        }
        new_content = {k: _content(r) for k, r in rows.items()}
        gone = [rowid for k, (rowid, c) in live.items() if new_content.get(k) != c]
        db.executemany(f"UPDATE {table} SET tx_to = ? WHERE rowid = ?", [(b, rowid) for rowid in gone])
        placeholders = ", ".join("?" for _ in cols + ["content", "tx_from"])
        db.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}, content, tx_from) VALUES ({placeholders})",
            [
                tuple(r.get(c) for c in cols) + (new_content[k], b)
                for k, r in rows.items()
                if live.get(k, (None, None))[1] != new_content[k]
            ],
        )

    # -- reading --------------------------------------------------------------------------------

    def content_hash(self, b: int, searched: str) -> str:
        """Hash of everything alive at build b, independent of how the build got there, with the store's schema and
        ``searched``, the digest of what its full-text index holds: two builds with the same hash read the same, so
        the hub may serve one's file for the other."""
        parts: list = [SCHEMA_VERSION, searched]
        for table, order in (
            ("units", "unit_id"),
            ("nodes", "node_id"),
            ("edges", "edge_id"),
            ("derivations", "elem_id, deriv_id"),
            ("where_prov", "elem_id, unit_id, span"),
        ):
            rows = self.db.execute(f"SELECT content FROM {table} WHERE {LIVE} ORDER BY {order}", {"b": b})
            parts.append([r[0] for r in rows])
        # A build without clusters hashes as before clusters came, so its file and an older one stay interchangeable.
        clustered = []
        if self.has_clusters():
            for table, order in (
                ("clusters", "cluster_id"),
                ("cluster_members", "node_id"),
                ("cluster_links", "link_id"),
            ):
                rows = self.db.execute(f"SELECT content FROM {table} WHERE {LIVE} ORDER BY {order}", {"b": b})
                clustered.append([r[0] for r in rows])
        if any(clustered):
            parts.append(clustered)
        # A build without review items hashes as before they came, for the same reason.
        if self.has_review():
            rows = self.db.execute(f"SELECT content FROM review_items WHERE {LIVE} ORDER BY fingerprint", {"b": b})
            reviewed = [r[0] for r in rows]
            if reviewed:
                parts.append({"review": reviewed})
        return sha256(canonical_json(parts))

    # -- history -------------------------------------------------------------------------------

    def history_floor(self, cutoff: str, keep: int = KEEP_BUILDS) -> int | None:
        """The oldest build retention keeps (``evo_agents.kg.retention``): of the latest ``keep`` ready builds and
        every ready build finished at ``cutoff`` or later, the first. None when the store has no ready build."""
        latest = self.db.execute(
            "SELECT build_id FROM builds WHERE status = 'ready' ORDER BY build_id DESC LIMIT ?", (keep,)
        ).fetchall()
        if not latest:
            return None
        recent = self.db.execute(
            "SELECT min(build_id) FROM builds WHERE status = 'ready' AND finished_at >= ?", (cutoff,)
        ).fetchone()[0]
        return min(row[0] for row in latest) if recent is None else min(recent, *(row[0] for row in latest))

    def history(self, floor: int) -> dict[str, int]:
        """What no build from ``floor`` on reads: per table, the rows closed at ``floor`` or before, and the builds
        before ``floor``."""
        out = {
            table: self.db.execute(
                f"SELECT count(*) FROM {table} WHERE tx_to IS NOT NULL AND tx_to <= ?", (floor,)
            ).fetchone()[0]
            for table in self._history_tables()
        }
        out["builds"] = self.db.execute("SELECT count(*) FROM builds WHERE build_id < ?", (floor,)).fetchone()[0]
        return out

    def drop_history(self, floor: int) -> dict[str, int]:
        """Delete what ``history(floor)`` counts, in one transaction; what went. Every row a build from ``floor`` on
        reads stays, so its content hash and the next build's diff are those they were."""
        with self.db:
            out = {
                table: self.db.execute(f"DELETE FROM {table} WHERE tx_to IS NOT NULL AND tx_to <= ?", (floor,)).rowcount
                for table in self._history_tables()
            }
            out["builds"] = self.db.execute("DELETE FROM builds WHERE build_id < ?", (floor,)).rowcount
            self.db.execute("DELETE FROM cluster_warm WHERE build_id < ?", (floor,))  # what only those builds read
        return out

    def _history_tables(self) -> tuple[str, ...]:
        absent = (() if self.has_clusters() else CLUSTER_TABLES) + (() if self.has_review() else REVIEW_TABLES)
        return tuple(t for t in HISTORY_TABLES if t not in absent)

    def counts(self, b: int) -> dict:
        out = {}
        for table in ("nodes", "edges", "units", "derivations", "where_prov"):
            out[table] = self.db.execute(f"SELECT count(*) FROM {table} WHERE {LIVE}", {"b": b}).fetchone()[0]
        return out

    def kind_counts(self, b: int) -> list[tuple[str, int, int, str, int]]:
        """How many nodes live at build b, of an audience within ``reader``, each kind has under each label: rows of
        (kind, level, location, integrity, count), in no particular order."""
        return self.db.execute(
            f"SELECT kind, label_lvl, label_loc, label_int, count(*) FROM nodes WHERE {LIVE} AND {SEEN}"
            " GROUP BY kind, label_lvl, label_loc, label_int",
            {"b": b},
        ).fetchall()

    def seen(self, b: int) -> dict:
        """What build b holds within ``reader``'s audience, whatever their level: its live nodes, edges and units;
        ``items``, per source, its items with a unit so seen; and ``audience``, the sources with a unit that has an
        audience, the reader's or not."""
        out = {
            table: self.db.execute(f"SELECT count(*) FROM {table} WHERE {LIVE} AND {SEEN}", {"b": b}).fetchone()[0]
            for table in ("nodes", "edges", "units")
        }
        sql = f"SELECT source, count(DISTINCT item_id) FROM units WHERE {LIVE} AND {SEEN} GROUP BY source"
        out["items"] = dict(self.db.execute(sql, {"b": b}).fetchall())
        sql = f"SELECT DISTINCT source FROM units WHERE {LIVE} AND label_aud IS NOT NULL"
        out["audience"] = {row[0] for row in self.db.execute(sql, {"b": b})}
        return out

    def summary(self) -> dict:
        b = self.latest_ready()
        if b is None:
            return {"ready": False}
        row = self.build_row(b)
        counts = self.counts(b)
        return {
            "ready": True,
            "build_id": b,
            "built_at": row["finished_at"],
            "nodes": counts["nodes"],
            "edges": counts["edges"],
            "units": counts["units"],
            "derivations": counts["derivations"],
            "content_hash": row["content_hash"],
            "coverage": row["coverage"],
            "stats": row["stats"],
            "file_bytes": self.path.stat().st_size,
        }

    def node(self, node_id: str, b: int) -> dict | None:
        row = self.db.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE node_id = :id AND {LIVE} AND {SEEN}",
            {"id": node_id, "b": b},
        ).fetchone()
        return _node_row(row) if row else None

    def nodes(self, ids, b: int) -> dict[str, dict]:
        out = {}
        ids = list(ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in self.db.execute(
                f"SELECT {NODE_COLUMNS} FROM nodes WHERE node_id IN ({marks})"
                f" AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?) AND {SEEN}",
                (*chunk, b, b),
            ):
                out[row[0]] = _node_row(row)
        return out

    def edges_of(self, node_id: str, b: int, direction: str = "both", limit: int = 200) -> list[dict]:
        clauses = {"out": "src = :id", "in": "dst = :id", "both": "(src = :id OR dst = :id)"}[direction]
        rows = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE {clauses} AND {LIVE} AND {SEEN} ORDER BY rel, src, dst LIMIT :lim",
            {"id": node_id, "b": b, "lim": limit},
        )
        return [_edge_row(r) for r in rows]

    @staticmethod
    def _into(node_id: str, b: int, rels, statuses, ceiling: tuple[int, int]) -> tuple[str, dict]:
        """The WHERE clause of the live edges into a node with one of ``rels`` and ``statuses`` whose label is at
        most ``ceiling`` (level, location) and whose audience is within ``reader``, and its parameters."""
        params = {"id": node_id, "b": b, "lvl": ceiling[0], "loc": ceiling[1]}
        marks = []
        for name, values in (("r", rels), ("s", statuses)):
            params.update({f"{name}{i}": v for i, v in enumerate(values)})
            marks.append(", ".join(f":{name}{i}" for i in range(len(values))))
        where = (
            f"dst = :id AND {LIVE} AND rel IN ({marks[0]}) AND status IN ({marks[1]})"
            f" AND label_lvl <= :lvl AND label_loc <= :loc AND {SEEN}"
        )
        return where, params

    def edges_into(
        self, node_id: str, b: int, rels, statuses, ceiling: tuple[int, int], *, limit: int, offset: int = 0
    ) -> list[dict]:
        """One page of the edges ``count_into`` counts, in the order of ``edges_of``."""
        where, params = self._into(node_id, b, rels, statuses, ceiling)
        rows = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE {where} ORDER BY rel, src, dst LIMIT :lim OFFSET :off",
            {**params, "lim": limit, "off": offset},
        )
        return [_edge_row(r) for r in rows]

    def count_into(self, node_id: str, b: int, rels, statuses, ceiling: tuple[int, int]) -> int:
        """How many live edges lead into a node with one of ``rels`` and ``statuses``, under ``ceiling``."""
        where, params = self._into(node_id, b, rels, statuses, ceiling)
        return self.db.execute(f"SELECT count(*) FROM edges WHERE {where}", params).fetchone()[0]

    def path_nodes(self, b: int) -> list[dict]:
        """Live File and Document nodes that carry a path: what a changed path can name."""
        rows = self.db.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind IN ('File', 'Document') AND {LIVE} AND {SEEN} ORDER BY node_id",
            {"b": b},
        )
        return [n for n in map(_node_row, rows) if n["props"].get("path")]

    def file_symbols(self, file_ids, b: int) -> dict[str, list[dict]]:
        """Live Symbol nodes grouped by the file item that defines them (``props.file``)."""
        out: dict[str, list[dict]] = defaultdict(list)
        ids = list(file_ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in self.db.execute(
                f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind = 'Symbol' AND json_extract(props, '$.file') IN ({marks})"
                f" AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?) AND {SEEN} ORDER BY node_id",
                (*chunk, b, b),
            ):
                n = _node_row(row)
                out[n["props"]["file"]].append(n)
        return dict(out)

    def degree(self, node_id: str, b: int) -> int:
        return self.db.execute(
            f"SELECT count(*) FROM edges WHERE (src = :id OR dst = :id) AND {LIVE} AND {SEEN}", {"id": node_id, "b": b}
        ).fetchone()[0]

    def degrees(self, ids, b: int, ceiling: tuple[int, int]) -> dict[str, int]:
        """How many live edges touch each of ``ids`` whose label is at most ``ceiling`` (level, location) and whose
        audience is within ``reader``; an edge from a node to itself counts twice. Ids without one are left out."""
        out: dict[str, int] = defaultdict(int)
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            marks = ", ".join("?" for _ in chunk)
            where = (
                "tx_from <= ? AND (tx_to IS NULL OR tx_to > ?) AND label_lvl <= ? AND label_loc <= ?"
                " AND (label_aud IS NULL OR aud_seen(label_aud))"
            )
            params = (b, b, ceiling[0], ceiling[1])
            for column in ("src", "dst"):
                for node_id, count in self.db.execute(
                    f"SELECT {column}, count(*) FROM edges WHERE {column} IN ({marks}) AND {where} GROUP BY {column}",
                    (*chunk, *params),
                ):
                    out[node_id] += count
        return dict(out)

    def shape_counts(self, b: int) -> list[tuple]:
        """The live edges of build b whose edge and both nodes have an audience within ``reader``, counted by the
        kind of their source, their type, the kind of their target and the three labels: rows of (source kind, rel,
        target kind, edge label, source label, target label, count), each label (level, location, integrity), in no
        particular order. An edge whose node is not live at b is left out."""

        def live(t: str) -> str:
            return f"{t}.tx_from <= :b AND ({t}.tx_to IS NULL OR {t}.tx_to > :b)"

        def seen(t: str) -> str:
            return f"({t}.label_aud IS NULL OR aud_seen({t}.label_aud))"

        def label(t: str) -> str:
            return f"{t}.label_lvl, {t}.label_loc, {t}.label_int"

        rows = self.db.execute(
            f"SELECT s.kind, e.rel, d.kind, {label('e')}, {label('s')}, {label('d')}, count(*) FROM edges e"
            f" JOIN nodes s ON s.node_id = e.src AND {live('s')} AND {seen('s')}"
            f" JOIN nodes d ON d.node_id = e.dst AND {live('d')} AND {seen('d')}"
            f" WHERE {live('e')} AND {seen('e')}"
            f" GROUP BY s.kind, e.rel, d.kind, {label('e')}, {label('s')}, {label('d')}",
            {"b": b},
        )
        return [(r[0], r[1], r[2], tuple(r[3:6]), tuple(r[6:9]), tuple(r[9:12]), r[12]) for r in rows]

    def cluster_nodes(self, b: int) -> list[dict]:
        """The live members of build b's clusters whose node is live and has an audience within ``reader``: each with
        its node's id, kind, name, path, label (level, location, integrity, audience), level-1 cluster and level-2
        cluster (None outside one), by id. Empty for a file without the cluster tables."""
        if not self.has_clusters():
            return []
        rows = self.db.execute(
            "SELECT m.node_id, n.kind, n.name, json_extract(n.props, '$.path'), m.cluster_id, m.sub_id,"
            " n.label_lvl, n.label_loc, n.label_int, n.label_aud FROM cluster_members m"
            f" JOIN nodes n ON n.node_id = m.node_id AND {_live('n')} AND {_seen('n')}"
            f" WHERE {_live('m')} ORDER BY m.node_id",
            {"b": b},
        )
        return [
            {
                "id": r[0],
                "kind": r[1],
                "name": r[2],
                "path": r[3],
                "cluster": r[4],
                "sub": r[5],
                "label": (r[6], r[7], r[8], stored_audience(r[9])),
            }
            for r in rows
        ]

    def member_links(self, b: int, skip_rels) -> list[tuple]:
        """The live edges of build b between two members of its clusters, as clusters project them
        (``evo_agents.kg.clusters``): a Symbol counts as the file of its ``props.file``, a Section as the item of its
        ``props.item``. Edges of ``skip_rels``, edges whose node is not live or has an audience beyond ``reader``, and
        edges inside one member are left out. Rows of (source member, target member, edge label, source label, target
        label, count), each label (level, location, integrity, audience), in no particular order."""
        if not self.has_clusters():
            return []
        skip = sorted(skip_rels)
        marks = ", ".join(f":r{i}" for i in range(len(skip))) or "''"

        def label(t: str) -> str:
            return f"{t}.label_lvl, {t}.label_loc, {t}.label_int, {t}.label_aud"

        members = f"(SELECT node_id FROM cluster_members m WHERE {_live('m')})"
        rows = self.db.execute(
            f"SELECT {_owner('s')} AS a, {_owner('d')} AS z, {label('e')}, {label('s')}, {label('d')}, count(*)"
            " FROM edges e"
            f" JOIN nodes s ON s.node_id = e.src AND {_live('s')} AND {_seen('s')}"
            f" JOIN nodes d ON d.node_id = e.dst AND {_live('d')} AND {_seen('d')}"
            f" WHERE {_live('e')} AND {_seen('e')} AND e.rel NOT IN ({marks})"
            f" AND a IN {members} AND z IN {members} AND a <> z"
            f" GROUP BY a, z, {label('e')}, {label('s')}, {label('d')}",
            {"b": b, **{f"r{i}": rel for i, rel in enumerate(skip)}},
        )

        def as_label(r, i: int) -> tuple:
            return (r[i], r[i + 1], r[i + 2], stored_audience(r[i + 3]))

        return [(r[0], r[1], as_label(r, 2), as_label(r, 6), as_label(r, 10), r[14]) for r in rows]

    def live_graph(self, b: int) -> tuple[list[dict], list[tuple]]:
        """Build b's live nodes with an audience within ``reader``, by id, each with its id, kind, name, owner (the
        node clusters count it as: a Symbol's ``props.file``, a Section's ``props.item``, else itself) and label
        (level, location, integrity, audience); and its live edges with an audience within ``reader``, as rows of
        (source, type, target, label)."""
        nodes = [
            {"id": r[0], "kind": r[1], "name": r[2], "owner": r[3], "label": (r[4], r[5], r[6], stored_audience(r[7]))}
            for r in self.db.execute(
                f"SELECT node_id, kind, name, {_owner('nodes')}, label_lvl, label_loc, label_int, label_aud FROM nodes"
                f" WHERE {LIVE} AND {SEEN} ORDER BY node_id",
                {"b": b},
            )
        ]
        edges = [
            (r[0], r[1], r[2], (r[3], r[4], r[5], stored_audience(r[6])))
            for r in self.db.execute(
                f"SELECT src, rel, dst, label_lvl, label_loc, label_int, label_aud FROM edges WHERE {LIVE} AND {SEEN}"
                " ORDER BY src, rel, dst",
                {"b": b},
            )
        ]
        return nodes, edges

    def search(
        self, query: str, b: int, limit: int = 50, *, kinds: list[str] | None = None, any_word: bool = True
    ) -> list[str]:
        """Ids of the nodes matching a query, best first, at most ``limit * 3``: a node whose id, alias or name is the
        query, then the full-text matches of all its words, of all its words as prefixes, and (``any_word``) of any
        of its words, ranked together by BM25 times the prior and the weight of the way that scores them best
        (``evo_agents.kg.search``). ``kinds`` keeps only nodes of those kinds."""
        want = limit * 3
        kinds = sorted({k for k in kinds or [] if isinstance(k, str)})
        hits = self.exact(query, b, want, kinds)
        queries = list(text_search.fts_queries(text_search.query_words(query)))
        scores: dict[str, float] = {}
        for weight, fts_query in zip(text_search.STAGE_WEIGHTS, queries if any_word else queries[:2], strict=False):
            if len(hits) + len(scores.keys() - set(hits)) >= want:
                break
            for node_id, score in self._ranked(fts_query, want, kinds):
                scores[node_id] = min(score * weight, scores.get(node_id, math.inf))  # bm25 is negative
        ranked = sorted(scores, key=lambda node_id: (scores[node_id], node_id))
        return [*hits, *(h for h in ranked if h not in hits)][:want]

    def exact(self, query: str, b: int, limit: int, kinds: list[str] = ()) -> list[str]:
        """Live nodes whose id, alias or name is ``query``, by id; each through an index, never a scan of nodes."""
        params = {"q": query, "b": b, "lim": limit, **{f"k{i}": k for i, k in enumerate(kinds)}}
        of_kind = f" AND nodes.kind IN ({', '.join(f':k{i}' for i in range(len(kinds)))})" if kinds else ""
        live = "nodes.tx_from <= :b AND (nodes.tx_to IS NULL OR nodes.tx_to > :b)" + of_kind
        live += " AND (nodes.label_aud IS NULL OR aud_seen(nodes.label_aud))"
        rows = self.db.execute(
            f"SELECT node_id FROM nodes WHERE node_id = :q AND {live}"
            f" UNION SELECT node_id FROM nodes WHERE name = :q AND {live}"
            " UNION SELECT nodes.node_id FROM node_alias JOIN nodes ON nodes.node_id = node_alias.node_id"
            f" WHERE node_alias.alias = :q AND {live} ORDER BY 1 LIMIT :lim",
            params,
        )
        return [r[0] for r in rows]

    def alias_of(self, alias: str, b: int) -> str | None:
        """The live node that holds ``alias`` (an id merged into it), or None."""
        row = self.db.execute(
            "SELECT nodes.node_id FROM node_alias JOIN nodes ON nodes.node_id = node_alias.node_id"
            " WHERE node_alias.alias = :a AND nodes.tx_from <= :b AND (nodes.tx_to IS NULL OR nodes.tx_to > :b)"
            " AND (nodes.label_aud IS NULL OR aud_seen(nodes.label_aud))",
            {"a": alias, "b": b},
        ).fetchone()
        return row[0] if row else None

    def _ranked(self, fts_query: str, limit: int, kinds: list[str]) -> list[tuple[str, float]]:
        """(node id, score) of the nodes matching an FTS5 query, best first: the best ``RERANK_POOL`` by BM25, scored
        again with the prior by kind (``evo_agents.kg.search``); [] for a query FTS5 cannot read. The rows of ``node_fts_aud`` are read only
        for a reader with an audience, and only those within it, each with the score ``node_fts`` would give it
        (``_as_public``)."""
        of_kind = f" AND kind IN ({', '.join('?' for _ in kinds)})" if kinds else ""
        weights = ", ".join(str(w) for w in text_search.WEIGHTS)
        rows = []
        for table in FTS_TABLES if self.reader else FTS_TABLES[:1]:
            seen = " AND aud_seen(aud)" if table == "node_fts_aud" else ""
            try:
                found = self.db.execute(
                    f"SELECT node_id, kind, bm25({table}, {weights}) AS score, rowid FROM {table} WHERE {table} MATCH ?"
                    f"{of_kind}{seen} ORDER BY score, node_id LIMIT ?",
                    (fts_query, *kinds, max(limit, text_search.RERANK_POOL)),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            rows += self._as_public(fts_query, found) if table == "node_fts_aud" else found
        prior = text_search.PRIOR
        scored = sorted(((r[0], r[2] * prior.get(r[1], 1.0)) for r in rows), key=lambda r: (r[1], r[0]))
        return scored[:limit]  # bm25 is negative: lower is better

    def _as_public(self, fts_query: str, rows: list[tuple]) -> list[tuple]:
        """``rows`` of ``node_fts_aud`` (node id, kind, BM25, rowid) with the BM25 ``node_fts`` would give them. BM25
        adds up, over the phrases of the query, the phrase's IDF times a part that only the row's own counts and
        length make. ``node_fts_aud`` holds few rows (a project's chat messages), so the IDF it gives a word is a
        fraction of what ``node_fts`` gives it, and a message would rank below any symbol or section that matches no
        better. Each phrase's part is read back from the BM25 of the phrase alone and weighed with the phrase's IDF
        in ``node_fts``, so no row of an audience the reader lacks weighs in it through the IDF either."""
        if not rows:
            return rows
        weights = ", ".join(str(w) for w in text_search.WEIGHTS)
        marks = ", ".join("?" for _ in rows)
        held = dict(self.db.execute("SELECT aud, count(*) FROM node_fts_rows GROUP BY aud").fetchall())
        totals = {table: held.get(aud, 0) for aud, table in enumerate(FTS_TABLES)}  # its rows, as _update_fts keeps
        scores = dict.fromkeys((r[3] for r in rows), 0.0)
        for phrase in text_search.phrases(fts_query):
            weight = self._idf("node_fts", phrase, totals) / self._idf("node_fts_aud", phrase, totals)
            for rowid, part in self.db.execute(
                f"SELECT rowid, bm25(node_fts_aud, {weights}) FROM node_fts_aud WHERE node_fts_aud MATCH ?"
                f" AND rowid IN ({marks})",
                (phrase, *scores),
            ):
                scores[rowid] += part * weight
        return [(r[0], r[1], scores[r[3]], r[3]) for r in rows]

    def _idf(self, table: str, phrase: str, totals: dict[str, int]) -> float:
        """The IDF FTS5's bm25 gives ``phrase`` in ``table``: ln((N - n + 0.5) / (n + 0.5)) for its N rows, n of which
        hold the phrase, or 1e-6 when that is not above 0."""
        held = self.db.execute(f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (phrase,)).fetchone()[0]
        idf = math.log((totals[table] - held + 0.5) / (held + 0.5))
        return idf if idf > 0 else 1e-6

    def matched(self, ids: list[str], query: str, snippets: set[str] = frozenset()) -> dict[str, dict]:
        """What the full-text index says of each node of ``ids`` against ``query``: ``words``, how many of the
        query's words (``search.query_words``) its name, path or body holds whole, of ``of``; and for a node of
        ``snippets``, ``snippet``: the passage of its body where those words gather (FTS5's ``snippet``, about
        ``search.SNIPPET_TOKENS`` words, white space run together). A node the index holds no row for is left out.
        The rows of ``node_fts_aud`` are read only for a reader with an audience, and only those within it, as
        ``search`` reads them."""
        words = text_search.query_words(query)
        if not ids or not words:
            return {}
        whole = [next(text_search.fts_queries([w])) for w in words]  # each word as the all-words way matches it
        marks = ", ".join("?" for _ in ids)
        rows = self.db.execute(f"SELECT node_id, fts_rowid, aud FROM node_fts_rows WHERE node_id IN ({marks})", ids)
        by_table: dict[int, dict[int, str]] = defaultdict(dict)
        for node_id, rowid, aud in rows:
            by_table[aud][rowid] = node_id
        out: dict[str, dict] = {}
        for aud, table in enumerate(FTS_TABLES):
            of_table = by_table.get(aud)
            if not of_table or (aud and not self.reader):
                continue
            seen = " AND aud_seen(aud)" if aud else ""
            within = f"rowid IN ({', '.join('?' for _ in of_table)}){seen}"
            held: dict[int, int] = dict.fromkeys(of_table, 0)
            try:
                for phrase in whole:
                    for (rowid,) in self.db.execute(
                        f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND {within}", (phrase, *of_table)
                    ):
                        held[rowid] += 1
                wanted = [rowid for rowid, node_id in of_table.items() if node_id in snippets]
                passages = {}
                if wanted:
                    passages = dict(
                        self.db.execute(
                            f"SELECT rowid, snippet({table}, 4, '', '', '…', {text_search.SNIPPET_TOKENS}) FROM {table}"
                            f" WHERE {table} MATCH ? AND rowid IN ({', '.join('?' for _ in wanted)}){seen}",
                            (" OR ".join(whole), *wanted),
                        )
                    )
            except sqlite3.OperationalError:
                continue
            for rowid, node_id in of_table.items():
                out[node_id] = {"words": held[rowid], "of": len(words)}
                passage = " ".join(str(passages.get(rowid) or "").split())
                if passage:
                    out[node_id]["snippet"] = passage
        return out

    def derivations(self, elem_id: str, b: int) -> list[dict]:
        rows = self.db.execute(
            f"SELECT units, needs FROM derivations WHERE elem_id = :e AND {LIVE} ORDER BY deriv_id",
            {"e": elem_id, "b": b},
        )
        return [{"units": json.loads(r[0]), "needs": json.loads(r[1])} for r in rows]

    def where(self, elem_id: str, b: int) -> list[dict]:
        rows = self.db.execute(
            f"SELECT unit_id, span FROM where_prov WHERE elem_id = :e AND {LIVE} ORDER BY unit_id, span",
            {"e": elem_id, "b": b},
        )
        return [{"unit": r[0], "span": r[1]} for r in rows]

    def unit(self, unit_id: str, b: int) -> dict | None:
        row = self.db.execute(
            f"SELECT unit_id, item_id, anchor, rev, hash, source, span FROM units WHERE unit_id = :u AND {LIVE}"
            f" AND {SEEN}",
            {"u": unit_id, "b": b},
        ).fetchone()
        keys = ("unit", "item", "anchor", "rev", "hash", "source", "span")
        return dict(zip(keys, row, strict=True)) if row else None

    def alive_ids(self, table: str, b: int) -> set[str]:
        col = {"nodes": "node_id", "edges": "edge_id", "units": "unit_id"}[table]
        return {r[0] for r in self.db.execute(f"SELECT {col} FROM {table} WHERE {LIVE}", {"b": b})}

    def predict_retraction(self, removed_units: set[str], b: int) -> set[str]:
        """Evaluate the provenance circuit with some units set to false: which elements would die."""
        derivs: dict[str, list[tuple[list, list]]] = {}
        for elem, units, needs in self.db.execute(
            f"SELECT elem_id, units, needs FROM derivations WHERE {LIVE}", {"b": b}
        ):
            derivs.setdefault(elem, []).append((json.loads(units), json.loads(needs)))
        alive = set(derivs)
        changed = True
        while changed:
            changed = False
            for elem in list(alive):
                if not any(
                    not (set(u) & removed_units) and all(n in alive for n in needs) for u, needs in derivs[elem]
                ):
                    alive.discard(elem)
                    changed = True
        return set(derivs) - alive


def _size(graph: Graph) -> dict:
    return {"nodes": len(graph.nodes), "edges": len(graph.edges), "units": len(graph.units)}


def _top_label(graph: Graph) -> list[int]:
    """The highest level and the highest location among the labels of ``graph``'s nodes, edges and units."""
    pairs = [(e["label"].level, e["label"].location) for e in (*graph.nodes.values(), *graph.edges.values())]
    pairs += [(u["label_lvl"], u["label_loc"]) for u in graph.units.values()]
    return [max((p[0] for p in pairs), default=0), max((p[1] for p in pairs), default=0)]


def _top_audience(graph: Graph) -> dict:
    """``top_audience``, the join of the audiences of ``graph``'s nodes, edges and units, when one of them has one."""
    found = frozenset()
    for audience in [e["label"].audience for e in (*graph.nodes.values(), *graph.edges.values())] + [
        stored_audience(u.get("label_aud")) for u in graph.units.values()
    ]:
        found = audience_join(found, audience)
    return {"top_audience": sorted(found)} if found else {}


def _node_row(row) -> dict:
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


def _edge_row(row) -> dict:
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
