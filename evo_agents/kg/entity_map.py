"""The entity map an agent wrote of a project, read by the build of strategy ``entity-graph-agent`` instead of a model
(docs/kg-strategies.md, "The entity-graph-agent strategy").

A run of the Curator reads the project's documents, plans and code itself and posts what it found as one JSON object
of FORMAT; the hub checks it against its latest build and keeps it (``evo_agents.hub.kg_entity_maps``), and every
build of the project reads all the maps it kept (``read_maps``, the files of MAPS_DIR under the project's root). The
build calls no model:

    {"format": "evo-entity-map/1", "model": "claude-opus-5-5", "run": 1969,
     "entities": [{"kind": "Component", "name": "Entity relay", "type": null, "description": "...",
                   "aliases": ["relay"], "conf": 0.9,
                   "mentions": [{"node": "<Document, Section, Plan or PlanStep id>", "hash": "<the item's hash read>",
                                 "quote": "...", "line": 12}],
                   "code": [{"node": "<Directory, File or Symbol id>", "reason": "...", "conf": 0.8}]}],
     "relations": [{"src": "Entity relay", "dst": "Build", "keywords": "...", "description": "...", "strength": 7,
                    "mentions": [...]}]}

What the build states of it (``run``), every node and edge ``extracted``, so kg_impact never walks them:

- one node ``entity:KIND:slug`` an entity, its names folded as ``entity_links.group`` folds them (so several maps, or
  a map and its later parts, name one node), with its description, aliases and ``type``: a kind outside the strategy's
  node kinds is OTHER, its own word in ``props.type``. Its label is the join of the labels of the items it cites;
- an edge ``mentions`` from each Document, Section, Plan or PlanStep cited to the entity, with the quote and line; a
  citation whose ``hash`` is not the hash the item has now is ``stale`` (the item changed since the agent read it);
- an edge ``relates_to`` between two entities of the map, keywords, description and strength, its ends in order;
- an edge IMPLEMENTED_BY from the entity to each Directory, File or Symbol it names, with the reason, conf, run and
  model; its label joins the entity's items with the code's;
- an edge ``refers_to`` when the entity's names name one Symbol or File, as the entities step links names
  (``entity_links.code_link``).

``check`` is the one rule of what a map may cite, used by the hub when it receives a map (the nodes the run's sink
reads) and by the build (the nodes of the build): an entry that cites a node that is not there, or not of the kinds
it may be, is dropped with a reason and the rest is kept. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from evo_agents.kg import entity_links as links

FORMAT = "evo-entity-map/1"
STATUS = "extracted"
IMPLEMENTED_BY = "implemented_by"
RELATES_TO = "relates_to"
OTHER = "Other"
MAPS_DIR = "entity-maps"  # under the project's root: one file per map the hub kept, ``<sha256>.json``
MENTION_KINDS = ("Document", "Section", "Plan", "PlanStep")  # what an entity or a relation may cite
CODE_KINDS = ("Directory", "File", "Symbol")  # what an entity may be implemented by
DEFAULT_CONF = 0.8  # of an entity or a code link that gives none
MAX_BYTES = 8 * 1024 * 1024  # one map
MAX_ENTITIES = 5000
MAX_RELATIONS = 20000
MAX_MENTIONS = 50  # of one entity or relation
MAX_CODE = 50  # code links of one entity
MAX_NAME = 200
MAX_DESCRIPTION = 1200
MAX_QUOTE = 300
MAX_REASON = 600
MAX_ALIASES = 10
SAMPLES = 20

Lookup = Callable[[str], "tuple[str, str] | None"]  # a node id -> (its stored id, its kind), None when not there


class EntityMapError(ValueError):
    """A map that is not of FORMAT at all; an entry that is wrong is rejected alone (``parse``, ``check``)."""


def _text(value, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _unit(value, default: float) -> float:
    try:
        return round(min(max(float(value), 0.0), 1.0), 4)
    except (TypeError, ValueError):
        return default


def _mentions(raw, where: str, rejected: list) -> list[dict]:
    found = []
    for i, m in enumerate(raw if isinstance(raw, list) else []):
        node = _text(m.get("node") or m.get("item"), 400) if isinstance(m, dict) else None
        if node is None:
            rejected.append({"entry": f"{where}.mentions[{i}]", "reason": "a mention names no node"})
            continue
        kept = {"node": node}
        for key, limit in (("hash", 200), ("quote", MAX_QUOTE)):
            if _text(m.get(key), limit):
                kept[key] = _text(m.get(key), limit)
        if isinstance(m.get("line"), int) and not isinstance(m.get("line"), bool) and m["line"] >= 1:
            kept["line"] = m["line"]
        found.append(kept)
    return found[:MAX_MENTIONS]


def _code(raw, where: str, rejected: list) -> list[dict]:
    found = []
    for i, c in enumerate(raw if isinstance(raw, list) else []):
        node = _text(c.get("node"), 400) if isinstance(c, dict) else None
        if node is None:
            rejected.append({"entry": f"{where}.code[{i}]", "reason": "a code link names no node"})
            continue
        found.append(
            {
                "node": node,
                "reason": _text(c.get("reason"), MAX_REASON) or "",
                "conf": _unit(c.get("conf"), DEFAULT_CONF),
            }
        )
    return found[:MAX_CODE]


def parse(data) -> tuple[dict, list[dict]]:
    """(the map ``data`` holds, its values cut to what the build reads; the entries rejected, each ``{"entry",
    "reason"}``). ``data`` is the object or its JSON bytes. EntityMapError when it is not a map of FORMAT."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            raise EntityMapError("the entity map is not JSON") from None
    if not isinstance(data, dict):
        raise EntityMapError("the entity map is not an object")
    if data.get("format") != FORMAT:
        raise EntityMapError(f"the entity map's format is {data.get('format')!r}, not {FORMAT!r}")
    raw_entities, raw_relations = data.get("entities") or [], data.get("relations") or []
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        raise EntityMapError("entities and relations are lists")
    if len(raw_entities) > MAX_ENTITIES or len(raw_relations) > MAX_RELATIONS:
        raise EntityMapError(f"a map holds at most {MAX_ENTITIES} entities and {MAX_RELATIONS} relations: post parts")
    rejected: list[dict] = []
    entities = []
    for i, e in enumerate(raw_entities):
        where = f"entities[{i}]"
        name = _text(e.get("name"), MAX_NAME) if isinstance(e, dict) else None
        if name is None or not links.key(name):
            rejected.append({"entry": where, "reason": "an entity needs a name"})
            continue
        kept = {"kind": _text(e.get("kind"), 80) or OTHER, "name": name, "conf": _unit(e.get("conf"), DEFAULT_CONF)}
        for key, limit in (("type", 80), ("description", MAX_DESCRIPTION)):
            if _text(e.get(key), limit):
                kept[key] = _text(e.get(key), limit)
        aliases = e.get("aliases") if isinstance(e.get("aliases"), list) else []
        own = links.key(name)
        kept["aliases"] = sorted(
            {a.strip()[:MAX_NAME] for a in aliases if isinstance(a, str) and links.key(a) not in ("", own)}
        )[:MAX_ALIASES]
        kept["mentions"] = _mentions(e.get("mentions"), where, rejected)
        kept["code"] = _code(e.get("code"), where, rejected)
        entities.append(kept)
    relations = []
    for i, r in enumerate(raw_relations):
        where = f"relations[{i}]"
        src = _text(r.get("src"), MAX_NAME) if isinstance(r, dict) else None
        dst = _text(r.get("dst"), MAX_NAME) if isinstance(r, dict) else None
        if src is None or dst is None:
            rejected.append({"entry": where, "reason": "a relation names its two entities, src and dst"})
            continue
        kept = {"src": src, "dst": dst}
        for key, limit in (("keywords", 200), ("description", MAX_DESCRIPTION)):
            words = r.get(key)
            words = ", ".join(w for w in words if isinstance(w, str)) if isinstance(words, list) else words
            if _text(words, limit):
                kept[key] = _text(words, limit)
        try:
            kept["strength"] = min(max(float(r.get("strength")), 0.0), 10.0)
        except (TypeError, ValueError):
            kept["strength"] = 1.0
        kept["mentions"] = _mentions(r.get("mentions"), where, rejected)
        relations.append(kept)
    found = {"format": FORMAT, "model": _text(data.get("model"), 120), "entities": entities, "relations": relations}
    run = data.get("run")
    found["run"] = run if isinstance(run, int) and not isinstance(run, bool) else None
    return found, rejected


