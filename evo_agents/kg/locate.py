"""Locating code for a question: which files and symbols of a code graph a bug report or a task is about.

``Store.search`` ranks every node by its words alone. A report of a defect is a poor query for that: it is long,
it quotes a template, a log and a snippet, and the nodes it is about are named in it (a path, a traceback line, a
function) more often than they share its words. ``arrange`` reads the text matches again, the way a developer reads
the report, in four steps, none of which trains anything:

1. Anchors. What the text names exactly (``anchors``): a path, a ``File "x.py", line 12, in f`` line of a traceback
   and the symbol that holds that line, a dotted or camelCase or snake_case name, a backticked word. Each is looked up
   by its exact name; a name that many nodes carry anchors each of them less.
2. Neighbours. The best nodes lend part of their score to what they call, what calls them, what they inherit from or
   override, the files they import and are imported by, and the files and symbols the commits that match the query
   changed or mention. Only edges read from the source (``parsed``) or resolved with a confidence of CONF_FLOOR or
   more carry anything.
3. Files. A file scores what its best node scores, and a little of what the next ones add.
4. Regions. Results are the best symbol of each file in the order of the files, then the next ones: an agent reads
   five results as five places, not five spots of one file.

A graph that is not code (a hub's plans, chats and documents) is left as ``Store.search`` ranks it: ``arrange``
returns None unless most of the best matches are code.

The numbers of ``Params`` are tuned on the tune part of ``kg bench`` (docs/kg-bench.md), the harness's own question set
and the public benches alike, by random search from outside this module: nothing here reads a question set.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

CODE_EXT = frozenset(
    "py pyi js jsx ts tsx mjs cjs java kt kts scala go rs c h cc cpp cxx hpp hh cs rb php swift m mm lua sh r jl dart "
    "ex exs erl hs ml clj vue svelte".split()
)
CODE_KINDS = frozenset({"Symbol", "File"})
CONF_FLOOR = 0.5  # a resolved edge below this confidence carries no score
EXPAND_RELS = ("calls", "imports", "inherits", "overrides", "changes", "mentions", "defines", "member_of")


@dataclass(frozen=True)
class Params:
    """What ``arrange`` weighs. Every number is tuned; see the module's last paragraph."""

    pool: int = 80  # text matches read
    expand_from: int = 12  # the best nodes that lend their score to their neighbours
    anchor: float = 4.0  # the score an exactly named node gains, times its strength
    name_hits: int = 12  # a name that more nodes carry than this anchors none
    expand: float = 0.2  # the share of a node's score a neighbour gains
    expand_rels: tuple = (
        ("calls", 0.42),
        ("imports", 1.12),
        ("inherits", 0.06),
        ("overrides", 0.91),
        ("changes", 1.09),
        ("mentions", 0.92),
        ("tests", 0.72),
        ("cochange", 0.57),
    )
    sharp: float = 3.0  # the text match is raised to this power: above 1 favours the best matches
    size_pen: float = 0.0  # a symbol of many lines weighs 1 / (1 + this * log(lines / 20)) of one of a few
    file_extra: float = 0.5  # the share of its next two nodes' scores a file adds to its best
    non_code: float = 0.2  # a file or a document that is not source weighs this much against source
    other_kind: float = 1.0  # a node that is neither a symbol nor a file (a commit, a section) weighs this much
    test_path: float = 0.5  # a test file weighs this much against the rest (the search already weighs it down)
    second: float = 1.5  # a file's second node comes after this many files' first nodes, its third after twice as many
    commit: float = 0.5  # the share of a commit's score its changed files gain
    weak: float = 0.0  # a file under this share of the best file's score is left out


DEFAULT = Params()


@dataclass
class Cand:
    id: str
    kind: str
    name: str
    path: str | None
    line: int | None
    end: int | None
    rel: float = 0.0  # the text match, 1 for the best
    anchor: float = 0.0
    from_pool: bool = True

    @property
    def code(self) -> bool:
        return self.kind == "Symbol" or (self.kind == "File" and (self.path or "").rsplit(".", 1)[-1] in CODE_EXT)

    @property
    def source_file(self) -> bool:
        return (self.path or "").rsplit(".", 1)[-1].lower() in CODE_EXT


