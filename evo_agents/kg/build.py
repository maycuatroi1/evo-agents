"""``evo-agents kg build``: the corpus state in, a graph snapshot out.

Stages run as memoized tasks: map and structure per item, then one identifier index, then link and
binding per item. Assembly merges facts, applies entity resolution (only declared ``same_as``),
evaluates the provenance circuit, computes labels, checks node keys, and writes the store as one build.
Its coverage names, per source, what the build holds; for a git source that includes the commit read
(``evo_agents.kg.freshness``).
A store written by an older schema is rebuilt from the corpus log into a new file that replaces it.

A build does only what changed. A link task is done again only for an item whose own text or structure changed or
that read an entry of the identifier index that now holds something else (``Memo.run_traced``), and the store
rewrites only the rows of the full-text index that changed. A build whose corpus state and inputs (the code, the
config, the cursors: ``build_inputs``) are those of the store's latest build is not done at all (``unchanged``).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg import audit
from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg import review as kg_review
from evo_agents.kg import search as text_search
from evo_agents.kg import store as kg_store
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.freshness import add_reads
from evo_agents.kg.ids import utc_now
from evo_agents.kg.memo import Memo
from evo_agents.kg.pipeline import curation
from evo_agents.kg.pipeline.binding import BINDING_VERSION
from evo_agents.kg.pipeline.link import LINK_VERSION, Held, build_index, index_digest, link_traced
from evo_agents.kg.pipeline.stages import (
    DIRECTORY_CONNECTORS,
    MAP_VERSION,
    STRUCTURE_VERSION,
    code_coverage,
    code_extractor,
    declares,
    frag_unit,
    item_unit,
    map_item,
    missing_graphify,
    structure_item,
)
from evo_agents.kg.policy import Label, Policy, any_of, join_all
from evo_agents.kg.project import Project, ProjectError
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.schema import known_kinds
from evo_agents.kg.store import Clusters, Graph, StaleSchema, Store, edge_id, key_conflicts, label_columns
from evo_agents.kg.text import split_passages

STATUS_RANK = {"declared": 5, "parsed": 4, "introspected": 3, "resolved": 2, "proposed": 1}
# config: the aliases of tsconfig.json; commit and pr: the files a commit changed, the commit a pull request merged as
STRUCTURED_KINDS = {"manifest", "contracts", "plan", "code", "markdown", "config", "commit", "pr"}
DEFINITION_SAMPLES = 15
# Their text reaches node_fts through symbols, docstrings, plans and steps; code and markup reach code_fts whole.
UNSEARCHED_ITEMS = ("code", "plan", "markup")
CODE_TEXT_ITEMS = ("code", "markup")  # the items whose text kg_search looks up words in (``kg.search``, code_fts)
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
    warnings: list[str] = field(default_factory=list)  # coverage gaps a build survives, e.g. a missing extra
    coverage: dict = field(default_factory=dict)
    memo: dict = field(default_factory=dict)
    seconds: float = 0.0
    verify: dict | None = None
    rebuilt_from: str | None = None  # the older store schema this build replaced, if any
    skipped: bool = False  # nothing changed since build ``build_id``, which stays the latest (``unchanged``)

    def summary_line(self) -> str:
        if not self.ok:
            return f"build FAILED for {self.project}: " + "; ".join(self.errors[:5])
        if self.skipped:
            return (
                f"build {self.build_id} for {self.project} is current: {self.nodes} nodes, {self.edges} edges;"
                " the corpus and the config have not changed since, so nothing was built"
            )
        rebuilt = (
            f", store rebuilt from the log (schema {self.rebuilt_from} -> {kg_store.SCHEMA_VERSION})"
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
    backends = {sid: (s.get("code") or {}).get("backend", "auto") for sid, s in sources.items()}
    explicit = [sid for sid, backend in sorted(backends.items()) if backend == "graphify-ast"]
    if explicit:
        from evo_agents.kg.extract import graphify as gfy

        if not gfy.available():
            return Graph(), {}, [missing_graphify(sid) for sid in explicit], []
    identifiers = [
        {"kind": i["kind"], "pattern": i["pattern"], **({"aliases": i["aliases"]} if i.get("aliases") else {})}
        for i in project.knowledge.get("identifiers") or []
    ]
    identifiers_digest = sha256(canonical_json(identifiers))
    records = [r for r in corpus.items() if r.source in sources]

    # Units and their labels.
    graph = Graph()
    unit_labels: dict[str, Label] = {}
    for rec in records:
        r = rec.record
        label = policy.source_label(rec.source, r.get("label"))
        base = {"item_id": rec.item_id, "rev": r.get("rev"), "source": rec.source, **label_columns(label)}
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
    code_items: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    for rec in records:
        r = rec.record
        src = sources[rec.source]
        backend = backends[rec.source]
        dirs = src.get("connector") in DIRECTORY_CONNECTORS
        mapped, _ = memo.run(
            "map",
            MAP_VERSION,
            {"item": rec.item_id, "hash": r["hash"], "rev": r.get("rev", ""), "source": rec.source, "dirs": dirs},
            lambda r=r, s=rec.source, d=dirs: map_item(r, s, d),
        )
        if is_structured(r):
            extractor = code_extractor(r, backend)
            if r["kind"] == "code":
                code_items[rec.source].append((r, extractor))
            inputs = {"item": rec.item_id, "hash": r["hash"], "extractor": extractor}
            if r["kind"] in ("markdown", "code"):  # a frontmatter id, a comment, is a code only if a pattern says so
                inputs["identifiers"] = identifiers_digest
            structured, shash = memo.run(
                "structure",
                STRUCTURE_VERSION,
                inputs,
                lambda rec=rec, r=r, b=backend: structure_item(r, rec.source, body(rec), b, identifiers),
            )
        else:
            structured, shash = [], "none"
        structure_out[rec.item_id] = (structured, shash)
        first[rec.item_id] = mapped + structured

    index = build_index(first, identifiers)
    second = link_items(records, structure_out, index, memo, corpus, body)

    # Assembly.
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    derivs: dict[str, list[dict]] = defaultdict(list)
    where: dict[str, set] = defaultdict(set)
    same: list[dict] = []  # identities, and the deprecations a curation item declared
    notes: dict[str, list[str]] = defaultdict(list)  # node id -> descriptions and docstrings, for search
    errors: list[str] = []
    issues: list[str] = []
    stats: dict[str, dict] = defaultdict(lambda: {"items": 0, "issues": 0, **dict.fromkeys(STAT_KEYS, 0)})
    samples: dict[str, dict[str, list]] = defaultdict(lambda: {"ambiguous": [], "dangling": [], "calls_unresolved": []})
    rejected: set[tuple[str, str]] = set()
    item_mentions: dict[str, dict] = {}  # item id -> its dangling and ambiguous mentions, for the review queue
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
            elif t in curation.CURATION_FACTS:
                same.append(f)
            elif t in ("text", "doc") and f.get("field") not in UNSEARCHED_FIELDS:
                notes[f["src"]].append(f["text"])
            elif t == "error":
                errors.append(f["message"])
            elif t == "issue":
                issues.append(f["message"])
                stats[rec.source]["issues"] += 1
            elif t == "stats":
                for k in STAT_KEYS:
                    stats[rec.source][k] += f.get(k, 0)
                if f.get("dangling") or f.get("ambiguous"):
                    found = item_mentions.setdefault(rec.item_id, {"dangling": 0, "ambiguous": 0, "samples": {}})
                    for k in ("dangling", "ambiguous"):
                        found[k] += f.get(k, 0)
                        found["samples"].setdefault(k, []).extend((f.get("samples") or {}).get(k) or [])
                for k, values in (f.get("samples") or {}).items():
                    bucket = samples[rec.source][k]
                    bucket.extend(v for v in values if len(bucket) < 30 and v not in bucket)

    # Entity resolution: declared identities only, smallest id represents the class.
    uf = _UnionFind()
    for a, b in curation.split(same, graph.deprecations):
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

    # Labels: the meet over derivations (any_of: an audience a reader sees one of whole) of the join over what each
    # derivation uses.
    node_label: dict[str, Label] = {}
    for nid in merged_nodes:
        node_label[nid] = any_of(_deriv_label(d, unit_labels, node_label) for d in _dedupe(derivs[nid]))
    for item_id, found in item_mentions.items():
        graph.mentions.setdefault(remap(item_id), found)
    for nid, n in merged_nodes.items():
        graph.nodes[nid] = {**n, "label": node_label[nid], "aliases": sorted(aliases.get(nid, []))}
        graph.derivations[nid] = _dedupe(derivs[nid])
        graph.where[nid] = sorted(where[nid])
    for (src, rel, dst), e in merged_edges.items():
        eid = edge_id(src, rel, dst)
        ds = _dedupe(edge_derivs[(src, rel, dst)])
        label = any_of(_deriv_label(d, unit_labels, node_label) for d in ds)
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

    cut = search_texts(graph, records, corpus, body, notes)

    coverage = {
        "sources": [],
        "dangling_edges": dangling,
        "issues": issues[:50],
        "errors": errors[:50],
        "warnings": [],
        "multiple_definitions": multiple_definitions(graph, {i["kind"] for i in identifiers}),
        "search": {"cut": len(cut), "max_chars": text_search.MAX_TEXT_CHARS, "samples": cut[:DEFINITION_SAMPLES]},
    }
    for sid in sorted(sources):
        s = stats[sid]
        entry = {
            "id": sid,
            **s,
            "samples": samples[sid],
            "resolved_ratio": (s["resolved"] / s["mentions"]) if s["mentions"] else 0.0,
        }
        if code_items[sid]:
            entry["code"] = code_coverage(backends[sid], code_items[sid])
            if entry["code"]["warning"]:
                coverage["warnings"].append(f"source {sid}: {entry['code']['warning']}")
        coverage["sources"].append(entry)
    return graph, coverage, errors, issues


def link_items(records, structure_out: dict, index: dict, memo: Memo, corpus: Corpus, body) -> dict[str, list[dict]]:
    """Link and binding, per item. A link task is traced (``Memo.run_traced``): it runs again when its item or its
    structure changed, or when an entry of the index it read holds something else. A binding task is keyed by the
    whole index, whose digest is taken only when the project has bindings."""
    held = Held(index)
    idx = None
    second: dict[str, list[dict]] = {}
    for rec in records:
        r = rec.record
        structured, shash = structure_out[rec.item_id]
        if r["kind"] == "binding":
            idx = idx or index_digest(index)
            out, _ = memo.run(
                "binding",
                f"{BINDING_VERSION}.{curation.CURATION_VERSION}",
                {"item": rec.item_id, "hash": r["hash"], "index": idx},
                lambda rec=rec, r=r: curation.bind(r, body(rec), index),
            )
        else:
            out, _ = memo.run_traced(
                "link",
                LINK_VERSION,
                {"item": rec.item_id, "hash": r["hash"], "structure": shash},
                lambda rec=rec, r=r, st=structured: link_traced(r, corpus.fragments(r), body(rec), st, index),
                held.digest,
            )
        second[rec.item_id] = out
    return second


def search_texts(graph: Graph, records, corpus: Corpus, body, notes: dict[str, list[str]]) -> list[str]:
    """Fill ``graph.texts`` and ``graph.paths``, what the full-text index holds besides names (``kg.search``): the
    sections of a document, the text of an item that is neither code, markup nor a plan (a plan's text is on its plan
    and step nodes), the descriptions and docstrings in ``notes``, and a requirement's code; ``graph.code``, the text
    of a code or markup file without an audience, for ``code_fts``; the path of a node that has
    one, a section's being its document's; and ``graph.passages``, a section or a file read whole longer than
    ``kg.search.PASSAGE_FROM`` cut into passages with their lines, each after the document's name and the headings
    above it. The ids of the nodes whose body was cut (``kg.search.MAX_TEXT_CHARS``)."""
    parts: dict[str, list[str]] = defaultdict(list)
    long: list[tuple[str, int, str, list[str]]] = []  # (node id, first line, text, context) of a body to cut
    for rec in records:
        r = rec.record
        node = graph.nodes.get(r["id"])
        if node is None:
            continue
        if r.get("fragments") and node["kind"] == "Document":
            frags = corpus.fragments(r)
            heads = {f["anchor"]: f for f in frags}
            for f in frags:
                sid = f"{r['id']}#{f['anchor']}"
                target = sid if sid in graph.nodes else r["id"]
                parts[target].append(f["text"])
                first = _first_line(f.get("span"))
                if first is not None and len(f["text"]) > text_search.PASSAGE_FROM:
                    long.append((target, first, f["text"], [node["name"] or r["id"], *_headings(f, heads)]))
        elif r["kind"] not in UNSEARCHED_ITEMS:
            text = body(rec) or ""
            parts[r["id"]].append(text)
            if node["kind"] in ("File", "Document") and len(text) > text_search.PASSAGE_FROM:  # lines of a file
                long.append((r["id"], 1, text, [node["name"] or r["id"]]))
        if r["kind"] in CODE_TEXT_ITEMS and node["kind"] == "File" and not node["label"].audience:
            text = body(rec)
            if text:
                graph.code[r["id"]] = text
    for nid, first, text, context in long:  # past MAX_TEXT_CHARS, a passage would index what the body cut
        prefix = " > ".join(dict.fromkeys(c for c in context if c))
        found = split_passages(text[: text_search.MAX_TEXT_CHARS], first, text_search.PASSAGE_CHARS)
        graph.passages.setdefault(nid, []).extend({**p, "text": f"{prefix}\n{p['text']}"} for p in found)
    for nid, texts in notes.items():
        if nid in graph.nodes:
            parts[nid] += texts
    cut = []
    for nid, n in graph.nodes.items():
        props = n["props"]
        if props.get("code"):
            parts[nid].append(str(props["code"]))
        item = graph.nodes.get(props.get("item")) if n["kind"] == "Section" else None
        path = props.get("path") or (item["props"].get("path") if item else None)
        if path:
            graph.paths[nid] = str(path)
        if parts.get(nid):
            graph.texts[nid], was_cut = text_search.body_text(parts[nid])
            if was_cut:
                cut.append(nid)
    return sorted(cut)


def _first_line(span: str | None) -> int | None:
    """The first line of a fragment's ``lines A-B`` span, or None."""
    found = re.match(r"lines? (\d+)", span or "")
    return int(found.group(1)) if found else None


