"""Identifier index and the link stage: edges from explicit identifiers, never from guesses.

A mention is ``parsed`` when it is fully qualified (a URL of a known item, ``repo:path``,
``path::Symbol``, ``plan#step``, a registered repo, seam or plan name) and ``resolved`` when it is bare
but matches exactly one target (a path or symbol name unique across the project). Ambiguous mentions
link nothing; they are counted, so coverage shows how much prose is still unlinked. A path that names a
directory of a repo links to its Directory node, always ``parsed``: the directory exists exactly because
items of that repo sit under it.

A path written short is read, in order, from the directories above the document's own, from the package roots of
its repo (a top-level Python package, a tsconfig ``baseUrl`` or ``paths`` target: ``hub/server/runs.py`` means
``evo_agents/hub/server/runs.py``), then of other repos, and last as the tail of one path of the project (its own
repo first, then the files a commit or pull request changed). A path written from a root (``/master-plan/x.md``, as a
docs bundle links from its ``docs/``) is read as the tail of one file of the document's own repo. A path whose
directory no repo holds is outside the project, like a URL, and is not counted. A path whose file name holds spaces
(``reports/Backend cho agent.md``) and a document at a repo's root named in capitals (``CLUSTER.md``, of the plan step's repo, then of the item's) link only
when they are files of the project, and are not counted otherwise. A plain lower-case word in backticks (```name```) is prose, not a symbol;
a short name several symbols share goes to the one nearest the document: its repo, the files a commit changed,
the directories it shares with the document, the topic its file name gives, and outside tests for a text that is
not a test's. It stays ambiguous only when they tie.

A path ``x.py`` the project no longer holds names the package ``x/__init__.py`` when that one is there: the module
became a package. A path still not found is not counted as dangling when the text says it is of another tree, or
when it was a file the history read: in a shell command after ``cd ~/github/<name>`` (until the subshell closes),
written next to a repo's name (``evo-cli `tests/x.py```, ``tests/x.py of evo-cli``), in a table row whose first
cell is a repo, or in a plan step of a repo, it is read in that repo, and is outside when that repo is one
harness.yaml declares but no source reads (a ``repo:path`` of such a repo is outside too, and ``name@repo:path``
is a plugin id, not a repo path); a plan file moved between ``plans/active/`` and ``plans/completed/`` names its
new path; a placeholder (``tests/test_x.py``, ``release/X.Y.Z``, ``images/name.png``) and a lockfile name no
file the graph holds; the text of a test fixture describes a project of its own. A dated item (a commit, a pull
request, a chat message, a plan or document under ``completed/`` or an archive directory) wrote of the tree as it
was then: a path some commit read changed there is ``past``, not a missing file.

A call into another file (a ``calls`` reference of the structure stage) links its caller to the symbol
the file's imports name, ``resolved``: each ``via`` entry names the files to look in (a module, candidate
paths, a package, a Go import path, a JS or TS specifier read through the nearest tsconfig) and the
qualified name to find there. The first entry that finds a definition decides; two definitions are
ambiguous and link nothing. Python module names resolve only inside the caller's source: a matching
module in another repo does not prove a dependency. Cross-repo dependencies can be declared in bindings.
A call through ``this``, ``self`` or ``super`` names a class of its own file
instead; the index's class hierarchy (``pipeline.hierarchy``) finds the method up that class's chain, and
gives each class its ``inherits`` edges and each method its ``overrides`` edges. A Python call through a typed
receiver (``extract.pytypes``) names the class by a type expression: the class an annotation names, or what a
callee returns, an instance of it when it is a class, else the class its return annotation names, which the
index resolves once a build in the callee's own file; the method is then found up that class's chain. A call of a
method also links, at confidence 0.6, to each method that overrides it below, and for a method of a
``typing.Protocol`` to the method of each class that defines all the protocol's methods: what the call may run.
A read of a function without a call (a property, a callback ``self._m`` passed on) links as a call, never to a
class. Calls are counted (``calls_resolved``, ``calls_ambiguous``, ``calls_unresolved``) when they name a scope of the
project; one into a library the index does not hold is not.

A SHA names a commit a git source read from its history when exactly one such commit starts with it: ``repo@sha``
among the commits of that repo, a bare SHA in a plan step's evidence among those of the step's repo first, then of
every repo; such a mention links to the commit's item, and one that names no commit read gets a Commit node of its own
as before. A pull request is named by its URL, ``owner/repo#57``, ``repo#57`` with a repo name of harness.yaml, or
"PR #57" in the text of a step of that repo.

A link task notes what it reads of the index (``Reads``), and the build memoizes it by those entries rather than by
the whole index (``Memo.run_traced``): a changed file links again the items that looked up one of its names or
paths, not every item of the project.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from urllib.parse import unquote

from evo_agents.kg.extract.xref import module_files
from evo_agents.kg.health import is_test_path
from evo_agents.kg.pipeline import hierarchy
from evo_agents.kg.pipeline.stages import edge, frag_unit, item_unit, node, normalize_url
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.schema import node_kind_for_item

LINK_VERSION = "18"

URL = re.compile(r"https?://[^\s<>()\[\]{}`'\"|]+")
BACKTICK = re.compile(r"`([^`\n]{2,200})`")
QUALIFIED_SYMBOL = re.compile(r"(?<![\w/.-])(?:([A-Za-z0-9_.-]+):)?((?:[\w.-]+/)*[\w.-]+\.\w{1,6})::([A-Za-z_][\w.]*)")
REPO_PATH = re.compile(r"(?<![\w/.:@-])([A-Za-z0-9_.-]+):((?:[\w.-]+/)*[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
PATH = re.compile(r"(?<![\w/.:@-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
# A path whose file name holds spaces (`reports/Backend tập trung cho agent.md`), up to its first extension: linked only
# when it is a file of the project as written.
SPACED_PATH = re.compile(
    r"(?<![\w/.:@-])((?:[\w.-]+/)+[\w.-][^\n/`'\"()\[\]<>|*]{0,150}?\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])"
)
# A document at a repo's root named in capitals (CLUSTER.md, AGENTS.md): one of the step's or the item's repo.
ROOT_DOC = re.compile(r"(?<![\w/.:@-])([A-Z][A-Z0-9_-]*\.md)(?![\w/])")
DOI = re.compile(r"(?<![\w/.])10\.\d{4,9}/[^\s)\]>,;]+")
MD_LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
REPO_SHA = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+)@([0-9a-f]{7,40})\b")
BARE_SHA = re.compile(r"(?<![\w/.@-])([0-9a-f]{7,40})(?![\w-])")
PLAN_STEP = re.compile(r"(?<![\w/.-])([a-z0-9][a-z0-9-]{2,})#(\d+)\b")
GITHUB_PULL = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")  # owner/repo#57
PULL_WORD = re.compile(r"\b(?:PR|[Pp]ull [Rr]equest) ?#(\d+)\b")  # "PR #57" in the text of a step of that repo
TOKEN = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9])(?![\w-])")
SYMBOLISH = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*(?:\(\))?$")
# `cd ~/github/evo-cli`, `cd /srv/app`, `cd "$HOME/x"`: a shell command moving into a tree named by its last segment.
CD = re.compile(r"(?<![\w-])cd\s+['\"]?((?:~|/|\$)[^\s;&|()'\"]*)")
REPO_BEFORE = re.compile(r"([A-Za-z0-9_.-]+)(?:'s)?[\s`'\"]*$")  # `evo-cli tests/x.py`, evo-cli's `tests/x.py`
REPO_AFTER = re.compile(r"^[`'\"]*\s+(?:of|in|from|của|trong)\s+[`'\"]?([A-Za-z0-9_.-]+)")  # tests/x.py of evo-cli
PLACEHOLDERS = frozenset({"x", "xx", "xxx", "y", "z", "n", "foo", "bar", "baz", "name", "example", "placeholder"})
# The directories of a dated plan or document: what sits there wrote of the tree as it was then.
DATED_DIR = re.compile(r"(^|/)(completed|done|archive|archived)/", re.IGNORECASE)
DATED_KINDS = frozenset({"commit", "pr", "message"})
PLAN_FILE = re.compile(r"(^|/)plans/(active|completed)/([^/]+\.ya?ml)$")
FIXTURE_DIRS = frozenset({"fixtures", "evals", "mocks"})  # test data and eval scenarios: a project of their own
LOCKFILE = re.compile(
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum)$"
)


def is_placeholder(path: str) -> bool:
    """Whether a path's file name is a placeholder: its words (split at ``.``, ``_``, ``-``, the extension too) are
    ``test`` and placeholders such as ``x``, ``X.Y.Z``, ``name``, at least one a placeholder."""
    words = [w.lower() for w in re.split(r"[._-]", posixpath.basename(path.rstrip("/"))) if w]
    return any(w in PLACEHOLDERS for w in words) and all(w in PLACEHOLDERS or w == "test" for w in words[:-1])


def build_index(facts_by_item: dict[str, list[dict]], identifiers: list[dict]) -> dict:
    index: dict = {
        "ids": set(),
        "repo": {},
        "seam": {},
        "plan": {},
        "step": {},
        "url": {},
        "pr": {},  # "<repo>#57" and "<owner>/<repo>#57" -> the pull request
        "commit": defaultdict(list),  # the first 7 characters of a SHA -> the commits read that start with them
        "path": defaultdict(list),
        "dir": defaultdict(list),
        "module": defaultdict(list),
        "modsuffix": defaultdict(list),  # "core.log" -> modules ending with it, "src.core.log": a src/ layout
        "symbol": defaultdict(list),
        "symname": defaultdict(list),
        "def": {},  # "<file item>::<qualname>" -> the symbol it defines there
        "pkgdef": defaultdict(list),  # "<package>::<qualname>" -> the symbols there, "jvm:com.acme.a::A.f"
        "tsconfig": {},  # "<repo>:<dir>" -> baseUrl and paths of the tsconfig.json there
        "classes": {},  # class symbol -> (its file item, its qualname)
    }
    with_bases: list[dict] = []
    typed: list[dict] = []
    methods: list[dict] = []
    for facts in facts_by_item.values():
        for f in facts:
            if f["t"] == "node":
                index["ids"].add(f["id"])
            elif f["t"] == "ident":
                _learn(index, f)
                if f["type"] == "symbol" and f.get("bases"):
                    with_bases.append(f)
                if f["type"] == "symbol" and (f.get("returns") or f.get("items")):
                    typed.append(f)
                if f["type"] == "symbol" and f.get("kind") == "method" and f.get("file"):
                    methods.append(f)
    index["gosuffix"] = _go_suffixes({key.partition("::")[0] for key in index["pkgdef"]})
    index["roots"] = _package_roots(index["path"], index["tsconfig"])
    index["pathsuffix"] = _path_suffixes(index["path"])
    index["dirsuffix"] = {"/".join(d.split("/")[i:]) for d in index["dir"] for i in range(d.count("/") + 1)}
    index["read_repos"] = {e["repo"] for entries in index["path"].values() for e in entries if e.get("repo")}
    index["past"] = _past_paths(index["commit"])
    # Once per build, not once per item: a project with thousands of known URLs spent most of a link run here.
    index["url_prefix"] = {_url_prefix(u) for u in index["url"]}
    index["identifiers"] = [
        {"kind": i["kind"], "pattern": i["pattern"], **({"aliases": i["aliases"]} if i.get("aliases") else {})}
        for i in identifiers
    ]
    hierarchy.resolve(index, with_bases, lambda file, repo, path: Linker(index, file, repo, path))
    _overriders(index, methods, with_bases)
    _return_types(index, typed)
    return index


def _return_types(index: dict, functions: list[dict]) -> None:
    """``index["returns"]``: function -> the class its return annotation names; ``index["returns_items"]``:
    function -> the class of each item of the tuple it returns (None for an item that names none). Each is
    resolved in the function's own file, as a base is."""
    contexts: dict[tuple, Linker] = {}
    returns: dict[str, str] = {}
    items: dict[str, tuple] = {}
    for f in sorted(functions, key=lambda f: f["node"]):
        key = (f["file"], f.get("repo"), f.get("path"))
        if key not in contexts:
            contexts[key] = Linker(index, *key)
        ctx, target = contexts[key], {"repo": f.get("repo"), "source": f.get("source")}
        if f.get("returns"):
            found = ctx.first_found(f["returns"], target, index["classes"])
            if found:
                returns[f["node"]] = found
        if f.get("items"):
            found_items = tuple(ctx.first_found(v, target, index["classes"]) if v else None for v in f["items"])
            if any(found_items):
                items[f["node"]] = found_items
    index["returns"], index["returns_items"] = returns, items


