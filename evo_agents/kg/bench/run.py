"""``kg bench run``: each task through kg_search (and the other methods asked for) on its own graph, no model called,
scored as `kg eval` scores a question, into one summary JSON.

The summary (FORMAT) holds what makes a number comparable: the revision of each dataset, evo-ak's version and the
commit of its checkout, the extractor of the graphs, the settings, the seed and the sample, then per set and part
the `kg eval` row of the primary method, the primary metric (PRIMARY) per language, and per task the metrics
``compare`` pairs. A set's primary metric: ContextBench and SWE-Explore F1 by line (SWE-Explore with 5 results, as
its explorers return 5 regions), LocBench and MuLocBench Acc@5 by file, a local set such as evo-agents hit@5.

A set too large to build within ``max_hours`` (at the mean build time measured so far) is sampled (``tasks.sample``),
and the summary names the sample. The ``test`` part only runs with ``sealed``.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg.bench import datasets, graphs
from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.bench.tasks import HOLDOUT, TEST, TUNE, Task, load_tasks, sample
from evo_agents.kg.evaluate import (
    HIT_K,
    LINE_BUDGET,
    Outcome,
    QuestionSet,
    evaluate,
    p95,
    project_checkouts,
    summarize,
)
from evo_agents.kg.ids import utc_now

FORMAT = "evo-kg-bench/1"
PRIMARY = {
    datasets.CONTEXTBENCH: "line_f1",
    datasets.SWE_EXPLORE: "line_f1",
    datasets.LOCBENCH: "acc_file@5",
    datasets.MULOCBENCH: "acc_file@5",
}
LOCAL_PRIMARY = f"hit@{HIT_K}"  # a local question set (evo-agents)
LIMITS = {datasets.SWE_EXPLORE: 5}
DEFAULT_LIMIT = 10
DEFAULT_BUDGET_TOKENS = 3000
MAX_HOURS = 6.0
SEARCHES = ("graph", "bm25", "plain", "hybrid")  # plain: plain BM25 (Store.plain_search), the sets' baseline


@dataclass
class Options:
    parts: tuple[str, ...] = (TUNE, HOLDOUT)
    sealed: bool = False
    methods: tuple[str, ...] = ("kg_search",)
    limit: int | None = None  # None: LIMITS, else DEFAULT_LIMIT
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    line_budget: int = LINE_BUDGET
    searches: tuple[str, ...] = ("graph",)  # each asked on the same graphs in one pass, into a summary of its own
    sample: int | None = None
    seed: int = 0
    max_hours: float | None = MAX_HOURS
    git_url: str = graphs.GITHUB_URL
    prune: bool = False  # delete each graph after its last task, each clone after its repo's (graphs.prune_*)
    jobs: int = 1  # repos asked at once, each in a process of its own
    progress: object = None  # a callable(str) for progress lines

    @property
    def search(self) -> str:
        return self.searches[0]

    def limit_of(self, bench: str) -> int:
        return self.limit or LIMITS.get(bench, DEFAULT_LIMIT)


@dataclass
class Selection:
    bench: str
    tasks: list[Task]
    total: int
    deduped: int
    sample: dict | None = None


def check_parts(parts, sealed: bool) -> tuple[str, ...]:
    parts = tuple(parts)
    if TEST in parts and not sealed:
        raise BenchError("the test part is sealed: it runs only with --sealed")
    return parts


def select(cache: Path, bench: str, lay: graphs.Layout, options: Options) -> Selection:
    """The tasks of ``bench`` in the parts asked for, sampled when asked or when building them would pass max_hours."""
    tasks, deduped = load_tasks(cache, bench)
    mine = [t for t in tasks if t.split in options.parts]
    chosen, why, size = mine, None, options.sample
    if size is not None:
        why = f"--sample {size}"
    elif options.max_hours is not None:
        hours = graphs.estimate(lay, bench, mine)
        if hours is not None and hours > options.max_hours:
            size = max(1, math.floor(len(mine) * options.max_hours / hours))
            why = f"building every graph would take about {hours:.1f} h, over {options.max_hours:g} h"
    picked = None
    if size is not None and size < len(mine):
        chosen = sample(mine, size, options.seed)
        picked = {"size": len(chosen), "of": len(mine), "seed": options.seed, "why": why, "ids": [t.id for t in chosen]}
    return Selection(bench, chosen, len(mine), deduped, picked)


def write_questions(path: Path, selection: Selection) -> None:
    """The selected tasks as a `kg eval` question set (with each task's part and repo, which `kg eval` keeps unread)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"set": f"bench-{selection.bench}", "version": 1}) + "\n")
        for task in selection.tasks:
            line = {**task.eval_line(), "part": task.split, "bench_repo": task.repo, "commit": task.commit}
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def evo_ak_sha() -> str | None:
    """The commit of the evo-agents checkout this code runs from, None when it is installed from a package."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


# -- scoring -------------------------------------------------------------------------------------------------------


def task_metrics(outcome: Outcome) -> dict:
    """The numbers of one task that ``compare`` pairs: line scores, Acc@k, hit@5, MRR, tokens and time."""
    q = outcome.question
    files = len(q.files)
    file_ranks, node_ranks = outcome.ranks[:files], outcome.ranks[files:]
    metrics: dict = {
        f"hit@{HIT_K}": float(outcome.hit(HIT_K)),
        f"recall@{HIT_K}": round(outcome.recall(HIT_K), 4),
        "mrr": round(outcome.reciprocal_rank(), 4),
    }
    for k in (1, 5, 10):
        if file_ranks:
            metrics[f"acc_file@{k}"] = float(all(r is not None and r <= k for r in file_ranks))
        if node_ranks:
            metrics[f"acc_func@{k}"] = float(all(r is not None and r <= k for r in node_ranks))
    if outcome.line_stats is not None:
        for key in ("precision", "recall", "f1"):
            metrics[f"line_{key}"] = outcome.line_stats[key]
        metrics["context_efficiency"] = outcome.line_stats["context_efficiency"]
        metrics["ndcg_lines"] = outcome.line_stats["ndcg"]
    return metrics


def _record(bench: str, part: str, repo: str, language: str, outcome: Outcome) -> dict:
    return {
        "bench": bench,
        "id": outcome.question.id,
        "part": part,
        "repo": repo,
        "language": language,
        "method": outcome.method,
        "metrics": task_metrics(outcome),
        "tokens": outcome.tokens,
        "ms": round(outcome.ms, 1),
        "error": outcome.error,
        "skipped": outcome.skipped,
    }


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def part_numbers(records: list[dict], outcomes: list[Outcome], method: str, primary: str) -> dict:
    """A part of a set: n, the primary metric, the `kg eval` row of ``method``, the primary per language."""
    mine = [r for r in records if r["method"] == method and r["skipped"] is None]
    picked = [o for o in outcomes if o.method == method]
    languages: dict[str, dict] = {}
    for r in mine:
        languages.setdefault(r["language"], []).append(r["metrics"].get(primary))
    return {
        "n": len(mine),
        "primary": _mean(r["metrics"].get(primary) for r in mine),
        "mean_tokens": _mean(r["tokens"] for r in mine),
        "p95_ms": round(p95([r["ms"] for r in mine]), 1),
        "precision": _mean(r["metrics"].get("line_precision") for r in mine),
        "row": summarize(picked, "all", method, extended=True) if picked else None,
        "languages": {lang: {"n": len(v), "primary": _mean(v)} for lang, v in sorted(languages.items())},
    }


def _ensure_vectors(project_dir: Path, cache: Path) -> None:
    from evo_agents.kg import semantic

    path = project_dir / "graph.sqlite"
    found = semantic.index_file(path, semantic.Settings("on"), cache, forget=False)
    if found.error:
        raise BenchError(f"hybrid search needs the vectors of {path}: {found.line()}")


@dataclass
class Asked:
    """What asking one repo's tasks gave: per search, the task records and the outcomes by part; build counts."""

    records: dict[str, list[dict]]
    outcomes: dict[str, dict[str, list[Outcome]]]
    built: int = 0
    reused: int = 0
    failed: int = 0
    seconds: float = 0.0


def session_of(project, search: str):
    """A CLI session on ``project`` that ranks the way ``search`` says, as `kg eval --search` does, or by plain BM25."""
    from evo_agents.kg.serve import Session

    session = Session(project, "cli")
    if search != "graph":
        session.semantic = search == "hybrid"
    session.plain = search == "plain"
    return session


def ask_repo(cache: Path, bench: str, tasks: list[Task], options: Options) -> Asked:
    """Build each graph ``tasks`` need (one repo's) and ask each task with every search of ``options``; with ``prune``,
    a graph goes once its last task was asked, and the clone once the repo's last was."""
    lay = graphs.layout(cache)
    say = options.progress or (lambda line: None)
    asked = Asked({s: [] for s in options.searches}, {s: {} for s in options.searches})
    last = {(t.repo.lower(), t.commit.lower()): i for i, t in enumerate(tasks)}
    for i, task in enumerate(tasks):
        result = graphs.build_graph(lay, task, git_url=options.git_url)
        if result.error:
            asked.failed += 1
            say(f"{bench} {task.id}: {result.error}")
            for search in options.searches:
                for method in options.methods:
                    asked.records[search].append(
                        {
                            **_record(bench, task.split, task.repo, task.language, Outcome(task.as_question(), method)),
                            "skipped": f"no graph: {result.error[:200]}",
                        }
                    )
        else:
            asked.reused += result.reused
            asked.built += not result.reused
            asked.seconds += result.seconds
            clone = lay.clone(task.repo)
            for search in options.searches:
                with graphs.bench_home(lay.home):
                    project = graphs.project_of(lay, task)
                    if search == "hybrid":
                        _ensure_vectors(lay.project_dir(task), lay.vectors(task.repo))
                    report = evaluate(
                        [task.as_question()],
                        session_for=lambda _project, project=project, search=search: session_of(project, search),
                        checkouts_for=lambda _project, clone=clone: {clone.name: clone},
                        methods=options.methods,
                        limit=options.limit_of(bench),
                        budget_tokens=options.budget_tokens,
                        grep_ref=task.commit,
                        line_budget=options.line_budget,
                        acc=True,
                    )
                for outcome in report.outcomes:
                    asked.records[search].append(_record(bench, task.split, task.repo, task.language, outcome))
                    asked.outcomes[search].setdefault(task.split, []).append(outcome)
        if options.prune and last[(task.repo.lower(), task.commit.lower())] == i:
            graphs.prune_graph(lay, task)
    if options.prune and tasks:
        graphs.prune_repo(lay, tasks[0].repo)
    if tasks:
        state = f"{asked.built} built, {asked.reused} reused, {asked.failed} failed"
        say(f"{bench} {tasks[0].repo}: {len(tasks)} asked ({state})")
    return asked


def by_repo(tasks: list[Task]) -> list[list[Task]]:
    """The tasks in groups of one repo each, in the order the repos first come, each in its tasks' order."""
    groups: dict[str, list[Task]] = {}
    for task in tasks:
        groups.setdefault(task.repo.lower(), []).append(task)
    return list(groups.values())


def _asked_all(cache: Path, bench: str, groups: list[list[Task]], options: Options) -> list[Asked]:
    if options.jobs <= 1 or len(groups) <= 1:
        return [ask_repo(cache, bench, group, options) for group in groups]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    # spawn: a child starts clean, with no lock or thread (the embedding model's) a fork would copy mid-use
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=options.jobs, mp_context=context) as pool:
        largest = sorted(range(len(groups)), key=lambda g: -len(groups[g]))  # the long repos start first
        futures = {g: pool.submit(ask_repo, cache, bench, groups[g], options) for g in largest}
        return [futures[g].result() for g in range(len(groups))]


