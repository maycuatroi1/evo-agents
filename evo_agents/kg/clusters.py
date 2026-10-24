"""Clusters of a build: the graph's communities in two levels, named by their words (the optional extra ``clusters``).

A build with ``graspologic-native`` installed (``pip install 'evo-ak[clusters]'``) groups its nodes into clusters and
writes them with the graph (``Store.write``: tables ``clusters``, ``cluster_members``, ``cluster_links``). Without it
the build is the same graph with no clusters, and its coverage says why (``coverage["clusters"]``).

The projection clusters work on:

- a Symbol counts as the File that defines it, a Section as its Document;
- Source, Repo, Directory and Commit nodes, and the structural relations (STRUCTURAL), are left out;
- so are the noise of a corpus: image files, and plan steps whose name recurs across plans ("Merge and release");
- only nodes every reader of the project may read take part (``shared``): no audience, and a level and location
  within the clearance of the project's hub sink (or, without one, the lowest clearance its sinks have). A node of a
  narrower audience is neither a member nor a word of a label, so a cluster's name never shows what a reader may
  not read;
- the weight of a pair is how many edges join it; each weight is divided by the square root of the product of the two
  degrees, and the most connected nodes (HUB_SHARE of them) are left out of the clustering and joined afterwards to
  the cluster their edges reach most, so a few documents every plan names do not glue unrelated clusters together.

Two levels come from ``hierarchical_leiden``: level 1, the clusters of the whole graph; level 2, the parts of a level-1
cluster larger than MAX_CLUSTER_SIZE. A cluster's label is its first LABEL_WORDS words by c-TF-IDF over the names and
paths of its members, against the other clusters of its level.

Stable across builds: the seed is fixed, the clustering starts from the clusters of the previous build (``warm``), and
a new cluster takes the id of the previous cluster of its level it matches with a Jaccard index of at least JACCARD
(each id once, the best matches first). Any other cluster's id is a digest of its members. The same graph, the same
previous clusters and the same parameters give the same clusters, so ``inputs`` (the algorithm's version, its
parameters and the engine's version) is an input of the build (``evo_agents.kg.build.build_inputs``).
"""

from __future__ import annotations

import importlib.util
import math
import re
from collections import Counter, defaultdict
from importlib import metadata

from evo_agents.kg.policy import Policy
from evo_agents.kg.protocol import sha256
from evo_agents.kg.store import Clusters, Graph

ALGORITHM_VERSION = "1"
ENGINE = "graspologic-native"
SEED = 7
RESOLUTION = 1.0
MAX_CLUSTER_SIZE = 150
HUB_SHARE = 0.005  # the share of the most connected projected nodes left out of the clustering
LABEL_WORDS = 5
JACCARD = 0.5
PARAMS = {
    "seed": SEED,
    "resolution": RESOLUTION,
    "max_cluster_size": MAX_CLUSTER_SIZE,
    "hub_share": HUB_SHARE,
    "label_words": LABEL_WORDS,
    "jaccard": JACCARD,
}
STRUCTURAL = frozenset({"in_source", "in_repo", "child_of", "defines", "part_of", "member_of"})
SKIP_KINDS = frozenset({"Source", "Repo", "Directory", "Commit"})
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif", ".tif", ".tiff")
STOP_WORDS = frozenset(
    "py ts tsx js jsx mjs md mdx json yaml yml toml txt the and for with from into not are was this that test tests src "
    "lib init index main file files docs doc của và cho là có không một khi trên với được từ đã các theo những này".split()
)
INSTALL = "pip install 'evo-ak[clusters]'"


def available() -> bool:
    return importlib.util.find_spec("graspologic_native") is not None


def engine_version() -> str | None:
    try:
        return metadata.version(ENGINE)
    except metadata.PackageNotFoundError:
        return None


def inputs() -> dict:
    """What decides the clusters of a graph besides the graph and the previous clusters."""
    return {"version": ALGORITHM_VERSION, "params": PARAMS, "engine": engine_version() if available() else None}


def missing() -> dict:
    """The coverage of a build without the extra."""
    return {"computed": False, "reason": f"{ENGINE} is not installed: {INSTALL}"}


def shared_ceiling(policy: Policy) -> tuple[int, int]:
    """The level and location every reader of the project may read: the hub sink's clearance, else the lowest
    clearance of the sinks the project declares, else the lowest level and location."""
    hub = policy.hub_sink()
    if hub is not None:
        return hub.level, hub.location
    if policy.sinks:
        return min(s.level for s in policy.sinks.values()), min(s.location for s in policy.sinks.values())
    return 0, 0


def shared(node: dict, ceiling: tuple[int, int]) -> bool:
    label = node["label"]
    return not label.audience and label.level <= ceiling[0] and label.location <= ceiling[1]


