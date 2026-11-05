"""Edges the graph may lack, guessed by a graph model and only ever proposed (model M3, ``evo-agents kg links``).

The model is a knowledge graph embedding, DistMult (Yang et al., 2015): each node and each relation is a vector, and
an edge (h, r, t) is likely when the sum of their elementwise product is high. It trains on CPU with numpy alone (the
``links`` extra) on the ``parsed`` edges of one build, which a source states, never on ``resolved`` ones, which matched
a name and can be wrong. Negatives are the true edge with its head or its tail swapped for another node of the same
kind, weighed by how likely the model finds them (self-adversarial sampling). RotatE (Sun et al., 2019), each node a
vector of complex numbers and each relation a rotation of them, is the other model ``--model`` takes: on the graphs of
bench/results/m3-links.json it reached the same precision with lower recall and AUC, in about twice the time.

What it scores (``candidates``), each a ``Candidate`` with the reason it came up:

- ``ambiguous``: a name of an item that matched several nodes, or a ``resolved`` edge whose target shares its name
  with nodes of its kind: every such node is a candidate target of the same relation;
- ``dangling``: a name of an item that matched nothing: the files with its base name and the symbols with its last
  part are candidates (``mentions``);
- ``doc``: a Document and the DOC_TOP code files of its repo the model ranks highest that it has no edge to
  (``mentions``), only when asked (``--docs``): no name backs them, no hidden edge measures them, and on the
  evo-agents graph the 427 it proposed included test fakes and migrations a reader would not tie to the document;
- ``test``: a test file with no edge to the code file of its stem (``verifies``).

A candidate is proposed when its probability (the sigmoid of its score) reaches the threshold an evaluation chose
for a precision (``evaluate``): one in ten parsed edges is hidden from training, another share chooses the threshold,
and each hidden edge is paired with one false edge whose target is a node of its kind, of the same name when there is
one. A name with several candidates proposes its best only, and only when it leads the next by MARGIN.

Nothing here writes to a graph. ``evo-agents kg links propose`` lists the proposals; with ``--hub`` it sends each to
the hub as a proposal (the MCP tool ``kg_propose``), which changes nothing until a writer or the Curator accepts it.
"""

from __future__ import annotations

import json
import math
import posixpath
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from evo_agents.kg.store import Store
from evo_agents.kg.store_filters import LIVE, SEEN

MODELS = ("distmult", "rotate")  # the first is the default
DIM = 64  # complex dimensions of RotatE, real ones of DistMult
GAMMA = 6.0  # RotatE's margin
EPOCHS = 20
BATCH = 1024
NEGATIVES = 16  # negatives per positive in training
LR = 0.02
ADV = 1.0  # temperature of self-adversarial sampling
L2 = 1e-6  # DistMult's weight decay
SEED = 20261103
HIDE = 0.10  # parsed edges hidden for the test
CHOOSE = 0.05  # parsed edges hidden to choose the threshold
PRECISION = 0.85  # the precision the threshold is chosen for on its share: a margin over the 0.8 the test must reach
MARGIN = 0.1  # how far a name's best candidate leads its next to be proposed
MAX_TRAIN = 600_000  # parsed edges a model trains on at most (a sample past it, by the seed)
MAX_PER_NAME = 25  # candidate targets of one name
DOC_TOP = 3  # code files a document is scored against, the model's best it has no edge to
TEST_NAME = re.compile(r"(^|/)(test_(?P<a>[^/]+)\.py|(?P<b>[^/]+)_test\.(py|go)|(?P<c>[^/]+)\.(test|spec)\.[jt]sx?)$")
CODE_KINDS = ("File",)
DOC_KINDS = ("Document",)


class LinksError(RuntimeError):
    pass


def _numpy():
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - the extra is installed where tests run
        raise LinksError("the graph model needs numpy: pip install 'evo-ak[links]'") from None
    return np


# -- the graph -------------------------------------------------------------------------------------------------------