def run_bench(cache: Path, bench: str, options: Options) -> tuple[dict[str, dict], dict[str, list[dict]], Selection]:
    """Build what is missing and ask every selected task of ``bench`` once a search of ``options``, a repo at a time
    (``jobs`` repos at once); per search, the set's part of the summary and its task records."""
    lay = graphs.layout(cache)
    selection = select(cache, bench, lay, options)
    write_questions(datasets.data_dir(cache, bench) / "questions.jsonl", selection)
    results = _asked_all(cache, bench, by_repo(selection.tasks), options)
    primary = PRIMARY[bench]
    method = options.methods[0]
    build = {
        "built": sum(a.built for a in results),
        "reused": sum(a.reused for a in results),
        "failed": sum(a.failed for a in results),
        "seconds": round(sum(a.seconds for a in results), 1),
    }
    parts: dict[str, dict] = {}
    records: dict[str, list[dict]] = {}
    for search in options.searches:
        mine = [r for a in results for r in a.records[search]]
        outcomes: dict[str, list[Outcome]] = {}
        for a in results:
            for p, found in a.outcomes[search].items():
                outcomes.setdefault(p, []).extend(found)
        records[search] = mine
        parts[search] = {
            "revision": datasets.revision(bench),
            "fetched": [
                {k: f[k] for k in ("dataset", "file", "sha256")}
                for f in (datasets.fetched_info(cache, bench) or {}).get("files", [])
            ],
            "primary": primary,
            "method": method,
            "limit": options.limit_of(bench),
            "tasks": selection.total,
            "deduped": selection.deduped,
            "asked": len(selection.tasks),
            "sample": selection.sample,
            "build": build,
            "parts": {
                p: part_numbers([r for r in mine if r["part"] == p], outcomes.get(p, []), method, primary)
                for p in options.parts
            },
        }
    return parts, records, selection


