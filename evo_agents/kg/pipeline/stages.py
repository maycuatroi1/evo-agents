"""Per-item stages: map and structure. Each is a deterministic function of one item (and its text).

Outputs are lists of facts (JSON-serializable dicts, memoized):

    {"t": "node", "id", "kind", "name", "props", "status", "conf", "units", "needs", "where"}
    {"t": "edge", "src", "rel", "dst", "props", "status", "conf", "units", "needs", "where"}
    {"t": "ref", "src", "rel", "target": {"type", "value", ...}, "status", "units", "where"}
    {"t": "text", "src", "unit", "text", "field"}       free text the link stage scans
    {"t": "doc", "src", "text"}                           a docstring or leading comment, for search only
    {"t": "ident", "type", "key", "node", ...}            an identifier the index learns
    {"t": "issue", "message", "unit"}                     reported in coverage, never fatal

An item of any kind may declare nodes and edges of its own in ``props.graph`` (``declared``): a source the hub reads
itself, such as a core plugin's (``evo_agents.hub.kg_sources``), states what it knows instead of leaving it to the
link stage, and every such fact is ``declared``.

``units`` are ownership units: ``<item_id>#<anchor>`` for a fragment, ``<item_id>#`` for the whole item.
An element is alive while one of its derivations has every unit and every needed element alive.

Symbol nodes carry ``props.key = [repo, path, qualname]`` (see ``symbol_key``), unique in the store.
Directory nodes are ``dir:<repo>:<path>``: one per directory of a repo, whichever source read it.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

import yaml

from evo_agents.kg.extract import python as py_extract
from evo_agents.kg.ids import normalize_time
from evo_agents.kg.memo import Stage
from evo_agents.kg.pipeline.code import CODE_EXTRACTORS
from evo_agents.kg.pipeline.facts import edge, frag_unit, item_unit, node, ref
from evo_agents.schema.ontology import node_kind_for_item

MAP_VERSION = "8"
STRUCTURE_VERSION = "14"
MAP = Stage("map", MAP_VERSION)  # an item's own node, its units and the directories of its path
STRUCTURE = Stage("structure", STRUCTURE_VERSION)  # what an item declares and what its kind holds

# Props of a commit, a pull request, a ticket, a chat's item or a document of the hub that its node keeps, besides path
# and repo (``map_item``). A document of the hub (kind ``doc``: of the features docs and srs) keeps its slug and version,
# and a document of the feature docs that version's author.
# A commit's node also keeps ``time``, its committer time as UTC text that sorts (``normalize_time``), so the
# commits that changed a file can be read newest first (``evo_agents.hub.kg_graph``).
NODE_PROPS = {
    "commit": ("sha", "files", "merge", "author_email"),
    "pr": ("number", "status", "head", "base", "merge_commit"),
    "ticket": ("number", "type", "status", "status_group", "priority", "sprint", "assignees", "due_on"),
    "chat": ("chat",),
    "thread": ("chat", "root"),
    "message": ("chat", "thread", "seq", "author_kind", "author", "sent_at", "run"),
    "doc": ("document", "version", "author"),
}
DECLARED_KIND = re.compile(r"^[A-Z][A-Za-z0-9]*$")  # the kind of a node an item declares, as an ontology names one
DECLARED_REL = re.compile(r"^[a-z][a-z_]*$")  # the relation of an edge it declares
MAX_DECLARED_ID = 500  # characters of the id of a node an item declares, or of an edge's ends
PROBLEM_KIND = "Problem"  # the node of a problem of a hub project, problem:P-3, as the hub's source declares it
PROBLEM_CODE = re.compile(r"P-[1-9][0-9]{0,8}")  # evo_agents.hub.problems.REF, which this package does not import

FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)^(?:---|\.\.\.)[ \t]*\r?$", re.S | re.M)


def normalize_url(url: str) -> str:
    url = url.strip().rstrip(".,;:!?)]}>\"'")
    url = url.split("#", 1)[0].split("?", 1)[0]
    if "://" in url:
        scheme, rest = url.split("://", 1)
        host, _, path = rest.partition("/")
        url = f"{scheme.lower()}://{host.lower()}/{path}"
    return url.rstrip("/")


# ---------------------------------------------------------------------------------------------
# map


def directory_id(repo: str, path: str) -> str:
    return f"dir:{repo}:{path}"


def _directories(item_id: str, repo: str, path: str, unit: str) -> list[dict]:
    """A Directory for every ancestor of one file below the repo root (the root is its Source), each
    ``child_of`` the next. Every item under a directory derives it from its own unit, so assembly ORs
    those derivations: the directory lives while any item under it does, and each item's map stays its
    own memo entry."""
    facts = []
    child = item_id
    for parent in map(str, list(PurePosixPath(path).parents)[:-1]):
        did = directory_id(repo, parent)
        facts.append(node(did, "Directory", f"{parent}/", [unit], props={"path": parent, "repo": repo}))
        facts.append(edge(child, "child_of", did, [unit]))
        facts.append({"t": "ident", "type": "dir", "key": parent, "repo": repo, "node": did})
        child = did
    return facts


def map_item(record: dict, source_id: str, directories: bool = False) -> list[dict]:
    item_id = record["id"]
    unit = item_unit(item_id)
    kind = node_kind_for_item(record["kind"])
    props = {"item_kind": record["kind"], "rev": record.get("rev"), "source": source_id}
    for key in ("uri",):
        if record.get(key):
            props[key] = record[key]
    extra = record.get("props") or {}
    for key in ("path", "repo", *NODE_PROPS.get(record["kind"], ())):
        if extra.get(key):
            props[key] = extra[key]
    if record["kind"] == "commit" and record.get("rev_time"):
        props["time"] = normalize_time(record["rev_time"])
    name = record.get("title") or extra.get("path") or item_id
    facts = [
        node(item_id, kind, name, [unit], props=props),
        node(f"source:{source_id}", "Source", source_id, [unit]),
        edge(item_id, "in_source", f"source:{source_id}", [unit]),
    ]
    if record.get("parent"):
        facts.append(edge(item_id, "child_of", record["parent"], [unit]))
    if record.get("uri"):
        facts.append({"t": "ident", "type": "url", "key": normalize_url(record["uri"]), "node": item_id})
    if extra.get("path"):
        facts.append(
            {
                "t": "ident",
                "type": "path",
                "key": extra["path"],
                "repo": extra.get("repo"),
                "source": source_id,
                "node": item_id,
            }
        )
        module = py_extract.module_name(extra["path"])
        if module:
            facts.append({"t": "ident", "type": "module", "key": module, "source": source_id, "node": item_id})
        if directories:
            facts += _directories(item_id, extra.get("repo") or source_id, extra["path"], unit)
    facts.append({"t": "ident", "type": "id", "key": item_id, "node": item_id})
    if kind == "Document":
        for frag in record.get("fragments", []):
            anchor = frag["anchor"]
            if anchor == "_preamble":
                continue
            sid = f"{item_id}#{anchor}"
            funit = frag_unit(item_id, anchor)
            facts.append(
                node(
                    sid,
                    "Section",
                    frag.get("title") or anchor,
                    [funit],
                    props={"anchor": anchor, "level": frag.get("level", 0), "item": item_id, "span": frag.get("span")},
                    where=[[funit, frag.get("span") or ""]],
                )
            )
            parent = f"{item_id}#{frag['parent']}" if frag.get("parent") else item_id
            facts.append(edge(sid, "part_of", parent, [funit]))
            facts.append({"t": "ident", "type": "id", "key": sid, "node": sid})
    return facts


# ---------------------------------------------------------------------------------------------
# structure


def _yaml(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def origin_url(origin: str) -> str | None:
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|git@|ssh://git@)([^/:]+)[:/](.+?)(?:\.git)?/?$", origin.strip())
    return f"https://{m.group(1).lower()}/{m.group(2)}" if m else None


def _manifest(item_id: str, data: dict, unit: str, record: dict) -> list[dict]:
    facts = []
    repos = [r for r in data.get("repos") or [] if isinstance(r, dict) and r.get("name")]
    own = (record.get("props") or {}).get("repo")
    if own and own not in {r["name"] for r in repos}:
        repos.append({"name": own, "role": "harness"})
    for repo in repos:
        rid = f"repo:{repo['name']}"
        props = {k: repo[k] for k in ("role", "origin", "default_branch", "path") if repo.get(k)}
        facts.append(node(rid, "Repo", repo["name"], [unit], props=props))
        facts.append(edge(item_id, "declares", rid, [unit]))
        facts.append({"t": "ident", "type": "repo", "key": repo["name"], "node": rid})
        url = origin_url(repo.get("origin") or "")
        if url:
            facts.append({"t": "ident", "type": "url", "key": normalize_url(url), "node": rid})
    return facts


def _locations(source) -> list[dict]:
    if source is None:
        return []
    entries = source if isinstance(source, list) else [source]
    out = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path"):
            out.append({"repo": entry.get("repo"), "path": entry["path"]})
        elif isinstance(entry, str):
            out.append({"repo": None, "path": entry})
    return out


def _contracts(item_id: str, data: dict, unit: str) -> list[dict]:
    facts = []
    for seam in data.get("seams") or []:
        if not isinstance(seam, dict) or not seam.get("name"):
            continue
        sid = f"seam:{seam['name']}"
        props = {k: seam[k] for k in ("kind", "verify", "status") if seam.get(k)}
        facts.append(node(sid, "Seam", seam["name"], [unit], props=props))
        facts.append(edge(item_id, "declares", sid, [unit]))
        facts.append({"t": "ident", "type": "seam", "key": seam["name"], "node": sid})
        if seam.get("owner"):
            facts.append(ref(sid, "owned_by", {"type": "repo", "value": seam["owner"]}, [unit]))
        for consumer in seam.get("consumers") or []:
            facts.append(ref(sid, "consumed_by", {"type": "repo", "value": consumer}, [unit]))
        for loc in _locations(seam.get("source")):
            repo = loc["repo"] or seam.get("owner")
            facts.append(ref(sid, "defined_in", {"type": "path", "value": loc["path"], "repo": repo}, [unit]))
        for field_name in ("notes", "verify"):
            if isinstance(seam.get(field_name), str):
                facts.append({"t": "text", "src": sid, "unit": unit, "text": seam[field_name], "field": field_name})
    return facts


def _step_text(step: dict) -> list[tuple[str, str]]:
    out = []
    for key in ("title", "what", "verify", "evidence", "note", "why"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            out.append((key, value))
    return out


def _plan_problems(pid: str, data: dict, unit: str) -> list[dict]:
    """The plan ``addresses`` each problem of its hub project it names (``problems``): the node the hub's source of
    problems declares (``evo_agents.hub.kg_problems``), which the edge needs, so without it there is no edge."""
    found = data.get("problems") if isinstance(data.get("problems"), list) else []
    facts = []
    for code in found:
        if isinstance(code, str) and PROBLEM_CODE.fullmatch(code):
            target = f"{PROBLEM_KIND.lower()}:{code}"
            facts.append(edge(pid, "addresses", target, [unit], needs=[target]))
    return facts


def _plan(item_id: str, data: dict, unit: str) -> list[dict]:
    plan_id = data.get("id")
    if not isinstance(plan_id, str):
        return [{"t": "issue", "message": "plan without id", "unit": unit}]
    pid = f"plan:{plan_id}"
    steps = [s for s in data.get("steps") or [] if isinstance(s, dict)]
    statuses = [str(s.get("status", "pending")) for s in steps]
    props = {
        "goal": (data.get("goal") or "").strip()[:500],
        "steps": len(steps),
        "done": sum(1 for s in statuses if s == "done"),
    }
    facts = [
        node(pid, "Plan", data.get("title") or plan_id, [unit], props=props),
        edge(pid, "defined_in", item_id, [unit]),
        {"t": "ident", "type": "plan", "key": plan_id, "node": pid},
    ]
    for key in ("goal", "context"):
        if isinstance(data.get(key), str):
            facts.append({"t": "text", "src": pid, "unit": unit, "text": data[key], "field": key})
    for repo in data.get("repos") or []:
        if isinstance(repo, dict) and repo.get("repo"):
            facts.append(ref(pid, "touches", {"type": "repo", "value": repo["repo"]}, [unit]))
    for seam in data.get("seams_touched") or []:
        name = seam.get("name") if isinstance(seam, dict) else seam
        if isinstance(name, str):
            facts.append(ref(pid, "touches", {"type": "seam", "value": name}, [unit]))
    facts += _plan_problems(pid, data, unit)
    for index, step in enumerate(steps):
        number = step.get("id", index + 1)
        stid = f"{pid}/step:{number}"
        title = step.get("title") or (step.get("what") or "").strip().split("\n")[0][:80] or str(number)
        sprops = {"number": number, "status": step.get("status", "pending")}
        for key in ("repo", "verify", "done_at"):
            if isinstance(step.get(key), str):
                sprops[key] = step[key]
        facts.append(node(stid, "PlanStep", title, [unit], props=sprops))
        facts.append(edge(pid, "has_step", stid, [unit]))
        facts.append({"t": "ident", "type": "step", "key": f"{plan_id}#{number}", "node": stid})
        for dep in step.get("depends_on") or []:
            facts.append(edge(stid, "depends_on", f"{pid}/step:{dep}", [unit]))
        repo = step["repo"] if isinstance(step.get("repo"), str) else None
        if repo:
            facts.append(ref(stid, "in_repo", {"type": "repo", "value": repo}, [unit]))
        for key, text in _step_text(step):
            # The step's repo: a bare commit SHA or "PR #57" in its text names a commit or pull request of that repo.
            facts.append({"t": "text", "src": stid, "unit": unit, "text": text, "field": key, "repo": repo})
    return facts


def _commit(item_id: str, record: dict, unit: str) -> list[dict]:
    """A commit read from a git source's history: the files it changed, and its SHA for the link stage, which links
    a mention of the commit (``repo@sha``, a bare SHA in evidence) and a pull request's merge commit to it."""
    props = record.get("props") or {}
    repo, sha = props.get("repo"), props.get("sha")
    if not isinstance(sha, str):
        return [{"t": "issue", "message": f"{item_id}: a commit without its sha", "unit": unit}]
    paths = sorted({p for p in props.get("paths") or [] if isinstance(p, str)})
    facts: list[dict] = [{"t": "ident", "type": "commit", "key": sha, "repo": repo, "node": item_id, "paths": paths}]
    for path in paths:
        facts.append(ref(item_id, "changes", {"type": "paths", "values": [path], "repo": repo}, [unit]))
    return facts


