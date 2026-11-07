"""The run that maps a project's entities to its code (plan kg-entities-to-code, criterion a3), without the database.
``evo_agents.hub.server.kg_entity_map_run`` holds the rows, the routes and the job that moves the runs on.

A project whose strategy is entity-graph-agent gets its entities from the maps an agent posts
(``evo_agents.kg.entity_map``). The hub queues a review run with lens ``kg_entity_map`` on the project's default agent
pool (``kg_init.queue_repoless``: the runtime the charter names for the Reviewer, claude-code by default, on the pool's
own login, under the charter's review cost cap or ``kg_init.HUB_CAP_USD``) when the first successful build under that
strategy is recorded, when a writer asks (``evo-agents hub kg entity-map --run``, POST /v1/kg/{project}/entity-map/run),
and when a build finds too many of the map's citations stale or too many items no map cites (``wants_run``). One run of
a project is in flight at a time.

Its prompt holds counts and classes of data only (``prompt``). The agent asks the hub for the rest through the sink of
the run, as it reads the graph: the structure of the project (``outline``: the repos and their directories with the
files and symbols under each, the clusters that hold code, the entities the graph has, the documents and the plans) and
what is new or changed since the map before (``changed``: the items whose revision the snapshot of the last run that
posted a map lacks or holds otherwise, and the citable nodes no map cites). It reads the documents, plans and code it
needs through the kg_* tools or the run's checkout, picks for each code link the level (Directory, File or Symbol) that
fits, hands areas to subagents when the uncited items are many, and posts the map in parts as it goes (POST
/v1/kg/{project}/entity-map). The prompt tells it the run's caps; it works until every uncited item is read and cited
or a cap nears, and a run that reaches its cap leaves the rest to the next one, from ``changed``.

Standard library only, besides the graph's own modules.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from evo_agents.kg import entity_map, entity_map_reads
from evo_agents.kg import profile as kg_profile

LENS = "kg_entity_map"
STALE_SHARE = 0.2  # a map whose citations are stale beyond this share asks for a run
UNCITED_SHARE = 0.5  # as do the citable nodes no map cites beyond this share
COOLDOWN = timedelta(hours=24)  # the least time between two runs a build asks for (not one a writer asks for)
QUEUE_WAIT = timedelta(hours=24)  # a run that no node could take in this long fails
LEFT_OUT = frozenset({"binary", "history"})  # classes the map reads nothing of: assets, commits
CITABLE = entity_map.MENTION_KINDS
ENTITY_PREFIX = "entity:"
MAX_DIRECTORIES = 3000
MAX_ENTITIES = 5000
MAX_DOCUMENTS = 3000
MAX_PLANS = 500
MAX_CLUSTERS = 300
CLUSTER_SAMPLE = 5
MAX_CHANGED = 2000  # items of each list of ``changed``
SPLIT_AT = 200  # uncited items past which the prompt asks the run to split the reading among subagents
MAX_SNAPSHOT = 100_000  # items a snapshot keeps
REV_CHARS = 16
ALL = (1 << 30, 1 << 30)  # a ceiling above every label

# The states of a run (kg_entity_map_runs.state) and what asked for it (via)
ACTIVE = ("queued", "running")
STATES = (*ACTIVE, "done", "failed")
VIAS = ("first_build", "stale", "manual")


def wants_run(
    coverage: Mapping | None, *, had_run: bool, last_asked: datetime | None, now: datetime
) -> tuple[str, list[str]] | None:
    """(via, why) when a build of ``coverage`` asks for a run: the first build under strategy entity-graph-agent
    (``had_run`` False), or a map whose citations went stale or whose items went uncited past the shares, at least
    COOLDOWN after the last run was asked for. None otherwise."""
    mapped = (coverage or {}).get("entity_map")
    if not isinstance(mapped, Mapping) or not mapped.get("enabled"):
        return None
    if not had_run:
        return "first_build", ["the first build under strategy entity-graph-agent"]
    if last_asked is not None and last_asked + COOLDOWN > now:
        return None
    why = []
    mentions, stale = int(mapped.get("mentions") or 0), int(mapped.get("stale") or 0)
    if mentions and stale / mentions > STALE_SHARE:
        why.append(f"{stale} of {mentions} citations are stale, over {STALE_SHARE:.0%}")
    cited, uncited = int(mapped.get("cited_items") or 0), int(mapped.get("uncited_items") or 0)
    if cited + uncited and uncited / (cited + uncited) > UNCITED_SHARE:
        why.append(f"{uncited} of {cited + uncited} citable items are cited by no map, over {UNCITED_SHARE:.0%}")
    return ("stale", why) if why else None


# Reading the graph as the run's sink does


@dataclass(frozen=True)
class Node:
    node_id: str
    kind: str
    name: str
    props: dict


def _build(store) -> int | None:
    return store.latest_ready()


def nodes(store, ceiling: tuple[int, int] | None, kinds=()) -> Iterator[Node]:
    """The live nodes of the latest ready build of ``store`` (of ``kinds``, every kind when empty) that a reader up to
    ``ceiling`` (level, location) and the store's audience sees."""
    b = _build(store)
    if b is None:
        return
    for node_id, kind, name, props in entity_map_reads.node_rows(store, b, ceiling, kinds):
        yield Node(node_id, kind, name or "", json.loads(props or "{}"))