@dataclass
class LinkGraph:
    """The live nodes and edges of one build, by index: each node's id, kind, name and path; the ``parsed`` edges the
    model learns from (h, r, t); and every edge of any status, as (h, t) pairs and as (h, r) -> {t}."""

    ids: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    rels: list[str] = field(default_factory=list)
    parsed: list[tuple[int, int, int]] = field(default_factory=list)
    resolved: list[tuple[int, int, int]] = field(default_factory=list)
    linked: set[tuple[int, int]] = field(default_factory=set)
    known: dict[tuple[int, int], set[int]] = field(default_factory=lambda: defaultdict(set))
    index: dict[str, int] = field(default_factory=dict)
    rel_index: dict[str, int] = field(default_factory=dict)

    def rel(self, name: str) -> int:
        if name not in self.rel_index:
            self.rel_index[name] = len(self.rels)
            self.rels.append(name)
        return self.rel_index[name]

    def add_node(self, nid: str, kind: str, name: str | None, path: str | None) -> int:
        self.index[nid] = len(self.ids)
        self.ids.append(nid)
        self.kinds.append(kind)
        self.names.append(name or "")
        self.paths.append(path or "")
        return self.index[nid]

    def add_edge(self, src: str, rel: str, dst: str, status: str | None) -> None:
        h, t = self.index.get(src), self.index.get(dst)
        if h is None or t is None or h == t:
            return
        r = self.rel(rel)
        self.linked.add((h, t))
        self.linked.add((t, h))
        self.known[(h, r)].add(t)
        if status == "parsed":
            self.parsed.append((h, r, t))
        elif status == "resolved":
            self.resolved.append((h, r, t))

    def by_kind(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = defaultdict(list)
        for i, kind in enumerate(self.kinds):
            out[kind].append(i)
        return out


def read(store: Store, b: int) -> LinkGraph:
    """Build b's live nodes and edges the store's reader sees."""
    g = LinkGraph()
    for nid, kind, name, props in store.db.execute(
        f"SELECT node_id, kind, name, props FROM nodes WHERE {LIVE} AND {SEEN} ORDER BY node_id", {"b": b}
    ):
        try:
            path = (json.loads(props) if props else {}).get("path")
        except ValueError:
            path = None
        g.add_node(nid, kind, name, path if isinstance(path, str) else None)
    for src, rel, dst, status in store.db.execute(
        f"SELECT src, rel, dst, status FROM edges WHERE {LIVE} AND {SEEN} ORDER BY src, rel, dst", {"b": b}
    ):
        g.add_edge(src, rel, dst, status)
    return g


# -- the model -------------------------------------------------------------------------------------------------------


class Model:
    """RotatE or DistMult over the nodes and relations of one LinkGraph, trained with Adam on numpy arrays."""

    def __init__(self, kind: str, nodes: int, rels: int, dim: int = DIM, seed: int = SEED, gamma: float = GAMMA):
        if kind not in MODELS:
            raise LinksError(f"unknown model {kind!r}: one of {', '.join(MODELS)}")
        np = _numpy()
        self.kind, self.dim, self.gamma = kind, dim, gamma
        rng = np.random.default_rng(seed)
        if kind == "rotate":
            bound = (gamma + 2.0) / dim
            self.params = {
                "re": rng.uniform(-bound, bound, (nodes, dim)),
                "im": rng.uniform(-bound, bound, (nodes, dim)),
                "phase": rng.uniform(-math.pi, math.pi, (rels, dim)),
            }
        else:
            scale = 1.0 / math.sqrt(dim)
            self.params = {"e": rng.normal(0, scale, (nodes, dim)), "r": rng.normal(0, scale, (rels, dim))}
        self._m = {k: np.zeros_like(v) for k, v in self.params.items()}
        self._v = {k: np.zeros_like(v) for k, v in self.params.items()}
        self._step = 0

    # scores and their gradients, for arrays of indices of one shape
    def score(self, h, r, t):
        return self._score(h, r, t)[0]

    def _score(self, h, r, t):
        np = _numpy()
        p = self.params
        if self.kind == "distmult":
            eh, er, et = p["e"][h], p["r"][r], p["e"][t]
            return (eh * er * et).sum(-1), (eh, er, et)
        cos, sin = np.cos(p["phase"][r]), np.sin(p["phase"][r])
        hre, him = p["re"][h], p["im"][h]
        are, aim = hre * cos - him * sin, hre * sin + him * cos
        dre, dim_ = are - p["re"][t], aim - p["im"][t]
        mod = np.sqrt(dre * dre + dim_ * dim_ + 1e-9)
        return self.gamma - mod.sum(-1), (cos, sin, hre, him, are, aim, dre, dim_, mod)

    def _grads(self, h, r, t, coef, cache) -> dict[str, list]:
        """Gradients of sum(coef * score) by parameter, as (indices, rows) to add."""
        c = coef[..., None]
        if self.kind == "distmult":
            eh, er, et = cache
            return {"e": [(h, c * er * et), (t, c * eh * er)], "r": [(r, c * eh * et)]}
        cos, sin, hre, him, are, aim, dre, dim_, mod = cache
        ure, uim = dre / mod, dim_ / mod  # d|d|/d(dre, dim)
        g_are, g_aim = -c * ure, -c * uim  # d score / d a
        return {
            "re": [(h, g_are * cos + g_aim * sin), (t, c * ure)],
            "im": [(h, -g_are * sin + g_aim * cos), (t, c * uim)],
            "phase": [(r, g_are * -aim + g_aim * are)],
        }

    def _adam(self, grads: dict[str, list], lr: float) -> None:
        np = _numpy()
        self._step += 1
        b1, b2, eps = 0.9, 0.999, 1e-8
        for name, parts in grads.items():
            idx = np.concatenate([i.reshape(-1) for i, _ in parts])
            rows = np.concatenate([g.reshape(-1, g.shape[-1]) for _, g in parts])
            uniq, inv = np.unique(idx, return_inverse=True)
            g = np.zeros((len(uniq), rows.shape[1]))
            np.add.at(g, inv, rows)
            g = -g  # ascend the log-likelihood
            if self.kind == "distmult":
                g += L2 * self.params[name][uniq]
            m, v = self._m[name], self._v[name]
            m[uniq] = b1 * m[uniq] + (1 - b1) * g
            v[uniq] = b2 * v[uniq] + (1 - b2) * g * g
            mhat = m[uniq] / (1 - b1**self._step)
            vhat = v[uniq] / (1 - b2**self._step)
            self.params[name][uniq] -= lr * mhat / (np.sqrt(vhat) + eps)

    def fit(
        self,
        triples,
        kinds: list[str],
        *,
        epochs: int = EPOCHS,
        batch: int = BATCH,
        negatives: int = NEGATIVES,
        lr: float = LR,
        seed: int = SEED,
        deadline: float | None = None,
    ) -> dict:
        """Train on ``triples`` (an (n, 3) int array); a negative swaps the head or the tail for a node of its kind.
        Stops at ``deadline`` (time.monotonic) when given. Returns how it went."""
        np = _numpy()
        rng = np.random.default_rng(seed)
        kind_of = {k: i for i, k in enumerate(sorted(set(kinds)))}
        node_kind = np.array([kind_of[k] for k in kinds])
        pools = [np.flatnonzero(node_kind == i) for i in range(len(kind_of))]
        started, done, loss = time.monotonic(), 0, float("nan")
        for _ in range(epochs):
            order = rng.permutation(len(triples))
            total, count = 0.0, 0
            for at in range(0, len(order), batch):
                part = triples[order[at : at + batch]]
                h, r, t = part[:, 0], part[:, 1], part[:, 2]
                swap_tail = rng.random(len(part)) < 0.5
                side = np.where(swap_tail, t, h)
                fake = _same_kind(rng, pools, node_kind[side], negatives)
                nh = np.where(swap_tail[:, None], h[:, None], fake)
                nt = np.where(swap_tail[:, None], fake, t[:, None])
                nr = np.broadcast_to(r[:, None], nh.shape)
                ps, pc = self._score(h, r, t)
                ns, nc = self._score(nh, nr, nt)
                weight = np.exp(ADV * (ns - ns.max(1, keepdims=True)))
                weight /= weight.sum(1, keepdims=True)
                sig_p, sig_n = _sigmoid(ps), _sigmoid(ns)
                total += float(-(np.log(sig_p + 1e-12)).sum() - (weight * np.log(1 - sig_n + 1e-12)).sum(1).sum())
                count += len(part)
                grads = self._grads(h, r, t, 1 - sig_p, pc)  # d log sig(s) / d s
                for name, rows in self._grads(nh, nr, nt, -weight * sig_n, nc).items():
                    grads[name] = grads[name] + rows
                self._adam(grads, lr)
            done += 1
            loss = total / max(count, 1)
            if deadline is not None and time.monotonic() > deadline:
                break
        return {"epochs": done, "loss": round(loss, 4), "seconds": round(time.monotonic() - started, 1)}

    def probability(self, h, r, t):
        np = _numpy()
        return _sigmoid(self.score(np.asarray(h), np.asarray(r), np.asarray(t)))


def _sigmoid(x):
    np = _numpy()
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def _same_kind(rng, pools, kinds, k: int):
    """For each kind of ``kinds``, k nodes drawn from the pool of that kind."""
    np = _numpy()
    out = np.empty((len(kinds), k), dtype=np.int64)
    for kind in np.unique(kinds):
        rows = np.flatnonzero(kinds == kind)
        pool = pools[kind]
        out[rows] = pool[rng.integers(0, len(pool), (len(rows), k))]
    return out


# -- evaluation: hidden edges and the threshold ---------------------------------------------------------------------


def split(g: LinkGraph, seed: int = SEED, hide: float = HIDE, choose: float = CHOOSE):
    """The parsed edges in three: to train on, to choose the threshold by, and hidden for the test. A hidden edge
    whose head or tail would be left with no training edge stays in training (the model cannot place a node it never
    saw), so the parts are of about ``hide`` and ``choose``."""
    np = _numpy()
    rng = np.random.default_rng(seed)
    triples = np.array(g.parsed, dtype=np.int64).reshape(-1, 3)
    order = rng.permutation(len(triples))
    degree = np.bincount(np.concatenate([triples[:, 0], triples[:, 2]]), minlength=len(g.ids))
    n_test, n_choose = int(len(triples) * hide), int(len(triples) * choose)
    test, chosen, train = [], [], []
    for i in order:
        h, _, t = triples[i]
        if (len(test) < n_test or len(chosen) < n_choose) and degree[h] > 1 and degree[t] > 1:
            degree[h] -= 1
            degree[t] -= 1
            (test if len(test) < n_test else chosen).append(i)
        else:
            train.append(i)
    return triples[train], triples[chosen], triples[test]


def false_edges(g: LinkGraph, edges, seed: int = SEED):
    """One false edge per edge of ``edges``: the same head and relation, a tail of the true tail's kind that no edge
    of the graph links from the head by that relation, of the true tail's name when such a node exists (as an
    ambiguous name would offer), else any. Returns the array and how many share the true tail's name."""
    np = _numpy()
    rng = np.random.default_rng(seed + 1)
    by_kind = {k: np.array(v) for k, v in g.by_kind().items()}
    by_name: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, (kind, name) in enumerate(zip(g.kinds, g.names, strict=True)):
        if name:
            by_name[(kind, _last(name))].append(i)
    out, same = [], 0
    for h, r, t in edges:
        known = g.known.get((int(h), int(r)), set())
        twins = [x for x in by_name.get((g.kinds[t], _last(g.names[t])), []) if x != t and x not in known]
        if twins:
            fake = twins[int(rng.integers(len(twins)))]
            same += 1
        else:
            pool = by_kind[g.kinds[t]]
            fake = int(t)
            for _ in range(20):
                fake = int(pool[int(rng.integers(len(pool)))])
                if fake != t and fake not in known:
                    break
            if fake == t:
                continue
        out.append((h, r, fake))
    return np.array(out, dtype=np.int64).reshape(-1, 3), same


def threshold_for(pos, neg, precision: float = PRECISION) -> float | None:
    """The lowest probability at which the edges at or above it reach ``precision`` (true ones over all), None when
    no threshold does."""
    np = _numpy()
    scores = np.concatenate([pos, neg])
    truth = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    order = np.argsort(-scores, kind="stable")
    scores, truth = scores[order], truth[order]
    tp = np.cumsum(truth)
    prec = tp / np.arange(1, len(truth) + 1)
    best = None
    for i in range(len(scores)):
        last = i == len(scores) - 1 or scores[i + 1] < scores[i]
        if last and prec[i] >= precision:
            best = float(scores[i])
    return best


def at(pos, neg, threshold: float | None) -> dict:
    """Precision, recall and counts at ``threshold``, and the area under the ROC curve."""
    auc = _auc(pos, neg)
    if threshold is None:
        return {"precision": None, "recall": 0.0, "proposed": 0, "true": 0, "auc": auc}
    tp, fp = int((pos >= threshold).sum()), int((neg >= threshold).sum())
    return {
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / len(pos), 4) if len(pos) else 0.0,
        "proposed": tp + fp,
        "true": tp,
        "auc": auc,
    }