def _pull(item_id: str, record: dict, unit: str) -> list[dict]:
    """A pull request of a repo on GitHub: the keys a mention names it by (``repo#57`` with the repo's name in
    harness.yaml, ``owner/repo#57``, "PR #57" in a step of that repo) and the commit that merged it."""
    props = record.get("props") or {}
    repo, number = props.get("repo"), props.get("number")
    if type(number) is not int:
        return [{"t": "issue", "message": f"{item_id}: a pull request without its number", "unit": unit}]
    keys = {f"{repo}#{number}"} if repo else set()
    if isinstance(props.get("github"), str):
        keys.add(f"{props['github'].lower()}#{number}")
    facts: list[dict] = [{"t": "ident", "type": "pr", "key": key, "node": item_id} for key in sorted(keys)]
    if isinstance(props.get("merge_commit"), str):
        target = {"type": "commit", "value": props["merge_commit"], "repo": repo}
        facts.append(ref(item_id, "merged_as", target, [unit]))
    return facts


def frontmatter(text: str) -> tuple[str, int] | None:
    """The YAML block a markdown file opens with, between ``---`` lines, and the line number closing it."""
    m = FRONTMATTER.match(text)
    return (m.group(1), text.count("\n", 0, m.end()) + 1) if m else None


def _frontmatter_unit(record: dict, last_line: int) -> str:
    """The ``_preamble`` fragment owns the frontmatter unless a ``# comment`` line in it split the preamble."""
    for frag in record.get("fragments", []):
        if frag["anchor"] == "_preamble":
            end = (frag.get("span") or "").rpartition("-")[2]
            if end.isdigit() and int(end) >= last_line:
                return frag_unit(record["id"], "_preamble")
    return item_unit(record["id"])