@dataclass
class Pool:
    cands: dict[str, Cand] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # the text order
    links: dict[str, list[tuple[str, str, float]]] = field(default_factory=dict)  # id -> [(other, rel, conf)]
    text: bool = True  # the matches were scored by BM25 (False: by rank alone)
    exact: set[str] = field(default_factory=set)  # the matches the search gave no score: an id or a name is the query
    scores: dict[str, float] = field(default_factory=dict)  # the text score of each match, as the search gave it


# -- what a question names -----------------------------------------------------------------------------------------

_TRACE = re.compile(r'File "([^"\n]+)", line (\d+)(?:, in ([\w<>.]+))?')
_PATH = re.compile(r"(?<![\w/.\\-])((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,5})(?::(\d+))?(?![\w/])")
_TICKED = re.compile(r"`([^`\n]{2,80})`")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\(")
_WORDLIKE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_VERSION = re.compile(r"^\d+(?:\.\d+)+$")


@dataclass(frozen=True)
class Anchor:
    kind: str  # "path", "line" or "name"
    text: str
    line: int | None = None
    strength: float = 1.0


_COMMENT = re.compile(r"<!--.*?-->", re.S)
_URL = re.compile(r"https?://\S+")


def clean(query: str) -> str:
    """``query`` without what a report's template wrote for the reader and not about the problem: HTML comments (the
    instructions of an issue form, which every report of a repo repeats) and links."""
    return _URL.sub(" ", _COMMENT.sub(" ", query))


def _is_name(word: str) -> bool:
    if len(word) < 3 or word.lower() in _COMMON:
        return False
    if "_" in word.strip("_"):
        return True
    return word[0].isalpha() and any(c.isupper() for c in word[1:]) and any(c.islower() for c in word)


_COMMON = frozenset(
    "the and for with from this that none true false self return import class def print error raise except exception "
    "test tests data value values name type list dict set str int float bool object file path line use used using "
    "when what where which while should would could does didn expected actual result output input example code "
    "issue bug fix fixed feature request version python numpy pandas".split()
)


def anchors(query: str) -> list[Anchor]:
    """What ``query`` names, strongest kinds first: traceback lines, paths, qualified and plain names."""
    out: list[Anchor] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, text: str, line: int | None = None, strength: float = 1.0) -> None:
        key = (kind, text, line) if kind == "line" else (kind, text)
        if key not in seen and text:
            seen.add(key)  # type: ignore[arg-type]
            out.append(Anchor(kind, text, line, strength))

    for m in _TRACE.finditer(query):
        add("line", m[1].replace("\\", "/"), int(m[2]), 1.0)
        if m[3] and m[3] not in ("<module>", "<lambda>"):
            add("name", m[3].split(".")[-1], None, 0.6)
    for m in _PATH.finditer(query):
        text = m[1].replace("\\", "/")
        ext = text.rsplit(".", 1)[-1].lower()
        if _VERSION.match(text) or (ext not in CODE_EXT and "/" not in text):
            continue
        add("path", text, int(m[2]) if m[2] else None, 0.9 if "/" in text else 0.5)
    for m in _TICKED.finditer(query):
        body = m[1].strip().rstrip("()")
        if re.fullmatch(r"[\w.]+", body) and not _VERSION.match(body):
            add("name", body, None, 0.8)
    for m in _DOTTED.finditer(query):
        if not _VERSION.match(m[0]) and m[0].rsplit(".", 1)[-1] not in CODE_EXT:
            add("name", m[0], None, 0.6)
    for m in _CALL.finditer(query):
        if m[1].lower() not in _COMMON:
            add("name", m[1], None, 0.5)
    for m in _WORDLIKE.finditer(query):
        if _is_name(m[0]):
            add("name", m[0], None, 0.5)
    return out[:64]


# -- reading the graph ----------------------------------------------------------------------------------------------

_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|e2e|testing)/|(?:^|/)test_[^/]*$|_test\.[a-z]+$|\.(?:test|spec)\.[a-z]+$"
)
_FILE_ID = re.compile(r"^(?P<src>[^:]+):file:(?P<path>.+?)(?:#.*)?$")


def where(node: dict) -> tuple[str | None, int | None, int | None]:
    """(path, first line, last line) of a node: a symbol's span, a section's file, a file whole."""
    props = node.get("props") or {}
    path = props.get("path")
    if not path and isinstance(props.get("item"), str):
        m = _FILE_ID.match(props["item"])
        path = m["path"] if m else None
    line, end = props.get("line"), props.get("end_line")
    if type(line) is not int:
        line = end = None
    elif type(end) is not int or end < line:
        end = line
    return path, line, end


