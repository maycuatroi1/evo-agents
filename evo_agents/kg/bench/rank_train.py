"""Training the learned order of kg_search's first results (``evo_agents.kg.rank``, model M1) from labels:
`kg rank train`.

A label is a question, the graph it is asked of, the files that answer it and, when known, their lines. Four kinds,
none from a repo of the bench's holdout or test part (``tasks.repo_split``):

- ``bench``: the tune part of the code benches (their issues, gold files and gold lines; ContextBench's
  /workspace/<repo>/ paths read as the repo's own);
- ``git``: the history of the tune repos after the commit each graph was built at, so the graph never holds the commit
  it is asked about: a commit's subject and body is the question, the files it changed that the graph holds are the
  answer and the lines its hunks replaced are the answer's lines. Commits of a bot, with fewer than ``min_words``
  words, changing more than ``max_files`` files once lock files and generated files are left out, or changing a file
  that changed since the graph's commit (its lines would not be the graph's) are not labels (the thresholds were chosen
  on the tune part; docs/kg-bench.md);
- ``plan``: a `kg eval-questions` set of a local project (its plans' steps, each to the files its commits changed),
  the questions of its tune split only (``evaluate.split_of``), so the project's holdout stays unseen;
- ``click``: a project's kg_search labels as the hub keeps them (``evo_agents.hub.kg_labels``, one JSON object per
  line), each clicked file weighed by the inverse propensity of its rank (``evo_agents.kg.asked.propensities``).

Each label is asked the way kg_search asks it with the model on (the text search, then ``locate``), and each node the
model orders (``rank.node_rows``) becomes a row graded by ``grade``: 0 outside the answer, 1 in an answering file but
off its lines (or in an answering file of a label without lines), 2 to 5 as more of the node's lines are answer
lines. The model learns from numbers only, so it holds no text of the repos or projects it learned from. ``fit``
trains LightGBM's LambdaMART on the rows, picking the number of trees by cross-validation over repos;
``evaluate_cv`` scores the model's first ``rank.SLOTS`` nodes against locate's on the bench labels of held-out repos.
"""

from __future__ import annotations

import json
import math
import random
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from evo_agents.kg import asked, locate, rank
from evo_agents.kg.bench import graphs
from evo_agents.kg.bench.tasks import TUNE, Task, load_tasks, repo_split
from evo_agents.kg.commit_labels import (  # noqa: F401 (the filters are shared with kg eval-questions)
    BOTS,
    GENERATED,
    MAX_FILES,
    MIN_WORDS,
    PR_REF,
    commit_label,
)
from evo_agents.kg.evaluate import TUNE as EVAL_TUNE
from evo_agents.kg.evaluate import load_questions, split_of

TUNED_SETS = ("swe-explore", "contextbench", "mulocbench")  # the sets with a tune part (LocBench has none)
SEARCH_LIMITS = {"swe-explore": 5}  # kg_search's limit in the bench, which sets how many text matches it reads
DEFAULT_LIMIT = 10
PER_GRAPH = 40  # the commits after each graph's own commit taken as labels, the nearest first
WEIGHTS = {"bench": 1.0, "git": 1.0, "plan": 0.5, "click": 1.0}  # git at 1: chosen on tune (docs/kg-bench.md)
WORKSPACE = re.compile(r"^/workspace/[^/]+/")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")
GRADES = (0.1, 0.3, 0.6)  # the shares of a node's lines that are answer lines past which its grade rises


@dataclass
class Label:
    source: str  # bench, git, plan or click
    set: str  # the bench set, or the project
    repo: str
    query: str
    gold: frozenset[str]  # paths of the answering files, as the graph's File nodes hold them
    weight: float = 1.0
    limit: int = DEFAULT_LIMIT
    task: Task | None = None  # the bench task whose graph it is asked of
    project: str | None = None  # else the local project
    lines: tuple[tuple[str, int, int], ...] = ()  # (path, first, last) of the answer, when known