def _markdown(record: dict, text: str, identifiers: Sequence[dict]) -> list[dict]:
    """A frontmatter ``id`` that fully matches an identifier pattern makes the document the place its code
    is defined. A copy names its original in ``derived_from`` and defines nothing (design 4.6)."""
    found = frontmatter(text) if identifiers else None
    if found is None:
        return []
    item_id = record["id"]
    block, last = found
    unit = _frontmatter_unit(record, last)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return [{"t": "issue", "message": f"{item_id}: frontmatter is not valid YAML; its id is ignored", "unit": unit}]
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or data.get("derived_from"):
        return []
    code = data["id"].strip()
    for ident in identifiers:
        if re.fullmatch(ident["pattern"], code):
            nid = f"{ident['kind'].lower()}:{code}"  # the id Linker.scan gives the same code
            where = [[unit, f"lines 1-{last}"]]
            return [
                node(nid, ident["kind"], code, [unit], props={"code": code}, where=where),
                edge(item_id, "defines", nid, [unit], where=where),
            ]
    return []


LINE_COMMENTS = {  # the line comment of a language and the suffixes of its files; a block comment is /* ... */
    "#": (".py", ".sh", ".rb", ".pl", ".r", ".ex", ".exs"),
    "//": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cc", ".cpp"),
    "--": (".sql", ".lua", ".hs"),
}
COMMENT_MARKERS = {suffix: marker for marker, suffixes in LINE_COMMENTS.items() for suffix in suffixes}
COMMENT_MARKERS.update({suffix: "//" for suffix in (".hpp", ".cs", ".swift", ".scala", ".php", ".dart")})
BLOCK_COMMENT_MARKER = {suffix: "/*" for suffix, marker in COMMENT_MARKERS.items() if marker != "#"}