def _cand(node: dict, rel: float = 0.0, from_pool: bool = True) -> Cand:
    path, line, end = where(node)
    return Cand(node["id"], node["kind"], node["name"], path, line, end, rel, 0.0, from_pool)


def _rels(scored, top_only: bool = False) -> dict[str, float]:
    """The text match of each id of ``scored`` in 0..1: its score over the best one's; by rank when it has none."""
    positive = [s for _, s in scored if s is not None and s > 0]
    best = max(positive, default=0.0)
    rels = {}
    for place, (node_id, score) in enumerate(scored):
        if best and score is not None:
            rels[node_id] = max(score, 0.0) / best
        elif best:
            rels[node_id] = 1.0  # an exact hit
        else:
            rels[node_id] = 1.0 / (1.0 + place / 5.0)
    return rels


def _file_index(store) -> dict[str, str]:
    """path -> id of each live File node, read once per store and build."""
    key = getattr(store, "_locate_files", None)
    if key is None:
        rows = store.db.execute("SELECT node_id FROM nodes WHERE kind = 'File' AND tx_to IS NULL")
        key = {}
        for (node_id,) in rows:
            m = _FILE_ID.match(node_id)
            if m:
                key[m["path"]] = node_id
        store._locate_files = key
    return key


def _suffix_matches(files: dict[str, str], path: str) -> list[str]:
    path = path.lstrip("./")
    if path in files:
        return [files[path]]
    tail = "/" + path
    found = [i for p, i in files.items() if p.endswith(tail)]
    if not found and "/" in path:  # an absolute path of another machine: the longest tail that names a file
        parts = path.split("/")
        for cut in range(1, len(parts) - 1):
            tail = "/" + "/".join(parts[cut:])
            found = [i for p, i in files.items() if p.endswith(tail) or p == tail[1:]]
            if found:
                break
    return found


def _symbols_at(store, b: int, file_id: str, line: int) -> list[str]:
    """The innermost symbols of a file that hold ``line``."""
    m = _FILE_ID.match(file_id)
    if not m:
        return []
    low = f"symbol:{m['src']}:{m['path']}::"
    rows = store.db.execute(
        "SELECT node_id, props FROM nodes WHERE node_id >= ? AND node_id < ? AND tx_to IS NULL", (low, low[:-2] + ":;")
    ).fetchall()
    import json

    best, size = [], math.inf
    for node_id, props in rows:
        try:
            p = json.loads(props)
        except ValueError:
            continue
        first, last = p.get("line"), p.get("end_line")
        if type(first) is int and type(last) is int and first <= line <= last:
            if last - first < size:
                best, size = [node_id], last - first
    return best


def _lookup(store, b: int, found: list[Anchor], params: Params) -> dict[str, float]:
    """node id -> anchor strength of the nodes ``found`` names exactly."""
    out: dict[str, float] = {}

    def give(node_id: str, strength: float) -> None:
        out[node_id] = max(out.get(node_id, 0.0), min(strength, 1.0))

    files = _file_index(store)
    for a in found:
        if a.kind in ("path", "line"):
            ids = _suffix_matches(files, a.text)
            for node_id in ids:
                give(node_id, a.strength / math.sqrt(len(ids)))
                if a.line:
                    for sym in _symbols_at(store, b, node_id, a.line):
                        give(sym, a.strength)
        else:
            names = [a.text]
            if "." in a.text:
                names += [".".join(a.text.split(".")[-2:]), a.text.split(".")[-1]]
            for rank, name in enumerate(dict.fromkeys(names)):
                ids = store.exact(name, b, params.name_hits + 1, ["Symbol", "File"])
                if 0 < len(ids) <= params.name_hits:
                    for node_id in ids:
                        give(node_id, a.strength * (0.85**rank) / math.sqrt(len(ids)))
                    break
    return out


