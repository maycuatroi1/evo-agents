"""The review queue of a build: what in a project's knowledge graph a person should look at, computed by six rules over
a build that has clusters (``evo_agents.kg.clusters``), with no model.

- ``dangling_mention``: an item (a document, a plan) mentions code the graph does not hold, the dangling mentions its
  source's coverage counts (``evo_agents.kg.pipeline.link``);
- ``unmentioned_document``: a Document no other node mentions or links to, its own sections aside;
- ``cluster_without_knowledge``: a level-1 cluster with code (File, Symbol) and no Document, Plan, Problem or Claim;
- ``possible_duplicate``: nodes of one kind whose normalised names are the same and whose ids differ;
- ``ambiguous_match``: an item with mentions whose name matched several nodes, or a path edge resolved to one of
  several files with that path;
- ``bulk_noise``: images, generated files and plan steps whose name repeats across plans, BULK or more of a kind in
  one place.

An item holds its rule, the ids of the nodes it is about (``evidence``), the level-1 cluster its first node counts in,
with the level-1 cluster of each of its nodes, the action it suggests and what it found (``detail``: a count, samples).
Its fingerprint hashes the rule with what the item is about (a node id, an edge id, a cluster id, a kind
and name, a directory) and nothing that changes from build to build, so a dismissal kept by fingerprint holds across
builds. A reader sees an item only through its nodes: the first ``required`` nodes of its evidence must all be
visible, the rest are shown when visible, and the item needs ``least`` visible nodes (``visible_item``).
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable

from evo_agents.kg.clusters import IMAGE_SUFFIXES, STRUCTURAL
from evo_agents.kg.protocol import sha256
from evo_agents.kg.store import Graph

RULES = (
    "dangling_mention",
    "unmentioned_document",
    "cluster_without_knowledge",
    "possible_duplicate",
    "ambiguous_match",
    "bulk_noise",
)
ACTIONS = {
    "dangling_mention": "fix_mention",
    "unmentioned_document": "link_or_deprecate",
    "cluster_without_knowledge": "add_knowledge",
    "possible_duplicate": "merge",
    "ambiguous_match": "disambiguate",
    "bulk_noise": "exclude",
}
KNOWLEDGE_KINDS = frozenset({"Document", "Plan", "Problem", "Claim"})
CODE_KINDS = frozenset({"File", "Symbol"})
# Kinds whose names repeat by design: a symbol or a section is named within its file, a file by its base name.
UNIQUE_SKIP = frozenset(
    {"Symbol", "Section", "File", "Directory", "Source", "Repo", "Commit", "ChatMessage", "PlanStep", "PullRequest"}
)
BULK = 3  # nodes of one kind of noise in one place that make an item
EVIDENCE = 50  # node ids an item keeps; ``detail.count`` says how many there were
SAMPLES = 5
GENERATED = re.compile(
    r"(^|/)(node_modules|dist|build|vendor|__generated__|generated|\.next|coverage)/"
    r"|(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum)$"
    r"|\.(min\.js|min\.css|map|pb\.go|lock)$|_pb2\.py$|\.generated\.[a-z]+$",
    re.IGNORECASE,
)

Visible = Callable[[tuple], bool]


def fingerprint(rule: str, key: str) -> str:
    """The id of an item across builds: its rule and what it is about."""
    return "r:" + sha256(f"{rule}\x1f{key}")[7:23]


def normal(name: str | None) -> str:
    """A name as duplicates compare it: accents dropped, case folded, letters and digits only."""
    folded = unicodedata.normalize("NFKD", name or "").casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _path(node: dict) -> str:
    return str(node["props"].get("path") or "")


def compute(graph: Graph) -> list[dict]:
    """The review items of ``graph`` (with clusters), by fingerprint; ``graph.mentions`` holds, per item id, its
    dangling and ambiguous mentions with samples. Empty when the graph has no clusters."""
    found = graph.clusters
    if found is None or not found.clusters:
        return []
    run = _Run(graph, found)
    run.mentions()
    run.ambiguous_paths()
    run.unmentioned_documents()
    run.clusters_without_knowledge()
    run.possible_duplicates()
    run.bulk_noise()
    return [run.items[fp] for fp in sorted(run.items)]


class _Run:
    """One computation of the review items of a graph: the degrees and mentions the rules share, and the items found."""

    def __init__(self, graph: Graph, found) -> None:
        self.graph = graph
        self.found = found
        self.nodes = graph.nodes
        self.degree: Counter = Counter()
        self.mentioned: set[str] = set()
        self.items: dict[str, dict] = {}
        for e in graph.edges.values():
            if e["rel"] not in STRUCTURAL:
                self._count(e)

    def _count(self, e: dict) -> None:
        self.degree[e["src"]] += 1
        self.degree[e["dst"]] += 1
        dst = self.nodes.get(e["dst"])
        if dst is not None and dst["kind"] == "Document":
            src = self.nodes.get(e["src"])
            own = src is not None and src["kind"] == "Section" and src["props"].get("item") == e["dst"]
            if not own:
                self.mentioned.add(e["dst"])

    def owner(self, nid: str) -> str:
        n = self.nodes.get(nid)
        if n is None:
            return nid
        if n["kind"] == "Symbol":
            return n["props"].get("file") or nid
        if n["kind"] == "Section":
            return n["props"].get("item") or nid
        return nid

    def cluster_of(self, nid: str) -> str | None:
        member = self.found.members.get(self.owner(nid))
        return member[0] if member else None

    def ranked(self, ids) -> list[str]:
        return sorted(set(ids), key=lambda i: (-self.degree[i], i))

    def add(self, rule: str, key: str, evidence: list[str], detail: dict, *, required: int = 1, least: int = 1) -> None:
        fp = fingerprint(rule, key)
        kept = evidence[:EVIDENCE]
        clusters = [self.cluster_of(nid) for nid in kept]
        self.items[fp] = {
            "fingerprint": fp,
            "rule": rule,
            "cluster": next((c for c in clusters if c), None),
            "evidence": kept,
            "clusters": clusters,
            "action": ACTIONS[rule],
            "detail": {"count": len(evidence), **detail, "required": required, "least": least},
        }

    def mentions(self) -> None:
        """1 and 5: the mentions of each item that matched nothing, or several nodes."""
        for item_id, m in sorted(self.graph.mentions.items()):
            if item_id not in self.nodes:
                continue
            for kind, rule in (("dangling", "dangling_mention"), ("ambiguous", "ambiguous_match")):
                if m.get(kind):
                    samples = (m.get("samples") or {}).get(kind, [])[:SAMPLES]
                    self.add(rule, item_id, [item_id], {"mentions": m[kind], "samples": samples})

    def ambiguous_paths(self) -> None:
        """5: path edges resolved to one of several files with that path."""
        nodes = self.nodes
        by_path: dict[str, list[str]] = defaultdict(list)
        for nid, n in nodes.items():
            if n["kind"] == "File" and _path(n):
                by_path[_path(n)].append(nid)
        for eid, e in sorted(self.graph.edges.items()):
            if e["status"] != "resolved" or e["rel"] in STRUCTURAL:
                continue
            dst = nodes.get(e["dst"])
            if dst is None or dst["kind"] != "File" or len(by_path.get(_path(dst), [])) < 2:
                continue
            others = [c for c in sorted(by_path[_path(dst)]) if c != e["dst"]]
            self.add(
                "ambiguous_match",
                eid,
                [e["src"], e["dst"], *others],
                {"edge": eid, "rel": e["rel"], "path": _path(dst)},
                required=2,
                least=2,
            )

    def unmentioned_documents(self) -> None:
        """2: documents no other node mentions."""
        for nid, n in sorted(self.nodes.items()):
            if n["kind"] == "Document" and nid not in self.mentioned and nid in self.found.members:
                self.add("unmentioned_document", nid, [nid], {"path": _path(n) or None})

    def clusters_without_knowledge(self) -> None:
        """3: clusters with code and no knowledge."""
        kinds_of: dict[str, set[str]] = defaultdict(set)
        code_of: dict[str, list[str]] = defaultdict(list)
        for nid, (top, _sub) in self.found.members.items():
            n = self.nodes.get(nid)
            if n is None:
                continue
            kinds_of[top].add(n["kind"])
            if n["kind"] in CODE_KINDS:
                code_of[top].append(nid)
        for cid, c in sorted(self.found.clusters.items()):
            if c["level"] != 1 or not code_of[cid] or kinds_of[cid] & KNOWLEDGE_KINDS:
                continue
            self.add("cluster_without_knowledge", cid, self.ranked(code_of[cid]), {"label": c["label"]}, required=0)

    def possible_duplicates(self) -> None:
        """4: nodes of one kind with the same normalised name."""
        named: dict[tuple[str, str], list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            key = normal(n["name"])
            if n["kind"] not in UNIQUE_SKIP and len(key) >= 3:
                named[(n["kind"], key)].append(nid)
        for (kind, key), ids in sorted(named.items()):
            if len(ids) >= 2:
                self.add(
                    "possible_duplicate", f"{kind}\x1f{key}", self.ranked(ids), {"kind": kind}, required=0, least=2
                )

    def bulk_noise(self) -> None:
        """6: bulk noise, per directory or per repeated step name."""
        noise: dict[tuple[str, str], list[str]] = defaultdict(list)
        step_plans: dict[str, set[str]] = defaultdict(set)
        for e in self.graph.edges.values():
            if e["rel"] == "has_step":
                step_plans[e["dst"]].add(e["src"])
        steps: dict[str, list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            path = _path(n)
            if n["kind"] == "PlanStep":
                steps[normal(n["name"])].append(nid)
            elif path.lower().endswith(IMAGE_SUFFIXES):
                noise[("image", posixpath.dirname(path))].append(nid)
            elif path and GENERATED.search(path):
                noise[("generated", posixpath.dirname(path))].append(nid)
        for (what, where), ids in sorted(noise.items()):
            if len(ids) >= BULK:
                self.add("bulk_noise", f"{what}\x1f{where}", sorted(ids), {"kind": what, "where": where}, required=0)
        for name, ids in sorted(steps.items()):
            plans = {p for i in ids for p in step_plans.get(i) or {i}}
            if name and len(plans) >= BULK:
                self.add("bulk_noise", f"step\x1f{name}", sorted(ids), {"kind": "step", "where": None}, required=0)


def counts(items: list[dict]) -> dict[str, int]:
    """How many items each rule has, every rule named."""
    found = Counter(item["rule"] for item in items)
    return {rule: found.get(rule, 0) for rule in RULES}


def visible_item(item: dict, labels: dict[str, tuple], visible: Visible) -> dict | None:
    """``item`` as a reader sees it, ``labels`` holding the label of each of its nodes the reader's audience holds:
    None when a required node is not visible or fewer than ``least`` are; else with its evidence cut to the visible
    nodes, its cluster that of the first of them in one and, when some were cut, its count to theirs."""
    detail = item["detail"]
    required, least = detail.get("required", 1), detail.get("least", 1)
    seen = [nid for nid in item["evidence"] if nid in labels and visible(labels[nid])]
    if any(nid not in seen for nid in item["evidence"][:required]) or len(seen) < max(least, 1):
        return None
    where = dict(zip(item["evidence"], item.get("clusters") or [], strict=False))
    out = {
        **item,
        "evidence": seen,
        "cluster": next((where[nid] for nid in seen if where.get(nid)), None),
        "detail": {k: v for k, v in detail.items() if k not in ("required", "least")},
    }
    out.pop("clusters", None)
    if len(seen) < len(item["evidence"]):
        out["detail"]["count"] = len(seen)
    return out