@dataclass
class Group:
    """The rows of one label: the features of the nodes the model orders and the grade of each (``grade``)."""

    source: str
    set: str
    repo: str
    weight: float
    rows: list[list[float]]
    labels: list[int]
    gold: int  # answering files, found in the rows or not

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "set": self.set,
            "repo": self.repo,
            "weight": self.weight,
            "rows": [[None if isinstance(v, float) and math.isnan(v) else v for v in r] for r in self.rows],
            "labels": self.labels,
            "gold": self.gold,
        }

    @classmethod
    def from_json(cls, d: dict) -> Group:
        rows = [[math.nan if v is None else v for v in r] for r in d["rows"]]
        return cls(d["source"], d["set"], d["repo"], d["weight"], rows, d["labels"], d["gold"])


# -- the labels --------------------------------------------------------------------------------------------------------


def _gold_of(task: Task) -> frozenset[str]:
    paths = set(task.files) | {p for p, _, _ in task.lines}
    return frozenset(WORKSPACE.sub("", p) for p in paths if p)


def bench_labels(cache: Path, sets: Iterable[str] = TUNED_SETS) -> list[Label]:
    """The tune tasks of ``sets`` whose graph is built."""
    lay = graphs.layout(cache)
    out = []
    for bench in sets:
        tasks, _ = load_tasks(cache, bench)
        for task in tasks:
            if task.split != TUNE or not lay.built(task):
                continue
            gold = _gold_of(task)
            if gold:
                lines = tuple((WORKSPACE.sub("", p), a, b) for p, a, b in task.lines if p)
                out.append(
                    Label(
                        "bench",
                        bench,
                        task.repo,
                        task.question,
                        gold,
                        WEIGHTS["bench"],
                        SEARCH_LIMITS.get(bench, DEFAULT_LIMIT),
                        task,
                        lines=lines,
                    )
                )
    return out


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, errors="replace")
    return done.stdout if done.returncode == 0 else ""


def commits_after(clone: Path, commit: str) -> list[dict]:
    """The commits of the clone's default branch that descend from ``commit``, oldest first: sha, author, message
    and the files each changed."""
    out = _git(
        clone,
        "log",
        "--no-merges",
        "--ancestry-path",
        "--format=%x1e%H%x1f%an%x1f%ae%x1f%B%x1f",
        "--name-only",
        f"{commit}..HEAD",
    )
    found = []
    for chunk in out.split("\x1e")[1:]:
        parts = chunk.split("\x1f")
        if len(parts) < 5:
            continue
        sha, name, email, message, files = parts[:5]
        found.append(
            {
                "sha": sha,
                "author": f"{name} {email}",
                "message": message.strip(),
                "files": [f for f in files.strip().splitlines() if f.strip()],
            }
        )
    found.reverse()
    return found


def hunks(clone: Path, commit: str, base: str, files: Iterable[str]) -> tuple[tuple[str, int, int], ...] | None:
    """(path, first, last) of the lines of ``files`` that ``commit`` replaced, as they read at ``base`` (an insertion
    marks the two lines around it); None when one of the files changed between ``base`` and the commit's parent."""
    files = sorted(files)
    parent = f"{commit}^"
    same = subprocess.run(
        ["git", "-C", str(clone), "diff", "--quiet", base, parent, "--", *files], capture_output=True
    ).returncode
    if same != 0:
        return None
    out = []
    path = None
    for line in _git(clone, "diff", "-U0", parent, commit, "--", *files).splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        m = HUNK.match(line)
        if m and path:
            first, n = int(m[1]), int(m[2] if m[2] is not None else 1)
            out.append((path, max(first, 1), first + n - 1 if n else first + 1))
    return tuple(out)