PROTOCOL_MODULES = ("typing", "typing_extensions")


def _overriders(index: dict, methods: list[dict], classes: list[dict]) -> None:
    """Set ``index["overriders"]``: method -> ``[method, class]`` of each method that overrides it, at any depth
    below, what a call of it may run (``self.m()`` in a base reaches the ``m`` of each subclass); and
    ``index["implements"]``: class -> the ``typing.Protocol`` classes it implements the way Python checks one, by
    defining every method the protocol does. A method of a protocol is overridden by those classes' methods."""
    direct: dict[str, set[str]] = defaultdict(set)
    owner_of: dict[str, str] = {}
    members: dict[str, dict[str, str]] = defaultdict(dict)  # class -> its methods by name
    for f in methods:
        owner, _, name = f["qualname"].rpartition(".")
        cls = index["def"].get(f"{f['file']}::{owner}")
        if not cls:
            continue
        members[cls][name], owner_of[f["node"]] = f["node"], cls
        for base in index["bases"].get(cls, ()):
            found = hierarchy.find_member(index, base, name)
            if found and found[0] != f["node"]:
                direct[found[0]].add(f["node"])
    protocols = {
        f["node"]
        for f in classes
        if any(
            kind == "module" and value in PROTOCOL_MODULES and qual == "Protocol"
            for via in f["bases"]
            for kind, value, qual in via
        )
    }
    implements: dict[str, list[str]] = defaultdict(list)
    for proto in sorted(protocols):
        wanted = members.get(proto, {})
        for cls, own in members.items():
            if cls not in protocols and wanted and wanted.keys() <= own.keys():
                implements[cls].append(proto)
                for name, method in wanted.items():
                    direct[method].add(own[name])
    out: dict[str, list[list[str]]] = {}
    for method in direct:
        seen, todo = set(), [method]
        while todo:
            for below in direct.get(todo.pop(), ()):
                if below not in seen:
                    seen.add(below)
                    todo.append(below)
        out[method] = [[m, owner_of[m]] for m in sorted(seen - {method})]
    index["overriders"], index["implements"] = out, dict(implements)