def _headings(frag: dict, heads: dict[str, dict]) -> list[str]:
    """The titles of the headings above a markdown fragment, outermost first, then its own."""
    chain, seen, at = [], set(), frag
    while at is not None and at["anchor"] not in seen:
        seen.add(at["anchor"])
        if at.get("title"):
            chain.append(at["title"])
        at = heads.get(at.get("parent") or "")
    return chain[::-1]


def multiple_definitions(graph: Graph, code_kinds: set[str]) -> dict:
    """Codes with ``defines`` from more than one item. Design 4.6 wants one definition per code, copies
    marked ``derived_from``. A section defines on behalf of its document, so one item counts once."""
    places: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e["rel"] == "defines" and graph.nodes[e["dst"]]["kind"] in code_kinds:
            src = graph.nodes[e["src"]]
            places[e["dst"]].add(src["props"].get("item", e["src"]) if src["kind"] == "Section" else e["src"])
    codes = sorted(code for code, items in places.items() if len(items) > 1)
    return {
        "codes": len(codes),
        "samples": [{"code": code, "places": sorted(places[code])} for code in codes[:DEFINITION_SAMPLES]],
    }


def is_structured(record: dict) -> bool:
    """Whether the structure stage reads an item: one of STRUCTURED_KINDS, or one that declares nodes or edges."""
    return record["kind"] in STRUCTURED_KINDS or declares(record)


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
    project: Project,
    *,
    store: Store | None = None,
    memo: Memo | None = None,
    verify: bool = False,
    cold: bool = False,
    force: bool = False,
) -> BuildReport:
    """Build the project's graph and append one line to its audit log, whether the build succeeds, fails or
    raises. ``verify`` rebuilds into a temporary store, which adds no line of its own: the outcome is this
    build's ``verified``. A build into the project's own store is skipped, unless ``force``, when its latest build
    was made of the same corpus state with the same inputs (``unchanged``): the report names that build, with
    ``skipped`` set, and the audit log gets no line, since nothing was built."""
    if store is None and not force:
        report = unchanged(project)
        if report is not None:
            if verify:
                report.verify = verify_build(project, report.content_hash, cold=cold)
                report.ok = report.verify["match"]
            _vectors(project, report)
            return report
    started_at = utc_now()
    report = BuildReport(project.name, ok=False)
    crashed = None
    try:
        warm = _build(project, report, store, memo)
        if verify and report.ok:
            report.verify = verify_build(project, report.content_hash, cold=cold, warm=warm)
            report.ok = report.verify["match"]
    except BaseException as exc:
        crashed = type(exc).__name__
        raise
    finally:
        audit.record_build(project.root, report, started=started_at, finished=utc_now(), exception=crashed)
    if store is None and report.ok:
        _vectors(project, report)
    return report