def _comment_lines(text: str, suffix: str) -> dict[int, str]:
    """The comments of a source file, by line number: what follows a line comment marker, and the lines of a
    ``/* ... */`` block. A marker inside a string counts as one, so the text may hold a little more than comments."""
    markers = [m for m in (COMMENT_MARKERS.get(suffix), BLOCK_COMMENT_MARKER.get(suffix)) if m]
    found: dict[int, str] = {}
    inside = False
    for number, line in enumerate(text.splitlines()):
        kept, rest = [], line
        while rest:
            if inside:
                end = rest.find("*/")
                kept.append(rest if end < 0 else rest[:end])
                inside, rest = end < 0, "" if end < 0 else rest[end + 2 :]
                continue
            starts = sorted((rest.find(m), m) for m in markers if m in rest)
            if not starts:
                break
            at, marker = starts[0]
            if marker == "/*":
                inside, rest = True, rest[at + 2 :]
                continue
            kept.append(rest[at + len(marker) :])
            break
        if kept:
            found[number] = " ".join(kept)
    return found


def _comments(record: dict, text: str, identifiers: Sequence[dict]) -> list[dict]:
    """The comments of a source file that hold a code of an identifier (a use case's, say), as one text for the link
    stage with each comment on its line of the file, so a mention sits where it was written; the link stage reads
    only the codes in it. None of the file's other text is read."""
    path = (record.get("props") or {}).get("path") or ""
    suffix = PurePosixPath(path).suffix.lower()
    if not identifiers or suffix not in COMMENT_MARKERS:
        return []
    patterns = [re.compile(i["pattern"]) for i in identifiers]
    lines = _comment_lines(text, suffix)
    held = {n: c for n, c in lines.items() if any(p.search(c) for p in patterns)}
    if not held:
        return []
    joined = "\n".join(held.get(n, "") for n in range(max(held) + 1))
    return [{"t": "text", "src": record["id"], "unit": item_unit(record["id"]), "text": joined, "field": "comment"}]


