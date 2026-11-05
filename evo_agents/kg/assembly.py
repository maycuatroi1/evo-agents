"""The assembly stage of a build (``kg.build.run_pipeline``): the facts of every item merged into one graph.

Facts merge by id, the strongest status winning; entity resolution applies only declared identities (``same_as``),
the smallest id representing its class; an edge lives only while every element it names does; a node's or an edge's
label is the meet over its derivations of the join over what each uses; and a key two nodes would share fails the
build.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from evo_agents.kg.pipeline import curation
from evo_agents.kg.policy import Label, any_of, audience_below, join_all
from evo_agents.kg.store import Graph, edge_id, key_conflicts

STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "extracted": 1.5, "proposed": 1}
UNSEARCHED_FIELDS = ("title", "verify", "evidence")  # the name already; commands and run output
STAT_KEYS = (
    "mentions",
    "resolved",
    "ambiguous",
    "dangling",
    "refs",
    "refs_resolved",
    "refs_ambiguous",
    "refs_dangling",
    "calls",
    "calls_resolved",
    "calls_ambiguous",
    "calls_unresolved",
)


def _source_stats() -> dict:
    return {"items": 0, "issues": 0, **dict.fromkeys(STAT_KEYS, 0)}


def _source_samples() -> dict[str, list]:
    return {"ambiguous": [], "dangling": [], "calls_unresolved": []}


@dataclass
class Facts:
    """The facts of every item, gathered by id before entity resolution; ``kinds`` the node kinds a build accepts
    (None: every kind)."""

    kinds: set[str] | None
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[tuple, dict] = field(default_factory=dict)
    derivs: dict = field(default_factory=lambda: defaultdict(list))  # node id or edge key -> its derivations
    props_of: dict[str, list[tuple[dict, dict]]] = field(default_factory=lambda: defaultdict(list))  # (deriv, props)
    where: dict = field(default_factory=lambda: defaultdict(set))
    same: list[dict] = field(default_factory=list)  # identities, and the deprecations a curation item declared
    notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # descriptions, docstrings
    errors: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    stats: dict[str, dict] = field(default_factory=lambda: defaultdict(_source_stats))
    samples: dict[str, dict[str, list]] = field(default_factory=lambda: defaultdict(_source_samples))
    rejected: set[tuple[str, str]] = field(default_factory=set)
    item_mentions: dict[str, dict] = field(default_factory=dict)  # item id -> its dangling and ambiguous mentions

    def add_item(self, rec, facts: Iterable[dict]) -> None:
        self.stats[rec.source]["items"] += 1
        for f in facts:
            self.add(rec, f)

    def add(self, rec, f: dict) -> None:
        t = f["t"]
        if t == "node" and self.kinds is not None and f["kind"] not in self.kinds:
            self._reject(rec.item_id, f)
        elif t == "node":
            self._node(f)
        elif t == "edge":
            self._edge(f)
        elif t in curation.CURATION_FACTS:
            self.same.append(f)
        elif t in ("text", "doc") and f.get("field") not in UNSEARCHED_FIELDS:
            self.notes[f["src"]].append(f["text"])
        elif t == "error":
            self.errors.append(f["message"])
        elif t == "issue":
            self.issues.append(f["message"])
            self.stats[rec.source]["issues"] += 1
        elif t == "stats":
            self._stats(rec, f)

    def _reject(self, item_id: str, f: dict) -> None:
        if (item_id, f["kind"]) not in self.rejected:
            self.rejected.add((item_id, f["kind"]))
            self.errors.append(
                f"{item_id}: node {f['id']} has kind {f['kind']!r}, neither a hub kind nor in the ontology"
            )

    def _node(self, f: dict) -> None:
        if f["id"] not in self.nodes:
            self.nodes[f["id"]] = {k: f[k] for k in ("kind", "name", "props", "status", "conf")}
        else:
            _strongest(self.nodes[f["id"]], f)
        self.derivs[f["id"]].append({"units": f["units"], "needs": f["needs"]})
        self.props_of[f["id"]].append((self.derivs[f["id"]][-1], f["props"]))
        self.where[f["id"]].update(tuple(w) for w in f.get("where", []))

    def _edge(self, f: dict) -> None:
        key = (f["src"], f["rel"], f["dst"])
        if key not in self.edges:
            self.edges[key] = {k: f[k] for k in ("props", "status", "conf")}
        else:
            _strongest(self.edges[key], f)
        self.derivs[key].append({"units": f["units"], "needs": sorted(set(f["needs"]) | {f["src"], f["dst"]})})
        self.where[key].update(tuple(w) for w in f.get("where", []))

    def _stats(self, rec, f: dict) -> None:
        for k in STAT_KEYS:
            self.stats[rec.source][k] += f.get(k, 0)
        if f.get("dangling") or f.get("ambiguous"):
            found = self.item_mentions.setdefault(rec.item_id, {"dangling": 0, "ambiguous": 0, "samples": {}})
            for k in ("dangling", "ambiguous"):
                found[k] += f.get(k, 0)
                found["samples"].setdefault(k, []).extend((f.get("samples") or {}).get(k) or [])
        for k, values in (f.get("samples") or {}).items():
            bucket = self.samples[rec.source][k]
            bucket.extend(v for v in values if len(bucket) < 30 and v not in bucket)


@dataclass
class Merged:
    """The nodes and edges after entity resolution; ``rep`` maps a node id to the id of its class."""

    rep: dict[str, str]
    nodes: dict[str, dict]
    aliases: dict[str, list[str]]
    edges: dict[tuple, dict] = field(default_factory=dict)
    edge_derivs: dict[tuple, list[dict]] = field(default_factory=lambda: defaultdict(list))
    edge_where: dict[tuple, set] = field(default_factory=lambda: defaultdict(set))

    def remap(self, x: str) -> str:
        return self.rep.get(x, x)


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))  # the smallest id represents the class: order-independent
            self.parent[hi] = lo


def assemble(graph: Graph, facts: Facts, unit_labels: dict[str, Label]) -> int:
    """Fill ``graph``'s nodes, edges, derivations, places and mentions from ``facts`` and append a key conflict to
    ``facts.errors``; the number of edges dropped because an element they name is gone."""
    merged = _merge_nodes(facts, graph.deprecations)
    _merge_edges(facts, merged)
    dangling = _drop_dangling(merged)
    _write_labelled(graph, facts, merged, unit_labels)
    facts.errors.extend(_key_errors(graph))
    return dangling


def _merge_nodes(facts: Facts, deprecations) -> Merged:
    """Entity resolution: declared identities only, smallest id represents the class."""
    uf = _UnionFind()
    for a, b in curation.split(facts.same, deprecations):
        if a in facts.nodes and b in facts.nodes:
            uf.union(a, b)
    merged = Merged(rep={n: uf.find(n) for n in facts.nodes}, nodes={}, aliases=defaultdict(list))
    derivs, props_of, where = facts.derivs, facts.props_of, facts.where
    for nid in sorted(facts.nodes):
        target = merged.rep[nid]
        if target != nid:
            merged.aliases[target].append(nid)
        if target not in merged.nodes:
            merged.nodes[target] = dict(facts.nodes[target])
        if target != nid:
            derivs[target] = derivs[target] + derivs.pop(nid)
            props_of[target] += props_of.pop(nid, [])
            where[target] |= where.pop(nid, set())
    return merged


def _merge_edges(facts: Facts, merged: Merged) -> None:
    remap = merged.remap
    for key in sorted(facts.edges):
        src, rel, dst = remap(key[0]), key[1], remap(key[2])
        if src == dst:
            continue
        nkey = (src, rel, dst)
        if nkey not in merged.edges:
            merged.edges[nkey] = dict(facts.edges[key])
        else:
            _strongest(merged.edges[nkey], facts.edges[key])
        for d in facts.derivs.pop(key):
            merged.edge_derivs[nkey].append({"units": d["units"], "needs": sorted({remap(n) for n in d["needs"]})})
        merged.edge_where[nkey] |= facts.where.pop(key, set())


def _drop_dangling(merged: Merged) -> int:
    """Liveness: a node lives on its units; an edge also needs every element it names."""
    alive = set(merged.nodes)
    dangling = 0
    for key in list(merged.edges):
        ds = [d for d in merged.edge_derivs[key] if all(n in alive for n in d["needs"])]
        if not ds:
            dangling += 1
            del merged.edges[key]
            continue
        merged.edge_derivs[key] = ds
    return dangling


def _write_labelled(graph: Graph, facts: Facts, merged: Merged, unit_labels: dict[str, Label]) -> None:
    """Labels: the meet over derivations (any_of: an audience a reader sees one of whole) of the join over what each
    derivation uses."""
    derivs = facts.derivs
    node_label: dict[str, Label] = {}
    for nid in merged.nodes:
        node_label[nid] = _any_of([_deriv_label(d, unit_labels, node_label) for d in _dedupe(derivs[nid])])
    for item_id, found in facts.item_mentions.items():
        graph.mentions.setdefault(merged.remap(item_id), found)
    for nid, n in merged.nodes.items():
        # The props of the first derivation the node's label reads, so a node a public item also states shows none
        # of what a restricted item said of it.
        readable = (
            p for d, p in facts.props_of[nid] if _reads(node_label[nid], _deriv_label(d, unit_labels, node_label))
        )
        n = {**n, "props": next(readable, {})}
        graph.nodes[nid] = {**n, "label": node_label[nid], "aliases": sorted(merged.aliases.get(nid, []))}
        graph.derivations[nid] = _dedupe(derivs[nid])
        graph.where[nid] = sorted(facts.where[nid])
    for (src, rel, dst), e in merged.edges.items():
        eid = edge_id(src, rel, dst)
        ds = _dedupe(merged.edge_derivs[(src, rel, dst)])
        label = _any_of([_deriv_label(d, unit_labels, node_label) for d in ds])
        graph.edges[eid] = {"src": src, "rel": rel, "dst": dst, **e, "label": label}
        graph.derivations[eid] = ds
        graph.where[eid] = sorted(merged.edge_where[(src, rel, dst)])


def _key_errors(graph: Graph) -> list[str]:
    """Keys: a key two nodes would share fails the build, naming the nodes and the sources behind them."""
    errors = []
    for (kind, key), ids in key_conflicts(graph).items():
        holders = []
        for nid in ids:
            srcs = sorted(
                {graph.units[u]["source"] for d in graph.derivations[nid] for u in d["units"] if u in graph.units}
            )
            holders.append(f"{nid} (source {', '.join(srcs)})")
        errors.append(f"duplicate {kind} key {key}: " + ", ".join(holders))
    return errors


def _strongest(current: dict, new: dict) -> None:
    if STATUS_RANK.get(new["status"], 0) > STATUS_RANK.get(current["status"], 0):
        current["status"] = new["status"]
    current["conf"] = max(current["conf"], new["conf"])


def _dedupe(ds: list[dict]) -> list[dict]:
    seen = {}
    for d in ds:
        key = (tuple(sorted(d["units"])), tuple(sorted(d["needs"])))
        seen.setdefault(key, {"units": list(key[0]), "needs": list(key[1])})
    return [seen[k] for k in sorted(seen)]


def _any_of(labels: list[Label]) -> Label:
    """``any_of`` of the labels of an element's derivations; a node has one at least, a live edge too."""
    found = any_of(labels)
    assert found is not None, "an element without a derivation"
    return found


def _deriv_label(d: dict, unit_labels: dict[str, Label], node_label: dict[str, Label]) -> Label:
    """The join of the labels of what a derivation uses. One without any (no unit of the corpus, no labelled node)
    has no label, and its element could not be labelled: the build stops there."""
    parts = [unit_labels[u] for u in d["units"] if u in unit_labels]
    parts += [node_label[n] for n in d["needs"] if n in node_label]
    found = join_all(parts)
    assert found is not None, f"a derivation without a labelled unit or node: {d}"
    return found


def _reads(reader: Label, label: Label) -> bool:
    """Whether a reader of ``reader``'s level, location and audience reads what is labelled ``label``."""
    return (
        label.level <= reader.level
        and label.location <= reader.location
        and audience_below(label.audience, reader.audience)
    )