def _vectors(project: Project, report: BuildReport) -> None:
    """Make the project's graph hold the vectors its ``semantic.json`` asks for (``evo_agents.kg.semantic``), outside
    the build's content hash; a model that cannot be loaded is a warning, and the graph searches by words."""
    from evo_agents.kg import semantic

    path = project.root / "graph.sqlite"
    try:
        settings = semantic.read_settings(project.root)
    except ValueError as exc:
        report.warnings.append(f"semantic: {exc}")
        return
    if not path.exists():
        return
    found = semantic.index_file(path, settings)
    if found.error:
        report.warnings.append(found.line())


def build_inputs(project: Project, corpus: Corpus) -> str | None:
    """Digest of what a build makes of the corpus state besides the state: the code (evo-agents' version, the store
    schema, each stage's version, the graphify it would use, the clustering's version, parameters and engine), the
    project's knowledge.yaml and ontology, and the cursors its coverage copies (``freshness.add_reads``). None when the ontology does not load, which fails a build."""
    from evo_agents.kg.extract import graphify as gfy

    try:
        ontology = project.ontology
    except ProjectError:
        return None
    sources = sorted(s["id"] for s in project.sources() if isinstance(s.get("id"), str))
    parts = {
        "code": [__version__, kg_store.SCHEMA_VERSION, MAP_VERSION, STRUCTURE_VERSION, LINK_VERSION, BINDING_VERSION],
        "graphify": gfy.version() if gfy.available() else None,
        "knowledge": project.knowledge,
        "ontology": ontology,
        "cursors": {sid: corpus.cursor(sid) for sid in sources},
        "clusters": kg_clusters.inputs(),
    }
    return sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def unchanged(project: Project) -> BuildReport | None:
    """The report of the latest build in the project's store (``graph.sqlite``) when it was made of the corpus state
    the project has now, with the inputs it has now (``build_inputs``): a build would write that graph again. None
    otherwise, and when the store is missing, of another schema, or holds no ready build."""
    corpus = project.corpus()
    try:
        state, inputs = corpus.state_digest(), build_inputs(project, corpus)
    finally:
        corpus.close()
    path = project.root / "graph.sqlite"
    if inputs is None or not path.exists():
        return None
    try:
        store = Store.open_file(path)
    except (StaleSchema, sqlite3.Error):
        return None
    try:
        b = store.latest_ready()
        row = store.build_row(b) if b is not None else None
        if row is None or row["state_digest"] != state or row["stats"].get("inputs") != inputs:
            return None
        size = row["stats"].get("size") or store.counts(b)
    finally:
        store.close()
    coverage = row["coverage"]
    report = BuildReport(project.name, ok=True, build_id=b, content_hash=row["content_hash"], skipped=True)
    report.nodes, report.edges, report.units = size["nodes"], size["edges"], size["units"]
    report.coverage, report.warnings = coverage, coverage.get("warnings", [])
    report.issues, report.dangling_edges = coverage.get("issues", []), coverage.get("dangling_edges", 0)
    return report


