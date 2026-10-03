"""Identifier index and the link stage: edges from explicit identifiers, never from guesses.

A mention is ``parsed`` when it is fully qualified (a URL of a known item, ``repo:path``,
``path::Symbol``, ``plan#step``, a registered repo, seam or plan name) and ``resolved`` when it is bare
but matches exactly one target (a path or symbol name unique across the project). Ambiguous mentions
link nothing; they are counted, so coverage shows how much prose is still unlinked. A path that names a
directory of a repo links to its Directory node, always ``parsed``: the directory exists exactly because
items of that repo sit under it.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from urllib.parse import unquote

from evo_agents.kg.pipeline.stages import edge, frag_unit, item_unit, node, normalize_url
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.schema import node_kind_for_item

LINK_VERSION = "7"

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
        "symbol": defaultdict(list),
        "symname": defaultdict(list),
    }
    for facts in facts_by_item.values():
        for f in facts:
            if f["t"] == "node":
                index["ids"].add(f["id"])
            elif f["t"] == "ident":
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
                elif kind == "symbol":
                    entry = {"node": f["node"], "repo": f.get("repo"), "source": f["source"]}
                    index["symbol"][f["key"]].append(entry)
                    index["symname"][f["qualname"]].append(f["node"])
                    if f["name"] != f["qualname"]:
                        index["symname"][f["name"]].append(f["node"])
    index["identifiers"] = [{"kind": i["kind"], "pattern": i["pattern"]} for i in identifiers]
    return index


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
        self.url_prefixes = {_url_prefix(u) for u in index["url"]}
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

    def resolve_ref(self, f: dict) -> None:
        target, ttype = f["target"], f["target"]["type"]
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
    return linker.facts()
