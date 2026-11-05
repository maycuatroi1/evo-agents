"""What the entities step (``evo_agents.kg.entities``) does with all the answers of a build at once: one node for the
names of one entity, an edge to the code an entity names, and an edge between two entities that mean the same.

- Folding (a2). Two names of one kind are one entity when they read the same once case, accents (đ as d), spaces and
  punctuation are folded (``key``), when one is the singular of the other (``singulars``: the last word only, so "Search
  Servers" is "Search Server"), or when the model gave one as an alias of the other. The node takes the name, and the
  id, of the most public item that stated it, so a reader who sees only a public item never reads the wording of a
  secret one in a node's name or id; ties go to the name stated most often, then the first by order.
- Code (a3). An entity whose name or alias names exactly one Symbol or File, looked up as ``locate`` looks names up (the
  id or the name, then for a dotted name its last two parts and its last part; ``code_key`` folds case and accents), gets
  an edge ``refers_to`` to it. A name that names more than one is not linked and coverage counts it. The edge is
  ``extracted``, so ``kg_impact``, which follows ``resolved`` and above, never walks it.
- Model merges (a2). ``group`` also folds the pairs a model said name one entity (``evo_agents.kg.entity_merge``), of
  one kind or two, the node taking its name and kind from the most public item as above.
- Synonyms (a2). When the project's graph has vectors (semantic ``index`` or ``on``), the names of two entities of one
  kind whose cosine under that model is at least the mapping's ``synonym_threshold`` get an edge ``synonym_of`` (from
  the smaller id), each entity at most SYNONYM_NEIGHBOURS. The pairs are memoized by the model, the threshold and the
  names, so a verification reads them and calls no model; the name vectors are kept in ``entity_vectors.sqlite`` next
  to the graph.

Every edge is stated by each item that stated one of its ends, so its label joins that item's with both nodes': it is
seen only by a reader who sees both ends.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.protocol import canonical_json, sha256

SYNONYM_REL = "synonym_of"
CODE_REL = "refers_to"
CODE_KINDS = ("Symbol", "File")
DEFAULT_SYNONYM_THRESHOLD = 0.8  # HippoRAG 2's synonymy_edge_sim_threshold
SYNONYM_NEIGHBOURS = 10
SYNONYM_VERSION = "1"
SYNONYM_STAGE = "entity_synonyms"
VECTOR_CACHE = "entity_vectors.sqlite"
CASE_CONF = 0.9  # of a code link whose name matches the code's only once case and accents are folded
MAX_KEY = 80
_WORD = re.compile(r"[a-z0-9]+")
_FOLD = str.maketrans({"đ": "d", "Đ": "D"})


def fold(text: str) -> str:
    """``text`` lower case, without accents, đ read as d."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_FOLD))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def key(name: str) -> str:
    """The words of ``name``, folded, joined by ``-``: what two names of one entity share."""
    return "-".join(_WORD.findall(fold(name)))[:MAX_KEY].strip("-")


def singulars(k: str) -> list[str]:
    """The keys ``k`` reads as when its last word is a plural: "servers" is "server", "entries" "entry", "boxes"
    "box"; a word ending in ss, us or is is no plural."""
    head, _, last = k.rpartition("-")
    if len(last) < 4 or not last.isalpha():
        return []
    out = []
    if last.endswith("ies"):
        out.append(last[:-3] + "y")
    if last.endswith("es"):
        out.append(last[:-2])
    if last.endswith("s") and not last.endswith(("ss", "us", "is")):
        out.append(last[:-1])
    return [f"{head}-{w}" if head else w for w in out]


def code_key(name: str) -> str:
    """A name as code is looked up by: folded, without backticks or a trailing ``()``."""
    text = fold(name).strip().strip("`").strip()
    return text[:-2] if text.endswith("()") else text


def entity_id(kind: str, k: str) -> str:
    return f"entity:{kind}:{k}"


