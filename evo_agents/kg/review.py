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

The queue respects the decisions writers took (``evo_agents.kg.pipeline.curation``), which the build applied before
the review: a deprecated node or edge, two node ids merged into one, a node a decision linked.

- An item whose required node is deprecated is not open; the deprecated nodes of an item about several drop out of
  it, and an item left with too few (a duplicate alone, noise below BULK, a cluster without code) is not open.
- Merged ids are one node (the other is an alias), so a duplicate pair a merge joined is no item.
- An item about the mentions of a node is not open once decision links leave that node, as many as its mentions, or
  once every link of its fix (below) is in the graph and the fix covers each of its mentions. A document a decision
  links to is mentioned. A path edge to one of several files is not open once a decision declares it, deprecates
  it, or links its source to one of the files with the same relation.

Each open item may carry its fix in ``detail.fix``: the decisions that would close it, each ``{kind, src, rel, dst,
reason}`` as the hub takes one (``evo_agents.hub.kg_decisions``), proposed only when one target is plausible.

- ``dangling_mention``, ``ambiguous_match`` of mentions: link the item (``mentions``) to the one file whose base name
  and directory name are a sample's (a moved file), the one symbol of the item's repo with a sample's name, or for
  ``path::name`` the one symbol of that name in the one file of that path;
- ``ambiguous_match`` of a path edge: when one of the files lives in the source's repo, declare the edge to it, and
  deprecate the edge the resolver chose when that was another;
- ``unmentioned_document``: deprecate a document under an archive directory (ARCHIVE), else link the nearest
  directory document of its repo (README, index, INSTRUCTIONS, SKILL or AGENTS) at most DOC_LEVELS directories up
  to it;
- ``possible_duplicate``: merge the nodes of one label, unless two are files at different paths;
- ``bulk_noise``: deprecate each node of it;
- ``cluster_without_knowledge``: no decision; ``detail.report`` says the item is for a person to read, in a report.
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
ARCHIVE = re.compile(r"(^|/)(archive|archived|attic|deprecated|legacy|old|obsolete)/", re.IGNORECASE)
PARENT_DOCS = ("readme", "index", "instructions", "skill", "agents")  # a directory's document names, as stems
DOC_LEVELS = 2  # an unmentioned document looks this many directories up for its parent document
FIX_REL = "mentions"  # what a fix links an item to its target by

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


def _deprecated(x: dict) -> bool:
    return bool((x.get("props") or {}).get("deprecated"))


FILE_SUFFIX = re.compile(
    r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|md|mdx|rst|txt|yaml|yml|json|toml|ini|cfg|sh|go|rs|java|kt|swift|dart|sql|css|"
    r"scss|html|vue|svelte|rb|php|c|h|cc|cpp|hpp|cs)$",
    re.IGNORECASE,
)
SYMBOLISH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def decision(kind: str, src: str, rel: str | None, dst: str | None, reason: str) -> dict:
    """One decision of a fix, as the hub takes one (``evo_agents.hub.kg_decisions``)."""
    return {"kind": kind, "src": src, "rel": rel, "dst": dst, "reason": reason}


def fix_nodes(fix: list[dict]) -> set[str]:
    """The node ids the decisions of a fix name."""
    return {x for d in fix for x in (d["src"], d["dst"]) if x}


def fix_edges(fix: list[dict]) -> list[tuple[str, str, str]]:
    """The (src, rel, dst) edges the decisions of a fix deprecate."""
    return [(d["src"], d["rel"], d["dst"]) for d in fix if d["kind"] == "deprecate" and d["rel"] and d["dst"]]


def _clean(text: str) -> str:
    text = text.strip().strip("`'\"").rstrip(".,:;)").strip()
    return text[2:] if text.startswith("./") else text