def git_labels(
    cache: Path,
    tasks: list[Task],
    *,
    per_graph: int = PER_GRAPH,
    min_words: int = MIN_WORDS,
    max_files: int = MAX_FILES,
    held_of=None,
) -> list[Label]:
    """Labels from the commits after each tune graph's commit (one graph per repo and commit), a commit once per repo."""
    lay = graphs.layout(cache)
    seen: dict[str, set[str]] = defaultdict(set)
    out = []
    graphs_of: dict[tuple[str, str], Task] = {}
    for task in tasks:
        if task.split == TUNE and lay.built(task):
            graphs_of.setdefault((task.repo.lower(), task.commit.lower()), task)
    for (_, _), task in sorted(graphs_of.items()):
        held = held_of(task) if held_of else _held(lay, task)
        if not held:
            continue
        taken = 0
        for commit in commits_after(lay.clone(task.repo), task.commit):
            if taken >= per_graph:
                break
            if commit["sha"] in seen[task.repo.lower()]:
                continue
            found = commit_label(commit, held, min_words, max_files)
            if found is None:
                continue
            lines = hunks(lay.clone(task.repo), commit["sha"], task.commit, found[1])
            if not lines:
                continue
            seen[task.repo.lower()].add(commit["sha"])
            out.append(
                Label(
                    "git", task.bench, task.repo, found[0], found[1], WEIGHTS["git"], DEFAULT_LIMIT, task, lines=lines
                )
            )
            taken += 1
    return out


def _held(lay, task: Task) -> set[str]:
    """The paths of the File nodes of a task's graph."""
    import sqlite3

    path = lay.project_dir(task) / "graph.sqlite"
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT node_id FROM nodes WHERE kind = 'File' AND tx_to IS NULL").fetchall()
    finally:
        db.close()
    out = set()
    for (node_id,) in rows:
        m = locate._FILE_ID.match(node_id)
        if m:
            out.add(m["path"])
    return out


def plan_labels(path: Path, project: str) -> list[Label]:
    """The tune questions of a `kg eval-questions` set of ``project``."""
    out = []
    for q in load_questions(path):
        if split_of(q.id) != EVAL_TUNE:
            continue
        gold = frozenset(p for _, p in q.files if p)
        if gold:
            out.append(Label("plan", project, project, q.text, gold, WEIGHTS["plan"], DEFAULT_LIMIT, None, project))
    return out


def click_labels(path: Path, project: str) -> list[Label]:
    """The searches of ``project``'s hub labels (JSON lines: query, results, clicks) that led to a file, each weighed
    by the mean inverse propensity of its clicks' ranks (``asked.propensities``)."""
    raw = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    labels = [
        asked.Label(
            query=r.get("query") or "",
            results=[x for x in r.get("results") or [] if isinstance(x, dict)],
            clicks=[x for x in r.get("clicks") or [] if isinstance(x, dict)],
        )
        for r in raw
    ]
    prop = asked.propensities(labels)
    out = []
    for label in labels:
        files, weights = set(), []
        for click in label.clicks:
            target = click.get("path")
            if not isinstance(target, str) or not target:
                continue
            files.add(target)
            k = asked._rank(click)
            weights.append(1.0 / prop[k] if k else 1.0)
        if files and label.query.strip():
            weight = WEIGHTS["click"] * sum(weights) / len(weights)
            out.append(
                Label("click", project, project, label.query, frozenset(files), weight, DEFAULT_LIMIT, None, project)
            )
    return out


# -- the rows ------------------------------------------------------------------------------------------------------


def grade(path: str | None, first: int | None, last: int | None, label: Label, answer: dict[str, set[int]]) -> int:
    """The grade of a node of ``path`` over ``first``..``last`` (see the module); ``answer`` is the label's lines by
    path."""
    if not path or path not in label.gold:
        return 0
    if not label.lines or first is None or last is None:
        return 1
    want = answer.get(path)
    if not want:
        return 1
    last = min(last, first + 499)  # the bench reads at most 500 lines of a result
    hit = sum(1 for x in range(first, last + 1) if x in want)
    if not hit:
        return 1
    share = hit / (last - first + 1)
    return 2 + sum(share >= g for g in GRADES)