def gather(store, b: int, query: str, scored, params: Params = DEFAULT, found: list[Anchor] | None = None) -> Pool:
    """The candidates of a question: the text matches, the nodes it names, and the neighbours of the best."""
    scored = list(scored)[: params.pool]
    rels = _rels(scored)
    pool = Pool(text=any(s is not None for _, s in scored))
    nodes = store.nodes([i for i, _ in scored], b)
    for node_id, score in scored:
        if node_id in nodes:
            pool.cands[node_id] = _cand(nodes[node_id], rels[node_id])
            pool.order.append(node_id)
            if score is None:
                pool.exact.add(node_id)
            else:
                pool.scores[node_id] = score
    named = _lookup(store, b, anchors(query) if found is None else found, params)
    fresh = [i for i in named if i not in pool.cands]
    for node_id, node in store.nodes(fresh, b).items():
        pool.cands[node_id] = _cand(node, 0.0, False)
    for node_id, strength in named.items():
        if node_id in pool.cands:
            pool.cands[node_id].anchor = strength
    lenders = sorted(pool.cands.values(), key=lambda c: -(c.rel * kind_factor(c, params) + params.anchor * c.anchor))
    near: set[str] = set()
    for c in lenders[: params.expand_from]:
        links: list[tuple[str, str, float]] = []
        for e in store.edges_of(c.id, b, "both", limit=120):
            rel = e["rel"]
            if rel not in EXPAND_RELS or (e["status"] != "parsed" and (e.get("conf") or 0.0) < CONF_FLOOR):
                continue
            if rel in ("changes", "mentions"):
                if (
                    e["src"] != c.id
                ):  # a commit lends to what it changed; a file or a symbol does not walk to its commits
                    continue
                other = e["dst"]
            else:
                other = e["dst"] if e["src"] == c.id else e["src"]
            if other != c.id:
                links.append((other, rel, 1.0 if e["status"] == "parsed" else float(e.get("conf") or 0.0)))
                near.add(other)
        pool.links[c.id] = links
    _structural(store, b, pool, lenders[: params.expand_from])
    near = {o for links in pool.links.values() for o, _, _ in links}
    fresh = [i for i in near if i not in pool.cands]
    for node_id, node in store.nodes(fresh, b).items():
        pool.cands[node_id] = _cand(node, 0.0, False)
    return pool


COCHANGE_COMMITS = 200  # the commits that changed a file read for what changed with it
COCHANGE_MAX_FILES = 40  # a commit of more files than this says nothing of what belongs together
COCHANGE_KEEP = 6


def _file_of(store, c: Cand) -> str | None:
    """The id of the File node of a candidate's path."""
    if c.kind == "File":
        return c.id
    return _file_index(store).get(c.path or "")


def _test_names(path: str) -> list[str]:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return [
        f"test_{stem}",
        f"{stem}_test",
        f"{stem}.test",
        f"{stem}.spec",
        f"{stem}Test",
        f"{stem}_spec",
        f"test{stem}",
    ]


def _structural(store, b: int, pool: Pool, lenders: list[Cand]) -> None:
    """The links no edge says: the test file named after a file, and the files that changed in the same commits."""
    files = _file_index(store)
    stems = getattr(store, "_locate_stems", None)
    if stems is None:
        stems = {}
        for path, node_id in files.items():
            stems.setdefault(path.rsplit("/", 1)[-1].rsplit(".", 1)[0], []).append(node_id)
        store._locate_stems = stems
    seen: set[str] = set()
    for c in lenders:
        file_id = _file_of(store, c)
        if file_id is None or file_id in seen or not c.path:
            continue
        seen.add(file_id)
        links = pool.links.setdefault(file_id, [])
        for name in _test_names(c.path):
            for other in stems.get(name, [])[:3]:
                links.append((other, "tests", 1.0))
        commits = [
            r[0]
            for r in store.db.execute(
                "SELECT src FROM edges WHERE dst = ? AND rel = 'changes' AND tx_to IS NULL LIMIT ?",
                (file_id, COCHANGE_COMMITS),
            )
        ]
        if len(commits) < 2:
            continue
        size = {}
        for start in range(0, len(commits), 400):
            chunk = commits[start : start + 400]
            marks = ", ".join("?" for _ in chunk)
            for src, dst in store.db.execute(
                f"SELECT src, dst FROM edges WHERE src IN ({marks}) AND rel = 'changes' AND tx_to IS NULL", chunk
            ):
                size.setdefault(src, []).append(dst)
        together: dict[str, float] = {}
        for members in size.values():
            if 1 < len(members) <= COCHANGE_MAX_FILES:
                for other in members:
                    if other != file_id:
                        together[other] = together.get(other, 0.0) + 1.0 / (len(members) - 1)
        for other, mass in sorted(together.items(), key=lambda kv: -kv[1])[:COCHANGE_KEEP]:
            links.append((other, "cochange", min(1.0, mass / len(commits) * 2)))


