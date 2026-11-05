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
directory no repo holds is outside the project, like a URL, and is not counted. A path whose file or directory names
hold spaces (``reports/Backend cho agent.md``, ``research_notes/Browser cho agent/literature.md``) and a document at a repo's root named in capitals (``CLUSTER.md``, of the plan step's repo, then of the item's) link only
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


A SHA names a commit a git source read from its history when exactly one such commit starts with it: ``repo@sha``
among the commits of that repo, a bare SHA in a plan step's evidence among those of the step's repo first, then of
every repo; such a mention links to the commit's item, and one that names no commit read gets a Commit node of its own
as before. A pull request is named by its URL, ``owner/repo#57``, ``repo#57`` with a repo name of harness.yaml, or
"PR #57" in the text of a step of that repo.

The index these mentions are looked up in is built by ``pipeline.link_index``; the written forms are in
``pipeline.link_forms``, paths are read by ``pipeline.link_paths``, the references and calls the structure stage left
open by ``pipeline.link_refs``, and what a task reads of the index is noted by ``pipeline.link_reads``.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote

from evo_agents.kg.health import is_test_path
from evo_agents.kg.pipeline import hierarchy
from evo_agents.kg.pipeline.link_forms import (
    BACKTICK,
    BARE_SHA,
    DOI,
    GITHUB_PULL,
    MD_LINK,
    PATH,
    PLAN_STEP,
    PULL_WORD,
    QUALIFIED_SYMBOL,
    REPO_PATH,
    REPO_SHA,
    ROOT_DOC,
    SPACED_DIR_PATH,
    SPACED_PATH,
    SPACED_START,
    SYMBOLISH,
    TOKEN,
    URL,
    looks_like_symbol,
    unique,
    url_prefix,
)
from evo_agents.kg.pipeline.link_reads import Reads
from evo_agents.kg.pipeline.link_refs import RefResolver
from evo_agents.kg.pipeline.stages import edge, frag_unit, item_unit, node, normalize_url
from evo_agents.kg.schema import node_kind_for_item

LINK_VERSION = "19"

# The directories of a dated plan or document: what sits there wrote of the tree as it was then.
DATED_DIR = re.compile(r"(^|/)(completed|done|archive|archived)/", re.IGNORECASE)
DATED_KINDS = frozenset({"commit", "pr", "message"})
FIXTURE_DIRS = frozenset({"fixtures", "evals", "mocks"})  # test data and eval scenarios: a project of their own


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


class Claims:
    """The spans of one text that a mention took: a later form overlapping one of them is not read again."""

    def __init__(self):
        self.taken: list[tuple[int, int]] = []

    def __call__(self, m) -> bool:
        """Take the span of a match (or a ``(start, end)`` pair) unless it overlaps one taken: whether it did."""
        a, b = m if isinstance(m, tuple) else m.span()
        if self.overlaps(a, b):
            return False
        self.taken.append((a, b))
        return True

    def overlaps(self, a: int, b: int) -> bool:
        return any(a < y and x < b for x, y in self.taken)

    def add(self, span: tuple[int, int]) -> None:
        """Take a span whether or not it overlaps one taken."""
        self.taken.append(span)


