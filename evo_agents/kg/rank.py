"""A learned order of the first results of a code question (model M1): a LightGBM LambdaMART model that picks, from
the nodes ``evo_agents.kg.locate`` found for a kg_search, the ones read first, from numbers alone.

``locate`` reads a question by hand-set weights (its step 3 orders files by a weighted sum, then places each file's
nodes). Here each of its first TOP_FILES files is described by FILE_FEATURES, numbers only: the text match (the BM25
score, whether the search matched it exactly, the share of the query's words in its name and its path), the anchors the
question names and how many hops of the graph a file is from one, the score each kind of edge lent it, its kind and
whether it is a test, a source file or a document, the PageRank of the candidates' graph seeded by their text match, its
degree in that graph and in the whole graph, the status of the edges that reached it, how many other candidates share
its directory, whether a test is linked to it, how many commits changed it and how long ago the last one did next to
the other candidates, the lines it spans, the prior an agent's clicks gave it (``evo_agents.kg.asked``), and the cosine
of its embedding when the search ranked by meaning. Each of the first PER_FILE nodes of the first FILES files then adds
NODE_FEATURES: where locate placed it, its own score, text match and anchor next to its file's best, its kind and the
lines it spans, whether the question names it, and the number of results asked for. No word of a project is a
feature, so the model file holds no text of any project, and one model serves every graph. The model scores each node;
the SLOTS it scores best are read first, then the others in ``locate.place``'s order. With the model on, a pool whose
best text matches are mostly documents is read the same way (``locate.arrange``).

The model ships in the package (MODEL_FILE). kg_search uses it when LightGBM is installed (the ``rank`` extra) and the
setting is on: ``Session.rank`` when a caller sets it, else $EVO_KG_RANK (on or off), else DEFAULT_ON. Without either
the order is the one ``locate`` gives. ``evo-agents kg rank train`` makes the model from labels this module never reads
(``evo_agents.kg.bench.rank_train``); docs/kg-bench.md, section Learned ranking (M1), has how and what it scored.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter, deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from evo_agents.kg import locate
from evo_agents.kg.asked import terms

MODEL_FILE = Path(__file__).with_name("rank_model.txt")
ENV = "EVO_KG_RANK"
DEFAULT_ON = False  # off: on by default it reorders what tests of serve, eval and hybrid fix (docs/kg-bench.md, M1)
TOP_FILES = 30  # the files of locate's order described by FILE_FEATURES (their neighbourhood is read among these)
FILES = 15  # the files whose nodes the model orders
PER_FILE = 6  # the nodes of each of them it orders, locate's best first
SLOTS = 3  # the first places of the reading the model fills; locate's order follows
PAGERANK_STEPS = 15
PAGERANK_DAMPING = 0.85
AGE_COMMITS = 64  # the commits of a file read for the time of its last change: the newest the edge index gives first
MAX_HOPS = 4  # a file this many hops or more from every anchor, or with no anchor at all
KIND_CODE = {"Symbol": 0, "File": 1, "Document": 2, "Section": 3}

FILE_FEATURES = (
    # the question
    "q_words",
    "q_anchors",
    "q_trace",
    "q_paths",
    "q_cands",
    "q_files",
    "q_text",
    # the file in locate's order
    "loc_score",
    "loc_ratio",
    "loc_rank",
    "loc_gap",
    # its text match
    "n_nodes",
    "n_text",
    "n_symbols",
    "best_rel",
    "sum_rel",
    "file_rel",
    "text_pos",
    "bm25",
    "exact",
    "name_cover",
    "path_cover",
    "sym_cover",
    # what the question names
    "anchor_max",
    "anchor_file",
    "anchor_hops",
    # what the graph lent it
    "base_max",
    "exp_calls",
    "exp_imports",
    "exp_inherits",
    "exp_overrides",
    "exp_changes",
    "exp_mentions",
    "exp_tests",
    "exp_cochange",
    "links_parsed",
    "links_resolved",
    "pagerank",
    "deg_pool",
    # what it is
    "kind",
    "is_test",
    "is_source",
    "is_doc",
    "depth",
    "span",
    # its neighbourhood among the candidates
    "same_dir",
    "same_dir_top",
    "same_parent",
    "has_test",
    # the whole graph
    "imports_in",
    "defines",
    "commits",
    "commit_age",
    "asked",
    "cosine",
)
NODE_FEATURES = (
    # where locate put the node
    "file_rank",
    "node_rank",
    "placed",
    "node_ratio",
    "node_in_file",
    "file_ratio",
    # the node itself
    "node_rel",
    "node_anchor",
    "node_kind",
    "node_lines",
    "node_whole",
    "node_bm25",
    "node_exact",
    "node_text_pos",
    "node_from_text",
    "node_member",
    "node_first",
    # what the question names of it
    "node_named",
    "node_named_any_case",
    "parent_ratio",
    # its file's nodes
    "file_lines",
    "file_nodes",
    # the results asked for
    "limit",
)
FEATURES = FILE_FEATURES + NODE_FEATURES
EXP_RELS = ("calls", "imports", "inherits", "overrides", "changes", "mentions", "tests", "cochange")


def enabled(want: bool | None = None) -> bool:
    """Whether kg_search orders files by the model: ``want`` when set, else $EVO_KG_RANK, else DEFAULT_ON."""
    if want is not None:
        return want
    value = os.environ.get(ENV, "").strip().lower()
    if value in ("on", "1", "true", "yes"):
        return True
    if value in ("off", "0", "false", "no"):
        return False
    return DEFAULT_ON


def ranker_for(want: bool | None = None) -> Ranker | None:
    """The shipped model when the setting is on and LightGBM is installed, else None (locate's order stands)."""
    if not enabled(want):
        return None
    return load(MODEL_FILE)


def state(want: bool | None = None) -> dict:
    """What a run ranked with, for its summary: on or off, and the digest of the model when on."""
    if not enabled(want):
        return {"on": False}
    model = load(MODEL_FILE)
    if model is None:
        return {"on": False, "why": "no LightGBM or no model file"}
    return {"on": True, "model": model.digest}


def load(path: Path) -> Ranker | None:
    """The model of ``path``, or None when LightGBM is not installed, the file is missing, or it scores other
    features than FEATURES."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load(path: str, _mtime: int, _size: int) -> Ranker | None:
    try:
        import lightgbm
    except ImportError:
        return None
    text = Path(path).read_text(encoding="utf-8")
    booster = lightgbm.Booster(model_str=text)
    if tuple(booster.feature_name()) != FEATURES:  # a model of other features (an older one) is not used
        return None
    return Ranker(booster, hashlib.sha256(text.encode()).hexdigest()[:12])


class Ranker:
    """A model that scores the rows of ``features``; more is better."""

    def __init__(self, model, digest: str = ""):
        self.model = model  # anything with predict(rows) -> scores
        self.digest = digest

    def scores(self, rows: list[list[float]]) -> list[float]:
        if not rows:
            return []
        import numpy

        return [float(s) for s in self.model.predict(numpy.asarray(rows, dtype=float))]

    def order(
        self, store, b: int, query: str, pool: locate.Pool, ranked: list, params=locate.DEFAULT, limit: int = 10
    ) -> list[str]:
        """The ids of ``ranked``'s nodes (``locate.files``) in reading order: the SLOTS nodes the model scores best
        of the first PER_FILE nodes of the first FILES files, then the others as ``locate.place`` lists them."""
        ids, rows = node_rows(store, b, query, pool, ranked, params, limit=limit)
        if len(ids) < 2:
            return locate.place(ranked, params)
        got = self.scores(rows)
        chosen = [ids[i] for i in sorted(range(len(ids)), key=lambda i: (-got[i], i))][:SLOTS]
        seen = set(chosen)
        return chosen + [i for i in locate.place(ranked, params) if i not in seen]


# -- the features ----------------------------------------------------------------------------------------------------


def _cover(words: set[str], text: str | None) -> float:
    if not words or not text:
        return 0.0
    found = set(terms(text.replace("/", " ").replace(".", " ").replace("_", " ")))
    return len(words & found) / len(words)


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _hops(pool: locate.Pool) -> dict[str, int]:
    """Hops of the candidates' graph from the nearest anchored candidate, either way along a link."""
    near: dict[str, set[str]] = {}
    for src, links in pool.links.items():
        for other, _, _ in links:
            near.setdefault(src, set()).add(other)
            near.setdefault(other, set()).add(src)
    dist = {i: 0 for i, c in pool.cands.items() if c.anchor > 0}
    queue = deque(dist)
    while queue:
        node = queue.popleft()
        if dist[node] + 1 >= MAX_HOPS:
            continue
        for other in near.get(node, ()):
            if other not in dist:
                dist[other] = dist[node] + 1
                queue.append(other)
    return dist


def _pagerank(pool: locate.Pool, base: dict[str, float]) -> dict[str, float]:
    """PageRank of the candidates' graph (links both ways), restarting at the candidates by their base score."""
    ids = list(pool.cands)
    if not ids:
        return {}
    out: dict[str, list[str]] = {i: [] for i in ids}
    for src, links in pool.links.items():
        if src not in out:
            continue
        for other, _, _ in links:
            if other in out and other != src:
                out[src].append(other)
                out[other].append(src)
    total = sum(max(base.get(i, 0.0), 0.0) for i in ids)
    seed = {i: (max(base.get(i, 0.0), 0.0) / total if total else 1.0 / len(ids)) for i in ids}
    rank = dict(seed)
    linked = [i for i in ids if out[i]]
    for _ in range(PAGERANK_STEPS):
        lone = sum(rank[i] for i in ids if not out[i])  # a candidate with no link gives back to the seeds
        nxt = {i: (1 - PAGERANK_DAMPING + PAGERANK_DAMPING * lone) * seed[i] for i in ids}
        for i in linked:
            share = PAGERANK_DAMPING * rank[i] / len(out[i])
            for o in out[i]:
                nxt[o] += share
        rank = nxt
    return rank


def _time(text) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _graph_counts(store, file_ids: list[str]) -> dict[str, dict]:
    """Per File node of ``file_ids``: the imports that reach it, the symbols it defines, the commits that changed it
    and the time of the last of the first AGE_COMMITS of them (a file of thousands of commits would cost a search
    tens of milliseconds more to read them all)."""
    out: dict[str, dict] = {i: {"imports_in": 0, "defines": 0, "commits": 0, "last": None} for i in file_ids}
    if not file_ids:
        return out
    marks = ", ".join("?" for _ in file_ids)
    for dst, rel, n in store.db.execute(
        f"SELECT dst, rel, count(*) FROM edges WHERE dst IN ({marks}) AND rel IN ('imports', 'changes')"
        " AND tx_to IS NULL GROUP BY dst, rel",
        file_ids,
    ):
        out[dst]["imports_in" if rel == "imports" else "commits"] = n
    for src, n in store.db.execute(
        f"SELECT src, count(*) FROM edges WHERE src IN ({marks}) AND rel = 'defines' AND tx_to IS NULL GROUP BY src",
        file_ids,
    ):
        out[src]["defines"] = n
    for file_id in file_ids:
        if out[file_id]["commits"]:
            (last,) = store.db.execute(
                "SELECT max(json_extract(n.props, '$.time')) FROM (SELECT src FROM edges WHERE dst = ? AND"
                " rel = 'changes' AND tx_to IS NULL LIMIT ?) e JOIN nodes n ON n.node_id = e.src AND n.tx_to IS NULL",
                (file_id, AGE_COMMITS),
            ).fetchone()
            out[file_id]["last"] = _time(last)
    return out


def _asked(store, ids: list[str]) -> dict[str, float]:
    try:
        return store._asked_priors(ids)
    except Exception:  # noqa: BLE001 - a graph without the asked index, or a store of another kind
        return {}


def features(store, b: int, query: str, pool: locate.Pool, ranked: list, params=locate.DEFAULT, cosine=None):
    """One row of FEATURES for each file of ``ranked`` (``locate.files``), in its order. ``cosine``, when given, is
    the cosine of the query's embedding with each node's (id -> float); without it the feature is missing (NaN)."""
    weights = dict(params.expand_rels)
    cands = pool.cands
    base = {i: c.rel**params.sharp * locate.kind_factor(c, params) + params.anchor * c.anchor for i, c in cands.items()}
    lent: dict[str, Counter] = {}
    status: dict[str, Counter] = {}
    deg: Counter = Counter()
    for src, links in pool.links.items():
        for other, rel, conf in links:
            if other not in cands:
                continue
            deg[other] += 1
            deg[src] += 1
            if (w := weights.get(rel)) and base.get(src):
                lent.setdefault(other, Counter())[rel] += params.expand * w * conf * base[src]
            status.setdefault(other, Counter())["parsed" if conf >= 1.0 else "resolved"] += 1
    hops = _hops(pool)
    pr = _pagerank(pool, base)
    position = {i: k for k, i in enumerate(pool.order)}
    words = set(terms(query))
    found = locate.anchors(query)
    n_order = max(len(pool.order), 1)
    best = ranked[0][0] if ranked and ranked[0][0] > 0 else 1.0
    paths = [key for _, key, _ in ranked]
    dirs = Counter(_dir(p) for p in paths)
    parents = Counter(_dir(_dir(p)) for p in paths)
    top_dir = _dir(paths[0]) if paths else ""
    tested = set()
    for src, links in pool.links.items():
        for other, rel, _ in links:
            if rel == "tests":
                for i in (src, other):
                    if i in cands and cands[i].path:
                        tested.add(cands[i].path)
    file_index = locate._file_index(store) if store is not None else {}
    file_ids = {key: file_index[key] for key in paths if key in file_index}
    counts = _graph_counts(store, list(file_ids.values())) if store is not None else {}
    lasts = [counts[f]["last"] for f in file_ids.values() if counts.get(f, {}).get("last")]
    newest = max(lasts) if lasts else None
    priors = _asked(store, [i for _, _, ids in ranked for i in ids]) if store is not None else {}
    q = [
        float(len(words)),
        float(len(found)),
        float(any(a.kind == "line" for a in found)),
        float(sum(a.kind == "path" for a in found)),
        float(len(cands)),
        float(len(ranked)),
        float(pool.text),
    ]
    rows = []
    for k, (top, key, ids) in enumerate(ranked):
        nodes = [cands[i] for i in ids]
        first = nodes[0]
        file_node = next((c for c in nodes if c.kind == "File"), None)
        nxt = ranked[k + 1][0] if k + 1 < len(ranked) else 0.0
        exp = Counter()
        st = Counter()
        for i in ids:
            exp.update(lent.get(i, {}))
            st.update(status.get(i, {}))
        symbol = next((c for c in nodes if c.kind == "Symbol"), None)
        span = (first.end - first.line + 1) if first.line and first.end else 0
        fid = file_ids.get(key)
        g = counts.get(fid, {}) if fid else {}
        last = g.get("last")
        cos = [cosine[i] for i in ids if cosine and i in cosine]
        raw = [pool.scores[i] for i in ids if i in pool.scores]
        rows.append(
            [
                *q,
                top,
                top / best,
                float(k),
                (top - nxt) / best,
                float(len(ids)),
                float(sum(c.from_pool for c in nodes)),
                float(sum(c.kind == "Symbol" for c in nodes)),
                max(c.rel for c in nodes),
                sum(c.rel for c in nodes),
                file_node.rel if file_node else 0.0,
                min((position[i] for i in ids if i in position), default=n_order) / n_order,
                math.log1p(max(raw)) if raw else 0.0,
                float(any(i in pool.exact for i in ids)),
                _cover(words, key.rsplit("/", 1)[-1]),
                _cover(words, key),
                _cover(words, symbol.name) if symbol else 0.0,
                max(c.anchor for c in nodes),
                file_node.anchor if file_node else 0.0,
                float(min((hops.get(i, MAX_HOPS) for i in ids), default=MAX_HOPS)),
                max(base[i] for i in ids),
                *(exp.get(rel, 0.0) for rel in EXP_RELS),
                float(st.get("parsed", 0)),
                float(st.get("resolved", 0)),
                sum(pr.get(i, 0.0) for i in ids) * len(cands),
                float(sum(deg.get(i, 0) for i in ids)),
                float(KIND_CODE.get(first.kind, 4)),
                float(bool(locate._TEST_PATH.search(key))),
                float(first.source_file),
                float(first.kind in ("Document", "Section") or (first.kind == "File" and not first.source_file)),
                float(key.count("/")),
                float(span),
                float(dirs[_dir(key)] - 1),
                float(_dir(key) == top_dir),
                float(parents[_dir(_dir(key))] - 1),
                float(key in tested),
                float(g.get("imports_in", 0)),
                float(g.get("defines", 0)),
                math.log1p(g.get("commits", 0)),
                (newest - last) / 86400.0 if newest and last else math.nan,
                max((priors.get(i, 1.0) for i in ids), default=1.0),
                max(cos) if cos else math.nan,
            ]
        )
    return rows


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _short(name: str) -> str:
    return name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def _file_lines(store, file_ids: list[str]) -> dict[str, int]:
    """The last line of the last symbol of each File node of ``file_ids``: the lines a whole-file result stands for,
    at least (a file's own length is not in the graph)."""
    out: dict[str, int] = {}
    if store is None:
        return out
    for file_id in file_ids:
        repo, sep, path = file_id.partition(":file:")
        if not sep:
            continue
        prefix = f"symbol:{repo}:{path}::"
        (last,) = store.db.execute(
            "SELECT max(json_extract(props, '$.end_line')) FROM nodes WHERE node_id >= ? AND node_id < ?"
            " AND tx_to IS NULL",
            (prefix, prefix + "\uffff"),
        ).fetchone()
        if isinstance(last, int):
            out[file_id] = last
    return out


def node_rows(
    store, b: int, query: str, pool: locate.Pool, ranked: list, params=locate.DEFAULT, cosine=None, limit: int = 10
):
    """(ids, rows of FEATURES) of the nodes the model orders: the first PER_FILE nodes of each of the first FILES
    files of ``ranked`` (``locate.files``), each its file's FILE_FEATURES then its own NODE_FEATURES."""
    head = ranked[:TOP_FILES]
    if not head:
        return [], []
    file_rows = features(store, b, query, pool, head, params, cosine)
    total = locate.scores(pool, params)
    picked = [(g, k, i) for g, (_, _, ids) in enumerate(head[:FILES]) for k, i in enumerate(ids[:PER_FILE])]
    placed = {i: p for p, i in enumerate(locate.place(head[:FILES], params))}
    position = {i: k for k, i in enumerate(pool.order)}
    cands = pool.cands
    best = max((total.get(i, 0.0) for _, _, i in picked), default=0.0) or 1.0
    top_file = head[0][0] or 1.0
    named = set(_NAME.findall(query))
    named_lower = {w.lower() for w in named}
    whole = [i for _, _, i in picked if cands[i].kind == "File"]
    lines_of = _file_lines(store, whole)
    ids, rows = [], []
    for g, (score, _, file_ids) in enumerate(head[:FILES]):
        mine = file_ids[:PER_FILE]
        names = {cands[i].name: total.get(i, 0.0) for i in file_ids}
        spans = []
        for i in mine:
            c = cands[i]
            if c.line and c.end:
                spans.append(c.end - c.line + 1)
            elif c.kind == "File" and i in lines_of:
                spans.append(lines_of[i])
        ftop = total.get(mine[0], 0.0) or 1.0
        for k, i in enumerate(mine):
            c = cands[i]
            name = c.name or ""
            short = _short(name)
            if c.line and c.end:
                span = float(c.end - c.line + 1)
            elif c.kind == "File" and i in lines_of:
                span = float(lines_of[i])
            else:
                span = math.nan
            parent = name.rsplit(".", 1)[0] if "." in name else ""
            ids.append(i)
            rows.append(
                file_rows[g]
                + [
                    float(g),
                    float(k),
                    float(placed.get(i, len(placed))),
                    total.get(i, 0.0) / best,
                    total.get(i, 0.0) / ftop,
                    score / top_file,
                    c.rel,
                    c.anchor,
                    float(KIND_CODE.get(c.kind, 4)),
                    math.log1p(span) if span == span else math.nan,
                    float(c.kind == "File"),
                    math.log1p(pool.scores[i]) if i in pool.scores else math.nan,
                    float(i in pool.exact),
                    float(position.get(i, -1)),
                    float(c.from_pool),
                    float("." in name),
                    math.log1p(c.line or 0),
                    float(short in named),
                    float(len(short) > 3 and short.lower() in named_lower),
                    names.get(parent, 0.0) / best if parent else 0.0,
                    math.log1p(sum(spans)),
                    float(len(file_ids)),
                    float(limit),
                ]
            )
    return ids, rows
