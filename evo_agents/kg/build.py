"""``evo-agents kg build``: the corpus state in, a graph snapshot out.

Stages run as memoized tasks: map and structure per item, then one identifier index, then link and
binding per item. Assembly merges facts, applies entity resolution (only declared ``same_as``),
evaluates the provenance circuit, computes labels, checks node keys, and writes the store as one build.
A store written by an older schema is rebuilt from the corpus log into a new file that replaces it.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.corpus import Corpus
from evo_agents.kg.memo import Memo
from evo_agents.kg.pipeline.binding import BINDING_VERSION, binding_item
from evo_agents.kg.pipeline.link import LINK_VERSION, build_index, index_digest, link_item
from evo_agents.kg.pipeline.stages import (
    MAP_VERSION,
    STRUCTURE_VERSION,
    code_extractor,
    frag_unit,
    item_unit,
    map_item,
    structure_item,
)
from evo_agents.kg.policy import Label, Policy, join_all, meet_all
from evo_agents.kg.project import Project, ProjectError
from evo_agents.kg.schema import known_kinds
from evo_agents.kg.store import SCHEMA_VERSION, Graph, Store, edge_id, key_conflicts

STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "proposed": 1}
STRUCTURED_KINDS = {"manifest", "contracts", "plan", "code"}
STAT_KEYS = (
    "mentions",
    "resolved",
    "ambiguous",
    "dangling",
    "refs",
    "refs_resolved",
    "refs_ambiguous",
    "refs_dangling",
)


@dataclass
class BuildReport:
    project: str
    ok: bool
    build_id: int | None = None
    content_hash: str | None = None
    nodes: int = 0
    edges: int = 0
    units: int = 0
    dangling_edges: int = 0
    errors: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    memo: dict = field(default_factory=dict)
    seconds: float = 0.0
    verify: dict | None = None
    rebuilt_from: str | None = None  # the older store schema this build replaced, if any

    def summary_line(self) -> str:
        if not self.ok:
            return f"build FAILED for {self.project}: " + "; ".join(self.errors[:5])
        rebuilt = (
            f", store rebuilt from the log (schema {self.rebuilt_from} -> {SCHEMA_VERSION})"
            if self.rebuilt_from
            else ""
        )
        return (
            f"build {self.build_id} for {self.project}: {self.nodes} nodes, {self.edges} edges, {self.units} units"
            f" in {self.seconds}s (memo {sum(s['hits'] for s in self.memo.values())} hits,"
            f" {sum(s['misses'] for s in self.memo.values())} misses){rebuilt}"
        )

    def to_json(self) -> dict:
        return self.__dict__


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


def run_pipeline(project: Project, corpus: Corpus, memo: Memo) -> tuple[Graph, dict, list[str], list[str]]:
    policy: Policy = project.policy
    try:
        ontology = project.ontology
    except ProjectError as exc:
        return Graph(), {}, [str(exc)], []
    # Without an ontology key every kind passes, as before ontologies were checked.
    kinds = known_kinds(ontology) if ontology is not None else None
    sources = {s["id"]: s for s in project.sources()}
    identifiers = list(project.knowledge.get("identifiers") or [])
    records = [r for r in corpus.items() if r.source in sources]

    # Units and their labels.
    graph = Graph()
    unit_labels: dict[str, Label] = {}
    for rec in records:
        r = rec.record
        label = policy.source_label(rec.source, r.get("label"))
        base = {
            "item_id": rec.item_id,
            "rev": r.get("rev"),
            "source": rec.source,
            "label_lvl": label.level,
            "label_loc": label.location,
            "label_int": label.integrity,
        }
        graph.units[item_unit(rec.item_id)] = {**base, "anchor": "", "hash": r.get("hash"), "span": None}
        unit_labels[item_unit(rec.item_id)] = label
        for f in r.get("fragments", []):
            uid = frag_unit(rec.item_id, f["anchor"])
            graph.units[uid] = {**base, "anchor": f["anchor"], "hash": f.get("hash"), "span": f.get("span")}
            unit_labels[uid] = label

    texts_cache: dict[str, str | None] = {}

    def body(rec) -> str | None:
        if rec.item_id not in texts_cache:
            texts_cache[rec.item_id] = corpus.body_text(rec.record)
        return texts_cache[rec.item_id]

    # Map and structure, per item.
    first: dict[str, list[dict]] = {}
    structure_out: dict[str, tuple[list[dict], str]] = {}
    for rec in records:
        r = rec.record
        src = sources[rec.source]
        backend = (src.get("code") or {}).get("backend", "auto")
        mapped, _ = memo.run(
            "map",
            MAP_VERSION,
            {"item": rec.item_id, "hash": r["hash"], "rev": r.get("rev", ""), "source": rec.source},
            lambda r=r, s=rec.source: map_item(r, s),
        )
        if r["kind"] in STRUCTURED_KINDS:
            structured, shash = memo.run(
                "structure",
                STRUCTURE_VERSION,
                {"item": rec.item_id, "hash": r["hash"], "extractor": code_extractor(r, backend)},
                lambda rec=rec, r=r, b=backend: structure_item(r, rec.source, body(rec), b),
            )
        else:
            structured, shash = [], "none"
        structure_out[rec.item_id] = (structured, shash)
        first[rec.item_id] = mapped + structured

    index = build_index(first, identifiers)
    idx = index_digest(index)

    # Link and binding, per item.
    second: dict[str, list[dict]] = {}
    for rec in records:
        r = rec.record
        structured, shash = structure_out[rec.item_id]
        if r["kind"] == "binding":
            out, _ = memo.run(
                "binding",
                BINDING_VERSION,
                {"item": rec.item_id, "hash": r["hash"], "index": idx},
                lambda rec=rec, r=r: binding_item(r, body(rec), index),
            )
        else:
            out, _ = memo.run(
                "link",
                LINK_VERSION,
                {"item": rec.item_id, "hash": r["hash"], "structure": shash, "index": idx},
                lambda rec=rec, r=r, st=structured: link_item(r, corpus.fragments(r), body(rec), st, index),
            )
        second[rec.item_id] = out

    # Assembly.
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    derivs: dict[str, list[dict]] = defaultdict(list)
    where: dict[str, set] = defaultdict(set)
    same: list[tuple[str, str]] = []
    errors: list[str] = []
    issues: list[str] = []
    stats: dict[str, dict] = defaultdict(lambda: {"items": 0, "issues": 0, **dict.fromkeys(STAT_KEYS, 0)})
    samples: dict[str, dict[str, list]] = defaultdict(lambda: {"ambiguous": [], "dangling": []})
    rejected: set[tuple[str, str]] = set()
    for rec in records:
        stats[rec.source]["items"] += 1
        for f in first[rec.item_id] + second[rec.item_id]:
            t = f["t"]
            if t == "node" and kinds is not None and f["kind"] not in kinds:
                if (rec.item_id, f["kind"]) not in rejected:
                    rejected.add((rec.item_id, f["kind"]))
                    errors.append(
                        f"{rec.item_id}: node {f['id']} has kind {f['kind']!r}, neither a hub kind nor in the ontology"
                    )
            elif t == "node":
                if f["id"] not in nodes:
                    nodes[f["id"]] = {k: f[k] for k in ("kind", "name", "props", "status", "conf")}
                else:
                    _strongest(nodes[f["id"]], f)
                derivs[f["id"]].append({"units": f["units"], "needs": f["needs"]})
                where[f["id"]].update(tuple(w) for w in f.get("where", []))
            elif t == "edge":
                key = (f["src"], f["rel"], f["dst"])
                if key not in edges:
                    edges[key] = {k: f[k] for k in ("props", "status", "conf")}
                else:
                    _strongest(edges[key], f)
                derivs[key].append({"units": f["units"], "needs": sorted(set(f["needs"]) | {f["src"], f["dst"]})})
                where[key].update(tuple(w) for w in f.get("where", []))
            elif t == "same":
                same.append((f["a"], f["b"]))
            elif t == "error":
                errors.append(f["message"])
            elif t == "issue":
                issues.append(f["message"])
                stats[rec.source]["issues"] += 1
            elif t == "stats":
                for k in STAT_KEYS:
                    stats[rec.source][k] += f.get(k, 0)
                for k, values in (f.get("samples") or {}).items():
                    bucket = samples[rec.source][k]
                    bucket.extend(v for v in values if len(bucket) < 30 and v not in bucket)

    # Entity resolution: declared identities only, smallest id represents the class.
    uf = _UnionFind()
    for a, b in same:
        if a in nodes and b in nodes:
            uf.union(a, b)
    rep = {n: uf.find(n) for n in nodes}
    aliases: dict[str, list[str]] = defaultdict(list)
    merged_nodes: dict[str, dict] = {}
    for nid in sorted(nodes):
        target = rep[nid]
        if target != nid:
            aliases[target].append(nid)
        if target not in merged_nodes:
            merged_nodes[target] = dict(nodes[target])
        if target != nid:
            derivs[target] = derivs[target] + derivs.pop(nid)
            where[target] |= where.pop(nid, set())

    def remap(x: str) -> str:
        return rep.get(x, x)

    merged_edges: dict[tuple, dict] = {}
    edge_derivs: dict[tuple, list[dict]] = defaultdict(list)
    edge_where: dict[tuple, set] = defaultdict(set)
    for key in sorted(edges):
        src, rel, dst = remap(key[0]), key[1], remap(key[2])
        if src == dst:
            continue
        nkey = (src, rel, dst)
        if nkey not in merged_edges:
            merged_edges[nkey] = dict(edges[key])
        else:
            _strongest(merged_edges[nkey], edges[key])
        for d in derivs.pop(key):
            edge_derivs[nkey].append({"units": d["units"], "needs": sorted({remap(n) for n in d["needs"]})})
        edge_where[nkey] |= where.pop(key, set())

    # Liveness: a node lives on its units; an edge also needs every element it names.
    alive = set(merged_nodes)
    dangling = 0
    for key in list(merged_edges):
        ds = [d for d in edge_derivs[key] if all(n in alive for n in d["needs"])]
        if not ds:
            dangling += 1
            del merged_edges[key]
            continue
        edge_derivs[key] = ds

    # Labels: meet over derivations of the join over what each derivation uses.
    node_label: dict[str, Label] = {}
    for nid in merged_nodes:
        node_label[nid] = meet_all(_deriv_label(d, unit_labels, node_label) for d in _dedupe(derivs[nid]))
    for nid, n in merged_nodes.items():
        graph.nodes[nid] = {**n, "label": node_label[nid], "aliases": sorted(aliases.get(nid, []))}
        graph.derivations[nid] = _dedupe(derivs[nid])
        graph.where[nid] = sorted(where[nid])
    for (src, rel, dst), e in merged_edges.items():
        eid = edge_id(src, rel, dst)
        ds = _dedupe(edge_derivs[(src, rel, dst)])
        label = meet_all(_deriv_label(d, unit_labels, node_label) for d in ds)
        graph.edges[eid] = {"src": src, "rel": rel, "dst": dst, **e, "label": label}
        graph.derivations[eid] = ds
        graph.where[eid] = sorted(edge_where[(src, rel, dst)])

    # Keys: a key two nodes would share fails the build, naming the nodes and the sources behind them.
    for (kind, key), ids in key_conflicts(graph).items():
        holders = []
        for nid in ids:
            srcs = sorted(
                {graph.units[u]["source"] for d in graph.derivations[nid] for u in d["units"] if u in graph.units}
            )
            holders.append(f"{nid} (source {', '.join(srcs)})")
        errors.append(f"duplicate {kind} key {key}: " + ", ".join(holders))

    # Searchable text: section and document bodies, short.
    for rec in records:
        r = rec.record
        if r["id"] in graph.nodes and r.get("fragments") and graph.nodes[r["id"]]["kind"] == "Document":
            for f in corpus.fragments(r):
                sid = f"{r['id']}#{f['anchor']}"
                target = sid if sid in graph.nodes else r["id"]
                graph.texts[target] = (graph.texts.get(target, "") + "\n" + f["text"])[:4000]
        elif r["id"] in graph.nodes and r["kind"] not in ("code",):
            text = body(rec)
            if text:
                graph.texts[r["id"]] = text[:4000]
    for nid, n in graph.nodes.items():
        extra = " ".join(str(v) for k, v in n["props"].items() if k in ("path", "goal", "code", "repo", "uri"))
        if extra:
            graph.texts[nid] = (graph.texts.get(nid, "") + " " + extra)[:4000]

    coverage = {"sources": [], "dangling_edges": dangling, "issues": issues[:50], "errors": errors[:50]}
    for sid in sorted(sources):
        s = stats[sid]
        coverage["sources"].append(
            {
                "id": sid,
                **s,
                "samples": samples[sid],
                "resolved_ratio": (s["resolved"] / s["mentions"]) if s["mentions"] else 0.0,
            }
        )
    return graph, coverage, errors, issues


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


def _deriv_label(d: dict, unit_labels: dict[str, Label], node_label: dict[str, Label]) -> Label:
    parts = [unit_labels[u] for u in d["units"] if u in unit_labels]
    parts += [node_label[n] for n in d["needs"] if n in node_label]
    return join_all(parts)


def memo_path(project: Project) -> Path:
    if project.policy.memo == "shared":
        return project.home / "_shared" / "memo.sqlite"
    return project.root / "memo.sqlite"


def build_project(
    project: Project, *, store: Store | None = None, memo: Memo | None = None, verify: bool = False, cold: bool = False
) -> BuildReport:
    started = time.monotonic()
    corpus = project.corpus()
    own_memo = memo is None
    if memo is None:
        path = memo_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        memo = Memo(path)
    report = BuildReport(project.name, ok=False)
    try:
        graph, coverage, errors, issues = run_pipeline(project, corpus, memo)
        memo.commit()
        report.memo = memo.stats()
        report.issues = issues[:50]
        report.coverage = coverage
        if errors:
            report.errors = errors
            return report
        target = project.root / "graph.sqlite"
        if store is None:
            found = Store.stored_schema(target)
            if found not in (None, SCHEMA_VERSION):
                report.rebuilt_from = found
            store = Store.staging(target) if report.rebuilt_from else Store(target)
        b = store.write(
            graph,
            state_digest=corpus.state_digest(),
            coverage=coverage,
            stats={"memo": report.memo},
            policy=project.policy,
        )
        if report.rebuilt_from:
            store = store.install(target)
        counts = store.counts(b)
        report.ok = True
        report.build_id = b
        report.content_hash = store.build_row(b)["content_hash"]
        report.nodes, report.edges, report.units = counts["nodes"], counts["edges"], counts["units"]
        report.dangling_edges = coverage["dangling_edges"]
    finally:
        if own_memo:
            memo.close()
    report.seconds = round(time.monotonic() - started, 3)
    if verify and report.ok:
        report.verify = verify_build(project, report.content_hash, cold=cold)
        report.ok = report.verify["match"]
    return report


def verify_build(project: Project, expected: str, *, cold: bool = False) -> dict:
    """Rebuild from the corpus into an empty store and compare content hashes (property P1).
    With ``cold`` the memo starts empty too, which also checks that memoized outputs are faithful."""
    tmp = Path(tempfile.mkdtemp(prefix="evo-kg-verify-"))
    try:
        store = Store(tmp / "graph.sqlite")
        memo = Memo(None) if cold else Memo(memo_path(project))
        corpus = project.corpus()
        graph, coverage, errors, _ = run_pipeline(project, corpus, memo)
        if errors:
            return {"match": False, "errors": errors[:10]}
        b = store.write(graph, state_digest=corpus.state_digest(), coverage=coverage, stats={}, policy=project.policy)
        got = store.build_row(b)["content_hash"]
        store.close()
        memo.close()
        return {"match": got == expected, "expected": expected, "rebuilt": got, "cold": cold}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