def _edges(store, ceiling, rel: str) -> Iterator[tuple[str, str]]:
    b = _build(store)
    if b is None:
        return
    yield from entity_map_reads.edge_rows(store, b, ceiling, rel)


def _class(props: Mapping) -> str:
    kind = props.get("item_kind")
    return kg_profile.class_of(kind) if isinstance(kind, str) else "other"


def item_revs(store, ceiling) -> tuple[dict[str, str], dict[str, str], int]:
    """(the revision of each item a reader up to ``ceiling`` sees, by item id; the class of each; the items above
    it). Assets and commits are left out: a map reads nothing of them."""
    b = _build(store)
    if b is None:
        return {}, {}, 0
    # the class of every item, those above the ceiling too, so that assets and commits stay out of ``above``
    classes = {n.node_id: _class(n.props) for n in nodes(store, ALL) if "item_kind" in n.props}
    revs: dict[str, str] = {}
    above = 0
    for item, rev, hashed, visible in entity_map_reads.item_rows(store, b, ceiling):
        cls = classes.get(item, "other")
        if cls in LEFT_OUT:
            continue
        if not visible:
            above += 1
            continue
        if len(revs) < MAX_SNAPSHOT:
            revs[item] = str(rev or hashed or "")[:REV_CHARS]
    return revs, {i: classes.get(i, "other") for i in revs}, above


@dataclass
class Readable:
    """What the run's sink reads of a graph, in counts: the items by class, those above its clearance, the entities,
    and of the newest map, the stale citations and the citable nodes it leaves out."""

    items: int = 0
    above: int = 0
    classes: dict[str, int] = field(default_factory=dict)
    entities: int = 0
    citable: int = 0
    uncited: int = 0
    changed: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        found = {k: getattr(self, k) for k in ("items", "above", "entities", "citable", "uncited")}
        return found | {"classes": dict(sorted(self.classes.items())), "changed": dict(sorted(self.changed.items()))}

    @classmethod
    def of(cls, data: Mapping | None) -> Readable:
        data = data or {}
        counts = {k: int(data.get(k) or 0) for k in ("items", "above", "entities", "citable", "uncited")}
        return cls(
            **counts,
            classes={str(k): int(v) for k, v in (data.get("classes") or {}).items()},
            changed={str(k): int(v) for k, v in (data.get("changed") or {}).items()},
        )


def _uncited(store, ceiling) -> tuple[list[str], int]:
    """(the citable nodes no entity's ``mentions`` edge leaves, the citable nodes)."""
    citable = sorted(n.node_id for n in nodes(store, ceiling, CITABLE))
    cited = {src for src, dst in _edges(store, ceiling, "mentions") if dst.startswith(ENTITY_PREFIX)}
    return [n for n in citable if n not in cited], len(citable)