def _auc(pos, neg) -> float | None:
    np = _numpy()
    if not len(pos) or not len(neg):
        return None
    scores = np.concatenate([pos, neg])
    ranks = scores.argsort().argsort() + 1.0
    return round(float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))), 4)


def evaluate(
    g: LinkGraph,
    *,
    model: str = MODELS[0],
    dim: int = DIM,
    epochs: int = EPOCHS,
    seed: int = SEED,
    precision: float = PRECISION,
    max_train: int = MAX_TRAIN,
    deadline: float | None = None,
) -> tuple[Model, dict]:
    """Train on the parsed edges but the hidden ones, choose the threshold on one share of them for ``precision``,
    and score the other share (``split``); the model and the numbers."""
    np = _numpy()
    train, chosen, test = split(g, seed)
    if len(train) > max_train:
        train = train[np.random.default_rng(seed).permutation(len(train))[:max_train]]
    m = Model(model, len(g.ids), max(len(g.rels), 1), dim, seed)
    fitted = m.fit(train, g.kinds, epochs=epochs, seed=seed, deadline=deadline)
    fake_c, _ = false_edges(g, chosen, seed)
    fake_t, same = false_edges(g, test, seed + 7)

    def probs(edges):
        return m.probability(edges[:, 0], edges[:, 1], edges[:, 2]) if len(edges) else np.zeros(0)

    threshold = threshold_for(probs(chosen), probs(fake_c), precision)
    numbers = at(probs(test), probs(fake_t), threshold)
    per_rel = {}
    for r, name in enumerate(g.rels):
        pick, fpick = test[:, 1] == r, fake_t[:, 1] == r
        if pick.sum() >= 20:
            row = at(probs(test[pick]), probs(fake_t[fpick]), threshold)
            per_rel[name] = {"n": int(pick.sum()), **{k: row[k] for k in ("precision", "recall", "auc")}}
    return m, {
        "model": model,
        "dim": dim,
        "nodes": len(g.ids),
        "parsed": len(g.parsed),
        "train": len(train),
        "chosen": len(chosen),
        "n": len(test),
        "same_name_false": same,
        "threshold": None if threshold is None else round(threshold, 4),
        **numbers,
        "fit": fitted,
        "relations": per_rel,
    }