@dataclass(frozen=True)
class Stated:
    """One entity one item stated: its kind, name, aliases, conf and how public the item is (lower is more)."""

    kind: str
    name: str
    aliases: tuple[str, ...]
    conf: float
    item: str
    rank: tuple = ()


@dataclass
class Groups:
    """The nodes the stated entities fold into."""

    of: dict[tuple[str, str], str] = field(default_factory=dict)  # (kind, key of a name or alias) -> node id
    name: dict[str, str] = field(default_factory=dict)  # node id -> its name
    kind: dict[str, str] = field(default_factory=dict)
    names: dict[str, list[str]] = field(default_factory=dict)  # node id -> every name and alias stated of it
    conf: dict[str, float] = field(default_factory=dict)
    merged: int = 0  # distinct names that became part of another's node

    def id_of(self, kind: str, name: str) -> str | None:
        return self.of.get((kind, key(name)))


def group(stated: list[Stated], joins=()) -> Groups:
    """Fold ``stated`` into nodes. The same answers give the same nodes, whatever order they come in. ``joins``, pairs
    of (kind, key) a model said name one entity (``evo_agents.kg.entity_merge``), fold their nodes into one, of
    whichever kind its name comes from."""
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for s in stated:
        k = (s.kind, key(s.name))
        find(k)
        for alias in s.aliases:
            if key(alias):
                union(k, (s.kind, key(alias)))
    for a, b in joins:
        if a in parent and b in parent:
            union(a, b)
    for kind, k in sorted(parent):
        for other in singulars(k):
            if (kind, other) in parent:
                union((kind, k), (kind, other))
    members: dict[tuple, list[Stated]] = defaultdict(list)
    for s in stated:
        members[find((s.kind, key(s.name)))].append(s)
    counts = Counter((s.kind, s.name) for s in stated)
    out = Groups()
    node_of_root = {}
    for root in sorted(members):
        ss = members[root]
        best = min(ss, key=lambda s: (s.rank, -counts[(s.kind, s.name)], s.name))
        nid = entity_id(best.kind, key(best.name))
        node_of_root[root] = nid
        out.name[nid] = best.name
        out.kind[nid] = best.kind
        out.names[nid] = sorted({s.name for s in ss} | {a for s in ss for a in s.aliases})
        out.conf[nid] = max(s.conf for s in ss)
        out.merged += len({s.name for s in ss}) - 1
    for k in parent:
        out.of[k] = node_of_root.get(find(k)) or ""
    out.of = {k: v for k, v in out.of.items() if v}
    return out


# -- code ---------------------------------------------------------------------------------------------------------


