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
then by its words. ``node_tri`` holds the name and path of each node's own row of ``node_fts`` again, under the same
rowid, cut in trigrams, for words inside a word; it is outside the schema version and the content hash, being made
from ``node_fts``: a file without it (written before it, or by an SQLite without the trigram tokenizer) searches by
words alone, and the next build that writes the index fills it whole.

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

What agents asked before they read a node (``evo_agents.kg.asked``) sits in ``node_asked`` (the words, full-text) and
``node_asked_rows`` (each row's node, its level, location and audience, and its prior), which ``write_asked`` makes
when a hub build has labels to learn from: they are outside the schema version and the content hash, and a file
without them (any local build, any file written before them) searches as before. ``Store.search`` reads them only for
a reader whose ``asked_clearance`` (level, location) is set, and only the rows within it and within ``reader``.

``Store`` keeps the opening of a file, its builds, its history and the reads of nodes, edges and units; its other
methods come from the modules of its seams: ``store_schema`` (the tables), ``store_filters`` (what a read leaves out:
rows not live, or of an audience beyond ``reader``), ``store_diff`` (the bitemporal diff of a build), ``store_fts``
(the full-text tables), ``store_asked`` (the asked index), ``store_rank`` (search) and ``store_clusters`` (clusters and
the review queue).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg import settings as kg_settings
from evo_agents.kg.ids import utc_now
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.store_clusters import Clusters, ClusterTables
from evo_agents.kg.store_diff import diff_graph, graph_stats
from evo_agents.kg.store_filters import CEILING, LIVE, SEEN, live, read_as, seen, stored_audience
from evo_agents.kg.store_rank import Ranking
from evo_agents.kg.store_schema import (
    CLUSTER_TABLES,
    EDGE_COLUMNS,
    KEYED_KINDS,
    NODE_COLUMNS,
    REVIEW_TABLES,
    SCHEMA,
    TRIGRAM_SCHEMA,
    schema_version,
)

SCHEMA_VERSION = "4"  # 4: units, nodes and edges gained label_aud, node_fts_aud came; 3: node_fts kind and path

_NO_WARM = object()  # Store.write: no warm start to record
# the tables whose rows carry tx_from, tx_to
HISTORY_TABLES = ("units", "nodes", "edges", "derivations", "where_prov", *CLUSTER_TABLES, *REVIEW_TABLES)
# Builds retention always keeps: the latest, and the one before it, which a reader that read the latest build id just
# before a build landed may still be reading.
KEEP_BUILDS = 2


def edge_id(src: str, rel: str, dst: str) -> str:
    return "e:" + sha256(f"{src}\x1f{rel}\x1f{dst}")[7:23]


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
    # node id -> the passages its long body is also indexed as (``kg.search``): [{"start", "end", "text"}], text with
    # its context first; never nodes
    passages: dict[str, list[dict]] = field(default_factory=dict)
    # file node id -> the text of its code or markup, where kg_search looks up the words of a query (``code_fts``)
    code: dict[str, str] = field(default_factory=dict)
    clusters: Clusters | None = None  # None: not computed (``evo_agents.kg.clusters``)
    mentions: dict[str, dict] = field(default_factory=dict)  # item id -> its dangling and ambiguous mentions, samples
    review: list[dict] = field(default_factory=list)  # the review items of a build with clusters (``kg.review``)
    # The deprecations a curation item declared (``evo_agents.kg.pipeline.curation``): applied to the props of the
    # nodes and edges they name before the graph is written, never written themselves.
    deprecations: list[dict] = field(default_factory=list)


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


def _tuning_of(project) -> kg_settings.Tuning:
    """What the project's settings.json tunes search by; a file that cannot be read leaves the defaults (``kg settings``
    reports it), so search never fails on it."""
    asked = kg_settings.override_of()
    if asked is not None:
        return asked
    try:
        return kg_settings.read(project.root)
    except kg_settings.SettingsError:
        return kg_settings.DEFAULT


class Store(Ranking, ClusterTables):
    """The graph store in one SQLite file: its builds, the bitemporal rows they hold and their reads."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        found = schema_version(self.db)
        if found is not None and found != SCHEMA_VERSION:
            self.db.close()
            raise StaleSchema(path, found)  # before SCHEMA runs: an old file is never half-migrated
        self._reads()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.executescript(SCHEMA)
        with contextlib.suppress(sqlite3.OperationalError):  # no trigram tokenizer: search reads words alone
            self.db.execute(TRIGRAM_SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.db.commit()

    @classmethod
    def for_project(cls, project) -> Store:
        return cls(project.root / "graph.sqlite")

    @classmethod
    def open_existing(cls, project) -> Store | None:
        """The project's store for reading; raises StaleSchema on a file from an older schema."""
        path = project.root / "graph.sqlite"
        if not path.exists():
            return None
        store = cls(path)
        store.tuning = _tuning_of(project)
        return store

    @classmethod
    def open_readonly(cls, project) -> Store | None:
        """The project's store for a reader that must stay fast, such as a hook: it never writes, so it never
        waits on a build holding the write lock. None when there is no store; StaleSchema on an older file."""
        path = project.root / "graph.sqlite"
        if not path.exists():
            return None
        store = cls.open_file(path)
        store.tuning = _tuning_of(project)
        return store

    @classmethod
    def open_file(cls, path: Path, *, immutable: bool = False) -> Store:
        """The store in the file at ``path``, read-only; StaleSchema on an older file. ``immutable`` is for a file
        nothing ever writes again, such as a graph the hub built: SQLite then takes no lock and makes no -shm file."""
        store = cls.__new__(cls)
        store.path = path
        mode = "mode=ro&immutable=1" if immutable else "mode=ro"
        store.db = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
        found = schema_version(store.db)
        if found != SCHEMA_VERSION:
            store.db.close()
            raise StaleSchema(path, found or "none")
        store._reads()
        return store

    def _reads(self) -> None:
        """Read as a reader with no audience until ``reader`` is set: rows with one are left out (fail closed)."""
        self.reader: frozenset[str] = frozenset()
        self.asked_clearance: tuple[int, int] | None = None  # the (level, location) of node_asked rows read; None: none
        self.ceiling: tuple[int, int] | None = None  # the (level, location) edges_of reads up to; None: every label
        self._asked: bool | None = None  # whether the file has the asked tables, once looked up
        self.tuning = kg_settings.DEFAULT  # what the project is tuned by at query time (``evo_agents.kg.settings``)
        self.asked_weight: float | None = None  # the weight of the asked rows to use in place of the file's (an eval)
        self._trigram: bool | None = None  # whether the file has a filled node_tri, once looked up
        self._asked_weight: tuple[float | None] | None = None  # (the weight the file records or None), once read
        read_as(self.db, self)

    @staticmethod
    def stored_schema(path: Path) -> str | None:
        """The schema version a store file was written with, without migrating it; None if there is none."""
        if not path.exists():
            return None
        db = sqlite3.connect(path)
        try:
            return schema_version(db)
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
            assert b is not None, "an INSERT sets lastrowid"
            diff_graph(db, b, graph)
            self._write_clusters(b, graph.clusters or Clusters())
            self._write_review(b, graph.review)
            searched = self._write_search(graph)
            content_hash = self.content_hash(b, searched)
            stats = {**stats, **graph_stats(graph)}
            if warm is not _NO_WARM:
                assert warm is None or isinstance(warm, Clusters), "warm is the clusters a clustering started from"
                stats["warm"] = self._write_warm(b, warm)
            db.execute(
                "UPDATE builds SET status = 'ready', finished_at = ?, content_hash = ?, coverage = ?, stats = ?"
                " WHERE build_id = ?",
                (utc_now(), content_hash, json.dumps(coverage, ensure_ascii=False), json.dumps(stats), b),
            )
        return b

    def labels(self, ids, b: int) -> dict[str, tuple]:
        """The label of each node of ``ids`` live at build b with an audience within ``reader``."""
        return {nid: n["label"] for nid, n in self.nodes(ids, b).items()}

    def edge_label(self, edge_id: str, b: int) -> tuple | None:
        """The label of edge ``edge_id`` live at build b with an audience within ``reader``; None without one."""
        row = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE edge_id = :id AND {LIVE} AND {SEEN}", {"id": edge_id, "b": b}
        ).fetchone()
        return _edge_row(row)["label"] if row else None

    def edge(self, edge_id: str, b: int) -> dict | None:
        """Edge ``edge_id`` live at build b with an audience within ``reader``; None without one."""
        row = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE edge_id = :id AND {LIVE} AND {SEEN}", {"id": edge_id, "b": b}
        ).fetchone()
        return _edge_row(row) if row else None

    def entity_graph(self, b: int, prefix: str = "entity:") -> tuple[list[dict], list[dict]]:
        """The live nodes of build b whose id starts with ``prefix`` (the entities step's) and the live edges into one
        of them, each with an audience within ``reader``, by id: the relations between two entities and the
        ``mentions`` from a document."""
        bounds = {"b": b, "lo": prefix, "hi": prefix[:-1] + chr(ord(prefix[-1]) + 1)}
        nodes = self.db.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE node_id >= :lo AND node_id < :hi AND {LIVE} AND {SEEN}"
            " ORDER BY node_id",
            bounds,
        )
        edges = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE dst >= :lo AND dst < :hi AND {LIVE} AND {SEEN} ORDER BY edge_id",
            bounds,
        )
        return [_node_row(r) for r in nodes], [_edge_row(r) for r in edges]

    def entity_code(self, b: int, rels, prefix: str = "entity:") -> list[dict]:
        """The live edges of build b with one of ``rels`` out of a node whose id starts with ``prefix``, each with an
        audience within ``reader``, by id: an entity's links to the code that implements it or that its name names."""
        rels = list(rels)
        bounds = {"b": b, "lo": prefix, "hi": prefix[:-1] + chr(ord(prefix[-1]) + 1)}
        bounds.update({f"r{i}": r for i, r in enumerate(rels)})
        marks = ", ".join(f":r{i}" for i in range(len(rels)))
        rows = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE src >= :lo AND src < :hi AND rel IN ({marks}) AND {LIVE}"
            f" AND {SEEN} ORDER BY edge_id",
            bounds,
        )
        return [_edge_row(r) for r in rows]

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
        """The live edges of a node, within ``ceiling`` and the reader's audience, filtered before the limit."""
        clauses = {"out": "src = :id", "in": "dst = :id", "both": "(src = :id OR dst = :id)"}[direction]
        params = {"id": node_id, "b": b, "lim": limit}
        if self.ceiling is not None:
            clauses += f" AND {CEILING}"
            params.update(lvl=self.ceiling[0], loc=self.ceiling[1])
        rows = self.db.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE {clauses} AND {LIVE} AND {SEEN} ORDER BY rel, src, dst LIMIT :lim",
            params,
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
        where = f"dst = :id AND {LIVE} AND rel IN ({marks[0]}) AND status IN ({marks[1]}) AND {CEILING} AND {SEEN}"
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
            where = f"tx_from <= ? AND (tx_to IS NULL OR tx_to > ?) AND label_lvl <= ? AND label_loc <= ? AND {SEEN}"
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

    def alias_of(self, alias: str, b: int) -> str | None:
        """The live node that holds ``alias`` (an id merged into it), or None."""
        row = self.db.execute(
            "SELECT nodes.node_id FROM node_alias JOIN nodes ON nodes.node_id = node_alias.node_id"
            f" WHERE node_alias.alias = :a AND {live('nodes')} AND {seen('nodes')}",
            {"a": alias, "b": b},
        ).fetchone()
        return row[0] if row else None

    # -- what locate and plain search read of the file's live rows, whatever the reader --------------------------------

    def file_ids(self) -> list[str]:
        """The ids of the live File nodes."""
        return [r[0] for r in self.db.execute("SELECT node_id FROM nodes WHERE kind = 'File' AND tx_to IS NULL")]

    def nodes_between(self, low: str, high: str) -> list[tuple[str, dict]]:
        """(id, props) of the live nodes whose id is at least ``low`` and below ``high``, by id."""
        rows = self.db.execute(
            "SELECT node_id, props FROM nodes WHERE node_id >= ? AND node_id < ? AND tx_to IS NULL ORDER BY node_id",
            (low, high),
        )
        out = []
        for node_id, props in rows:
            with contextlib.suppress(ValueError):
                out.append((node_id, json.loads(props or "{}")))
        return out

    def changed_by(self, file_id: str, limit: int) -> list[str]:
        """At most ``limit`` commits with a live ``changes`` edge to ``file_id``."""
        sql = "SELECT src FROM edges WHERE dst = ? AND rel = 'changes' AND tx_to IS NULL LIMIT ?"
        return [r[0] for r in self.db.execute(sql, (file_id, limit))]

    def changes_of(self, commits) -> list[tuple[str, str]]:
        """(commit, file) of the live ``changes`` edges from ``commits``."""
        commits, out = list(commits), []
        for start in range(0, len(commits), 400):
            chunk = commits[start : start + 400]
            marks = ", ".join("?" for _ in chunk)
            sql = f"SELECT src, dst FROM edges WHERE src IN ({marks}) AND rel = 'changes' AND tx_to IS NULL"
            out += self.db.execute(sql, chunk).fetchall()
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