def diff(revs: Mapping[str, str], before: Mapping[str, str] | None) -> dict[str, list[str]]:
    """The items of ``revs`` that ``before`` lacks (new) or holds at another revision (changed), and those of
    ``before`` gone; with no snapshot before, every item is new."""
    before = before or {}
    return {
        "new": sorted(i for i in revs if i not in before),
        "changed": sorted(i for i in revs if i in before and before[i] != revs[i]),
        "gone": sorted(i for i in before if i not in revs),
    }


def readable(store, ceiling, before: Mapping[str, str] | None) -> tuple[Readable, dict[str, str]]:
    """(the counts of what a reader up to ``ceiling`` reads of the graph in ``store``, the snapshot of its items'
    revisions). ``before`` is the snapshot of the last run that posted a map. Blocking."""
    revs, classes, above = item_revs(store, ceiling)
    uncited, citable = _uncited(store, ceiling)
    found = Readable(items=len(revs), above=above, classes=dict(Counter(classes.values())), citable=citable)
    found.entities = sum(1 for n in nodes(store, ceiling, ()) if n.node_id.startswith(ENTITY_PREFIX))
    found.uncited = len(uncited)
    found.changed = {k: len(v) for k, v in diff(revs, before).items()}
    return found, revs


def changed(store, ceiling, before: Mapping[str, str] | None, limit: int = MAX_CHANGED) -> dict:
    """What is new or changed since the snapshot ``before`` (None: no map yet), as a reader up to ``ceiling`` reads
    the graph in ``store``: item ids by list, at most ``limit`` each, with their counts. Blocking."""
    revs, classes, _ = item_revs(store, ceiling)
    lists = diff(revs, before)
    uncited, _ = _uncited(store, ceiling)
    lists["uncited"] = uncited
    out: dict = {"since_map": before is not None, "counts": {k: len(v) for k, v in lists.items()}}
    for key, ids in lists.items():
        out[key] = [{"id": i, "class": classes.get(i, "other")} if key != "uncited" else {"id": i} for i in ids[:limit]]
    return out


def _directories(found: list[Node], files: list[Node], symbols: list[Node]) -> list[dict]:
    """Each directory with the files and symbols under it, nested."""
    counts: dict[tuple[str, str], Counter] = {}
    for n in found:
        counts.setdefault((str(n.props.get("repo") or ""), str(n.props.get("path") or "")), Counter())
    per_file = Counter((str(s.props.get("file") or "")) for s in symbols)
    for f in files:
        repo, path = str(f.props.get("repo") or ""), str(f.props.get("path") or "")
        parts = path.split("/")[:-1]
        for depth in range(len(parts) + 1):
            key = (repo, "/".join(parts[:depth]))
            if key in counts or depth == 0:
                held = counts.setdefault(key, Counter())
                held["files"] += 1
                held["symbols"] += per_file.get(f.node_id, 0)
    rows = [
        {"repo": repo, "path": path or ".", "files": c["files"], "symbols": c["symbols"]}
        for (repo, path), c in counts.items()
    ]
    return sorted(rows, key=lambda r: (r["repo"], r["path"]))