def check(found: dict, lookup: Lookup) -> tuple[dict, list[dict]]:
    """(``found``, a map ``parse`` read, with only what cites nodes ``lookup`` finds, of the kinds each may be, each
    node by its stored id; the entries dropped, with their reason). A mention or a code link is dropped alone; an
    entity left citing nothing, and a relation whose end is not an entity kept, are dropped whole."""
    rejected: list[dict] = []
    cache: dict[str, tuple[str, str] | None] = {}

    def look(node: str):
        if node not in cache:
            cache[node] = lookup(node)
        return cache[node]

    def cited(mentions: list[dict], where: str) -> list[dict]:
        kept = []
        for i, m in enumerate(mentions):
            hit = look(m["node"])
            if hit is None:
                why = f"{m['node']} is not in the latest build as this run reads it"
            elif hit[1] not in MENTION_KINDS:
                why = f"{m['node']} is a {hit[1]}: a mention cites a {', '.join(MENTION_KINDS)}"
            else:
                kept.append({**m, "node": hit[0]})
                continue
            rejected.append({"entry": f"{where}.mentions[{i}]", "reason": why})
        return kept

    entities, names = [], {}
    for i, e in enumerate(found["entities"]):
        where = f"entities[{i}] ({e['name']})"
        mentions = cited(e["mentions"], where)
        code = []
        for j, c in enumerate(e["code"]):
            hit = look(c["node"])
            if hit is None:
                why = f"{c['node']} is not in the latest build as this run reads it"
            elif hit[1] not in CODE_KINDS:
                why = f"{c['node']} is a {hit[1]}: an entity is implemented by a {', '.join(CODE_KINDS)}"
            else:
                code.append({**c, "node": hit[0], "level": hit[1]})
                continue
            rejected.append({"entry": f"{where}.code[{j}]", "reason": why})
        if not mentions and not code:
            rejected.append({"entry": where, "reason": "the entity cites no item and no code that is kept"})
            continue
        entities.append({**e, "mentions": mentions, "code": code})
        for name in (e["name"], *e["aliases"]):
            names.setdefault(links.key(name), e["name"])
    relations = []
    for i, r in enumerate(found["relations"]):
        where = f"relations[{i}] ({r['src']} - {r['dst']})"
        src, dst = names.get(links.key(r["src"])), names.get(links.key(r["dst"]))
        if src is None or dst is None or links.key(src) == links.key(dst):
            rejected.append(
                {"entry": where, "reason": "a relation joins two different entities of the map that are kept"}
            )
            continue
        relations.append({**r, "src": src, "dst": dst, "mentions": cited(r["mentions"], where)})
    return {**found, "entities": entities, "relations": relations}, rejected