def project(graph: Graph, ceiling: tuple[int, int]) -> dict[tuple[str, str], int]:
    """The projected graph: per pair of projected node ids (sorted), how many edges join them."""
    nodes = graph.nodes
    definer: dict[str, str] = {}
    step_plans: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e["rel"] == "defines" and nodes.get(e["src"], {}).get("kind") == "File":
            definer.setdefault(e["dst"], e["src"])
        elif e["rel"] == "has_step":
            step_plans[e["dst"]].add(e["src"])
    names: dict[str, set[str]] = defaultdict(set)
    for nid, n in nodes.items():
        if n["kind"] == "PlanStep":
            names[_normal(n["name"])].update(step_plans.get(nid) or {nid})
    repeated = {name for name, plans in names.items() if name and len(plans) > 1}

    def keep(nid: str) -> str | None:
        n = nodes.get(nid)
        if n is None or n["kind"] in SKIP_KINDS or not shared(n, ceiling):
            return None
        if n["kind"] == "PlanStep" and _normal(n["name"]) in repeated:
            return None
        if str(n["props"].get("path") or n["name"] or "").lower().endswith(IMAGE_SUFFIXES):
            return None
        return nid

    def target(nid: str) -> str | None:
        n = nodes.get(nid)
        if n is None:
            return None
        if n["kind"] == "Symbol":
            owner = n["props"].get("file")
            return keep(owner if owner in nodes else definer.get(nid, ""))
        if n["kind"] == "Section":
            owner = n["props"].get("item")
            return keep(owner) if owner in nodes and nodes[owner]["kind"] != "Section" else None
        return keep(nid)

    projected = {nid: target(nid) for nid in nodes}
    weights: Counter = Counter()
    for e in graph.edges.values():
        if e["rel"] in STRUCTURAL or not shared(e, ceiling):
            continue
        a, b = projected.get(e["src"]), projected.get(e["dst"])
        if a and b and a != b:
            weights[(a, b) if a < b else (b, a)] += 1
    return dict(weights)


def compute(graph: Graph, policy: Policy, warm: Clusters | None = None) -> Clusters:
    """The clusters of ``graph``, started from ``warm``, the previous build's. Needs the extra (``available``)."""
    import graspologic_native as gn

    weights = project(graph, shared_ceiling(policy))
    if not weights:
        return Clusters()
    degree: Counter = Counter()
    for a, b in weights:
        degree[a] += 1
        degree[b] += 1
    hubs = _hubs(degree)
    edges = sorted(
        (a, b, c / math.sqrt(degree[a] * degree[b])) for (a, b), c in weights.items() if a not in hubs and b not in hubs
    )
    if not edges:
        return Clusters()
    in_edges = sorted({v for a, b, _ in edges for v in (a, b)})
    starting = _starting(in_edges, warm)
    tree = gn.hierarchical_leiden(
        edges=edges,
        starting_communities=starting,
        resolution=RESOLUTION,
        max_cluster_size=MAX_CLUSTER_SIZE,
        seed=SEED,
    )
    top: dict[str, int] = {}
    sub: dict[str, int] = {}
    for entry in tree:
        if entry.level == 0:
            top[entry.node] = entry.cluster
        elif entry.level == 1:
            sub[entry.node] = entry.cluster
    _join_hubs(hubs, weights, top, sub)

    groups: dict[int, set[str]] = defaultdict(set)
    for nid, c in top.items():
        groups[c].add(nid)
    top_ids = _name(list(groups.values()), _previous(warm, 1), 1)
    sub_groups: dict[int, set[str]] = defaultdict(set)
    for nid, c in sub.items():
        sub_groups[c].add(nid)
    sub_ids = _name(list(sub_groups.values()), _previous(warm, 2), 2)

    out = Clusters()
    member_top = {nid: top_ids[frozenset(groups[c])] for nid, c in top.items()}
    member_sub = {nid: sub_ids[frozenset(sub_groups[c])] for nid, c in sub.items()}
    out.members = {nid: (cid, member_sub.get(nid)) for nid, cid in sorted(member_top.items())}
    for level, assigned in ((1, member_top), (2, member_sub)):
        by_cluster: dict[str, list[str]] = defaultdict(list)
        for nid, cid in assigned.items():
            by_cluster[cid].append(nid)
        labels = _labels(by_cluster, graph)
        for cid, ids in sorted(by_cluster.items()):
            kinds = Counter(graph.nodes[nid]["kind"] for nid in ids)
            out.clusters[cid] = {
                "level": level,
                "parent": member_top[ids[0]] if level == 2 else None,
                "label": labels[cid],
                "size": len(ids),
                "kinds": dict(sorted(kinds.items())),
            }
        links: Counter = Counter()
        for (a, b), c in weights.items():
            ca, cb = assigned.get(a), assigned.get(b)
            if ca and cb and ca != cb:
                links[(ca, cb) if ca < cb else (cb, ca)] += c
        out.links.update({(level, a, b): w for (a, b), w in links.items()})
    return out