def outline(store, ceiling) -> dict:
    """The structure of the project as a reader up to ``ceiling`` reads the graph in ``store``: its repos with the
    directories under each (the files and symbols under each, nested), the clusters that hold code (their size and a
    few members), the entities the graph has, the documents with their sections and the plans with their steps; each
    list cut at its MAX_ and counted whole. Blocking."""
    every = list(nodes(store, ceiling))
    by_kind: dict[str, list[Node]] = {}
    for n in every:
        by_kind.setdefault(n.kind, []).append(n)
    files, symbols = by_kind.get("File", []), by_kind.get("Symbol", [])
    directories = _directories(by_kind.get("Directory", []), files, symbols)
    repos = Counter(str(f.props.get("repo") or "") for f in files)
    repo_of = {f.node_id: str(f.props.get("repo") or "") for f in files}
    repo_symbols = Counter(repo_of.get(str(s.props.get("file") or ""), "") for s in symbols)
    implemented = Counter(src for src, _ in _edges(store, ceiling, entity_map.IMPLEMENTED_BY))
    entities = sorted(
        (
            {"id": n.node_id, "kind": n.kind, "name": n.name, "code": implemented.get(n.node_id, 0)}
            for n in every
            if n.node_id.startswith(ENTITY_PREFIX)
        ),
        key=lambda e: e["id"],
    )
    sections = Counter(str(s.props.get("item") or "") for s in by_kind.get("Section", []))
    documents = sorted(
        (
            {"id": d.node_id, "repo": d.props.get("repo"), "path": d.props.get("path"), "sections": sections[d.node_id]}
            for d in by_kind.get("Document", [])
        ),
        key=lambda d: d["id"],
    )
    steps = Counter(s.node_id.split("/step:")[0] for s in by_kind.get("PlanStep", []))
    plans = sorted(
        ({"id": p.node_id, "name": p.name, "steps": steps[p.node_id]} for p in by_kind.get("Plan", [])),
        key=lambda p: p["id"],
    )
    clusters = _clusters(store, {n.node_id: n for n in every})
    return {
        "counts": {
            "repos": len(repos),
            "directories": len(directories),
            "files": len(files),
            "symbols": len(symbols),
            "clusters": len(clusters),
            "entities": len(entities),
            "documents": len(documents),
            "sections": sum(sections.values()),
            "plans": len(plans),
            "steps": sum(steps.values()),
        },
        "repos": [
            {"repo": repo, "files": n, "symbols": repo_symbols.get(repo, 0)} for repo, n in sorted(repos.items())
        ],
        "directories": directories[:MAX_DIRECTORIES],
        "clusters": clusters[:MAX_CLUSTERS],
        "entities": entities[:MAX_ENTITIES],
        "documents": documents[:MAX_DOCUMENTS],
        "plans": plans[:MAX_PLANS],
    }


def _clusters(store, visible: Mapping[str, Node]) -> list[dict]:
    """The clusters of the build that hold a File or Symbol the reader sees: their id, level, the code and other
    nodes the reader sees in each, and a few of its code members. Never a cluster's own label, which is made of the
    words of every member, those the reader does not see too."""
    b = _build(store)
    if b is None:
        return []
    rows = entity_map_reads.cluster_rows(store, b)
    found: dict[str, dict] = {}
    for cluster, level, node_id in rows:
        node = visible.get(node_id)
        if node is None:
            continue
        held = found.setdefault(cluster, {"id": cluster, "level": level, "code": 0, "other": 0, "members": []})
        if node.kind in ("File", "Symbol"):
            held["code"] += 1
            if len(held["members"]) < CLUSTER_SAMPLE:
                held["members"].append(node_id)
        else:
            held["other"] += 1
    return sorted((c for c in found.values() if c["code"]), key=lambda c: (-c["code"], c["id"]))


def _budget_line(caps: Mapping | None) -> str:
    """The run's caps as the prompt tells them: the cost cap, the turns and the agent time, all of which remain when
    the run starts."""
    caps = caps or {}
    usd, turns, seconds = caps.get("max_usd"), caps.get("max_turns"), caps.get("max_seconds")
    parts = [f"a cost cap of ${float(usd):.2f}" if usd is not None else ""]
    parts += [f"{int(turns)} turns" if turns is not None else "", f"{int(seconds) // 60} minutes" if seconds else ""]
    held = ", ".join(p for p in parts if p)
    if not held:
        return "The worker stops the run at its caps; work until they near, not until the first part is posted."
    return (
        f"This run has {held} of agent time, and all of it remains as you start. The worker stops the run at the "
        "first cap it reaches. Spend it on the map: stop only when the work is whole, or when about a tenth "
        "of a cap remains (note the time with `date` as you start and check it between parts). A subagent's turns "
        "do not count against yours, its cost does."
    )