def encode(found: dict) -> tuple[bytes, str]:
    """A map as the hub keeps it: its canonical JSON and the SHA-256 of that."""
    data = json.dumps(found, ensure_ascii=False, sort_keys=True).encode()
    return data, hashlib.sha256(data).hexdigest()


def maps_dir(root: Path) -> Path:
    return root / MAPS_DIR


def map_files(root: Path) -> list[Path]:
    """The maps kept under the project's ``root``, in the order they are read."""
    found = maps_dir(root)
    return sorted(found.glob("*.json")) if found.is_dir() else []


def digest(root: Path) -> list[str] | None:
    """The SHA-256 of each map under ``root`` (``build_inputs``), None when there is none."""
    files = map_files(root)
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in files] or None


def read_maps(root: Path) -> tuple[list[dict], list[str]]:
    """(the maps under ``root`` that read, the errors of those that do not, by file name)."""
    maps, errors = [], []
    for path in map_files(root):
        try:
            maps.append(parse(path.read_bytes())[0])
        except (OSError, EntityMapError) as exc:
            errors.append(f"{path.name}: {exc}")
    return maps, errors


def kind_of(said: str, node_kinds: list[str], given_type: str | None = None) -> tuple[str, str | None]:
    """The node kind of an entity said to be of ``said``, with its own type when that is OTHER."""
    folded = links.key(said).replace("-", "")
    for k in node_kinds:
        if links.key(k).replace("-", "") == folded:
            return k, given_type if k == OTHER else None
    return OTHER, given_type or said


