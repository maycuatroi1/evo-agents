"""Identifier index and the link stage: edges from explicit identifiers, never from guesses.

A mention is ``parsed`` when it is fully qualified (a URL of a known item, ``repo:path``,
``path::Symbol``, ``plan#step``, a registered repo, seam or plan name) and ``resolved`` when it is bare
but matches exactly one target (a path or symbol name unique across the project). Ambiguous mentions
link nothing; they are counted, so coverage shows how much prose is still unlinked. A path that names a
directory of a repo links to its Directory node, always ``parsed``: the directory exists exactly because
items of that repo sit under it.

A call into another file (a ``calls`` reference of the structure stage) links its caller to the symbol
the file's imports name, ``resolved``: each ``via`` entry names the files to look in (a module, candidate
paths, a package, a Go import path, a JS or TS specifier read through the nearest tsconfig) and the
qualified name to find there. The first entry that finds a definition decides; two definitions are
ambiguous and link nothing. A call through ``this``, ``self`` or ``super`` names a class of its own file
instead; the index's class hierarchy (``pipeline.hierarchy``) finds the method up that class's chain, and
gives each class its ``inherits`` edges and each method its ``overrides`` edges. A Python call through a typed
receiver (``extract.pytypes``) names the class by a type expression: the class an annotation names, or what a
callee returns, an instance of it when it is a class, else the class its return annotation names, which the
index resolves once a build in the callee's own file; the method is then found up that class's chain.

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
from evo_agents.kg.pipeline import hierarchy
from evo_agents.kg.pipeline.stages import edge, frag_unit, item_unit, node, normalize_url
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.schema import node_kind_for_item

LINK_VERSION = "10"

URL = re.compile(r"https?://[^\s<>()\[\]{}`'\"|]+")
BACKTICK = re.compile(r"`([^`\n]{2,200})`")
QUALIFIED_SYMBOL = re.compile(r"(?<![\w/.-])(?:([A-Za-z0-9_.-]+):)?((?:[\w.-]+/)*[\w.-]+\.\w{1,6})::([A-Za-z_][\w.]*)")
REPO_PATH = re.compile(r"(?<![\w/.:-])([A-Za-z0-9_.-]+):((?:[\w.-]+/)*[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
PATH = re.compile(r"(?<![\w/.:@-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
DOI = re.compile(r"(?<![\w/.])10\.\d{4,9}/[^\s)\]>,;]+")
MD_LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
REPO_SHA = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+)@([0-9a-f]{7,40})\b")
BARE_SHA = re.compile(r"(?<![\w/.@-])([0-9a-f]{7,40})(?![\w-])")
PLAN_STEP = re.compile(r"(?<![\w/.-])([a-z0-9][a-z0-9-]{2,})#(\d+)\b")
TOKEN = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9])(?![\w-])")
SYMBOLISH = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*(?:\(\))?$")


def build_index(facts_by_item: dict[str, list[dict]], identifiers: list[dict]) -> dict:
    index: dict = {
        "ids": set(),
        "repo": {},
        "seam": {},
        "plan": {},
        "step": {},
        "url": {},
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
    index["gosuffix"] = _go_suffixes({key.partition("::")[0] for key in index["pkgdef"]})
    # Once per build, not once per item: a project with thousands of known URLs spent most of a link run here.
    index["url_prefix"] = {_url_prefix(u) for u in index["url"]}
    index["identifiers"] = [{"kind": i["kind"], "pattern": i["pattern"]} for i in identifiers]
    hierarchy.resolve(index, with_bases, lambda file, repo, path: Linker(index, file, repo, path))
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


def _learn(index: dict, f: dict) -> None:
    """Add one ``ident`` fact to the index."""
    kind = f["type"]
    if kind in ("repo", "seam", "plan", "step", "url"):
        index[kind].setdefault(f["key"], f["node"])
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
    elif kind == "tsconfig":
        index["tsconfig"].setdefault(f["key"], {"base": f["base"], "paths": f["paths"]})


def _learn_symbol(index: dict, f: dict) -> None:
    entry = {"node": f["node"], "repo": f.get("repo"), "source": f["source"]}
    index["symbol"][f["key"]].append(entry)
    index["symname"][f["qualname"]].append(f["node"])
    if f["name"] != f["qualname"]:
        index["symname"][f["name"]].append(f["node"])
    if f.get("file"):
        index["def"].setdefault(f"{f['file']}::{f['qualname']}", f["node"])
        if f.get("kind") == "class":
            index["classes"].setdefault(f["node"], (f["file"], f["qualname"]))
    if f.get("package"):
        index["pkgdef"][f"{f['package']}::{f['qualname']}"].append(entry)


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
    """Forge URLs up to owner/repo, other URLs up to the host: a mention under a known prefix that matches
    no item is a dangling link; any other URL is just outside the project."""
    parts = url.split("/")
    if len(parts) < 3:
        return url
    host = "/".join(parts[:3])
    if any(marker in parts for marker in ("blob", "tree", "-")) or parts[2] in ("github.com", "gitlab.com"):
        return "/".join(parts[:5])
    return host


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
        }
        self.samples: dict[str, list[str]] = {"ambiguous": [], "dangling": []}
        self.url_prefixes = index["url_prefix"]
        self.patterns = [(i["kind"], re.compile(i["pattern"])) for i in index["identifiers"]]

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

    def path_target(self, path: str, repo: str | None) -> tuple[str | None, str]:
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
        if not entries:
            return None, "dangling"
        target = _unique(entries)
        return (target, "parsed" if is_dir else "resolved") if target else (None, "ambiguous")

    def resolve_call(self, f: dict) -> None:
        """Link a caller to the one symbol the first ``via`` entry with a definition finds."""
        found = self.first_found(f["target"]["via"], f["target"])
        key = (f["src"], "calls", found)
        if found and found != f["src"] and key not in self.edges:
            self.edges[key] = edge(
                f["src"], "calls", found, f["units"], status="resolved", conf=0.8, needs=[found], where=f["where"]
            )

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
        directory a ``src/`` layout puts on sys.path); the caller's own source first. Only calls use the
        second form: the qualified name they look up must then be defined there too."""
        found = []
        for table in ("module", "modsuffix"):
            entries = self.index[table].get(module, [])
            same = [e["node"] for e in entries if e["source"] == source]
            if same:
                return same
            found = found or [e["node"] for e in entries]
        return found

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
        elif ttype == "module":
            entries = self.index["module"].get(target["value"], [])
            same = [e for e in entries if e["source"] == target.get("source")]
            found = _unique(same or entries)
            if found is None:
                return  # third-party or optional import: outside the project, not a dangling link
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

    # -- scanning -------------------------------------------------------------------------------

    def scan(self, src: str, unit: str, text: str, field: str = "text", heading: str | None = None) -> None:
        taken: list[tuple[int, int]] = []

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
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(1), "parsed", "md-link", field)
            else:
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
            entries = self.index["symbol"].get(f"{path}::{qual}", [])
            if repo:
                entries = [e for e in entries if e.get("repo") == repo]
            target = _unique(entries)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), "parsed", "symbol", field)
            else:
                self._count("ambiguous" if entries else "dangling", m.group(0))

        for m in REPO_SHA.finditer(text):
            if not claim(m):
                continue
            repo, sha = m.group(1), m.group(2)
            commit = f"commit:{repo}@{sha}"
            self.nodes.setdefault(
                commit, node(commit, "Commit", f"{repo}@{sha[:10]}", [unit], props={"repo": repo, "sha": sha})
            )
            self._count("resolved")
            self._link(src, commit, unit, m.start(), "parsed", "commit", field)
            if repo in self.index["repo"]:
                key = (commit, "in_repo", self.index["repo"][repo])
                self.edges.setdefault(key, edge(commit, "in_repo", self.index["repo"][repo], [unit]))

        if field == "evidence":
            for m in BARE_SHA.finditer(text):
                sha = m.group(1)
                if not (re.search(r"[a-f]", sha) and re.search(r"\d", sha)) or not claim(m):
                    continue
                commit = f"commit:{sha}"
                self.nodes.setdefault(commit, node(commit, "Commit", sha[:10], [unit], props={"sha": sha}))
                self._count("resolved")
                self._link(src, commit, unit, m.start(), "parsed", "commit", field)

        for m in PLAN_STEP.finditer(text):
            target = self.index["step"].get(f"{m.group(1)}#{m.group(2)}")
            if target and claim(m):
                self._count("resolved")
                self._link(src, target, unit, m.start(), "parsed", "step", field)

        for m in REPO_PATH.finditer(text):
            repo, path = m.group(1), m.group(2)
            if repo not in self.index["repo"] or not claim(m):
                continue
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
            target, status = self.path_target(rest if repo else path, repo)
            if target is None and repo:
                target, status = self.path_target(path, None)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), status, "path", field)
            else:
                self._count(status if status in ("ambiguous", "dangling") else "dangling", m.group(0))

        for m in BACKTICK.finditer(text):
            inner = m.group(1).strip().rstrip("()")
            if any(m.start() < y and x < m.end() for x, y in taken):
                continue
            if inner in self.index["ids"]:
                taken.append(m.span())
                self._count("resolved")
                self._link(src, inner, unit, m.start(), "parsed", "id", field)
                continue
            if not SYMBOLISH.match(inner):
                continue
            looks_like_symbol = "_" in inner or "." in inner or re.search(r"[a-z][A-Z]", inner) or len(inner) >= 8
            candidates = self.index["symname"].get(inner, [])
            if not candidates or not looks_like_symbol:
                continue
            taken.append(m.span())
            target = _unique(candidates)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), "resolved", "symbol-name", field)
            else:
                self._count("ambiguous", inner)

        for kind, pattern in self.patterns:
            for m in pattern.finditer(text):
                if not claim(m):
                    continue
                code = m.group(0)
                nid = f"{kind.lower()}:{code}"
                self.nodes.setdefault(nid, node(nid, kind, code, [unit], props={"code": code}))
                self._count("resolved")
                defines = heading is not None and code in heading
                self._link(src, nid, unit, m.start(), "parsed", "code", field, rel="defines" if defines else "mentions")

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
    for f in structure:
        if f["t"] == "ref":
            linker.resolve_ref(f)
    texts = [f for f in structure if f["t"] == "text"]
    kind = record["kind"]
    if kind in ("manifest", "contracts", "plan", "knowledge", "binding", "ontology"):
        for t in texts:  # structured files: only the free-text fields, never comments or keys
            linker.scan(t["src"], t["unit"], t["text"], t.get("field", "text"))
    elif texts:
        for t in texts:
            linker.scan(t["src"], t["unit"], t["text"], t.get("field", "text"))
    elif kind in ("code", "config", "asset"):
        pass  # code links come from the structure stage; config and assets are not prose
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