def score_local(name: str, question_set: QuestionSet, session_for, checkouts_for, options: Options, digest: str):
    """A local `kg eval` set (such as evo-agents' 830 questions) on a graph of this machine, in tune and holdout."""
    parts = [p for p in options.parts if p != TEST]
    questions = [q for q in question_set.questions if q.split in parts]
    if not questions:
        raise BenchError(f"set {name} has no question in {', '.join(parts)}")
    report = evaluate(
        questions,
        session_for=session_for,
        checkouts_for=checkouts_for,
        methods=options.methods,
        limit=options.limit or DEFAULT_LIMIT,
        budget_tokens=options.budget_tokens,
        line_budget=options.line_budget,
        question_set=question_set,
    )
    records = [_record(name, o.question.split, o.question.repo or "", "local", o) for o in report.outcomes]
    method = options.methods[0]
    by_part: dict[str, list[Outcome]] = {}
    for o in report.outcomes:
        by_part.setdefault(o.question.split, []).append(o)
    part = {
        "revision": {"set": question_set.name, "version": question_set.version, "sha256": digest},
        "primary": LOCAL_PRIMARY,
        "method": method,
        "limit": options.limit or DEFAULT_LIMIT,
        "tasks": len(questions),
        "deduped": 0,
        "asked": len(questions),
        "sample": None,
        "parts": {
            p: part_numbers([r for r in records if r["part"] == p], by_part.get(p, []), method, LOCAL_PRIMARY)
            for p in parts
        },
    }
    return part, records


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_checkouts(project: str | None):
    return lambda question_project: project_checkouts(question_project or project)


