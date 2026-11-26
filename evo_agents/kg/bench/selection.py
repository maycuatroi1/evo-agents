"""What ``evo-agents kg bench`` reads from its arguments: the sets asked, the options of a build or a run, the search
settings of ``--settings FILE`` and the corpus graph of ``--strategy``; and the asking of the sets a run names, the
summary of each search and the data profile of the cached graphs (docs/kg-bench.md).

Every refusal is a ``BenchError`` whose message the command prints as it is.
"""

from __future__ import annotations

import json
from pathlib import Path


def names(text: str | None) -> list[str]:
    """The names of a comma-separated list, blanks left out."""
    return [n.strip() for n in (text or "").split(",") if n.strip()]


def benches(given: list[str] | None) -> list[str]:
    """The sets ``given`` names (``noncode`` standing for the non-code sets), the code benches when none is."""
    from evo_agents.kg.bench.datasets import CODE_BENCHES, NONCODE_BENCHES, NONCODE_GROUP, BenchError
    from evo_agents.kg.bench.memory import MEMORY_BENCHES

    found = [b for n in given or CODE_BENCHES for b in (NONCODE_BENCHES if n == NONCODE_GROUP else (n,))]
    known = (*CODE_BENCHES, *MEMORY_BENCHES, *(b for b in NONCODE_BENCHES if b not in MEMORY_BENCHES))
    unknown = [n for n in found if n not in known]
    if unknown:
        raise BenchError(f"no bench {', '.join(unknown)}: one of {', '.join(known)}")
    return found


def memory_options(options):
    """The options of a memory set (evo_agents.kg.bench.memory), from those of a run or build."""
    from evo_agents.kg.bench import memory
    from evo_agents.kg.bench.datasets import BenchError

    if options.limit is not None and not 1 <= options.limit <= memory.LIMIT:
        raise BenchError(f"a non-code set asks kg_search with --limit 1 to {memory.LIMIT}, not {options.limit}")
    return memory.Options(
        parts=options.parts,
        limit=options.limit or memory.LIMIT,
        sample=options.sample,
        seed=options.seed,
        max_hours=options.max_hours,
        room=options.room,
        jobs=options.jobs,
        progress=options.progress,
    )


def tuning_file(path: str | None) -> dict | None:
    """The search settings of ``--settings FILE`` (a json object of kg.settings keys, or {"settings": {...}}); a key
    that needs a build of the graphs (or the semantic mode, which --search sets) is refused."""
    if not path:
        return None
    from evo_agents.kg import settings as kg_settings
    from evo_agents.kg.bench.datasets import BenchError

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data = data.get("settings", data) if isinstance(data, dict) else data
        wanted = kg_settings.of(data)
    except (OSError, ValueError) as exc:
        raise BenchError(f"--settings {path}: {exc}") from None
    fixed = sorted(set(data) & (kg_settings.BUILD_KEYS | kg_settings.SEMANTIC_KEYS))
    if fixed:
        raise BenchError(
            f"--settings {path}: {', '.join(fixed)} change what a build writes or --search sets; "
            "kg bench run asks the graphs it has with the query keys: "
            + ", ".join(sorted(set(kg_settings.KEYS) - kg_settings.BUILD_KEYS - kg_settings.SEMANTIC_KEYS))
        )
    return wanted.overrides() or None


def graph_of(args):
    """The corpus graph of ``--strategy`` and ``--entities``; None without --strategy."""
    from evo_agents.kg.bench import corpus
    from evo_agents.kg.bench.datasets import BenchError

    name, path = getattr(args, "strategy", None), getattr(args, "entities", None)
    if not name:
        if path:
            raise BenchError("--entities goes with --strategy")
        return None
    data = {}
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BenchError(f"--entities {path}: {exc}") from None
        if not isinstance(data, dict):
            raise BenchError(f"--entities {path}: expected an object")
    graph = corpus.GraphConfig(name, corpus.check_entities(data))
    graph.digest()  # an unknown strategy fails here
    return graph


def options_of(args):
    """The options of a build or a run (evo_agents.kg.bench.run), from its arguments."""
    from evo_agents.kg.bench import graphs, run
    from evo_agents.kg.bench.tasks import HOLDOUT, TEST, TUNE

    parts = tuple(args.part) if args.part else (TUNE, HOLDOUT, *((TEST,) if args.sealed else ()))
    return run.Options(
        parts=run.check_parts(parts, args.sealed),
        sealed=args.sealed,
        methods=tuple(getattr(args, "method", None) or ("kg_search",)),
        limit=getattr(args, "limit", None),
        budget_tokens=getattr(args, "budget_tokens", run.DEFAULT_BUDGET_TOKENS),
        line_budget=getattr(args, "line_budget", run.LINE_BUDGET),
        searches=tuple(getattr(args, "search", None) or ("graph",)),
        sample=args.sample,
        seed=args.seed,
        max_hours=None if args.max_hours <= 0 else args.max_hours,
        git_url=args.git_url,
        prune=getattr(args, "prune", False),
        room=graphs.Room(
            None if args.cache_gb <= 0 else args.cache_gb,
            None if args.reserve_gb is None else max(0.0, args.reserve_gb),
        ),
        jobs=getattr(args, "jobs", 1),
        exclude=tuple(getattr(args, "exclude_repo", None) or ()),
        like=Path(args.like) if getattr(args, "like", None) else None,
        tuning=tuning_file(getattr(args, "settings", None)),
        progress=None if getattr(args, "json", False) and not args.verbose else run.progress_to_stderr,
    )