# -- ranking ---------------------------------------------------------------------------------------------------------


def kind_factor(c: Cand, params: Params) -> float:
    if c.kind == "Symbol" or (c.kind == "File" and c.source_file):
        factor = 1.0
    elif c.kind in ("File", "Document", "Section"):
        factor = params.non_code
    else:
        factor = params.other_kind
    if c.path and _TEST_PATH.search(c.path):
        factor *= params.test_path
    if params.size_pen and c.line and c.end:
        factor /= 1.0 + params.size_pen * math.log(max(c.end - c.line + 1, 20) / 20.0)
    return factor


def scores(pool: Pool, params: Params = DEFAULT) -> dict[str, float]:
    """The score of each candidate: its text match and its anchor, plus a share of its lenders' scores."""
    weights = dict(params.expand_rels)
    base = {i: c.rel**params.sharp * kind_factor(c, params) + params.anchor * c.anchor for i, c in pool.cands.items()}
    total = dict(base)
    for src, links in pool.links.items():
        for other, rel, conf in links:
            if other in total and (w := weights.get(rel)):
                total[other] += params.expand * w * conf * base.get(src, 0.0)
    return total


def files(pool: Pool, params: Params = DEFAULT, total: dict[str, float] | None = None) -> list[tuple[float, str, list]]:
    """(score, path, ids best first) of each file of the candidates, best first: a file scores its best node and
    ``file_extra`` of its next two. A candidate without a path is a file of its own."""
    total = scores(pool, params) if total is None else total
    groups: dict[str, list[str]] = {}
    for node_id, c in pool.cands.items():
        if total[node_id] <= 0:
            continue
        groups.setdefault(c.path or node_id, []).append(node_id)
    ranked = []
    for key, ids in groups.items():
        ids.sort(key=lambda i: (-total[i], pool.cands[i].kind != "Symbol", i))
        top = total[ids[0]] + params.file_extra * sum(total[i] for i in ids[1:3])
        ranked.append((top, key, ids))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    if ranked and params.weak:
        ranked = [t for t in ranked if t[0] >= params.weak * ranked[0][0]]
    return ranked


def place(ranked: list[tuple[float, str, list]], params: Params = DEFAULT) -> list[str]:
    """The ids of ranked files in reading order: each file's best node in the files' order, then the next ones, a
    file's second node after ``second`` files' first ones."""
    placed = []
    for g, (_, _, ids) in enumerate(ranked):
        for r, node_id in enumerate(ids[:8]):
            placed.append((g + params.second * r * (1 + r) / 2, g, r, node_id))
    placed.sort()
    return [node_id for _, _, _, node_id in placed]


def order(pool: Pool, params: Params = DEFAULT) -> list[str]:
    """The ids of the candidates in the order ``arrange`` lists them (see the module)."""
    return place(files(pool, params), params)


def code_like(pool: Pool, top: int = 10) -> bool:
    """Whether most of the best text matches are code."""
    best = [pool.cands[i] for i in pool.order[:top] if i in pool.cands]
    return bool(best) and sum(c.kind in CODE_KINDS for c in best) * 2 > len(best)


def arrange(
    store, b: int, query: str, scored, params: Params = DEFAULT, ranker=None, limit: int = 10
) -> list[str] | None:
    """The ids of a question's candidates in reading order, or None when the graph is not code and no ``ranker`` is
    given (the text order stands).

    ``scored`` is ``Store.search_scored``: (id, text score) of the text matches, best first. The text matches that the
    reading leaves out follow the ones it lists. ``ranker``, a learned model of ``evo_agents.kg.rank``, fills the first
    places of the reading, told the ``limit`` of results asked for, and reads a pool of mostly documents too (on the
    tune part, the file order of such pools found more answering files than the text order); None keeps the order of
    step 3.
    """
    scored = list(scored)
    if not scored:
        return None
    pool = gather(store, b, query, scored, params)
    if ranker is None and not code_like(pool):
        return None
    ranked = files(pool, params)
    listed = ranker.order(store, b, query, pool, ranked, params, limit) if ranker is not None else place(ranked, params)
    seen = set(listed)
    return listed + [i for i, _ in scored if i not in seen]