class _Fixer:
    """The one plausible target of what an item names, among the nodes no decision deprecated."""

    def __init__(self, graph: Graph, deprecated: set[str]):
        self.nodes = graph.nodes
        self.by_base: dict[str, list[str]] = defaultdict(list)  # a file's base name -> files and documents
        self.by_name: dict[str, list[str]] = defaultdict(list)  # a symbol's name, and its last part -> symbols
        self.docs: dict[tuple, list[str]] = defaultdict(list)  # (repo, directory) -> documents
        for nid, n in sorted(graph.nodes.items()):
            if nid in deprecated:
                continue
            path = _path(n)
            if n["kind"] in ("File", "Document") and path:
                self.by_base[posixpath.basename(path)].append(nid)
            if n["kind"] == "Document" and path:
                self.docs[(self.repo(nid), posixpath.dirname(path))].append(nid)
            if n["kind"] == "Symbol" and n["name"]:
                self.by_name[n["name"]].append(nid)
                last = n["name"].rsplit(".", 1)[-1]
                if last != n["name"]:
                    self.by_name[last].append(nid)

    def repo(self, nid: str) -> str | None:
        n = self.nodes.get(nid)
        if n is None:
            return None
        props = n["props"]
        if props.get("repo"):
            return props["repo"]
        up = props.get("file") if n["kind"] == "Symbol" else props.get("item") if n["kind"] == "Section" else None
        return self.repo(up) if up and up != nid else None

    def target(self, text: str, repo: str | None) -> str | None:
        """The one file a path names (its base name and the name of its directory), the one symbol of ``repo``
        with a name, or the one symbol with that name of the one file of ``path::name``."""
        text = _clean(text)
        if not text or " " in text:
            return None
        where, _, name = text.rpartition("::")
        name = name.removesuffix("()").removesuffix("(")
        if where:
            files = set(self.files(where, repo))
            found = [s for s in self.symbols(name) if self.nodes[s]["props"].get("file") in files] if files else []
        elif "/" in text or FILE_SUFFIX.search(text):
            found = self.files(text, repo)
        else:
            found = [s for s in self.symbols(name) if repo and self.repo(s) == repo]
        found = sorted(set(found))
        return found[0] if len(found) == 1 else None

    def files(self, path: str, repo: str | None) -> list[str]:
        path = path.rstrip("/")
        parent = posixpath.basename(posixpath.dirname(path))
        found = [
            f
            for f in self.by_base.get(posixpath.basename(path), [])
            if not parent or posixpath.basename(posixpath.dirname(_path(self.nodes[f]))) == parent
        ]
        return [f for f in found if repo and self.repo(f) == repo] or found

    def symbols(self, name: str) -> list[str]:
        return self.by_name.get(name, []) if len(name) >= 3 and SYMBOLISH.fullmatch(name) else []

    def mentions(self, item_id: str, samples: list[str], what: str) -> tuple[list[dict], bool]:
        """The links that fix the samples of an item's mentions, and whether each sample has one."""
        repo, out, every = self.repo(item_id), [], bool(samples)
        for text in dict.fromkeys(samples):
            target = self.target(text, repo)
            if target is None or target == item_id:
                every = False
            elif all(d["dst"] != target for d in out):
                out.append(decision("link", item_id, FIX_REL, target, f"a {what} mention {text!r} names {target}"))
        return out, every

    def edge(self, e: dict, files: list[str]) -> list[dict]:
        """Declare a path edge to the one of ``files`` in its source's repo; deprecate the one chosen if another."""
        repo = self.repo(e["src"])
        near = [f for f in files if repo and self.repo(f) == repo]
        if len(near) != 1:
            return []
        out = [decision("link", e["src"], e["rel"], near[0], f"the one file of that path in repo {repo}")]
        if near[0] != e["dst"]:
            out.append(decision("deprecate", e["src"], e["rel"], e["dst"], f"{near[0]} is the file of repo {repo}"))
        return out

    def document(self, nid: str) -> list[dict]:
        """Deprecate an archived document; else link to it the nearest directory document (README, index ...) above it,
        in its repo; none when one level holds several."""
        path = _path(self.nodes[nid])
        if ARCHIVE.search(path):
            return [decision("deprecate", nid, None, None, "a document under an archive directory")]
        repo = self.repo(nid)
        stem = posixpath.splitext(posixpath.basename(path))[0].lower()
        directory = posixpath.dirname(path)
        if stem in PARENT_DOCS:  # a directory's own document is mentioned from above
            directory = posixpath.dirname(directory) if directory else None
        for _level in range(DOC_LEVELS):
            if directory is None:
                break
            here = [d for d in self.docs.get((repo, directory), []) if d != nid]
            named = [
                d
                for d in here
                if posixpath.splitext(posixpath.basename(_path(self.nodes[d])))[0].lower() in PARENT_DOCS
            ]
            if len(named) == 1:
                return [decision("link", named[0], FIX_REL, nid, f"the document of directory {directory or '/'}")]
            if named:
                return []
            directory = posixpath.dirname(directory) if directory else None
        return []

    def merge(self, ids: list[str]) -> list[dict]:
        """Merge the duplicates of one label, unless two are files at different paths."""
        groups: dict[str, list[str]] = defaultdict(list)
        for nid in ids:
            groups[repr(self.nodes[nid]["label"])].append(nid)
        out = []
        for group in groups.values():
            if len(group) < 2 or len({_path(self.nodes[i]) for i in group if _path(self.nodes[i])}) > 1:
                continue
            first, *rest = sorted(group)
            out += [decision("merge", first, None, other, f"the same name as {first}") for other in rest]
        return out