# -- candidates and proposals ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """An edge to score: (src, rel, dst) by id, why it came up, and the name or path it is for (one group of rivals
    per (src, rel, name))."""

    src: str
    rel: str
    dst: str
    why: str
    name: str


def _last(name: str) -> str:
    return re.split(r"[.:/]", name.rstrip("()"))[-1] if name else ""


def _clean(text: str) -> str:
    text = text.strip().strip("`'\"").rstrip(".,:;)").strip()
    text = text[2:] if text.startswith("./") else text
    return text.split(":", 1)[1] if re.match(r"^[\w.-]+:[\w./-]", text) and "/" in text.split(":", 1)[1] else text


def _test_stem(path: str) -> str | None:
    m = TEST_NAME.search(path)
    if not m:
        return None
    return m.group("a") or m.group("b") or m.group("c")


def _is_test(path: str | None) -> bool:
    return bool(path) and (_test_stem(path) is not None or path.startswith("tests/") or "/tests/" in path)


def _lang(path: str | None) -> str:
    ext = posixpath.splitext(path or "")[1].lower()
    return {".tsx": ".ts", ".jsx": ".js", ".mjs": ".js", ".cjs": ".js", ".pyi": ".py"}.get(ext, ext)


def _path(g: LinkGraph, i: int) -> str | None:
    """A node's path, or for a symbol the path its id names (``repo:path::name``)."""
    if g.paths[i]:
        return g.paths[i]
    nid = g.ids[i]
    return nid.split("::", 1)[0].rsplit(":", 1)[-1] if "::" in nid else None