def group_of(store, b: int, label: Label, params=locate.DEFAULT, length=None) -> Group | None:
    """The rows of ``label`` asked of ``store`` the way kg_search asks it with the model on; None when the search
    finds nothing. ``length`` (path -> lines) gives a whole file's lines."""
    from evo_agents.kg.evaluate import node_file, node_span

    limit = label.limit
    scored = store.search_scored(label.query, b, limit=max(limit * 4, 30), deprecated=False)
    if not scored:
        return None
    pool = locate.gather(store, b, label.query, scored, params)
    ranked = locate.files(pool, params)
    ids, rows = rank.node_rows(store, b, label.query, pool, ranked, params, limit=limit)
    if not ids:
        return None
    answer: dict[str, set[int]] = defaultdict(set)
    for path, first, last in label.lines:
        answer[path].update(range(first, last + 1))
    nodes = store.nodes(ids, b)
    labels = []
    for i in ids:
        node = nodes.get(i)
        if node is None:
            labels.append(0)
            continue
        _, path = node_file(node)
        first, last = node_span(node)
        if path and first is None and length is not None:
            n = length(path)
            first, last = (1, n) if n else (None, None)
        labels.append(grade(path, first, last, label, answer))
    return Group(label.source, label.set, label.repo, label.weight, rows, labels, len(label.gold))


def collect(cache: Path, labels: list[Label], out: Path, *, progress=None) -> Counter:
    """Ask each label and append its group to ``out`` (JSON lines); the labels of a graph are asked together."""
    from evo_agents.kg.bench import run
    from evo_agents.kg.project import resolve_project
    from evo_agents.kg.serve import Session

    lay = graphs.layout(cache)
    stats: Counter = Counter()
    by_graph: dict[tuple, list[Label]] = defaultdict(list)
    for label in labels:
        key = ("bench", lay.name(label.task)) if label.task else ("project", label.project)
        by_graph[key].append(label)
    with out.open("a", encoding="utf-8") as fh:
        for (kind, name), group in by_graph.items():
            try:
                if kind == "bench":
                    task = group[0].task
                    graphs.warm(lay.project_dir(task))
                    with graphs.bench_home(lay.home):
                        store, b = run.session_of(graphs.project_of(lay, task), "graph")._store()
                else:
                    store, b = Session(resolve_project(name), "cli")._store()
            except Exception as exc:  # noqa: BLE001 - one graph that cannot be read leaves its labels out
                stats["graph_error"] += len(group)
                if progress:
                    progress(f"{name}: {exc}")
                continue
            length = None
            if kind == "bench":
                from evo_agents.kg.evaluate import _length_reader

                clone = lay.clone(group[0].task.repo)
                read = _length_reader({clone.name: clone}, group[0].task.commit, {}, None)

                def length(path, read=read):
                    return read(None, path)

            for label in group:
                found = group_of(store, b, label, length=length)
                if found is None:
                    stats[f"{label.source}:not_found"] += 1
                    continue
                stats[f"{label.source}:asked"] += 1
                stats[f"{label.source}:with_answer"] += any(found.labels)
                fh.write(json.dumps({**found.to_json(), "query_len": len(label.query)}) + "\n")
            store.db.close()
            if progress:
                progress(f"{name}: {len(group)} label(s)")
    return stats


def read_groups(path: Path) -> list[Group]:
    return [Group.from_json(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]


# -- the model -------------------------------------------------------------------------------------------------------


PARAMS = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "eval_at": [5],
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "lambdarank_truncation_level": 10,
    "verbosity": -1,
    "seed": 7,
    "deterministic": True,
    "num_threads": 4,
    "label_gain": [0, 1, 3, 7, 15, 31],
}


def _dataset(groups: list[Group]):
    import lightgbm
    import numpy

    usable = [g for g in groups if any(g.labels) and len(g.labels) > 1]
    rows = [r for g in usable for r in g.rows]
    labels = [v for g in usable for v in g.labels]
    weights = [g.weight for g in usable for _ in g.labels]
    data = lightgbm.Dataset(
        numpy.asarray(rows, dtype=float),
        label=numpy.asarray(labels),
        weight=numpy.asarray(weights),
        group=[len(g.labels) for g in usable],
        feature_name=list(rank.FEATURES),
        free_raw_data=False,
    )
    return data


def fit(groups: list[Group], rounds: int, params: dict | None = None):
    """A LambdaMART booster of ``rounds`` trees on the groups that hold an answer."""
    import lightgbm

    return lightgbm.train({**PARAMS, **(params or {})}, _dataset(groups), num_boost_round=rounds)


