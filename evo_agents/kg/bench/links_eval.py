"""``evo-agents kg links eval``: how well the edge model M3 (``evo_agents.kg.links``) finds edges it never saw.

For each graph, one in ten of its parsed edges is hidden, another share chooses the threshold at which the model
reaches the precision asked for, and the hidden edges, each paired with a false edge of a node of the same kind (of
the same name when one exists), give the precision and the recall at that threshold (``links.evaluate``). The graphs
are a project's own (``--set NAME=PROJECT``, its latest build) and a sample of the tune graphs of bench sets the cache
holds, drawn by the seed among those built.

The summary has the bench summary's format (``evo-kg-bench/1``), so ``kg bench show`` prints and checks it: each set
has one part, ``holdout`` (the hidden edges), whose ``precision`` and ``recall`` pool the true and the proposed edges
of its graphs, each graph at its own threshold; ``split`` names that part for ``show`` run without ``--split``. Each
graph's numbers, its threshold and per relation, are under ``graphs``. No task, question or answer of a bench is read:
only which repos and commits its tune part has, to find their graphs.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg import links
from evo_agents.kg.bench import graphs
from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.bench.run import FORMAT, evo_ak_sha
from evo_agents.kg.bench.tasks import HOLDOUT, TUNE, load_tasks
from evo_agents.kg.ids import utc_now
from evo_agents.kg.store import Store

KIND = "links"
METHOD = "links"
PRIMARY = "precision"


def tune_graphs(cache: Path, bench: str, count: int, seed: int) -> list[tuple[str, Path]]:
    """``count`` built tune graphs of ``bench``, one per repo, drawn with ``seed``: (name, graph.sqlite)."""
    lay = graphs.layout(cache)
    tasks, _ = load_tasks(cache, bench)
    by_repo: dict[str, str] = {}
    for task in sorted(tasks, key=lambda t: (t.repo, t.commit)):
        if task.split == TUNE and lay.built(task) and task.repo not in by_repo:
            by_repo[task.repo] = lay.name(task)
    repos = sorted(by_repo)
    random.Random(f"{seed}:{bench}").shuffle(repos)
    return [(by_repo[r], lay.home / by_repo[r] / "graph.sqlite") for r in repos[:count]]


def graph_numbers(path: Path, options: dict, say=None) -> dict:
    """The numbers of one graph (``links.evaluate``) at its latest build."""
    store = Store.open_file(path, immutable=True)
    try:
        b = store.latest_ready()
        if b is None:
            raise BenchError(f"{path} has no build")
        g = links.read(store, b)
    finally:
        store.close()
    started = time.monotonic()
    _, numbers = links.evaluate(g, **options)
    numbers["seconds"] = round(time.monotonic() - started, 1)
    if say:
        say(
            f"{path.parent.name}: {numbers['n']} hidden, precision {numbers['precision']}, recall {numbers['recall']}"
            f", auc {numbers['auc']}, threshold {numbers['threshold']}, {numbers['seconds']}s"
        )
    return numbers


def pooled(rows: list[dict]) -> dict:
    """One part from the numbers of several graphs: true and proposed edges summed."""
    n = sum(r["n"] for r in rows)
    true = sum(r["true"] for r in rows)
    proposed = sum(r["proposed"] for r in rows)
    precision = round(true / proposed, 4) if proposed else None
    aucs = [r["auc"] for r in rows if r.get("auc") is not None]
    return {
        "n": n,
        "primary": precision,
        "precision": precision,
        "recall": round(true / n, 4) if n else 0.0,
        "proposed": proposed,
        "true": true,
        "auc": round(sum(aucs) / len(aucs), 4) if aucs else None,
        "train_seconds": round(sum(r["fit"]["seconds"] for r in rows), 1),
    }


def summary(sets: dict[str, list[tuple[str, dict]]], options: dict, seed: int, graphs_per_set: int) -> dict:
    """The summary of an evaluation (see the module)."""
    benches = {}
    for name, rows in sets.items():
        benches[name] = {
            "primary": PRIMARY,
            "method": f"{METHOD}-{options.get('model', links.MODELS[0])}",
            "parts": {HOLDOUT: pooled([r for _, r in rows])},
            "graphs": {graph: numbers for graph, numbers in rows},
        }
    return {
        "format": FORMAT,
        "kind": KIND,
        "split": HOLDOUT,
        "created_at": utc_now(),
        "evo_ak": {"version": __version__, "sha": evo_ak_sha()},
        "extractor": graphs.extractor(),
        "settings": {
            "search": KIND,
            "seed": seed,
            "graphs_per_set": graphs_per_set,
            "hide": links.HIDE,
            "choose": links.CHOOSE,
            "false_edges": "one per hidden edge: same head and relation, a tail of its kind, of its name when one exists",
            **{k: v for k, v in options.items() if k != "deadline"},
        },
        "cost": {"usd": 0.0, "model_calls": 0},
        "benches": benches,
        "tasks": [],
    }