def _build(project: Project, report: BuildReport, store: Store | None, memo: Memo | None) -> Clusters | None:
    """Build into ``store`` (the project's graph.sqlite when None); the clusters the build started from."""
    started = time.monotonic()
    corpus = project.corpus()
    own_memo, own_store = memo is None, store is None
    if memo is None:
        path = memo_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        memo = Memo(path)
    try:
        graph, coverage, errors, issues = run_pipeline(project, corpus, memo)
        add_reads(coverage, corpus)
        memo.commit()
        report.memo = memo.stats()
        report.issues = issues[:50]
        report.warnings = coverage.get("warnings", [])
        report.coverage = coverage
        if errors:
            report.errors = errors
            return
        add_curation(graph, coverage)
        target = project.root / "graph.sqlite"
        warm = previous_clusters(store, target)
        add_clusters(project, graph, coverage, warm)
        add_review(graph, coverage)
        if store is None:
            found = Store.stored_schema(target)
            if found not in (None, kg_store.SCHEMA_VERSION):
                report.rebuilt_from = found
            store = Store.staging(target) if report.rebuilt_from else Store(target)
        b = store.write(
            graph,
            state_digest=corpus.state_digest(),
            coverage=coverage,
            stats={"memo": report.memo, "inputs": build_inputs(project, corpus)},
            policy=project.policy,
            warm=warm,
        )
        if report.rebuilt_from:
            store = store.install(target)
        counts = store.counts(b)
        report.ok = True
        report.build_id = b
        report.content_hash = store.build_row(b)["content_hash"]
        report.nodes, report.edges, report.units = counts["nodes"], counts["edges"], counts["units"]
        report.dangling_edges = coverage["dangling_edges"]
        return warm
    finally:
        # Since Python 3.11 a connection left open is freed only by the cycle collector (its statement cache refers back
        # to it), and Windows cannot replace a file a connection holds: the next rebuild would fail on graph.sqlite-wal.
        if own_store and store is not None:
            store.close()
        if own_memo:
            memo.close()
        corpus.close()
    report.seconds = round(time.monotonic() - started, 3)
    return None