def local_sets(entries: list[str] | None) -> list[tuple[str, Path]]:
    """The local question sets of ``--set NAME=QUESTIONS.jsonl``."""
    from evo_agents.kg.bench.datasets import CODE_BENCHES, BenchError

    out = []
    for entry in entries or []:
        name, sep, path = entry.partition("=")
        if not sep or not name or not path:
            raise BenchError(f"--set takes NAME=QUESTIONS.jsonl, not {entry!r}")
        if name in CODE_BENCHES:
            raise BenchError(f"--set {name}: that name is a code bench's")
        out.append((name, Path(path)))
    return out


def extraction_cost(summary: dict, graph) -> None:
    """A summary asked on strategy graphs: the graph it names, and the cost of their entities step in ``cost``."""
    if graph is None:
        return
    summary["settings"]["graph"] = {"strategy": graph.found().name, "digest": graph.digest()}
    found = [p["extraction"] for p in summary["benches"].values() if p.get("extraction")]
    summary["cost"] = {
        "usd": round(sum(f["usd"] for f in found), 4),
        "model_calls": sum(f["answers"] for f in found),
        "what": "the answers of the entities step in the graphs' memo, every build of them (a run calls no model)",
    }


class Asked:
    """The parts and records a run gathers, by search, set after set."""

    def __init__(self, searches: tuple[str, ...]):
        self.parts: dict[str, dict] = {s: {} for s in searches}
        self.records: dict[str, list[dict]] = {s: [] for s in searches}

    def add(self, search: str, name: str, part: dict, records: list[dict]) -> None:
        self.parts[search][name] = part
        self.records[search] += records


def ask_bench(asked: Asked, cache: Path, bench: str, options, graph) -> None:
    """Ask the selected tasks of bench set ``bench`` on their graphs, each search of ``options``."""
    from evo_agents.kg.bench import corpus, datasets, memory, run

    if graph is not None and not corpus.is_corpus(bench):
        raise datasets.BenchError(f"--strategy asks the graphs of corpus sets, not of {bench}")
    if corpus.is_corpus(bench):
        for search in options.searches:
            part, mine = corpus.run_corpus(cache, bench, memory_options(options), search=search, graph=graph)
            asked.add(search, bench, part, mine)
        return
    if memory.is_memory(bench):
        if options.searches != ("graph",):
            raise datasets.BenchError(f"{bench} is asked with --search graph only")
        part, mine = memory.run_memory(cache, bench, memory_options(options))
        asked.add("graph", bench, part, mine)
        return
    part, mine, _ = run.run_bench(cache, bench, options)
    for search in options.searches:
        asked.add(search, bench, part[search], mine[search])


def ask_local(asked: Asked, local: list[tuple[str, Path]], project_name: str | None, options) -> None:
    """Ask the local sets on a build in memory of the corpus of ``project_name`` on this machine."""
    from evo_agents.kg.bench import rows_file, run
    from evo_agents.kg.evaluate import load_set, project_checkouts
    from evo_agents.kg.project import resolve_project

    snap = None
    for name, path in local:
        project = resolve_project(project_name)
        snap = snap or rows_file.local_snapshot(project)  # a build in memory of the corpus on this machine
        for search in options.searches:
            part, mine = run.score_local(
                name,
                load_set(path),
                lambda _project, project=project, search=search, snap=snap: run.session_of(project, search, snap=snap),
                lambda _project: project_checkouts(project_name),
                options,
                run.file_digest(path),
            )
            asked.add(search, name, part, mine)


def profile_repos(lay) -> dict[str, dict]:
    """The data profile of every graph of this extractor in the bench cache of layout ``lay``, summed by repo."""
    from evo_agents.kg import profile as kg_profile
    from evo_agents.kg.bench import graphs, rows_file

    repos: dict[str, dict] = {}
    for graph in graphs.cached(lay):
        if not graph.current:
            continue
        summary = rows_file.summary_of(graph.project)
        found = kg_profile.of_coverage(summary.get("coverage")) if summary is not None else None
        if found is None:
            continue
        repo = graph.name.rsplit("-", 1)[0]
        row = repos.setdefault(repo, {"graphs": 0, "items": 0, "chars": 0, "noise": 0.0, "classes": {}})
        row["graphs"] += 1
        row["items"] += found["items"]
        row["chars"] += found["chars"]
        row["noise"] += found["noise"]["ratio"]
        for name, c in found["classes"].items():
            row["classes"][name] = row["classes"].get(name, 0) + c["item_share"]
    for row in repos.values():
        n = row["graphs"]
        row["noise"] = round(row["noise"] / n, 4)
        row["classes"] = {k: round(v / n, 4) for k, v in sorted(row["classes"].items()) if v}
    return repos