def _learn(index: dict, f: dict) -> None:
    """Add one ``ident`` fact to the index."""
    kind = f["type"]
    if kind in ("repo", "seam", "plan", "step", "url", "pr"):
        index[kind].setdefault(f["key"], f["node"])
    elif kind == "commit":
        entry = {"sha": f["key"], "repo": f.get("repo"), "node": f["node"], "paths": f.get("paths") or []}
        index["commit"][f["key"][:7]].append(entry)
    elif kind == "path":
        index["path"][f["key"]].append({"node": f["node"], "repo": f.get("repo"), "source": f["source"]})
    elif kind == "dir":  # every item under a directory names it: keep one entry per node
        entry = {"node": f["node"], "repo": f.get("repo")}
        if entry not in index["dir"][f["key"]]:
            index["dir"][f["key"]].append(entry)
    elif kind == "module":
        index["module"][f["key"]].append({"node": f["node"], "source": f["source"]})
        parts = f["key"].split(".")
        for i in range(1, len(parts)):
            index["modsuffix"][".".join(parts[i:])].append({"node": f["node"], "source": f["source"]})
    elif kind == "symbol":
        _learn_symbol(index, f)
    elif kind == "alias":  # a module-level name bound to a def of its file
        index["def"].setdefault(f["key"], f["node"])
    elif kind == "tsconfig":
        index["tsconfig"].setdefault(f["key"], {"base": f["base"], "paths": f["paths"]})


def _learn_symbol(index: dict, f: dict) -> None:
    entry = {"node": f["node"], "repo": f.get("repo"), "source": f["source"]}
    index["symbol"][f["key"]].append(entry)
    named = {"node": f["node"], "repo": f.get("repo"), "path": f.get("path") or ""}
    if f.get("kind") == "constant":
        named["constant"] = True
    index["symname"][f["qualname"]].append(named)
    if f["name"] != f["qualname"]:
        index["symname"][f["name"]].append(named)
    if f.get("file"):
        index["def"].setdefault(f"{f['file']}::{f['qualname']}", f["node"])
        if f.get("kind") == "class":
            index["classes"].setdefault(f["node"], (f["file"], f["qualname"]))
    if f.get("package"):
        index["pkgdef"][f"{f['package']}::{f['qualname']}"].append(entry)


def _package_roots(paths: dict, tsconfigs: dict) -> dict[str, list[str]]:
    """Repo -> the directories a path written short is read from: each top-level Python package (a directory with
    an ``__init__.py`` whose parent has none), and each directory a tsconfig's ``baseUrl`` or ``paths`` map onto
    (``web/src`` for ``"@/*": ["./src/*"]``). ``hub/server/runs.py`` in prose means ``evo_agents/hub/server/runs.py``."""
    inits: dict[str, set[str]] = defaultdict(set)
    for key, entries in paths.items():
        if posixpath.basename(key) == "__init__.py":
            for e in entries:
                inits[e.get("repo")].add(posixpath.dirname(key))
    roots: dict[str, set[str]] = defaultdict(set)
    for repo, dirs in inits.items():
        roots[repo] |= {d for d in dirs if d and posixpath.dirname(d) not in dirs}
    for key, config in tsconfigs.items():
        repo = key.partition(":")[0]
        targets = [t.partition("*")[0] for ts in config["paths"].values() for t in ts]
        for d in [*targets, config["base"] or ""]:
            d = d.rstrip("/")
            if d and d != ".":
                roots[repo].add(d)
    return {repo: sorted(dirs) for repo, dirs in sorted(roots.items(), key=lambda kv: str(kv[0]))}


def _path_suffixes(paths: dict) -> dict[str, list[dict]]:
    """Every trailing run of two segments or more of a file's path, to the files ending with it: ``worker/cli.py``
    -> ``evo_agents/worker/cli.py``. A bare file name is too weak to name one file."""
    out: dict[str, list[dict]] = defaultdict(list)
    for key, entries in paths.items():
        segments = key.split("/")
        for i in range(1, len(segments) - 1):
            out["/".join(segments[i:])].extend({**e, "path": key} for e in entries)
    return out


def _past_paths(commits: dict[str, list[dict]]) -> set[str]:
    """Every path a commit read changed, and each trailing run of two segments or more of it: the files the history
    knows, written as a dated item may write them (``hub/server/runs.py``)."""
    out: set[str] = set()
    for entries in commits.values():
        for e in entries:
            for path in e["paths"]:
                segments = path.split("/")
                out.update("/".join(segments[i:]) for i in range(max(len(segments) - 1, 1)))
    return out


def _go_suffixes(packages: set[str]) -> dict[str, list[dict]]:
    """Every trailing run of segments of a Go package directory, to the directories ending with it. A Go
    import path ends with its package's directory below the module root, which the corpus does not record."""
    out: dict[str, list[dict]] = defaultdict(list)
    for key in sorted(packages):
        if not key.startswith("go:") or key == "go:":
            continue
        segments = key[3:].split("/")
        for i in range(len(segments)):
            out["/".join(segments[i:])].append({"dir": key[3:], "whole": i == 0})
    return out


def index_digest(index: dict) -> str:
    plain = {
        k: (
            sorted(v)
            if isinstance(v, set)
            else {kk: sorted(vv, key=str) if isinstance(vv, list) else vv for kk, vv in sorted(v.items())}
            if isinstance(v, dict)
            else v
        )
        for k, v in index.items()
    }
    return sha256(canonical_json(plain))


# Tables a link task reads whole: small, seldom changed, and looked up once per word of prose, so noting each key
# would make the list of what a task read longer than its output.
WHOLE_TABLES = frozenset({"repo", "seam", "plan", "identifiers"})
WHOLE, KEY = 0, 1  # how a task read a table: whole, or one key of it
NONE_KEY = "\0none"  # the key None, as the list of what a task read writes it


class Reads:
    """The identifier index as one link task sees it, noting what the task reads: one key of a table (absent keys
    too), or a whole table of WHOLE_TABLES. ``keys()`` lists them for ``Memo.run_traced``; ``Held`` digests what
    they hold."""

    def __init__(self, index: dict):
        self._index = index
        self._keys: set[tuple] = set()

    def __getitem__(self, table: str) -> _Table:
        return _Table(self, table, self._index[table])

    def get(self, table: str, default=None):
        return _Table(self, table, self._index[table]) if table in self._index else default

    def note(self, table: str, key=None) -> None:
        self._keys.add((table, WHOLE, "") if key is None or table in WHOLE_TABLES else (table, KEY, key))

    def keys(self) -> list[list]:
        return [list(k) for k in sorted(self._keys, key=canonical_json)]


class _Table:
    """One table of a ``Reads``: every lookup is noted before it is answered as the table answers it."""

    __slots__ = ("_name", "_reads", "_table")

    def __init__(self, reads: Reads, name: str, table):
        self._reads, self._name, self._table = reads, name, table

    def _key(self, key):
        self._reads.note(self._name, NONE_KEY if key is None else key)
        return key

    def get(self, key, default=None):
        return self._table.get(self._key(key), default)

    def __getitem__(self, key):
        return self._table[self._key(key)]

    def __contains__(self, key) -> bool:
        return self._key(key) in self._table

    def __iter__(self):
        self._reads.note(self._name)
        return iter(self._table)

    def __len__(self) -> int:
        self._reads.note(self._name)
        return len(self._table)


class Held:
    """What the entries of one build's identifier index hold, each digested once: ``digest(keys)`` is the ``reads``
    input of a traced link task that read ``keys`` (``Reads.keys``)."""

    def __init__(self, index: dict):
        self.index = index
        self._cache: dict[tuple, str] = {}

    def digest(self, keys: list[list]) -> str:
        return sha256(canonical_json([self._one(*k) for k in keys]))

    def _one(self, table: str, how: int, key: str) -> str:
        found = self._cache.get((table, how, key))
        if found is None:
            held, real = self.index.get(table), None if key == NONE_KEY else key
            if how == WHOLE:
                value = ["whole", _plain(held)]
            elif isinstance(held, (set, frozenset)):
                value = ["member", real in held]
            else:
                value = ["value", _plain(held[real])] if held is not None and real in held else ["absent"]
            found = sha256(canonical_json([table, how, key, value]))
            self._cache[(table, how, key)] = found
        return found