def add_curation(graph: Graph, coverage: dict) -> None:
    """Mark the parts of ``graph`` its deprecations name (``evo_agents.kg.pipeline.curation``) and count them in
    ``coverage["curation"]``; a graph without a deprecation keeps its coverage as it is."""
    if graph.deprecations:
        coverage["curation"] = curation.apply(graph)


def add_clusters(project: Project, graph: Graph, coverage: dict, warm: Clusters | None) -> None:
    """Compute ``graph``'s clusters from ``warm`` when the extra is installed (``evo_agents.kg.clusters``), and say
    in ``coverage["clusters"]`` what came of it. A graph whose clustering fails is built without clusters."""
    if not kg_clusters.available():
        coverage["clusters"] = kg_clusters.missing()
        return
    try:
        graph.clusters = kg_clusters.compute(graph, project.policy, warm)
    except Exception as exc:  # the engine's errors are its own classes; the graph stands without clusters
        coverage["clusters"] = {"computed": False, "reason": f"clustering failed: {type(exc).__name__}: {exc}"}
        return
    coverage["clusters"] = {**kg_clusters.coverage(graph.clusters), "warm": warm is not None}


def add_review(graph: Graph, coverage: dict) -> None:
    """Compute the review items of a graph with clusters (``evo_agents.kg.review``) and count them per rule in
    ``coverage["review"]``; a graph without clusters has none, and its coverage says nothing of them."""
    if graph.clusters is None:
        return
    graph.review = kg_review.compute(graph)
    coverage["review"] = kg_review.counts(graph.review)