def folds(groups: list[Group], k: int = 5, seed: int = 7) -> list[set[str]]:
    """``k`` sets of repos, each about the same number of groups: a repo's labels stay on one side of a fold."""
    sizes = Counter(g.repo.lower() for g in groups)
    repos = sorted(sizes)
    random.Random(seed).shuffle(repos)
    out: list[set[str]] = [set() for _ in range(k)]
    load = [0] * k
    for repo in sorted(repos, key=lambda r: -sizes[r]):
        i = load.index(min(load))
        out[i].add(repo)
        load[i] += sizes[repo]
    return out


def slot_metrics(group: Group, order: list[int]) -> dict:
    """Of the first ``rank.SLOTS`` rows of a group in ``order``: their mean grade, and whether one of them lies on
    answer lines (grade 2 or more) or in an answering file."""
    head = [group.labels[i] for i in order[: rank.SLOTS]]
    return {
        "grade": sum(head) / len(head) if head else 0.0,
        "lines": float(any(v >= 2 for v in head)),
        "file": float(any(v >= 1 for v in head)),
    }


def placed_order(group: Group) -> list[int]:
    """The rows of a group in locate's own order (``rank.NODE_FEATURES``' placed)."""
    col = rank.FEATURES.index("placed")
    return sorted(range(len(group.rows)), key=lambda i: (group.rows[i][col], i))


def evaluate_cv(
    groups: list[Group],
    rounds: Iterable[int],
    params: dict | None = None,
    k: int = 5,
    sources: Iterable[str] = ("bench",),
) -> dict:
    """Per bench set and number of trees, the mean ``slot_metrics`` of the held-out repos' groups of ``sources``, with
    the model and with locate's order (``base``), over ``k`` folds by repo."""
    import numpy

    rounds = sorted(set(rounds))
    sources = set(sources)
    out: dict = defaultdict(lambda: defaultdict(list))
    for held in folds(groups, k):
        train = [g for g in groups if g.repo.lower() not in held]
        test = [g for g in groups if g.repo.lower() in held and g.source in sources]
        if not test:
            continue
        booster = fit(train, max(rounds), params)
        for g in test:
            out[g.set]["base"].append(slot_metrics(g, placed_order(g)))
            for n in rounds:
                got = booster.predict(numpy.asarray(g.rows, dtype=float), num_iteration=n)
                order = sorted(range(len(got)), key=lambda i: (-got[i], i))
                out[g.set][n].append(slot_metrics(g, order))
    summary = {}
    for name, by in out.items():
        summary[name] = {
            str(key): {m: round(sum(r[m] for r in rows) / len(rows), 4) for m in rows[0]} | {"n": len(rows)}
            for key, rows in by.items()
        }
    return summary


def best_rounds(cv: dict, rounds: Iterable[int], metric: str = "grade") -> int:
    """The number of trees whose mean ``metric`` over the sets of ``cv`` is highest, the fewest on a tie."""
    rounds = sorted(set(rounds))
    names = [n for n in cv if all(str(r) in cv[n] for r in rounds)]
    if not names:
        return rounds[0]
    return max(rounds, key=lambda r: (sum(cv[n][str(r)][metric] for n in names) / len(names), -r))


def importance(booster) -> list[tuple[str, float, int]]:
    """(feature, share of the total gain, splits) of each feature, the largest gain first."""
    gain = booster.feature_importance(importance_type="gain")
    split = booster.feature_importance(importance_type="split")
    total = float(sum(gain)) or 1.0
    found = [(name, round(float(g) / total, 4), int(s)) for name, g, s in zip(rank.FEATURES, gain, split, strict=True)]
    return sorted(found, key=lambda t: -t[1])


def save(booster, path: Path) -> int:
    """Write the model as LightGBM's text, which names the features and holds numbers only; returns its bytes."""
    text = booster.model_to_string()
    path.write_text(text, encoding="utf-8")
    return len(text.encode())


def tune_only(labels: list[Label]) -> list[Label]:
    """``labels`` without any of a bench repo outside the tune part: the guard of the module's first paragraph."""
    return [lb for lb in labels if lb.source not in ("bench", "git") or repo_split(lb.repo) == TUNE]