def summary(benches: dict[str, dict], records: list[dict], options: Options, search: str | None = None) -> dict:
    """The summary of a run (see the module): what it ran, with what, and its numbers per set, part and task."""
    return {
        "format": FORMAT,
        "created_at": utc_now(),
        "evo_ak": {"version": __version__, "sha": evo_ak_sha()},
        "extractor": graphs.extractor(),
        "settings": {
            "methods": list(options.methods),
            "search": search or options.search,
            "budget_tokens": options.budget_tokens,
            "line_budget": options.line_budget,
            "parts": list(options.parts),
            "sealed": options.sealed,
            "seed": options.seed,
            "sample": options.sample,
            "max_hours": options.max_hours,
        },
        "cost": {
            "usd": 0.0,
            "model_calls": 0,
        },  # run calls no model; a hybrid summary's cost is set by `merge --ledger`
        "benches": benches,
        "tasks": records,
    }


def progress_to_stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def build_bench(cache: Path, bench: str, options: Options) -> tuple[Selection, list[graphs.Built]]:
    """Build the graphs the selected tasks of ``bench`` need, scoring nothing: ``run`` then reuses them."""
    lay = graphs.layout(cache)
    selection = select(cache, bench, lay, options)
    say = options.progress or (lambda line: None)
    done = []
    wanted = graphs.snapshots(selection.tasks)
    for number, task in enumerate(wanted, 1):
        result = graphs.build_graph(lay, task, git_url=options.git_url)
        done.append(result)
        state = "reused" if result.reused else f"failed: {result.error}" if result.error else f"{result.seconds}s"
        say(f"{bench} {number}/{len(wanted)} {task.repo}@{task.commit[:12]} {state}")
    return selection, done