def previous_clusters(
    store: Store | None, target: Path, *, before: int | None = None, close: bool = False
) -> Clusters | None:
    """The clusters a build starts from: those of the latest ready build of ``store`` (before build ``before``), or,
    when ``store`` is None or has none and is not ``target`` (a staging store), those of the latest build in
    ``target``. None without one, and for a file of another schema. ``close`` closes ``store``."""
    try:
        if store is not None:
            b = store.latest_ready(before)
            if b is not None:
                return store.clusters(b)
            if before is not None or store.path == target:
                return None
        if not target.exists():
            return None
        try:
            found = Store.open_file(target)
        except (StaleSchema, sqlite3.Error):
            return None
        try:
            b = found.latest_ready()
            return found.clusters(b) if b is not None else None
        finally:
            found.close()
    finally:
        if close and store is not None:
            store.close()


def warm_start_of(project: Project, content_hash: str) -> Clusters | None:
    """The clusters the project's ready build of ``content_hash`` started from: those it recorded (``Store.warm_start``),
    else those of the ready build before it in the project's store. None when the store, that build or the one before
    it is gone."""
    target = project.root / "graph.sqlite"
    if not target.exists():
        return None
    try:
        store = Store.open_file(target)
    except (StaleSchema, sqlite3.Error):
        return None
    try:
        b = store.ready_build_of(content_hash)
    except sqlite3.Error:
        store.close()
        return None
    if b is None:
        store.close()
        return None
    try:
        recorded, warm = store.warm_start(b)
    except sqlite3.Error:  # a file written before cluster_warm came
        recorded, warm = False, None
    if recorded:
        store.close()
        return warm
    return previous_clusters(store, target, before=b, close=True)


