"""The rules a project's ontology.yaml states about its graph, and a check of a snapshot (``evo_agents.kg.snapshot``)
against them. A relation entry may give ``domain`` and ``range`` (the kinds of its source and its target),
``cardinality`` (one_to_one, many_to_one, one_to_many, many_to_many) and ``acyclic: true``; a node kind entry may
give ``keys`` (properties, or lists of them, whose values no two nodes of the kind share) and ``required_props``. A
plain list of names states no rule, and a hub kind or relation may be named only to add rules to it
(``evo_agents.kg.schema.validate_ontology``).

``check`` finds, over every live edge and node of a snapshot:

- ``wrong_endpoint``: an edge whose source kind is not in its relation's domain or whose target kind not in its range;
- ``cardinality``: a source with several targets of a many_to_one (or one_to_one) relation, or a target with several
  sources of a one_to_many (or one_to_one) one;
- ``cycle``: the nodes of a cycle of an acyclic relation, one violation per strongly connected component;
- ``duplicate_key``: nodes of one kind with the same values of one of its keys;
- ``missing_prop``: a node without a property its kind requires.

A violation says its rule, a stable key, the nodes and edges it is about and what it found; ``review_items`` turns
the violations into items of the review queue (``evo_agents.kg.review``), fingerprinted by rule and key so a
dismissal holds across builds, and a check of a draft graph reads the same list. The five rules are not yet among
``evo_agents.kg.review.RULES``: a build writes them to the queue only once the hub's Rule list, its dismissal check
(a migration, as 0199 did for conflicting_claim) and the web's REVIEW_RULES name them."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from evo_agents.kg.review import EVIDENCE, fingerprint
from evo_agents.kg.schema import entry_name

ONE_SOURCE = ("one_to_one", "one_to_many")  # a target has at most one source
ONE_TARGET = ("one_to_one", "many_to_one")  # a source has at most one target
SAMPLES = 5
ACTIONS = {  # slugs, as evo_agents.kg.review.ACTIONS gives its rules'
    "wrong_endpoint": "fix_endpoint",
    "cardinality": "keep_one",
    "cycle": "break_cycle",
    "duplicate_key": "merge",
    "missing_prop": "set_prop",
}


@dataclass(frozen=True)
class RelationRule:
    name: str
    domain: frozenset[str] | None = None
    range: frozenset[str] | None = None
    cardinality: str = "many_to_many"
    acyclic: bool = False


@dataclass(frozen=True)
class KindRule:
    name: str
    keys: tuple[tuple[str, ...], ...] = ()
    required_props: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rules:
    relations: dict[str, RelationRule] = field(default_factory=dict)
    kinds: dict[str, KindRule] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.relations or self.kinds)


@dataclass(frozen=True)
class Violation:
    rule: str
    key: str  # what it is about, stable across builds
    nodes: tuple[str, ...]
    edges: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)


def _kinds(value) -> frozenset[str] | None:
    return frozenset(value) if isinstance(value, list) and value else None


def _relation_rule(entry: dict, name: str) -> RelationRule | None:
    rule = RelationRule(
        name,
        _kinds(entry.get("domain")),
        _kinds(entry.get("range")),
        entry.get("cardinality") or "many_to_many",
        entry.get("acyclic") is True,
    )
    return rule if rule != RelationRule(name) else None


def _kind_rule(entry: dict, name: str) -> KindRule | None:
    keys = tuple((k,) if isinstance(k, str) else tuple(k) for k in entry.get("keys") or [] if k)
    rule = KindRule(name, keys, tuple(entry.get("required_props") or []))
    return rule if rule != KindRule(name) else None


def _entries(ontology: dict | None, key: str) -> list[tuple[str, dict]]:
    """The mapping entries of ``key`` with their names: the only ones that can state a rule."""
    entries = (ontology or {}).get(key) or []
    named = [(entry_name(e), e) for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict)]
    return [(name, e) for name, e in named if name]


def rules_of(ontology: dict | None) -> Rules:
    """The rules of an ontology the schema validated (``validate_ontology``); none for a list of names."""
    relations: dict[str, RelationRule] = {}
    kinds: dict[str, KindRule] = {}
    for name, entry in _entries(ontology, "relations"):
        if (rule := _relation_rule(entry, name)) is not None:
            relations[name] = rule
    for name, entry in _entries(ontology, "node_kinds"):
        if (kind := _kind_rule(entry, name)) is not None:
            kinds[name] = kind
    return Rules(relations, kinds)


# -- the check -------------------------------------------------------------------------------------------------------


def _endpoints(snap, rule: RelationRule, edges: list[int]) -> list[Violation]:
    out = []
    for e in edges:
        edge = snap.edges[e]
        src, dst = snap.nodes[snap.src[e]], snap.nodes[snap.dst[e]]
        bad = [
            (end, node["kind"])
            for end, node, allowed in (("source", src, rule.domain), ("target", dst, rule.range))
            if allowed is not None and node["kind"] not in allowed
        ]
        if bad:
            detail = {"relation": rule.name, "wrong": [{"end": end, "kind": kind} for end, kind in bad]}
            out.append(Violation("wrong_endpoint", edge["id"], (src["id"], dst["id"]), (edge["id"],), detail))
    return out


def _cardinality(snap, rule: RelationRule, edges: list[int]) -> list[Violation]:
    out = []
    for one, by, other, end in ((ONE_TARGET, snap.src, snap.dst, "source"), (ONE_SOURCE, snap.dst, snap.src, "target")):
        if rule.cardinality not in one:
            continue
        partners: dict[int, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
        for e in edges:
            partners[by[e]][other[e]].append(snap.edges[e]["id"])
        for node, held in sorted(partners.items()):
            if len(held) > 1:
                nid = snap.ids[node]
                ids = [nid, *sorted(snap.ids[o] for o in held)]
                eids = tuple(sorted(eid for found in held.values() for eid in found))
                detail = {"relation": rule.name, "cardinality": rule.cardinality, "end": end, "count": len(held)}
                out.append(Violation("cardinality", f"{rule.name}\x1f{end}\x1f{nid}", tuple(ids), eids, detail))
    return out


def _components(nodes: list[int], succ: dict[int, list[int]]) -> list[list[int]]:
    """The strongly connected components of a graph (iterative Tarjan), each with more than one node or a loop."""
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    on: set[int] = set()
    found: list[list[int]] = []
    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = len(index)
                stack.append(v)
                on.add(v)
            nexts = succ.get(v, [])
            if i < len(nexts):
                work.append((v, i + 1))
                w = nexts[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                part = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    part.append(w)
                    if w == v:
                        break
                if len(part) > 1 or v in succ.get(v, []):
                    found.append(sorted(part))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return found


def _cycles(snap, rule: RelationRule, edges: list[int]) -> list[Violation]:
    succ: dict[int, list[int]] = defaultdict(list)
    for e in edges:
        succ[snap.src[e]].append(snap.dst[e])
    out = []
    for part in _components(sorted(succ), succ):
        members = set(part)
        ids = tuple(sorted(snap.ids[n] for n in part))
        eids = tuple(sorted(snap.edges[e]["id"] for e in edges if snap.src[e] in members and snap.dst[e] in members))
        detail = {"relation": rule.name, "count": len(ids)}
        out.append(Violation("cycle", f"{rule.name}\x1f{ids[0]}", ids, eids, detail))
    return out


def _value(props: dict, key: tuple[str, ...]) -> str | None:
    values = [props.get(k) for k in key]
    return None if any(v is None or v == "" for v in values) else json.dumps(values, sort_keys=True, ensure_ascii=False)


def _kind_violations(rule: KindRule, nodes: list[dict]) -> list[Violation]:
    out = []
    for key in rule.keys:
        held: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            if (value := _value(n["props"], key)) is not None:
                held[value].append(n["id"])
        for value, ids in sorted(held.items()):
            if len(ids) > 1:
                detail = {"kind": rule.name, "key": list(key), "value": value, "count": len(ids)}
                ids = sorted(ids)
                out.append(
                    Violation("duplicate_key", f"{rule.name}\x1f{','.join(key)}\x1f{value}", tuple(ids), (), detail)
                )
    for n in nodes:
        missing = [p for p in rule.required_props if n["props"].get(p) in (None, "")]
        if missing:
            detail = {"kind": rule.name, "missing": missing}
            out.append(Violation("missing_prop", f"{n['id']}\x1f{','.join(missing)}", (n["id"],), (), detail))
    return out


def check(snap, rules: Rules, statuses: set[str] | None = None) -> list[Violation]:
    """Every violation of ``rules`` by the live nodes and edges of ``snap``, edges of ``statuses`` only when given
    (all of them, proposed ones too, by default), by rule then key."""
    by_rel: dict[str, list[int]] = defaultdict(list)
    for (rel, status), part in snap.partitions.items():
        if rel in rules.relations and (statuses is None or status in statuses):
            by_rel[rel].extend(sorted(set(part.out_edges)))
    found: list[Violation] = []
    for rel, edges in sorted(by_rel.items()):
        rule, edges = rules.relations[rel], sorted(set(edges))
        found += _endpoints(snap, rule, edges) + _cardinality(snap, rule, edges)
        if rule.acyclic:
            found += _cycles(snap, rule, edges)
    of_kind: dict[str, list[dict]] = defaultdict(list)
    for n in snap.nodes:
        if n["kind"] in rules.kinds:
            of_kind[n["kind"]].append(n)
    for kind, nodes in sorted(of_kind.items()):
        found += _kind_violations(rules.kinds[kind], nodes)
    return sorted(found, key=lambda v: (v.rule, v.key))


def review_items(violations: list[Violation], cluster_of=None) -> list[dict]:
    """The violations as items of the review queue (``evo_agents.kg.review``): fingerprint, rule, the level-1 cluster
    of its first node, evidence, clusters, action and detail. ``cluster_of`` gives a node's level-1 cluster, if any."""
    items = []
    for v in violations:
        kept = list(v.nodes[:EVIDENCE])
        clusters = [cluster_of(nid) if cluster_of else None for nid in kept]
        detail = {"count": len(v.nodes), **v.detail, "edges": list(v.edges[:SAMPLES]), "required": 1, "least": 1}
        items.append(
            {
                "fingerprint": fingerprint(v.rule, v.key),
                "rule": v.rule,
                "cluster": next((c for c in clusters if c), None),
                "evidence": kept,
                "clusters": clusters,
                "action": ACTIONS[v.rule],
                "detail": detail,
            }
        )
    return items
