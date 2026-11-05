"""How the link stage (``pipeline.link``) resolves the references the structure stage left open: YAML fields,
imports, commits and calls.

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
"""

from __future__ import annotations

import posixpath

from evo_agents.kg.extract.xref import module_files
from evo_agents.kg.pipeline import hierarchy
from evo_agents.kg.pipeline.link_forms import unique
from evo_agents.kg.pipeline.link_paths import PathResolver
from evo_agents.kg.pipeline.stages import edge


class RefResolver(PathResolver):
    """The reference and call resolution of a ``Linker``: what it reads and records, which ``Linker`` sets."""

    item_id: str
    edges: dict[tuple, dict]
    stats: dict[str, int]
    samples: dict[str, list[str]]

    def _ref(self, outcome: str, text: str | None = None) -> None:
        """Structured references (YAML fields, imports) are counted apart from mentions in prose."""
        self.stats["refs"] += 1
        self.stats[f"refs_{outcome}"] += 1
        if text and outcome in self.samples and len(self.samples[outcome]) < 5:
            self.samples[outcome].append(text[:120])

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
        return unique(same)