_FIND = object()


def verify_build(
    project: Project, expected: str, *, cold: bool = False, warm: Clusters | None | object = _FIND
) -> dict:
    """Rebuild from the corpus into an empty store and compare content hashes (property P1).
    With ``cold`` the memo starts empty too, which also checks that memoized outputs are faithful. ``warm`` is the
    clusters the build started from; by default those of the build before the project's ready build of
    ``expected`` (``warm_start_of``), since cluster ids follow the build before."""
    if warm is _FIND:
        warm = warm_start_of(project, expected)
    tmp = Path(tempfile.mkdtemp(prefix="evo-kg-verify-"))
    try:
        store = Store(tmp / "graph.sqlite")
        memo = Memo(None) if cold else Memo(memo_path(project))
        corpus = project.corpus()
        graph, coverage, errors, _ = run_pipeline(project, corpus, memo)
        if errors:
            return {"match": False, "errors": errors[:10]}
        add_curation(graph, coverage)
        add_clusters(project, graph, coverage, warm)
        add_review(graph, coverage)
        b = store.write(graph, state_digest=corpus.state_digest(), coverage=coverage, stats={}, policy=project.policy)
        got = store.build_row(b)["content_hash"]
        store.close()
        memo.close()
        return {"match": got == expected, "expected": expected, "rebuilt": got, "cold": cold}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