def _plausible(g: LinkGraph, h: int, rel: str, t: int) -> bool:
    """Whether a candidate is worth a reviewer's time: a test is a target only of a test, and a call stays within
    one language. The graph model scores central nodes high whatever their kind, so it cannot tell these apart."""
    src, dst = _path(g, h), _path(g, t)
    if _is_test(dst) and not _is_test(src):
        return False
    return rel != "calls" or _lang(src) == _lang(dst)


class _Names:
    """The nodes of a LinkGraph by base name (files and documents), by name and last name part (symbols), by stem
    (code files that are not tests)."""

    def __init__(self, g: LinkGraph):
        self.base: dict[str, list[int]] = defaultdict(list)
        self.symbol: dict[str, list[int]] = defaultdict(list)
        self.stem: dict[str, list[int]] = defaultdict(list)
        for i, kind in enumerate(g.kinds):
            path = g.paths[i]
            if kind in ("File", "Document") and path:
                base = posixpath.basename(path)
                self.base[base].append(i)
                if kind == "File" and _test_stem(path) is None:
                    self.stem[base.rsplit(".", 1)[0]].append(i)
            elif kind == "Symbol" and g.names[i]:
                self.symbol[g.names[i]].append(i)
                last = _last(g.names[i])
                if last != g.names[i]:
                    self.symbol[last].append(i)

    def targets(self, text: str) -> list[int]:
        text = _clean(text)
        if not text or " " in text or "://" in text:
            return []
        if "/" in text or re.search(r"\.[A-Za-z]{1,5}$", text) and not text.count(".") > 1:
            return self.base.get(posixpath.basename(text.rstrip("/")), [])[:MAX_PER_NAME]
        name = text.removesuffix("()")
        found = self.symbol.get(name) or self.symbol.get(_last(name)) or []
        return found[:MAX_PER_NAME]