class Linker(RefResolver):
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

    def _count(self, outcome: str, text: str | None = None) -> None:
        self.stats["mentions"] += 1
        self.stats[outcome] += 1
        if text and outcome in self.samples and len(self.samples[outcome]) < 5:
            self.samples[outcome].append(text[:120])

    def _missed(self, status: str, text: str) -> None:
        """Count a mention that found no target: ambiguous when it found two, else dangling."""
        self._count(status if status in ("ambiguous", "dangling") else "dangling", text)

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
        """Link the mentions of ``text``, each form in turn; a span one form took is not read by a later one.
        ``repo`` is the repo of the plan step the text is of."""
        step_repo = repo
        claim = Claims()
        for m in DOI.finditer(text):
            claim(m)  # DOIs look like paths; they are citations, not links into the project
        self._md_links(src, unit, text, field, claim, step_repo)
        self._urls(src, unit, text, field, claim)
        # The commits, pull requests and spaced paths read the repo of the last qualified symbol taken, when one is.
        repo = self._qualified_symbols(src, unit, text, field, claim, step_repo)
        self._commits(src, unit, text, field, claim, repo)
        self._plan_steps(src, unit, text, field, claim)
        self._pulls(src, unit, text, field, claim, repo)
        self._spaced_paths(src, unit, text, field, claim, repo)
        self._spaced_dir_paths(src, unit, text, field, claim, repo)
        self._repo_paths(src, unit, text, field, claim)
        self._paths(src, unit, text, field, claim, step_repo)
        self._root_docs(src, unit, text, field, claim, step_repo)
        self._symbol_names(src, unit, text, field, claim)
        self.scan_codes(src, unit, text, field, heading, claim)
        self._names(src, unit, text, field, claim)

    def _md_links(self, src: str, unit: str, text: str, field: str, claim: Claims, step_repo: str | None) -> None:
        """The target of a Markdown link: a path as written, else written short, else what ``unseen`` makes of it."""
        for m in MD_LINK.finditer(text):
            target_text = unquote(m.group(1)).split("#", 1)[0]
            if not target_text or "://" in target_text or target_text.startswith("mailto:"):
                continue
            span = (m.start(1), m.end(1))
            claim.add(span)
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
                self._missed(status, target_text)

    def _urls(self, src: str, unit: str, text: str, field: str, claim: Claims) -> None:
        """A URL of an item; one in a collection an item's URL is in that matches none is dangling."""
        for m in URL.finditer(text):
            url = normalize_url(m.group(0))
            if not claim(m):
                continue
            target = self.index["url"].get(url)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), "parsed", "url", field)
            elif url_prefix(url) in self.url_prefixes:
                self._count("dangling", url)

    def _qualified_symbols(
        self, src: str, unit: str, text: str, field: str, claim: Claims, step_repo: str | None
    ) -> str | None:
        """``repo:path::Symbol`` and ``path::Symbol``, the path also written short. Returns the repo of the last one
        taken (None for one without a repo), or ``step_repo`` when none is."""
        last = step_repo
        for m in QUALIFIED_SYMBOL.finditer(text):
            if not claim(m):
                continue
            repo, path, qual = m.group(1), m.group(2), m.group(3)
            last = repo
            if repo and repo not in self.index["repo"]:
                continue  # `app:app/pricing.py::total`: a symbol of a repo outside the project
            entries = self.index["symbol"].get(f"{path}::{qual}", [])
            if repo:
                entries = [e for e in entries if e.get("repo") == repo]
            target, how = unique(entries), "parsed"
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
        return last

    def _plan_steps(self, src: str, unit: str, text: str, field: str, claim: Claims) -> None:
        for m in PLAN_STEP.finditer(text):
            target = self.index["step"].get(f"{m.group(1)}#{m.group(2)}")
            if target and claim(m):
                self._count("resolved")
                self._link(src, target, unit, m.start(), "parsed", "step", field)

    def _spaced_target(self, path: str, repo: str | None) -> tuple[str | None, str]:
        """A path whose names hold spaces: a file of ``repo`` as written, else of any repo."""
        target, status = self.path_target(path, repo) if repo else (None, "dangling")
        if target is None:
            target, status = self.path_target(path, None)
        return target, status

    def _spaced_paths(self, src: str, unit: str, text: str, field: str, claim: Claims, repo: str | None) -> None:
        """A path whose file name holds spaces, linked only when it is a file of the project."""
        for m in SPACED_PATH.finditer(text):
            if " " not in m.group(1):
                continue
            target, status = self._spaced_target(m.group(1), repo)
            if target and claim(m):
                self._count("resolved")
                self._link(src, target, unit, m.start(), status, "path", field)

    def _spaced_dir_paths(self, src: str, unit: str, text: str, field: str, claim: Claims, repo: str | None) -> None:
        """A path whose directories hold spaces: the longest tail of it that is a file of the project."""
        for m in SPACED_DIR_PATH.finditer(text):
            whole = m.group(1)
            for start in SPACED_START.finditer(whole):
                path = whole[start.start() :]
                if " " not in posixpath.dirname(path):
                    break  # the passes above and below read it
                target, status = self._spaced_target(path, repo)
                if target:
                    if claim((m.start(1) + start.start(), m.end(1))):
                        self._count("resolved")
                        self._link(src, target, unit, m.start(1) + start.start(), status, "path", field)
                    break

    def _repo_paths(self, src: str, unit: str, text: str, field: str, claim: Claims) -> None:
        """``repo:path`` of a repo of harness.yaml; one of a repo no source reads is outside and not counted."""
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
                self._missed(status, m.group(0))

    def _paths(self, src: str, unit: str, text: str, field: str, claim: Claims, step_repo: str | None) -> None:
        """A path, under a repo when its first segment names one, else written short; else what ``unseen`` makes of
        it."""
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
                self._missed(status, m.group(0))

    def _root_docs(self, src: str, unit: str, text: str, field: str, claim: Claims, step_repo: str | None) -> None:
        """A document at a repo's root named in capitals: of the step's repo, then of the item's; else not counted."""
        for m in ROOT_DOC.finditer(text):
            if claim.overlaps(m.start(), m.end()):
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

    def _symbol_names(self, src: str, unit: str, text: str, field: str, claim: Claims) -> None:
        """A node id in backticks, else a name written as code there: the one symbol of that name ``nearest`` finds,
        outside tests for a text that is not a test's."""
        for m in BACKTICK.finditer(text):
            inner = m.group(1).strip().rstrip("()")
            if claim.overlaps(m.start(), m.end()):
                continue
            if inner in self.index["ids"]:
                claim.add(m.span())
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
            claim.add(m.span())
            target = self.nearest(candidates)
            if target:
                self._count("resolved")
                self._link(src, target, unit, m.start(), "resolved", "symbol-name", field)
            elif not all(e.get("constant") for e in candidates):
                self._count("ambiguous", inner)
            # a name only constants of several modules hold (`CACHE`, `LOOP`) names none of them: not counted

    def _names(self, src: str, unit: str, text: str, field: str, claim: Claims) -> None:
        """A registered repo, seam or plan name, four characters or more."""
        for m in TOKEN.finditer(text):
            name = m.group(1)
            if len(name) < 4 or claim.overlaps(m.start(), m.end()):
                continue
            for kind in ("repo", "seam", "plan"):
                target = self.index[kind].get(name)
                if target:
                    claim.add(m.span())
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
