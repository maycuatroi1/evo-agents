"""The identifier index of one build: every name, path, module, symbol, commit and URL the items define, from
their ``ident`` facts, with the tables derived from them once a build (path and module suffixes, package roots,
the class hierarchy, return types, overriders). The link stage (``pipeline.link``) reads it."""

from __future__ import annotations

import posixpath
from collections import defaultdict

from evo_agents.kg.pipeline import hierarchy
from evo_agents.kg.pipeline.link import Linker
from evo_agents.kg.pipeline.link_forms import url_prefix
from evo_agents.kg.protocol import canonical_json, sha256

PROTOCOL_MODULES = ("typing", "typing_extensions")


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
    index["url_prefix"] = {url_prefix(u) for u in index["url"]}
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