def _plain(value):
    """``value`` as canonical JSON can write it: sets sorted, tuples as lists, dict keys as they are."""
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _url_prefix(url: str) -> str:
    """The collection a URL is in: forge URLs up to the kind after owner/repo (``.../pull``, ``.../commit``,
    ``.../blob``, past GitLab's ``-``), other URLs without their last segment (``https://hub/chats`` of
    ``https://hub/chats/30``). A mention in a collection some item's URL is in that matches no item is a dangling
    link; any other URL (a host alone, ``/v1/health`` of the hub, a forge's ``actions/runs``) is just outside the
    project."""
    parts = url.split("/")
    if len(parts) < 4 or not parts[3]:
        return url
    if any(marker in parts[5:6] for marker in ("blob", "tree", "-")) or parts[2] in ("github.com", "gitlab.com"):
        kind = 6 if parts[5:6] == ["-"] else 5
        return "/".join(parts[: kind + 1])
    return "/".join(parts[:-1])


def looks_like_symbol(name: str) -> bool:
    """Whether a name in backticks may be a symbol: written as code (``_``, a dot, camelCase or a capital), not a
    plain lower-case word such as `settings` or `coverage`, which prose uses for the thing and not the code, nor a
    dunder alone."""
    if name.startswith("__") and name.endswith("__"):
        return False  # `__init__` names a protocol every class may have, not one symbol
    return "_" in name or "." in name or not name.islower()


def _segments(path: str) -> set[str]:
    """The directory names of a path and its file's stem: what a document named ``srs-plugin.md`` may be about."""
    *dirs, name = path.lower().split("/")
    return {*dirs, posixpath.splitext(name)[0]}


def _shared(a: list[str], b: list[str]) -> int:
    """How many leading segments two paths share."""
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _unique(entries: list) -> str | None:
    nodes = {e["node"] if isinstance(e, dict) else e for e in entries}
    return next(iter(nodes)) if len(nodes) == 1 else None


