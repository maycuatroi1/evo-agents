"""The clusters and the review queue of the graph store (``evo_agents.kg.store``): ``clusters``, ``cluster_members``,
``cluster_links`` and ``review_items``, bitemporal like the graph; ``cluster_warm``, the clusters a build's clustering
started from; and the reads clustering projects the graph by (``evo_agents.kg.clusters``)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from evo_agents.kg.store_diff import diff_rows
from evo_agents.kg.store_filters import LIVE, SEEN, live, seen, stored_audience
from evo_agents.kg.store_schema import CLUSTER_TABLES, REVIEW_TABLES, has_tables


@dataclass
class Clusters:
    """The clusters of a graph (``evo_agents.kg.clusters``): each cluster's level (1 or 2), parent (a level-2 cluster's
    level-1 cluster), label, size and members by kind; each member's level-1 cluster and level-2 cluster, if any; and
    the weight of the edges between two clusters of a level, keyed (level, a, b) with a < b."""

    clusters: dict[str, dict] = field(default_factory=dict)
    members: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    links: dict[tuple[int, str, str], float] = field(default_factory=dict)


def owner(t: str) -> str:
    """The node clusters count the node of table alias ``t`` as (``evo_agents.kg.clusters.project``): a Symbol's
    ``props.file``, a Section's ``props.item``, any other node itself."""
    return (
        f"CASE {t}.kind WHEN 'Symbol' THEN json_extract({t}.props, '$.file')"
        f" WHEN 'Section' THEN json_extract({t}.props, '$.item') ELSE {t}.node_id END"
    )


class ClusterTables:
    """The methods of ``Store`` that write and read the cluster and review tables."""

    db: sqlite3.Connection

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
        diff_rows(
            self.db,
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
        diff_rows(
            self.db,
            "cluster_members",
            "node_id",
            b,
            {nid: {"node_id": nid, "cluster_id": top, "sub_id": sub} for nid, (top, sub) in found.members.items()},
            ["node_id", "cluster_id", "sub_id"],
        )
        diff_rows(
            self.db,
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
        diff_rows(
            self.db,
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
        return has_tables(self.db, REVIEW_TABLES)

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

    def has_clusters(self) -> bool:
        """Whether the file has the cluster tables: one written before clusters came lacks them."""
        return has_tables(self.db, CLUSTER_TABLES)

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

    def cluster_nodes(self, b: int) -> list[dict]:
        """The live members of build b's clusters whose node is live and has an audience within ``reader``: each with
        its node's id, kind, name, path, label (level, location, integrity, audience), level-1 cluster and level-2
        cluster (None outside one), by id. Empty for a file without the cluster tables."""
        if not self.has_clusters():
            return []
        rows = self.db.execute(
            "SELECT m.node_id, n.kind, n.name, json_extract(n.props, '$.path'), m.cluster_id, m.sub_id,"
            " n.label_lvl, n.label_loc, n.label_int, n.label_aud FROM cluster_members m"
            f" JOIN nodes n ON n.node_id = m.node_id AND {live('n')} AND {seen('n')}"
            f" WHERE {live('m')} ORDER BY m.node_id",
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

        members = f"(SELECT node_id FROM cluster_members m WHERE {live('m')})"
        rows = self.db.execute(
            f"SELECT {owner('s')} AS a, {owner('d')} AS z, {label('e')}, {label('s')}, {label('d')}, count(*)"
            " FROM edges e"
            f" JOIN nodes s ON s.node_id = e.src AND {live('s')} AND {seen('s')}"
            f" JOIN nodes d ON d.node_id = e.dst AND {live('d')} AND {seen('d')}"
            f" WHERE {live('e')} AND {seen('e')} AND e.rel NOT IN ({marks})"
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
                f"SELECT node_id, kind, name, {owner('nodes')}, label_lvl, label_loc, label_int, label_aud FROM nodes"
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