TSCONFIG_NAMES = ("tsconfig.json", "jsconfig.json")


def _jsonc(text: str):
    """JSON with the comments and trailing commas tsconfig files allow; None when it still does not parse."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError:
        return None


def _tsconfig(record: dict, text: str) -> list[dict]:
    """The module aliases of a ``tsconfig.json`` (``baseUrl`` and ``paths``), as an ident the link stage
    reads to resolve a non-relative import of a file under its directory. ``extends`` is not followed."""
    props = record.get("props") or {}
    path = props.get("path") or ""
    data = _jsonc(text)
    options = data.get("compilerOptions") if isinstance(data, dict) else None
    if not isinstance(options, dict) or not ({"baseUrl", "paths"} & set(options)):
        return []
    directory = posixpath.dirname(path)
    base_url = options.get("baseUrl")
    base = posixpath.normpath(posixpath.join(directory, base_url)) if isinstance(base_url, str) else None
    root = base if base is not None else directory
    paths = {}
    for pattern, targets in sorted((options.get("paths") or {}).items()):
        if isinstance(targets, list):
            paths[pattern] = [posixpath.normpath(posixpath.join(root, t)) for t in targets if isinstance(t, str)]
    ident = {"t": "ident", "type": "tsconfig", "key": f"{props.get('repo')}:{directory}", "node": record["id"]}
    return [{**ident, "base": base, "paths": paths}]


def declares(record: dict) -> bool:
    """Whether an item declares nodes or edges of its own (``props.graph``)."""
    return isinstance((record.get("props") or {}).get("graph"), dict)


def _declared_unit(record: dict, entry: dict, what: str) -> tuple[str | None, dict | None]:
    """The unit that owns a fact an item declares: the fragment its ``anchor`` names, else the whole item; an issue
    instead when it names a fragment the item does not have."""
    anchor = entry.get("anchor")
    if anchor is None:
        return item_unit(record["id"]), None
    if isinstance(anchor, str) and any(f["anchor"] == anchor for f in record.get("fragments", [])):
        return frag_unit(record["id"], anchor), None
    message = f"{record['id']}: a declared {what} names fragment {anchor!r}, which the item does not have"
    return None, {"t": "issue", "message": message, "unit": item_unit(record["id"])}


def _declared_id(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_DECLARED_ID


def _declared_node(record: dict, entry) -> list[dict]:
    ok = isinstance(entry, dict) and _declared_id(entry.get("id")) and isinstance(entry.get("name"), str)
    if not ok or not DECLARED_KIND.match(str(entry.get("kind"))) or not isinstance(entry.get("props", {}), dict):
        return [{"t": "issue", "message": f"{record['id']}: a declared node is not {{id, kind, name}}", "unit": ""}]
    unit, issue = _declared_unit(record, entry, "node")
    if issue is not None:
        return [issue]
    return [node(entry["id"], entry["kind"], entry["name"], [unit], props=entry.get("props"), status="declared")]


def _declared_edge(record: dict, entry) -> list[dict]:
    ok = isinstance(entry, dict) and _declared_id(entry.get("src")) and _declared_id(entry.get("dst"))
    if not ok or not DECLARED_REL.match(str(entry.get("rel"))):
        return [{"t": "issue", "message": f"{record['id']}: a declared edge is not {{src, rel, dst}}", "unit": ""}]
    unit, issue = _declared_unit(record, entry, "edge")
    if issue is not None:
        return [issue]
    return [edge(entry["src"], entry["rel"], entry["dst"], [unit], status="declared")]


def declared(record: dict) -> list[dict]:
    """The nodes and edges an item declares in ``props.graph``: ``{"nodes": [{id, kind, name, props?, anchor?}],
    "edges": [{src, rel, dst, anchor?}]}``. Each is ``declared`` and lives on the fragment its ``anchor`` names, else on
    the whole item, so it carries that unit's label; an edge also needs both its ends, so one to a node no source gives
    is dropped as dangling. A node's label is the meet over every derivation of it: a mention of its code in a public
    document shows it to a sink that clears public, with the name and props of whichever derivation came first. So a
    source declares of a node what any mention could show (a code), and keeps its content in its own fragments, which
    keep its label. An entry that is not one is an issue in coverage, never fatal."""
    graph = (record.get("props") or {}).get("graph")
    if not isinstance(graph, dict):
        return []
    facts: list[dict] = []
    for entry in graph.get("nodes") or []:
        facts += _declared_node(record, entry)
    for entry in graph.get("edges") or []:
        facts += _declared_edge(record, entry)
    for fact in facts:
        if fact["t"] == "issue" and not fact["unit"]:
            fact["unit"] = item_unit(record["id"])
    return facts


def structure_item(
    record: dict, source_id: str, text: str | None, code_backend: str = "auto", identifiers: Sequence[dict] = ()
) -> list[dict]:
    """What one item says beyond its own node: the nodes and edges it declares (``declared``), then what its kind
    holds."""
    return declared(record) + _structure_of_kind(record, source_id, text, code_backend, identifiers)


def _structure_of_kind(
    record: dict, source_id: str, text: str | None, code_backend: str, identifiers: Sequence[dict]
) -> list[dict]:
    kind = record["kind"]
    item_id = record["id"]
    unit = item_unit(item_id)
    if kind == "markdown" and text is not None:
        return _markdown(record, text, identifiers)
    if kind in ("manifest", "contracts", "plan") and text is not None:
        data = _yaml(text)
        if not isinstance(data, dict):
            return [{"t": "issue", "message": f"{item_id}: not a YAML mapping", "unit": unit}]
        if kind == "manifest":
            return _manifest(item_id, data, unit, record)
        return {"contracts": _contracts, "plan": _plan}[kind](item_id, data, unit)
    if kind == "commit":
        return _commit(item_id, record, unit)
    if kind == "pr":
        return _pull(item_id, record, unit)
    if kind == "config" and text is not None:
        name = PurePosixPath((record.get("props") or {}).get("path") or "").name
        return _tsconfig(record, text) if name in TSCONFIG_NAMES else []
    if kind == "code" and text is not None:
        return CODE_EXTRACTORS.facts(record, source_id, text, code_backend) + _comments(record, text, identifiers)
    return []