class Linker:
    def __init__(
        self, index: dict, item_id: str, repo: str | None, path: str | None = None, defined: frozenset = frozenset()
    ):
        self.index = index
        self.defined = defined  # (src, code) pairs the structure stage already links with ``defines``
        self.item_id = item_id
        self.repo = repo
        self.base_dir = posixpath.dirname(path) if path else ""
        self.path = path or ""
        self.touched: frozenset[str] = frozenset()  # the files a commit changed: what its message most likely names
        self.dated = bool(DATED_DIR.search(self.path))  # wrote of the tree as it was (``link_item`` sets it by kind)
        self.fixture = bool(FIXTURE_DIRS & set(self.path.split("/")[:-1]))  # the text of a project of its own
        self.edges: dict[tuple, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.stats = {
            "mentions": 0,
            "resolved": 0,
            "ambiguous": 0,
            "dangling": 0,
            "refs": 0,
            "refs_resolved": 0,
            "refs_ambiguous": 0,
            "refs_dangling": 0,
            "calls": 0,
            "calls_resolved": 0,
            "calls_ambiguous": 0,
            "calls_unresolved": 0,
        }
        self.samples: dict[str, list[str]] = {"ambiguous": [], "dangling": [], "calls_unresolved": []}
        self.url_prefixes = index["url_prefix"]
        self.patterns = [(i["kind"], re.compile(i["pattern"]), i.get("aliases") or {}) for i in index["identifiers"]]

    # -- recording ------------------------------------------------------------------------------

    def _link(
        self, src: str, target: str, unit: str, start: int, status: str, form: str, field: str, rel: str = "mentions"
    ) -> None:
        if target == src or target == self.item_id or target.startswith(self.item_id + "#"):
            return
        if rel == "mentions" and (src, target) in self.defined:
            return  # a frontmatter id: the document defines the code, it does not also mention it
        key = (src, rel, target)
        if key in self.edges:
            self.edges[key]["props"]["count"] += 1
            return
        self.edges[key] = edge(
            src,
            rel,
            target,
            [unit],
            props={"form": form, "count": 1, "field": field},
            status=status,
            conf=1.0 if status == "parsed" else 0.9,
            needs=[target],
            where=[[unit, f"char {start}"]],
        )

    def _ref(self, outcome: str, text: str | None = None) -> None:
        """Structured references (YAML fields, imports) are counted apart from mentions in prose."""
        self.stats["refs"] += 1
        self.stats[f"refs_{outcome}"] += 1
        if text and outcome in self.samples and len(self.samples[outcome]) < 5:
            self.samples[outcome].append(text[:120])

    def _count(self, outcome: str, text: str | None = None) -> None:
        self.stats["mentions"] += 1
        self.stats[outcome] += 1
        if text and outcome in self.samples and len(self.samples[outcome]) < 5:
            self.samples[outcome].append(text[:120])

    # -- resolution -----------------------------------------------------------------------------

    def _at(self, key: str, as_dir: bool, keep=None) -> tuple[list[dict], bool]:
        """Index entries at one path that ``keep`` accepts, and whether they are directories: files when
        any is accepted, else directories. A path written with a trailing slash only names directories."""
        for table in ("dir",) if as_dir else ("path", "dir"):
            entries = [e for e in self.index[table].get(key, []) if keep is None or keep(e)]
            if entries:
                return entries, table == "dir"
        return [], False

    def path_target(self, path: str, repo: str | None, guess: bool = False) -> tuple[str | None, str]:
        """``_path_target``, and for a module ``x.py`` it finds nothing at, the package ``x/__init__.py`` it became."""
        found = self._path_target(path, repo, guess)
        if found[0] is None and path.endswith(".py") and posixpath.basename(path) != "__init__.py":
            package = self._path_target(f"{path[:-3]}/__init__.py", repo, guess)
            if package[0] is not None:
                return package
        return found

    def unseen(self, path: str, text: str, span: tuple[int, int], step_repo: str | None) -> tuple[str | None, str]:
        """What a path ``path_target`` found dangling names after all, by what the text around it says (the module
        docstring): the file of the tree it is of, ``outside`` for a tree no source reads, a placeholder or a fixture's
        text, ``past`` for a file the history read in a dated item; else still ``dangling``."""
        tree = self.tree_of(text, span) or step_repo
        if tree and tree != self.repo:
            if tree not in self.index["read_repos"]:
                return None, "outside"
            found = self.path_target(path, tree)
            if found[0] is not None:
                return found
        moved = PLAN_FILE.search(path)
        if moved:  # a plan closed or reopened since: plans/active/x.yaml is now plans/completed/x.yaml
            other = "completed" if moved.group(2) == "active" else "active"
            found = self.path_target(f"{path[: moved.start(2)]}{other}/{moved.group(3)}", None)
            if found[0] is not None:
                return found
        if self.fixture or is_placeholder(path) or LOCKFILE.search(path):
            return None, "outside"
        if self.dated and posixpath.normpath(path).lstrip("/") in self.index["past"]:
            return None, "past"
        return None, "dangling"

    def tree_of(self, text: str, span: tuple[int, int]) -> str | None:
        """The tree a path at ``span`` of ``text`` is written of: the last segment of the last ``cd`` before it whose
        subshell is still open, else a repo of harness.yaml named next to it or in the first cell of its table row."""
        before = text[: span[0]]
        line = before[before.rfind("\n") + 1 :].lstrip()
        if line.startswith("|"):
            first = line.split("|")[1].strip().strip("`*").strip()
            if first in self.index["repo"]:
                return first
        moves = list(CD.finditer(before))
        if moves:
            rest = before[moves[-1].end() :]
            if rest.count(")") <= rest.count("("):
                name = posixpath.basename(moves[-1].group(1).rstrip("/"))
                if name and not name.startswith("$"):
                    return name
        for m in (
            REPO_BEFORE.search(text, max(0, span[0] - 60), span[0]),
            REPO_AFTER.match(text[span[1] : span[1] + 60]),
        ):
            if m and m.group(1) in self.index["repo"]:
                return m.group(1)
        return None

    def _path_target(self, path: str, repo: str | None, guess: bool = False) -> tuple[str | None, str]:
        """The file or directory a path names, and how: ``parsed`` relative to this item's directory or with its repo,
        ``resolved`` as written from a repo's root, ``ambiguous`` or ``dangling`` when it finds two or none. With
        ``guess`` (a path in prose), a path not found as written is also tried ``resolved``: relative to a directory
        above this item's, under a package root of a repo (``_package_roots``), then as the end of one file's path,
        in this item's repo before any other; a path from a root (``/x/y.md``), as the end of one file's path in this
        item's repo alone (``_site_path``). A path still not found whose directory no directory of the project ends
        with is ``outside``, as a URL of an unknown host is: it names a file of another tree (``.evo-run/plan.yaml``,
        ``icons/x.svg`` of a vendored package), not a missing file of this one."""
        as_dir = path.endswith("/")
        if not repo and self.base_dir and not path.startswith("/"):
            relative = posixpath.normpath(posixpath.join(self.base_dir, path))
            near, _ = self._at(relative, as_dir, lambda e: e.get("repo") == self.repo)
            if len(near) == 1:
                return near[0]["node"], "parsed"
        key = posixpath.normpath(path).lstrip("/")
        if repo:
            scoped, _ = self._at(key, as_dir, lambda e: e.get("repo") == repo)
            if scoped:
                return _unique(scoped), "parsed"
            return None, "dangling"
        same, is_dir = self._at(key, as_dir, lambda e: self.repo and e.get("repo") == self.repo)
        if len(same) == 1:
            return same[0]["node"], "parsed" if is_dir else "resolved"
        entries, is_dir = self._at(key, as_dir)
        if entries:
            target = _unique(entries)
            return (target, "parsed" if is_dir else "resolved") if target else (None, "ambiguous")
        if guess and key not in (".", "") and not path.startswith(("./", "../", "//")):
            found = self._site_path(key, as_dir) if path.startswith("/") else self._guess_path(key, as_dir)
            parent = posixpath.dirname(key.rstrip("/"))
            if found[1] == "dangling" and parent and parent not in self.index["dirsuffix"]:
                return None, "outside"  # no directory of the project ends so: a path of another tree
            return found
        return None, "dangling"

    def _site_path(self, key: str, as_dir: bool) -> tuple[str | None, str]:
        """A path written from the root of a site or a docs bundle (``/master-plan/x.md`` for ``docs/master-plan/x.md``
        when the bundle's root is ``docs/``): the one file of this item's repo whose path ends with it, those under a
        directory above this item first (a fixture's copy of the bundle is another site)."""
        if as_dir or not self.repo:
            return None, "dangling"
        found = [e for e in self.index["pathsuffix"].get(key, []) if e.get("repo") == self.repo]
        if not found:
            return None, "dangling"
        above = f"{self.base_dir}/" if self.base_dir else None
        target = _unique([e for e in found if above and above.startswith(e["path"][: -len(key)])] or found)
        return (target, "resolved") if target else (None, "ambiguous")

    def _guess_path(self, key: str, as_dir: bool) -> tuple[str | None, str]:
        """A path written short: the one match of the first way that finds any (``path_target``)."""
        found: list[dict] = []
        directory = posixpath.dirname(self.base_dir) if self.base_dir else ""
        while directory and not found:  # the directories above this item's, nearest first
            found, _ = self._at(f"{directory}/{key}", as_dir, lambda e: e.get("repo") == self.repo)
            directory = posixpath.dirname(directory)
        if not found:
            for repo in [self.repo] if self.repo else []:
                for root in self.index["roots"].get(repo, []):
                    found += self._at(f"{root}/{key}", as_dir, lambda e, r=repo: e.get("repo") == r)[0]
        if not found:
            found, _ = self._at_roots(key, as_dir)
        if not found and not as_dir:
            entries = self.index["pathsuffix"].get(key, [])
            found = [e for e in entries if self.repo and e.get("repo") == self.repo] or entries
            if len(found) > 1 and self.touched:  # a commit's message names a file it changed
                found = [e for e in found if e["path"] in self.touched] or found
        if not found:
            return None, "dangling"
        target = _unique(found)
        return (target, "resolved") if target else (None, "ambiguous")

    def _at_roots(self, key: str, as_dir: bool) -> tuple[list[dict], bool]:
        """Entries at ``key`` under a package root of any repo other than this item's."""
        found: list[dict] = []
        for repo in self.index["roots"]:
            if repo == self.repo:
                continue
            for root in self.index["roots"].get(repo, []):
                found += self._at(f"{root}/{key}", as_dir, lambda e, r=repo: e.get("repo") == r)[0]
        return found, False

    def resolve_call(self, f: dict) -> None:
        """Link a caller to the one symbol the first ``via`` entry with a definition finds, and to the methods
        overriding it that the receiver may run (``dispatched``, confidence 0.6); only to those for ``dispatch``, a
        call the structure stage resolved in its file. A read without a call (``read``) links only to a function,
        never to a class, and is not counted: most reads are of data."""
        target = f["target"]
        found = self.first_found(target["via"], target)
        if target.get("read"):
            if found in self.index["classes"]:
                return
        elif not target.get("dispatch"):  # the structure stage already linked and counts a call it resolved
            self._call(found, target)
        if not found:
            return
        direct = [] if target.get("dispatch") else [(found, 0.8)]
        for dst, conf in direct + [(m, 0.6) for m in self.dispatched(found, target)]:
            key = (f["src"], "calls", dst)
            if dst != f["src"] and key not in self.edges:
                self.edges[key] = edge(
                    f["src"], "calls", dst, f["units"], status="resolved", conf=conf, needs=[dst], where=f["where"]
                )

    def dispatched(self, found: str, target: dict) -> list[str]:
        """The methods overriding ``found`` that a call through ``self`` or a typed receiver may run instead: those
        of a class whose chain holds the receiver's class, or a protocol it implements. ``super().m()`` and
        ``C.m()`` run ``found`` itself."""
        below = self.index["overriders"].get(found)
        if not below:
            return []
        receiver = None
        for kind, value, qual in target["via"]:
            if self.definitions(kind, value, qual, target):
                if kind == "self":
                    receiver = self.index["def"].get(f"{self.item_id}::{value}")
                elif kind == "typed":
                    receiver = self.class_of(value, target)
                break
        if receiver is None:
            return []
        implements = self.index["implements"]
        return [
            m
            for m, owner in below
            if any(c == receiver or receiver in implements.get(c, ()) for c in self.index["mro"].get(owner, (owner,)))
        ]

    def _call(self, found: str | None, target: dict) -> None:
        """Count a call: resolved when it found its one symbol; ambiguous when a ``via`` entry finds two; unresolved
        when an entry names a scope of the project (a class of this file, a typed receiver, a Python module the index
        holds) that has no such member. A call into a library the index does not hold is not counted."""
        outcome, named = ("resolved" if found else None), None
        for kind, value, qual in [] if found else target["via"]:
            if len(set(self.definitions(kind, value, qual, target))) > 1:
                outcome = "ambiguous"
                break
            if named is None and self._in_project(kind, value, qual, target):
                outcome, named = "unresolved", f"{kind}:{value}:{qual}"
        if outcome is None:
            return
        self.stats["calls"] += 1
        self.stats[f"calls_{outcome}"] += 1
        if named and len(self.samples["calls_unresolved"]) < 5:
            self.samples["calls_unresolved"].append(named[:120])

    def _in_project(self, kind: str, value, qual: str, target: dict) -> bool:
        """Whether a ``via`` entry that found nothing names a scope of the project: a class of this file, a typed
        receiver, a module the index holds (a dotted module only by suffix, ``secrets`` is the standard library's),
        and for a dotted ``qual`` its head a class defined there (``log.info`` is a method of data, ``client.get`` of a
        fixture's value). A class without its own ``__init__`` is called through ``object``'s."""
        head = qual.partition(".")[0]
        if qual == "__init__" or kind not in ("local", "self", "super", "typed", "module"):
            return False
        if kind == "local":
            return "." in qual and self.index["def"].get(f"{self.item_id}::{head}") in self.index["classes"]
        if kind != "module":
            return kind != "typed" or self.class_of(value, target) is not None
        files = [e["node"] for e in self.index["module"].get(value, [])]
        if not files and "." in value:
            files = [e["node"] for e in self.index["modsuffix"].get(value, [])]
        classes = self.index["classes"]
        return bool(files) and ("." not in qual or any(self.index["def"].get(f"{f}::{head}") in classes for f in files))

    def first_found(self, via: list, target: dict, keep=None) -> str | None:
        """The one symbol the first ``via`` entry with a definition (in ``keep``, when given) finds; None when
        that entry finds two, or none finds any."""
        for kind, value, qual in via:
            found = sorted({d for d in self.definitions(kind, value, qual, target) if keep is None or d in keep})
            if found:
                return found[0] if len(found) == 1 else None
        return None

    def class_of(self, kind: list, target: dict) -> str | None:
        """The one class a type expression of this file names (``extract.pytypes``): ``ann`` the class its via
        list finds; ``ret`` what the one callee its via list finds returns, the callee itself when it is a class,
        else the class of its return annotation (or of one item of the tuple it returns); ``one`` the class all
        its types name."""
        classes = self.index["classes"]
        if kind[0] == "one":
            found = {self.class_of(k, target) for k in kind[1]}
            return found.pop() if len(found) == 1 else None
        if kind[0] == "ann":
            return self.first_found(kind[1], target, classes)
        callee, item = self.first_found(kind[1], target), kind[2]
        if callee is None or callee in classes:
            return callee if item is None else None
        if item is None:
            return self.index.get("returns", {}).get(callee)
        items = self.index.get("returns_items", {}).get(callee, ())
        return items[item] if item < len(items) else None

    def definitions(self, kind: str, value, qual: str, target: dict) -> list[str]:
        """The symbols named ``qual`` in the scope one ``via`` entry names, same repo or source first.
        ``local`` is a definition of this file; ``self`` and ``super`` the method ``qual`` up the chain of
        the class ``value`` of this file, ``super`` past the class itself; ``typed`` the method ``qual`` up the
        chain of the class the type expression ``value`` names."""
        repo = target.get("repo")
        if kind == "local":
            found = self.index["def"].get(f"{self.item_id}::{qual}")
            return [found] if found else []
        if kind in ("self", "super", "typed"):
            cls = self.index["def"].get(f"{self.item_id}::{value}") if kind != "typed" else self.class_of(value, target)
            member = hierarchy.find_member(self.index, cls, qual, skip=kind == "super") if cls else None
            return [member[0]] if member else []
        if kind in ("package", "gopkg"):
            keys = [value] if kind == "package" else [f"go:{d}" for d in self._go_dirs(value)]
            entries = [e for key in keys for e in self.index["pkgdef"].get(f"{key}::{qual}", [])]
            return [e["node"] for e in [e for e in entries if e["repo"] == repo] or entries]
        if kind == "module":
            files = self._module_files(value, target.get("source"))
        else:
            files = self._first_file(value if kind == "paths" else self._ts_paths(value, repo), repo)
        return [d for file in files if (d := self.index["def"].get(f"{file}::{qual}"))]

    def _module_files(self, module: str, source: str | None) -> list[str]:
        """Files of a Python module: its exact dotted path, else a module whose path ends with it (the
        directory a ``src/`` layout puts on sys.path), in the caller's source only. Another source may
        have a different dependency environment. Only calls use the suffix form."""
        for table in ("module", "modsuffix"):
            entries = self.index[table].get(module, [])
            same = [e["node"] for e in entries if e["source"] == source]
            if same:
                return same
        return []

    def commit_target(self, sha: str, repo: str | None = None, strict: bool = False) -> str | None:
        """The item of the one commit read whose SHA starts with ``sha``: among the commits of ``repo`` alone when
        ``strict``, else those of ``repo`` first; None when no commit read has it, or two do. The same commit read by
        two sources is one commit: the smaller item id stands for it."""
        entries = [e for e in self.index["commit"].get(sha[:7], []) if e["sha"].startswith(sha)]
        scoped = [e for e in entries if e["repo"] == repo] if repo else []
        entries = scoped if strict or scoped else entries
        return min(e["node"] for e in entries) if len({e["sha"] for e in entries}) == 1 else None

    def _first_file(self, candidates: list[str], repo: str | None) -> list[str]:
        for candidate in candidates:
            found, _ = self.path_target(candidate, repo)
            if found:
                return [found]
        return []

    def _go_dirs(self, import_path: str) -> list[str]:
        """The directory a Go import path names: the longest run of its last segments that ends one
        directory, two segments at least unless that run is the whole directory."""
        segments = import_path.split("/")
        for k in range(len(segments), 0, -1):
            entries = [e for e in self.index["gosuffix"].get("/".join(segments[-k:]), []) if k >= 2 or e["whole"]]
            dirs = sorted({e["dir"] for e in entries})
            if dirs:
                return dirs if len(dirs) == 1 else []
        return []

    def _ts_paths(self, spec: str, repo: str | None) -> list[str]:
        """Candidate files of a JS or TS module specifier: relative to the importing file, or through the
        ``paths`` and ``baseUrl`` of the nearest tsconfig.json above it in the same repo."""
        if spec.startswith("."):
            targets = [posixpath.normpath(posixpath.join(self.base_dir, spec))]
        else:
            targets = self._ts_alias(spec, repo)
        return [c for t in targets for c in module_files(t)]

    def _ts_alias(self, spec: str, repo: str | None) -> list[str]:
        directory = self.base_dir
        while (config := self.index["tsconfig"].get(f"{repo}:{directory}")) is None:
            if not directory:
                return []
            directory = posixpath.dirname(directory)
        out = []
        for pattern in sorted(config["paths"], key=lambda p: (-len(p.partition("*")[0]), p)):
            prefix, star, suffix = pattern.partition("*")
            if star and spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix + suffix):
                middle = spec[len(prefix) : len(spec) - len(suffix)]
                out += [t.replace("*", middle, 1) for t in config["paths"][pattern]]
            elif not star and spec == pattern:
                out += config["paths"][pattern]
        if not out and config["base"] is not None:
            out = [posixpath.normpath(posixpath.join(config["base"], spec))]
        return out

    def resolve_ref(self, f: dict) -> None:
        target, ttype = f["target"], f["target"]["type"]
        if ttype == "call":
            self.resolve_call(f)
            return
        unit = f["units"][0]
        found, status = None, f.get("status", "parsed")
        if ttype in ("repo", "seam", "plan"):
            found = self.index[ttype].get(target["value"])
        elif ttype == "path":
            found, status = self.path_target(target["value"], target.get("repo"))
            if found is None:
                self._ref("dangling" if status != "ambiguous" else "ambiguous", target["value"])
                return
        elif ttype == "paths":
            for candidate in target["values"]:
                found, status = self.path_target(candidate, target.get("repo"))
                if found:
                    break
            if found is None:
                return  # a package import or a file outside the sources: not a dangling link
        elif ttype in ("module", "commit"):
            found = self._module_or_commit(ttype, target)
            if found is None:
                return  # third-party or optional import, or a commit no source read: not a dangling link
        if found is None:
            self._ref("dangling", f"{ttype}:{target['value']}")
            return
        self._ref("resolved")
        key = (f["src"], f["rel"], found)
        if key not in self.edges:
            self.edges[key] = edge(
                f["src"],
                f["rel"],
                found,
                f["units"],
                status=status,
                needs=[found],
                where=f.get("where") or [[unit, ""]],
            )

    def _module_or_commit(self, ttype: str, target: dict) -> str | None:
        if ttype == "commit":
            return self.commit_target(target["value"], target.get("repo"), strict=True)
        entries = self.index["module"].get(target["value"], [])
        same = [e for e in entries if e["source"] == target.get("source")]
        return _unique(same)

    # -- scanning -------------------------------------------------------------------------------

    def _commits(self, src: str, unit: str, text: str, field: str, claim, repo: str | None) -> None:
        """``repo@sha``, and a bare SHA in evidence: the commit read when one has it, else a Commit node of the
        mention's own."""
        for m in REPO_SHA.finditer(text):
            if not claim(m):
                continue
            name, sha = m.group(1), m.group(2)
            self._count("resolved")
            found = self.commit_target(sha, name, strict=True)
            if found:
                self._link(src, found, unit, m.start(), "parsed", "commit", field)
                continue
            commit = f"commit:{name}@{sha}"
            self.nodes.setdefault(
                commit, node(commit, "Commit", f"{name}@{sha[:10]}", [unit], props={"repo": name, "sha": sha})
            )
            self._link(src, commit, unit, m.start(), "parsed", "commit", field)
            if name in self.index["repo"]:
                key = (commit, "in_repo", self.index["repo"][name])
                self.edges.setdefault(key, edge(commit, "in_repo", self.index["repo"][name], [unit]))
        if field != "evidence":
            return
        for m in BARE_SHA.finditer(text):
            sha = m.group(1)
            if not (re.search(r"[a-f]", sha) and re.search(r"\d", sha)) or not claim(m):
                continue
            self._count("resolved")
            found = self.commit_target(sha, repo)
            if found:
                self._link(src, found, unit, m.start(), "resolved", "commit", field)
                continue
            commit = f"commit:{sha}"
            self.nodes.setdefault(commit, node(commit, "Commit", sha[:10], [unit], props={"sha": sha}))
            self._link(src, commit, unit, m.start(), "parsed", "commit", field)

    def nearest(self, candidates: list[dict]) -> str | None:
        """The one symbol of several with the name a mention gives: those of this item's repo first, then those a
        commit changed, then those sharing the most leading directories with this item (the same package), then those
        under a directory or in a file the item's own name names (``docs/srs-plugin.md``: ``srs``). None when that
        still leaves two."""
        nodes = {e["node"]: e for e in candidates}
        if len(nodes) > 1 and self.repo:
            nodes = {n: e for n, e in nodes.items() if e.get("repo") == self.repo} or nodes
        if len(nodes) > 1 and self.touched:
            nodes = {n: e for n, e in nodes.items() if e["path"] in self.touched} or nodes
        if len(nodes) > 1:
            here = self.base_dir.split("/") if self.base_dir else []
            shared = {n: _shared(here, e["path"].split("/")[:-1]) for n, e in nodes.items()}
            best = max(shared.values())
            nodes = {n: e for n, e in nodes.items() if shared[n] == best}
        if len(nodes) > 1 and self.path:
            topic = set(re.split(r"[-_.]", posixpath.splitext(posixpath.basename(self.path))[0].lower())) - {""}
            named = {n: e for n, e in nodes.items() if topic & _segments(e["path"])}
            nodes = named or nodes
        return next(iter(nodes)) if len(nodes) == 1 else None

    def _pulls(self, src: str, unit: str, text: str, field: str, claim, repo: str | None) -> None:
        """A pull request a source read: ``owner/repo#57`` and ``repo#57`` (parsed), "PR #57" in the text of a step
        of that repo (resolved)."""
        for pattern, status in ((GITHUB_PULL, "parsed"), (PLAN_STEP, "parsed"), (PULL_WORD, "resolved")):
            for m in pattern.finditer(text):
                if pattern is PULL_WORD:
                    key = f"{repo}#{m.group(1)}" if repo else None
                else:
                    key = f"{m.group(1).lower() if pattern is GITHUB_PULL else m.group(1)}#{m.group(2)}"
                target = self.index["pr"].get(key) if key else None
                if target and claim(m):
                    self._count("resolved")
                    self._link(src, target, unit, m.start(), status, "pr", field)

    def scan(
        self, src: str, unit: str, text: str, field: str = "text", heading: str | None = None, repo: str | None = None
    ) -> None:
        taken: list[tuple[int, int]] = []
        step_repo = repo  # the loop over REPO_PATH reuses the name

        def claim(m) -> bool:
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                return False
            taken.append((a, b))
            return True

        for m in DOI.finditer(text):
            claim(m)  # DOIs look like paths; they are citations, not links into the project

        for m in MD_LINK.finditer(text):
            target_text = unquote(m.group(1)).split("#", 1)[0]
            if not target_text or "://" in target_text or target_text.startswith("mailto:"):
                continue
            span = (m.start(1), m.end(1))
            taken.append(span)
            target, status = self.path_target(target_text, None)
            how = "parsed"
            if target is None and status == "dangling":
                target, status = self.path_target(target_text, None, guess=True)
                how = status
            if target is None and status == "dangling":
                target, status = self.unseen(target_text, text, span, step_repo)
                how = status
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(1), how, "md-link", field)
            elif status not in ("outside", "past"):
                self._count(status if status in ("ambiguous", "dangling") else "dangling", target_text)

        for m in URL.finditer(text):
            url = normalize_url(m.group(0))
            if not claim(m):
                continue
            target = self.index["url"].get(url)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), "parsed", "url", field)
            elif _url_prefix(url) in self.url_prefixes:
                self._count("dangling", url)

        for m in QUALIFIED_SYMBOL.finditer(text):
            if not claim(m):
                continue
            repo, path, qual = m.group(1), m.group(2), m.group(3)
            if repo and repo not in self.index["repo"]:
                continue  # `app:app/pricing.py::total`: a symbol of a repo outside the project
            entries = self.index["symbol"].get(f"{path}::{qual}", [])
            if repo:
                entries = [e for e in entries if e.get("repo") == repo]
            target, how = _unique(entries), "parsed"
            outcome = "ambiguous" if entries else "dangling"
            if not entries and not repo:  # `worker/cli.py::main`: the file written short
                file, status = self.path_target(path, None, guess=True)
                if file is None and status == "dangling":
                    file, status = self.unseen(path, text, m.span(2), step_repo)
                if status in ("outside", "past"):
                    continue
                target, how = (self.index["def"].get(f"{file}::{qual}") if file else None), "resolved"
                outcome = "ambiguous" if status == "ambiguous" else "dangling"
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), how, "symbol", field)
            else:
                self._count(outcome, m.group(0))

        self._commits(src, unit, text, field, claim, repo)

        for m in PLAN_STEP.finditer(text):
            target = self.index["step"].get(f"{m.group(1)}#{m.group(2)}")
            if target and claim(m):
                self._count("resolved")
                self._link(src, target, unit, m.start(), "parsed", "step", field)

        self._pulls(src, unit, text, field, claim, repo)

        for m in SPACED_PATH.finditer(text):
            if " " not in m.group(1):
                continue
            target, status = self.path_target(m.group(1), repo) if repo else (None, "dangling")
            if target is None:
                target, status = self.path_target(m.group(1), None)
            if target and claim(m):
                self._count("resolved")
                self._link(src, target, unit, m.start(), status, "path", field)

        for m in REPO_PATH.finditer(text):
            repo, path = m.group(1), m.group(2)
            if repo not in self.index["repo"] or not claim(m):
                continue
            if repo not in self.index["read_repos"]:
                continue  # a repo harness.yaml declares and no source reads: outside the project, not counted
            target, status = self.path_target(path, repo)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), status, "repo-path", field)
            else:
                self._count(status if status in ("ambiguous", "dangling") else "dangling", m.group(0))

        for m in PATH.finditer(text):
            path = m.group(1)
            first, _, rest = path.partition("/")
            repo = first if first in self.index["repo"] and rest else None
            if not claim(m):
                continue
            target, status = self.path_target(rest if repo else path, repo, guess=not repo)
            if target is None and repo:
                target, status = self.path_target(path, None, guess=True)
            if target is None and status == "dangling":
                target, status = self.unseen(path, text, m.span(1), step_repo)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), status, "path", field)
            elif status not in ("outside", "past"):
                self._count(status if status in ("ambiguous", "dangling") else "dangling", m.group(0))

        for m in ROOT_DOC.finditer(text):
            if any(m.start() < y and x < m.end() for x, y in taken):
                continue
            target = next(
                (
                    t
                    for r in dict.fromkeys((step_repo, self.repo))
                    if r
                    for t in [self.path_target(m.group(1), r)[0]]
                    if t
                ),
                None,
            )
            if target and claim(m):
                self._count("resolved")
                self._link(src, target, unit, m.start(), "resolved", "path", field)

        for m in BACKTICK.finditer(text):
            inner = m.group(1).strip().rstrip("()")
            if any(m.start() < y and x < m.end() for x, y in taken):
                continue
            if inner in self.index["ids"]:
                taken.append(m.span())
                self._count("resolved")
                self._link(src, inner, unit, m.start(), "parsed", "id", field)
                continue
            if not SYMBOLISH.match(inner) or not looks_like_symbol(inner):
                continue
            candidates = self.index["symname"].get(inner, [])
            if not inner.lower().startswith("test") and not is_test_path(self.path or "x"):
                candidates = [e for e in candidates if not is_test_path(e["path"])]
            if not candidates:
                continue
            taken.append(m.span())
            target = self.nearest(candidates)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), "resolved", "symbol-name", field)
            elif not all(e.get("constant") for e in candidates):
                self._count("ambiguous", inner)
            # a name only constants of several modules hold (`CACHE`, `LOOP`) names none of them: not counted

        self.scan_codes(src, unit, text, field, heading, claim)

        for m in TOKEN.finditer(text):
            name = m.group(1)
            if len(name) < 4 or any(m.start() < y and x < m.end() for x, y in taken):
                continue
            for kind in ("repo", "seam", "plan"):
                target = self.index[kind].get(name)
                if target:
                    taken.append(m.span())
                    self._count("resolved")
                    self._link(src, target, unit, m.start(), "parsed", kind, field)
                    break

    def scan_codes(self, src: str, unit: str, text: str, field: str, heading: str | None = None, claim=None) -> None:
        """The codes of the identifiers in ``text``: a node for each, and an edge from ``src``, ``defines`` when the
        heading names it, else ``mentions``. A code that is an alias of another (a use case's old number) is the node of
        the code it stands for, so a mention of either reaches the one node."""
        for kind, pattern, aliases in self.patterns:
            for m in pattern.finditer(text):
                if claim is not None and not claim(m):
                    continue
                code = m.group(0)
                code = aliases.get(" ".join(code.split()), code)
                nid = f"{kind.lower()}:{code}"
                self.nodes.setdefault(nid, node(nid, kind, code, [unit], props={"code": code}))
                self._count("resolved")
                defines = heading is not None and m.group(0) in heading
                self._link(src, nid, unit, m.start(), "parsed", "code", field, rel="defines" if defines else "mentions")

    def facts(self) -> list[dict]:
        out = [self.nodes[k] for k in sorted(self.nodes)]
        out += [self.edges[k] for k in sorted(self.edges)]
        out.append({"t": "stats", **self.stats, "samples": self.samples})
        return out