def compute(graph: Graph) -> list[dict]:
    """The review items of ``graph`` (with clusters) left open by its decisions, by fingerprint, each with its fix when
    it has one; ``graph.mentions`` holds, per item id, its dangling and ambiguous mentions with samples. Empty when
    the graph has no clusters."""
    found = graph.clusters
    if found is None or not found.clusters:
        return []
    nodes = graph.nodes
    deprecated = {nid for nid, n in nodes.items() if _deprecated(n)}
    degree: Counter = Counter()
    mentioned: set[str] = set()
    linked: dict[str, set[tuple[str, str]]] = defaultdict(set)  # src -> (rel, dst) of the edges decisions link
    for e in graph.edges.values():
        if e["rel"] in STRUCTURAL or _deprecated(e):
            continue
        if (e.get("props") or {}).get("decision") is not None:
            linked[e["src"]].add((e["rel"], e["dst"]))
        degree[e["src"]] += 1
        degree[e["dst"]] += 1
        dst = nodes.get(e["dst"])
        if dst is not None and dst["kind"] == "Document":
            src = nodes.get(e["src"])
            own = src is not None and src["kind"] == "Section" and src["props"].get("item") == e["dst"]
            if not own:
                mentioned.add(e["dst"])
    fixer = _Fixer(graph, deprecated)

    def owner(nid: str) -> str:
        n = nodes.get(nid)
        if n is None:
            return nid
        if n["kind"] == "Symbol":
            return n["props"].get("file") or nid
        if n["kind"] == "Section":
            return n["props"].get("item") or nid
        return nid

    def cluster_of(nid: str) -> str | None:
        member = found.members.get(owner(nid))
        return member[0] if member else None

    def ranked(ids) -> list[str]:
        return sorted(set(ids), key=lambda i: (-degree[i], i))

    items: dict[str, dict] = {}

    def add(
        rule: str,
        key: str,
        evidence: list[str],
        detail: dict,
        *,
        required: int = 1,
        least: int = 1,
        fix: list[dict] | None = None,
    ) -> None:
        fp = fingerprint(rule, key)
        kept = evidence[:EVIDENCE]
        clusters = [cluster_of(nid) for nid in kept]
        items[fp] = {
            "fingerprint": fp,
            "rule": rule,
            "cluster": next((c for c in clusters if c), None),
            "evidence": kept,
            "clusters": clusters,
            "action": ACTIONS[rule],
            "detail": {
                "count": len(evidence),
                **detail,
                **({"fix": fix} if fix else {}),
                "required": required,
                "least": least,
            },
        }

    # 1 and 5: the mentions of each item that matched nothing, or several nodes
    for item_id, m in sorted(graph.mentions.items()):
        if item_id not in nodes or item_id in deprecated:
            continue
        links = linked.get(item_id, set())
        for rule, what in (("dangling_mention", "dangling"), ("ambiguous_match", "ambiguous")):
            count = m.get(what)
            if not count:
                continue
            every = (m.get("samples") or {}).get(what, [])
            fix, whole = fixer.mentions(item_id, every, what)
            fixed = whole and len(every) >= count and all((d["rel"], d["dst"]) in links for d in fix)
            if len(links) >= count or fixed:
                continue  # decisions linked it
            fix = [d for d in fix if (d["rel"], d["dst"]) not in links]
            add(rule, item_id, [item_id], {"mentions": count, "samples": every[:SAMPLES]}, fix=fix)

    # 5: path edges resolved to one of several files with that path
    by_path: dict[str, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        if n["kind"] == "File" and _path(n) and nid not in deprecated:
            by_path[_path(n)].append(nid)
    for eid, e in sorted(graph.edges.items()):
        if e["status"] != "resolved" or e["rel"] in STRUCTURAL or _deprecated(e):
            continue
        dst = nodes.get(e["dst"])
        if dst is None or dst["kind"] != "File" or len(by_path.get(_path(dst), [])) < 2:
            continue
        if e["src"] in deprecated or e["dst"] in deprecated:
            continue
        others = [c for c in sorted(by_path[_path(dst)]) if c != e["dst"]]
        if any((e["rel"], f) in linked.get(e["src"], ()) for f in [e["dst"], *others]):
            continue  # a decision linked its source to one of the files
        add(
            "ambiguous_match",
            eid,
            [e["src"], e["dst"], *others],
            {"edge": eid, "rel": e["rel"], "path": _path(dst)},
            required=2,
            least=2,
            fix=fixer.edge(e, [e["dst"], *others]),
        )

    # 2: documents no other node mentions
    for nid, n in sorted(nodes.items()):
        if n["kind"] == "Document" and nid not in mentioned and nid in found.members and nid not in deprecated:
            add("unmentioned_document", nid, [nid], {"path": _path(n) or None}, fix=fixer.document(nid))

    # 3: clusters with code and no knowledge
    kinds_of: dict[str, set[str]] = defaultdict(set)
    code_of: dict[str, list[str]] = defaultdict(list)
    for nid, (top, _sub) in found.members.items():
        n = nodes.get(nid)
        if n is None or nid in deprecated:
            continue
        kinds_of[top].add(n["kind"])
        if n["kind"] in CODE_KINDS:
            code_of[top].append(nid)
    for cid, c in sorted(found.clusters.items()):
        if c["level"] != 1 or not code_of[cid] or kinds_of[cid] & KNOWLEDGE_KINDS:
            continue
        add("cluster_without_knowledge", cid, ranked(code_of[cid]), {"label": c["label"], "report": True}, required=0)

    # 4: nodes of one kind with the same normalised name
    named: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        key = normal(n["name"])
        if n["kind"] not in UNIQUE_SKIP and len(key) >= 3 and nid not in deprecated:
            named[(n["kind"], key)].append(nid)
    for (kind, key), ids in sorted(named.items()):
        if len(ids) >= 2:
            order = ranked(ids)
            add(
                "possible_duplicate",
                f"{kind}\x1f{key}",
                order,
                {"kind": kind},
                required=0,
                least=2,
                fix=fixer.merge(order[:EVIDENCE]),
            )

    # 6: bulk noise, per directory or per repeated step name
    noise: dict[tuple[str, str], list[str]] = defaultdict(list)
    step_plans: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e["rel"] == "has_step":
            step_plans[e["dst"]].add(e["src"])
    steps: dict[str, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        if nid in deprecated:
            continue
        path = _path(n)
        if n["kind"] == "PlanStep":
            steps[normal(n["name"])].append(nid)
        elif path.lower().endswith(IMAGE_SUFFIXES):
            noise[("image", posixpath.dirname(path))].append(nid)
        elif path and GENERATED.search(path):
            noise[("generated", posixpath.dirname(path))].append(nid)

    def exclude(ids: list[str], why: str) -> list[dict]:
        return [decision("deprecate", nid, None, None, why) for nid in ids[:EVIDENCE]]

    for (what, where), ids in sorted(noise.items()):
        if len(ids) >= BULK:
            order = sorted(ids)
            why = f"one of {len(ids)} {what} files in {where or '/'}"
            add(
                "bulk_noise",
                f"{what}\x1f{where}",
                order,
                {"kind": what, "where": where},
                required=0,
                fix=exclude(order, why),
            )
    for name, ids in sorted(steps.items()):
        plans = {p for i in ids for p in step_plans.get(i) or {i}}
        if name and len(plans) >= BULK:
            order = sorted(ids)
            why = f"a step whose name repeats in {len(plans)} plans"
            add(
                "bulk_noise",
                f"step\x1f{name}",
                order,
                {"kind": "step", "where": None},
                required=0,
                fix=exclude(order, why),
            )
    return [items[fp] for fp in sorted(items)]


def counts(items: list[dict]) -> dict[str, int]:
    """How many items each rule has, every rule named."""
    found = Counter(item["rule"] for item in items)
    return {rule: found.get(rule, 0) for rule in RULES}


def visible_item(item: dict, labels: dict[str, tuple], visible: Visible) -> dict | None:
    """``item`` as a reader sees it, ``labels`` holding the label of each of its nodes the reader's audience holds:
    None when a required node is not visible or fewer than ``least`` are; else with its evidence cut to the visible
    nodes, its cluster that of the first of them in one and, when some were cut, its count to theirs. Its fix is
    left out when a node it names is not visible, or when it was cut (a fix is over every node of the item)."""
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
        out["detail"].pop("fix", None)
    fix = out["detail"].get("fix")
    if fix and not all(nid in labels and visible(labels[nid]) for nid in fix_nodes(fix)):
        out["detail"].pop("fix")
    return out