# -- the build ----------------------------------------------------------------------------------------------------


def node_index(first: dict[str, list[dict]]) -> dict[str, tuple[str, str]]:
    """node id -> (its kind, the first item, in order, whose map facts state it)."""
    index: dict[str, tuple[str, str]] = {}
    for item in sorted(first):
        for f in first[item]:
            if f.get("t") == "node" and f["id"] not in index:
                index[f["id"]] = (f["kind"], item)
    return index


def run(maps: list[dict], records, unit_labels: dict, first: dict[str, list[dict]], node_kinds: list[str]):
    """The facts of ``maps`` by item and the coverage of the step. ``records`` the corpus state, ``unit_labels`` the
    label of each unit, ``first`` the map stage's facts by item (the nodes a map may cite), ``node_kinds`` the kinds an
    entity may be (the strategy's that the ontology holds)."""
    from evo_agents.kg import refs
    from evo_agents.kg.pipeline.stages import edge, item_unit, node

    index = node_index(first)
    hashes = {rec.item_id: rec.record.get("hash") for rec in records}
    cov = _coverage(maps)

    def lookup(ref: str):
        for candidate in refs.stored(ref):
            if candidate in index:
                return candidate, index[candidate][0]
        return None

    checked = []
    for m in maps:
        kept, rejected = check(m, lookup)
        checked.append(kept)
        cov["rejected"] += len(rejected)
        cov["rejected_samples"].extend(rejected[: SAMPLES - len(cov["rejected_samples"])])
    stated, said = [], []
    for m in checked:
        for e in m["entities"]:
            kind, own_type = kind_of(e["kind"], node_kinds, e.get("type"))
            items = sorted({index[x["node"]][1] for x in e["mentions"]}) or sorted(
                {index[c["node"]][1] for c in e["code"]}
            )
            rank = min((_rank(unit_labels[item_unit(i)]) for i in items), default=())
            stated.append(links.Stated(kind, e["name"], tuple(e["aliases"]), e["conf"], items[0], rank))
            said.append((m, e, kind, own_type, items))
    groups = links.group(stated)
    code_index, code_names = links.code_index(first)
    out: dict[str, list[dict]] = defaultdict(list)
    units_of: dict[tuple[int, str], list[str]] = {}  # (map, entity id) -> the units of its node
    levels: dict[str, int] = defaultdict(int)
    cited: set[str] = set()
    for m, e, kind, own_type, items in said:
        nid = groups.id_of(kind, e["name"])
        units = [item_unit(i) for i in items]
        units_of[(id(m), nid)] = units
        evidence = {"model": m.get("model"), "run": m.get("run")}
        props = {**evidence, **({"description": e["description"]} if e.get("description") else {})}
        if own_type:
            props["type"] = own_type
        if e["aliases"]:
            props["aliases"] = e["aliases"]
        anchor = items[0]
        out[anchor].append(
            node(nid, groups.kind[nid], groups.name[nid], units, props=props, status=STATUS, conf=e["conf"])
        )
        for x in e["mentions"]:
            item = index[x["node"]][1]
            cited.add(item)
            p = {**evidence, **({"evidence": x["quote"]} if x.get("quote") else {})}
            if x.get("line"):
                p["line"] = x["line"]
            if x.get("hash") and hashes.get(item) and x["hash"] != hashes[item]:
                p["stale"] = True
                cov["stale"] += 1
                if item not in cov["stale_items"] and len(cov["stale_items"]) < SAMPLES:
                    cov["stale_items"].append(item)
            cov["mentions"] += 1
            out[item].append(
                edge(x["node"], "mentions", nid, [item_unit(item)], props=p, status=STATUS, conf=e["conf"])
            )
        for c in e["code"]:
            levels[c["level"]] += 1
            p = {**evidence, "reason": c["reason"], "conf": c["conf"], "level": c["level"]}
            out[anchor].append(edge(nid, IMPLEMENTED_BY, c["node"], units, props=p, status=STATUS, conf=c["conf"]))
    relations = set()
    for m in checked:
        for r in m["relations"]:
            ends = []
            for name in (r["src"], r["dst"]):
                e = next(x for x in m["entities"] if links.key(x["name"]) == links.key(name))
                ends.append(groups.id_of(kind_of(e["kind"], node_kinds, e.get("type"))[0], e["name"]))
            src, dst = sorted(ends)
            if src == dst:
                continue
            own = sorted({index[x["node"]][1] for x in r["mentions"]})
            cited.update(own)
            units = [item_unit(i) for i in own] or sorted({*units_of[(id(m), src)], *units_of[(id(m), dst)]})
            p = {"model": m.get("model"), "run": m.get("run"), "strength": r["strength"]}
            p.update({k: r[k] for k in ("keywords", "description") if r.get(k)})
            quoted = [x for x in r["mentions"] if x.get("quote")]
            if quoted:
                p["evidence"] = quoted[0]["quote"]
                if quoted[0].get("line"):
                    p["line"] = quoted[0]["line"]
            relations.add((src, dst))
            anchor = units[0].removesuffix("#")
            out[anchor].append(edge(src, RELATES_TO, dst, units, props=p, status=STATUS, conf=DEFAULT_CONF))
    named, ambiguous = 0, 0
    for nid in sorted(groups.name):
        target, name, unclear = links.code_link(groups.names[nid], code_index)
        ambiguous += unclear
        if target is None:
            continue
        named += 1
        exact = name.strip().strip("`").removesuffix("()") in (code_names.get(target), target)
        conf = round(groups.conf[nid] * (1.0 if exact else links.CASE_CONF), 4)
        units = sorted({u for (_, n), us in units_of.items() if n == nid for u in us})
        p = {"name": name, "models": sorted({str(m.get("model")) for m in checked if m.get("model")})}
        out[units[0].removesuffix("#")].append(
            edge(nid, links.CODE_REL, target, units, props=p, status=STATUS, conf=conf)
        )
    citable = sorted(item for nid, (kind, item) in index.items() if nid == item and kind in MENTION_KINDS)
    uncited = [i for i in citable if i not in cited]
    cov.update(
        nodes=len(groups.name),
        distinct=len(groups.name),
        merged=groups.merged,
        edges=len(relations),
        relations=len(relations),
        code_links={k: levels.get(k, 0) for k in CODE_KINDS},
        name_links=named,
        code_ambiguous=ambiguous,
        cited_items=len([i for i in citable if i in cited]),
        uncited_items=len(uncited),
        uncited_samples=uncited[:SAMPLES],
    )
    return dict(out), cov


def _rank(label) -> tuple:
    """How public a label is, lower more (``entities._rank``): what picks the name of a node several maps state."""
    return (label.level, label.location, len(label.audience))


def _coverage(maps: list[dict]) -> dict:
    return {
        "enabled": True,
        "style": "agent",
        "model": ", ".join(sorted({str(m["model"]) for m in maps if m.get("model")})) or None,
        "maps": len(maps),
        "runs": sorted({m["run"] for m in maps if isinstance(m.get("run"), int)}),
        "mentions": 0,
        "stale": 0,
        "stale_items": [],
        "rejected": 0,
        "rejected_samples": [],
    }


__all__ = ["FORMAT", "IMPLEMENTED_BY", "EntityMapError", "check", "encode", "parse", "read_maps", "run"]