Pair = tuple[int, str, int, str, str]  # head, relation, tail, why, the name that suggests it


def _review_pairs(g: LinkGraph, names: _Names, review: list[dict]) -> Iterator[Pair]:
    """The targets the samples of ``dangling_mention`` and ``ambiguous_match`` review items name."""
    for item in review:
        rule = item.get("rule")
        if rule not in ("dangling_mention", "ambiguous_match"):
            continue
        nodes = item.get("evidence") or [n["id"] for n in item.get("nodes") or []]
        h = g.index.get(nodes[0]) if nodes else None
        if h is None:
            continue
        why = "dangling" if rule == "dangling_mention" else "ambiguous"
        for sample in dict.fromkeys((item.get("detail") or {}).get("samples") or []):
            for t in names.targets(sample):
                yield h, "mentions", t, why, sample


def _rival_pairs(g: LinkGraph) -> Iterator[Pair]:
    """For each resolved edge to a symbol, the other symbols of its name, when a few share it."""
    by_kind_name: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, (kind, name) in enumerate(zip(g.kinds, g.names, strict=True)):
        if kind == "Symbol" and name:
            by_kind_name[(kind, name)].append(i)
    for h, r, t in g.resolved:
        rivals = by_kind_name.get((g.kinds[t], g.names[t]), [])
        if 1 < len(rivals) <= MAX_PER_NAME:
            for x in rivals:
                yield h, g.rels[r], x, "ambiguous", g.names[t]


def _test_pairs(g: LinkGraph, names: _Names) -> Iterator[Pair]:
    """Each test file to the code files of its stem it has no edge to."""
    for h, kind in enumerate(g.kinds):
        stem = _test_stem(g.paths[h]) if kind == "File" else None
        if not stem:
            continue
        for t in names.stem.get(stem, [])[:MAX_PER_NAME]:
            if (h, t) not in g.linked:
                yield h, "verifies", t, "test", stem