def prompt(
    *,
    project: str,
    run_id: int,
    sink: str,
    found: Readable,
    why: list[str],
    since_map: bool,
    caps: Mapping | None = None,
) -> str:
    """The text the run starts with: counts, classes and the run's caps, never the name or the text of an item."""
    classes = ", ".join(f"{name} ({n} items)" for name, n in sorted(found.classes.items()))
    moved = found.changed
    since = (
        f"Since the last map, {moved.get('new', 0)} items are new, {moved.get('changed', 0)} changed and "
        f"{moved.get('gone', 0)} gone."
        if since_map
        else "No map of the project was posted before: every item is new to it."
    )
    cmd = f"evo-agents hub kg entity-map --project {project}"
    split = (
        f"- {found.uncited} items is more than one agent reads well: split the reading by area, one subagent (Task "
        "tool) per top-level package or code directory, per docs directory and per group of plans, several at once. "
        "Give each its area, the item ids of `--changed` in it, the format and these rules; have each post its own "
        "parts or hand you the JSON to post, and give an area that comes back unfinished to a fresh subagent."
        if found.uncited > SPLIT_AT
        else "- Hand parts to subagents (Task tool), one per area, when an area is large; post each part as it is done."
    )
    return "\n".join(
        [
            f"## The entity map of project {project} (entity map run #{run_id})",
            "",
            f"Asked for because {'; '.join(why) or 'a writer asked'}. The graph's strategy is entity-graph-agent: its "
            "entities come from the maps an agent posts, and the build calls no model. Through your sink "
            f"({sink}) the graph holds {found.items} items you may read, of these classes: {classes or 'none'}; "
            f"{found.above} more are above your clearance. It has {found.entities} entities, and {found.uncited} of "
            f"its {found.citable} documents, sections, plans and steps are cited by no map. {since}",
            "",
            "## The work, and when it is done",
            f"One run builds the map. The work is whole when every one of the {found.uncited} uncited documents, "
            "sections, plans and steps (and every item new or changed since the last map) has been read and is cited "
            "by each entity it speaks of; an item that speaks of no concept, component or feature of the code may "
            "stay uncited, and your summary counts those. Do not stop after a first part, and never leave work "
            '"for another run" while budget remains: a run stops early only at its caps, and then the next run goes '
            "on from `--changed`.",
            _budget_line(caps),
            "",
            "## How to map it",
            f"- `{cmd} --outline --json` gives the structure: the repos and their directories with the files and "
            "symbols under each, the clusters that hold code, the entities the graph has, the documents with their "
            f"sections and the plans with their steps. `{cmd} --changed --json --limit {MAX_CHANGED}` lists the items "
            "new or changed since the last map and what no map cites yet: that is your work list. A list longer than "
            "the limit shows its first items; the rest show once the builds that follow your posts take your parts "
            "in, so read it again after each few parts.",
            "- Read the documents, plans and code you need with kg_search, kg_context and kg_node, or in the run's "
            "checkout of the repos. What they do not show you, you may not use.",
            "- Name the concepts, components and features the documents and plans speak of, reusing the entities the "
            "graph has, and link each to the code that implements it at the finest level that holds it whole: a "
            "Symbol when one class or function is the implementation, a File when a module is, a Directory when a "
            "package or a subsystem is. Give every link a reason a reader can check.",
            "- Cite every document, section, plan or step that names or speaks of an entity, each with a short "
            "quote: all of them, not one per entity. An entity many documents speak of carries a mention from each.",
            split,
            "",
            "## How to send it",
            f'Write a JSON object of format {entity_map.FORMAT} (docs/kg-strategies.md, "The entity-graph-agent '
            f'strategy") to a file and run `{cmd} --post FILE`. The hub checks every cited node against its latest '
            "build through your sink, keeps what passes, lists each entry it rejected with the reason, and queues a "
            "build. Post the map in parts as you go, one part per area or every few dozen entities, so that nothing "
            "is lost if the run stops; fix what was rejected and post it again. A part that names an entity posted "
            "before, by the same name, adds its mentions and links to it. Record no finding and file no proposal.",
        ]
    )


def shown(body: Mapping | None) -> dict:
    """``body`` as the runs' listing shows it: without the prompt."""
    return {k: v for k, v in (body or {}).items() if k != "prompt"}


__all__ = [
    "LENS",
    "Readable",
    "changed",
    "diff",
    "item_revs",
    "outline",
    "prompt",
    "readable",
    "wants_run",
]