def coverage(found: Clusters) -> dict:
    """What a build's coverage says of its clusters."""
    levels = Counter(c["level"] for c in found.clusters.values())
    return {
        "computed": True,
        "engine": f"{ENGINE} {engine_version()}",
        "version": ALGORITHM_VERSION,
        "clusters": levels.get(1, 0),
        "subclusters": levels.get(2, 0),
        "members": len(found.members),
    }


def _normal(name: str | None) -> str:
    return " ".join((name or "").casefold().split())


def _hubs(degree: Counter) -> set[str]:
    """The HUB_SHARE most connected nodes, those above the degree of the next one; none in a small graph."""
    count = int(len(degree) * HUB_SHARE)
    if count == 0:
        return set()
    cut = sorted(degree.values(), reverse=True)[count]
    return {v for v, d in degree.items() if d > cut}


def _starting(in_edges: list[str], warm: Clusters | None) -> dict[str, int] | None:
    """The previous level-1 clusters as the community numbers ``hierarchical_leiden`` starts from: a node the previous
    build did not cluster starts alone."""
    if warm is None or not warm.members:
        return None
    previous = sorted({warm.members[nid][0] for nid in in_edges if nid in warm.members})
    number = {cid: i for i, cid in enumerate(previous)}
    out, fresh = {}, len(previous)
    for nid in in_edges:
        if nid in warm.members:
            out[nid] = number[warm.members[nid][0]]
        else:
            out[nid] = fresh
            fresh += 1
    return out


def _join_hubs(hubs: set[str], weights: dict, top: dict[str, int], sub: dict[str, int]) -> None:
    """Put each node left out as a hub into the cluster its edges reach most (the smallest number on a tie), and into
    the part of that cluster they reach most."""
    reach: dict[str, Counter] = defaultdict(Counter)
    for (a, b), c in weights.items():
        reach[a][b] += c
        reach[b][a] += c
    for hub in sorted(hubs):
        tops: Counter = Counter()
        for other, c in reach[hub].items():
            if other in top:
                tops[top[other]] += c
        if not tops:
            continue
        chosen = min(tops, key=lambda k: (-tops[k], k))
        top[hub] = chosen
        subs: Counter = Counter()
        for other, c in reach[hub].items():
            if top.get(other) == chosen and other in sub:
                subs[sub[other]] += c
        if subs:
            sub[hub] = min(subs, key=lambda k: (-subs[k], k))


def _previous(warm: Clusters | None, level: int) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    if warm is None:
        return out
    for nid, (top, sub) in warm.members.items():
        cid = top if level == 1 else sub
        if cid is not None:
            out[cid].add(nid)
    return out


def _name(groups: list[set[str]], previous: dict[str, set[str]], level: int) -> dict[frozenset, str]:
    """An id for each group: the id of the previous cluster it matches at Jaccard JACCARD or more, the best matches
    first and each id once; a digest of its level and members otherwise."""
    holders: dict[str, set[str]] = defaultdict(set)
    for cid, ids in previous.items():
        for nid in ids:
            holders[nid].add(cid)
    pairs = []
    for group in groups:
        key = frozenset(group)
        for cid in sorted({cid for nid in group for cid in holders.get(nid, ())}):
            old = previous[cid]
            score = len(group & old) / len(group | old)
            if score >= JACCARD:
                pairs.append((-score, cid, min(group), key))
    out: dict[frozenset, str] = {}
    used: set[str] = set()
    for _, cid, _, key in sorted(pairs, key=lambda p: (p[0], p[1], p[2])):
        if key not in out and cid not in used:
            out[key] = cid
            used.add(cid)
    for group in groups:
        key = frozenset(group)
        if key not in out:
            seed = "\x1f".join([str(level), *sorted(group)])
            cid = "c:" + sha256(seed)[7:19]
            while cid in used:  # a previous id the digest happens to repeat
                seed += "\x1f"
                cid = "c:" + sha256(seed)[7:19]
            out[key] = cid
            used.add(cid)
    return out


def _words(node: dict) -> list[str]:
    text = f"{node.get('name') or ''} {node['props'].get('path') or ''}".lower()
    return [t for t in re.findall(r"[^\W_]+", text) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS]


def _labels(by_cluster: dict[str, list[str]], graph: Graph) -> dict[str, str]:
    """The LABEL_WORDS words of each cluster with the highest c-TF-IDF against the others, joined by ", "."""
    docs = {cid: Counter(t for nid in ids for t in _words(graph.nodes[nid])) for cid, ids in by_cluster.items()}
    if not docs:
        return {}
    frequency = Counter(t for doc in docs.values() for t in doc)
    average = sum(sum(doc.values()) for doc in docs.values()) / len(docs)
    out = {}
    for cid, doc in docs.items():
        total = sum(doc.values()) or 1
        score = {t: n / total * math.log(1 + average / frequency[t]) for t, n in doc.items()}
        out[cid] = ", ".join(t for t, _ in sorted(score.items(), key=lambda x: (-x[1], x[0]))[:LABEL_WORDS])
    return out