def candidates(
    g: LinkGraph,
    review: list[dict] | None = None,
    *,
    model: Model | None = None,
    docs: bool = False,
    tests: bool = True,
):
    """The candidate edges of the graph (see the module): of the review items ``review`` (``dangling_mention`` and
    ``ambiguous_match``, as ``evo-agents hub kg review --json`` lists them, or as a build with clusters stores them),
    of resolved edges to a name several nodes share, of documents to code files (with ``docs`` and ``model``, which
    picks them; off by default, as no hidden edge measures them) and of tests to code files."""
    names = _Names(g)
    out: list[Candidate] = []
    seen: set[tuple[str, str, str]] = set()

    def add(h: int, rel: str, t: int, why: str, name: str) -> None:
        key = (g.ids[h], rel, g.ids[t])
        if h == t or key in seen or not _plausible(g, h, rel, t):
            return
        r = g.rel_index.get(rel)
        if r is not None and t in g.known.get((h, r), ()):
            return  # the graph has it
        seen.add(key)
        out.append(Candidate(g.ids[h], rel, g.ids[t], why, name))

    for pair in _review_pairs(g, names, review or []):
        add(*pair)
    for pair in _rival_pairs(g):
        add(*pair)
    if docs and model is not None and "mentions" in g.rel_index:
        for h, t in _doc_targets(g, model):
            add(h, "mentions", t, "doc", g.paths[t])
    if tests:
        for pair in _test_pairs(g, names):
            add(*pair)
    return out


def repo_of(nid: str) -> str:
    """The repo a node id names first (``repo:file:path``, ``repo:path::name``); bench graphs hold one repo."""
    return nid.split(":", 1)[0]


def _doc_targets(g: LinkGraph, model: Model) -> list[tuple[int, int]]:
    """For each Document, the DOC_TOP code files of its repo the model scores highest by ``mentions`` among those
    it has no edge to: what the document most likely speaks of without naming."""
    np = _numpy()
    r = g.rel_index["mentions"]
    files: dict[str, list[int]] = defaultdict(list)
    for i, kind in enumerate(g.kinds):
        if kind in CODE_KINDS and g.paths[i] and _test_stem(g.paths[i]) is None:
            files[repo_of(g.ids[i])].append(i)
    out = []
    for h, kind in enumerate(g.kinds):
        pool = files.get(repo_of(g.ids[h])) if kind in DOC_KINDS else None
        if not pool:
            continue
        ts = np.array(pool)
        p = model.probability(np.full(len(ts), h), np.full(len(ts), r), ts)
        taken = 0
        for i in np.argsort(-p):
            t = int(ts[i])
            if (h, t) not in g.linked:
                out.append((h, t))
                taken += 1
                if taken >= DOC_TOP:
                    break
    return out


def score(model: Model, g: LinkGraph, found: list[Candidate]):
    """Each candidate's probability, in order."""
    np = _numpy()
    keep = [c for c in found if c.rel in g.rel_index]
    if not keep:
        return []
    h = np.array([g.index[c.src] for c in keep])
    r = np.array([g.rel_index[c.rel] for c in keep])
    t = np.array([g.index[c.dst] for c in keep])
    return list(zip(keep, model.probability(h, r, t).tolist(), strict=True))


def proposals(scored: list[tuple[Candidate, float]], threshold: float, margin: float = MARGIN) -> list[dict]:
    """Of each group of rivals (one source, relation and name), the best candidate, when it reaches ``threshold``
    and leads the next by ``margin``; as decisions the hub takes (kind link), best first."""
    groups: dict[tuple[str, str, str], list[tuple[Candidate, float]]] = defaultdict(list)
    for c, p in scored:
        groups[(c.src, c.rel, c.name)].append((c, p))
    out = []
    for rivals in groups.values():
        rivals.sort(key=lambda x: (-x[1], x[0].dst))
        best, p = rivals[0]
        runner = rivals[1][1] if len(rivals) > 1 else 0.0
        if p < threshold or p - runner < margin:
            continue
        out.append(
            {
                "kind": "link",
                "src": best.src,
                "rel": best.rel,
                "dst": best.dst,
                "why": best.why,
                "name": best.name,
                "probability": round(p, 4),
                "rivals": len(rivals) - 1,
                "reason": (
                    f"kg links ({best.why}): the graph model gives {p:.2f} (threshold {threshold:.2f})"
                    + (f", next of {len(rivals) - 1} rivals {runner:.2f}" if len(rivals) > 1 else "")
                    + f" for {best.name!r}"
                ),
            }
        )
    out.sort(key=lambda d: (-d["probability"], d["src"], d["dst"]))
    return out