def link_item(record: dict, fragments: list[dict], body: str | None, structure: list[dict], index: dict) -> list[dict]:
    """Mentions in one item's text, plus resolution of the references its structure stage left open."""
    item_id = record["id"]
    props = record.get("props") or {}
    defined = frozenset((f["src"], f["dst"]) for f in structure if f["t"] == "edge" and f["rel"] == "defines")
    linker = Linker(index, item_id, props.get("repo"), props.get("path"), defined)
    linker.dated = linker.dated or record["kind"] in DATED_KINDS
    linker.touched = frozenset(
        v for f in structure if f["t"] == "ref" and f["rel"] == "changes" for v in f["target"].get("values") or []
    )
    if record["kind"] == "pr" and isinstance(props.get("merge_commit"), str):  # what the pull request merged
        sha = props["merge_commit"]
        merged = [e for e in index["commit"].get(sha[:7], []) if e["sha"] == sha and e["repo"] == props.get("repo")]
        linker.touched = frozenset(p for e in merged for p in e["paths"])
    for f in structure:
        if f["t"] == "ref":
            linker.resolve_ref(f)
    texts = [f for f in structure if f["t"] == "text"]
    kind = record["kind"]
    if kind == "code":
        for t in texts:  # the comments of a file that name a code: only the codes in them are links
            linker.scan_codes(t["src"], t["unit"], t["text"], t.get("field", "comment"))
    elif kind in ("manifest", "contracts", "plan", "knowledge", "binding", "ontology"):
        for t in texts:  # structured files: only the free-text fields, never comments or keys
            linker.scan(t["src"], t["unit"], t["text"], t.get("field", "text"), repo=t.get("repo"))
    elif texts:
        for t in texts:
            linker.scan(t["src"], t["unit"], t["text"], t.get("field", "text"), repo=t.get("repo"))
    elif kind in ("config", "asset", "markup"):
        pass  # config, assets, templates and stylesheets are not prose
    elif fragments:
        document_like = node_kind_for_item(kind) == "Document"  # only documents get Section nodes
        for frag in fragments:
            anchor = frag["anchor"]
            src = f"{item_id}#{anchor}" if document_like and anchor != "_preamble" else item_id
            linker.scan(src, frag_unit(item_id, anchor), frag["text"], heading=frag.get("title"))
    elif body:
        linker.scan(item_id, item_unit(item_id), body)
    return linker.facts() + hierarchy.item_facts(item_id, structure, index)


def link_traced(
    record: dict, fragments: list[dict], body: str | None, structure: list[dict], index: dict
) -> tuple[list[dict], list[list]]:
    """``link_item`` and the entries of the index it read, for ``Memo.run_traced``."""
    reads = Reads(index)
    return link_item(record, fragments, body, structure, reads), reads.keys()