def code_index(facts_by_item: dict[str, list[dict]]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(code key -> ids of the Symbols and Files whose id or name it is, id -> name) of an item's map facts."""
    index: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    for facts in facts_by_item.values():
        for f in facts:
            if f.get("t") == "node" and f.get("kind") in CODE_KINDS:
                names[f["id"]] = f["name"]
                for text in (f["id"], f["name"]):
                    if isinstance(text, str) and text:
                        index[code_key(text)].add(f["id"])
    return {k: sorted(v) for k, v in index.items()}, names


def code_link(names: list[str], index: dict[str, list[str]]) -> tuple[str | None, str | None, bool]:
    """(code node id, the name that found it, ambiguous): the one Symbol or File ``names`` name. Each name is looked up
    as ``locate`` does, whole, then by the tails of a dotted name, the first form that finds anything deciding; a form
    that finds more than one node, or two names that find two nodes, link nothing and say ambiguous."""
    found: dict[str, str] = {}
    ambiguous = False
    for name in sorted(names):
        forms = [name]
        if "." in name:
            parts = name.split(".")
            forms += [".".join(parts[-2:]), parts[-1]]
        for form in dict.fromkeys(forms):
            ids = index.get(code_key(form)) or []
            if len(ids) == 1:
                found.setdefault(ids[0], name)
                break
            if len(ids) > 1:
                ambiguous = True
                break
    if len(found) == 1:
        ((nid, name),) = found.items()
        return nid, name, False
    return None, None, ambiguous or len(found) > 1


# -- synonyms -----------------------------------------------------------------------------------------------------


class _Offline(Exception):
    pass


def synonyms(
    groups: Groups, settings, memo, cache_path: Path | None, threshold: float, *, offline: bool = False
) -> tuple[list[list], str]:
    """([smaller id, larger id, cosine], status) of the entities of one kind whose names are near; ``settings`` the
    project's semantic settings (None or off: the graph has no vector, no pair)."""
    if settings is None or not settings.indexes:
        return [], "off: the graph has no vectors"
    items = sorted((groups.kind[nid], nid, groups.name[nid]) for nid in groups.name)
    if len(items) < 2:
        return [], "ok"
    inputs = {
        "model": settings.model,
        "threshold": repr(float(threshold)),
        "names": sha256(canonical_json(items)),
    }

    def task():
        if offline:
            raise _Offline
        return _pairs(items, settings.model, cache_path, float(threshold))

    from evo_agents.kg.semantic import SemanticUnavailable

    try:
        pairs, _ = memo.run(SYNONYM_STAGE, SYNONYM_VERSION, inputs, task)
    except _Offline:
        return [], "offline: the memo holds no pairs for these names"
    except SemanticUnavailable as exc:
        return [], f"unavailable: {str(exc)[:200]}"
    return pairs, "ok"


def _pairs(items: list[tuple[str, str, str]], model: str, cache_path: Path | None, threshold: float) -> list[list]:
    from evo_agents.kg import semantic

    np = semantic._numpy()
    texts = [name for _, _, name in items]
    digests = [semantic.text_digest(model, t) for t in texts]
    cache = semantic.Cache(cache_path) if cache_path is not None else None
    try:
        held = cache.get(model, sorted(set(digests))) if cache else {}
        todo = sorted({d: t for d, t in zip(digests, texts, strict=True) if d not in held}.items())
        if todo:
            embedder = semantic.embedder(model)
            for start in range(0, len(todo), semantic.BATCH * 8):
                part = todo[start : start + semantic.BATCH * 8]
                vectors = embedder.embed([t for _, t in part])
                fresh = [
                    (d, np.asarray(v, dtype=np.float32).tobytes()) for (d, _), v in zip(part, vectors, strict=True)
                ]
                held.update(fresh)
                if cache:
                    cache.put(model, fresh)
    finally:
        if cache:
            cache.close()
    out: list[list] = []
    by_kind: dict[str, list[int]] = defaultdict(list)
    for i, (kind, _, _) in enumerate(items):
        by_kind[kind].append(i)
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        if len(rows) < 2:
            continue
        m = np.stack([_vector(np, held[digests[i]]) for i in rows])
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        m = m / np.where(norms == 0, 1.0, norms)
        near: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for start in range(0, len(rows), 512):
            scores = m[start : start + 512] @ m.T
            for a in range(scores.shape[0]):
                i = start + a
                for j in np.nonzero(scores[a] >= threshold)[0].tolist():
                    if j != i:
                        near[i].append((round(float(scores[a][j]), 4), j))
        chosen: set[tuple[int, int]] = set()
        for i, found in near.items():
            for _cos, j in sorted(found, key=lambda x: (-x[0], x[1]))[:SYNONYM_NEIGHBOURS]:
                chosen.add((min(i, j), max(i, j)))
        for i, j in sorted(chosen):
            a, b = items[rows[i]][1], items[rows[j]][1]
            cos = round(float(m[i] @ m[j]), 4)
            out.append([min(a, b), max(a, b), cos])
    return sorted(out)


def _vector(np, data: bytes):
    return np.frombuffer(data, dtype=np.float32)


__all__ = ["CODE_REL", "SYNONYM_REL", "Groups", "Stated", "code_index", "code_link", "group", "key", "synonyms"]
