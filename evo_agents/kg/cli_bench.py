"""``evo-agents kg bench``: fetch, build, run, merge, compare and show the code and memory benches (docs/kg-bench.md)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fail(message: str, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _names(text: str | None) -> list[str]:
    return [n.strip() for n in (text or "").split(",") if n.strip()]


def _benches(given: list[str] | None) -> list[str]:
    from evo_agents.kg.bench.datasets import CODE_BENCHES, NONCODE_BENCHES, NONCODE_GROUP, BenchError
    from evo_agents.kg.bench.memory import MEMORY_BENCHES

    names = [b for n in given or CODE_BENCHES for b in (NONCODE_BENCHES if n == NONCODE_GROUP else (n,))]
    known = (*CODE_BENCHES, *MEMORY_BENCHES, *(b for b in NONCODE_BENCHES if b not in MEMORY_BENCHES))
    unknown = [n for n in names if n not in known]
    if unknown:
        raise BenchError(f"no bench {', '.join(unknown)}: one of {', '.join(known)}")
    return names


def _memory_options(options):
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


def _tuning_file(path: str | None) -> dict | None:
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


def _graph(args):
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


def _options(args):
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
        tuning=_tuning_file(getattr(args, "settings", None)),
        progress=None if getattr(args, "json", False) and not args.verbose else run.progress_to_stderr,
    )


def cmd_fetch(args) -> int:
    from evo_agents.kg.bench import corpus, datasets, memory

    try:
        cache = datasets.cache_dir(args.cache)
        for bench in _benches(args.sets):
            fetch = (
                memory.fetch if memory.is_memory(bench) else corpus.fetch if corpus.is_corpus(bench) else datasets.fetch
            )
            got = fetch(bench, cache, mirror=Path(args.mirror) if args.mirror else None, force=args.force)
            size = sum(f["bytes"] for f in got["files"])
            left = ", ".join(f"{n} {why}" for why, n in sorted(got["skipped"].items())) or "none"
            print(f"{bench}: {got['tasks']} tasks, {size / 1e6:.1f} MB in {cache}; left out: {left}")
    except datasets.BenchError as exc:
        return _fail(str(exc), 1)
    return 0


def cmd_build(args) -> int:
    from evo_agents.kg.bench import corpus, datasets, memory, run

    try:
        cache = datasets.cache_dir(args.cache)
        options = _options(args)
        graph = _graph(args)
        failed = 0
        for bench in _benches(args.sets):
            if graph is not None and not corpus.is_corpus(bench):
                raise datasets.BenchError(f"--strategy builds the graphs of corpus sets, not of {bench}")
            if corpus.is_corpus(bench):
                done = corpus.build_only(cache, bench, _memory_options(options), graph)
                failed += bool(done.error)
                print(f"{bench}: 1 graph: " + ("reused" if done.reused else done.error or f"built in {done.seconds}s"))
                continue
            if memory.is_memory(bench):
                cases, done = memory.build_memory(cache, bench, _memory_options(options))
                failed += sum(1 for b in done if b.error)
                built = sum(1 for b in done if not b.reused and not b.error)
                reused = sum(1 for b in done if b.reused)
                print(f"{bench}: {len(done)} graphs for {len(cases)} cases: {built} built, {reused} reused")
                continue
            selection, done = run.build_bench(cache, bench, options)
            failed += sum(1 for b in done if b.error)
            built = sum(1 for b in done if not b.reused and not b.error)
            reused = sum(1 for b in done if b.reused)
            print(f"{bench}: {len(done)} graphs for {len(selection.tasks)} tasks: {built} built, {reused} reused")
    except datasets.BenchError as exc:
        return _fail(str(exc), 1)
    return 1 if failed else 0


def _local_sets(entries: list[str] | None) -> list[tuple[str, Path]]:
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


def cmd_run(args) -> int:
    from evo_agents.kg import settings as kg_settings
    from evo_agents.kg.bench import compare, corpus, datasets, memory, run
    from evo_agents.kg.evaluate import load_set, project_checkouts
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        options = _options(args)
        graph = _graph(args)
        outs = [Path(p) for p in args.out]
        if len(outs) != len(options.searches):
            raise datasets.BenchError(f"{len(options.searches)} searches need as many --out, not {len(outs)}")
        local = _local_sets(args.set)
        benches = [] if local and not args.sets else _benches(args.sets)
        cache = datasets.cache_dir(args.cache) if benches else None
        parts: dict[str, dict] = {s: {} for s in options.searches}
        records: dict[str, list[dict]] = {s: [] for s in options.searches}
        with kg_settings.overriding(options.tuning):
            for bench in benches:
                assert cache is not None  # a bench asked means its cache was named
                if graph is not None and not corpus.is_corpus(bench):
                    raise datasets.BenchError(f"--strategy asks the graphs of corpus sets, not of {bench}")
                if corpus.is_corpus(bench):
                    for search in options.searches:
                        part, mine = corpus.run_corpus(
                            cache, bench, _memory_options(options), search=search, graph=graph
                        )
                        parts[search][bench] = part
                        records[search] += mine
                    continue
                if memory.is_memory(bench):
                    if options.searches != ("graph",):
                        raise datasets.BenchError(f"{bench} is asked with --search graph only")
                    part, mine = memory.run_memory(cache, bench, _memory_options(options))
                    parts["graph"][bench] = part
                    records["graph"] += mine
                    continue
                part, mine, _ = run.run_bench(cache, bench, options)
                for search in options.searches:
                    parts[search][bench] = part[search]
                    records[search] += mine[search]
            for name, path in local:
                project = resolve_project(args.project)
                for search in options.searches:
                    part, mine = run.score_local(
                        name,
                        load_set(path),
                        lambda _project, project=project, search=search: run.session_of(project, search),
                        lambda _project: project_checkouts(args.project),
                        options,
                        run.file_digest(path),
                    )
                    parts[search][name] = part
                    records[search] += mine
        summaries = [run.summary(parts[s], records[s], options, s) for s in options.searches]
        for summary in summaries:
            _extraction_cost(summary, graph)
    except (datasets.BenchError, ProjectError, ValueError) as exc:
        return _fail(str(exc), 1)
    for out, summary in zip(outs, summaries, strict=True):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print("\n".join(compare.show_lines(summary)))
            print(f"summary written to {out}")
    return 0


def _extraction_cost(summary: dict, graph) -> None:
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


def cmd_compare(args) -> int:
    from evo_agents.kg.bench import compare
    from evo_agents.kg.bench.datasets import BenchError

    try:
        verdict = compare.compare(
            compare.load_summary(args.before),
            compare.load_summary(args.after),
            split=args.split,
            gate=args.gate,
            targets=_names(args.target),
            min_targets=args.min_targets,
            p95_ms=args.p95_ms,
            tokens_drop=args.tokens_drop,
        )
    except BenchError as exc:
        return _fail(str(exc))
    if args.json:
        print(json.dumps(verdict.to_json(), indent=2))
    else:
        print("\n".join(verdict.lines()))
    return 0 if verdict.ok else 1


def cmd_merge(args) -> int:
    from evo_agents.kg.bench import compare
    from evo_agents.kg.bench.datasets import BenchError

    try:
        merged = compare.merge([compare.load_summary(p) for p in args.summaries], args.ledger)
    except (BenchError, OSError, ValueError) as exc:
        return _fail(str(exc))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("\n".join(compare.show_lines(merged)))
    print(f"summary written to {out}")
    return 0


def cmd_show(args) -> int:
    from evo_agents.kg.bench import compare
    from evo_agents.kg.bench.datasets import BenchError

    try:
        summary = compare.load_summary(args.summary)
    except BenchError as exc:
        return _fail(str(exc))
    checks = compare.check(
        summary,
        require=_names(args.require),
        split=args.split,
        sealed=args.sealed,
        abstain_min=args.abstain_min,
        false_abstain_max=args.false_abstain_max,
        precision_min=args.precision_min,
        arms=_names(args.arms),
        cost_max=args.cost_max,
        curator_agrees=args.curator_agrees,
        base=Path(args.summary).parent,
    )
    ok = all(passed for _, passed, _ in checks)
    if args.json:
        listed = [{"check": n, "ok": p, "detail": d} for n, p, d in checks]
        print(json.dumps({"ok": ok, "checks": listed, "benches": summary.get("benches")}, indent=2))
    else:
        print("\n".join(compare.show_lines(summary)))
        print("\n".join(f"{'ok' if p else 'FAIL'}  {n}: {d}" for n, p, d in checks))
    return 0 if ok else 1


def cmd_profile(args) -> int:
    """The data profile of every graph of this extractor in the bench cache, summed by repo."""
    from evo_agents.kg import profile as kg_profile
    from evo_agents.kg.bench import datasets, graphs
    from evo_agents.kg.store import StaleSchema, Store

    try:
        lay = graphs.layout(datasets.cache_dir(args.cache))
    except datasets.BenchError as exc:
        return _fail(str(exc), 1)
    repos: dict[str, dict] = {}
    for graph in graphs.cached(lay):
        if not graph.current:
            continue
        try:
            store = Store.open_file(graph.project / "graph.sqlite")
        except (StaleSchema, OSError):
            continue
        try:
            found = kg_profile.of_coverage((store.summary() or {}).get("coverage"))
        finally:
            store.close()
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
    if args.json:
        print(json.dumps({"repos": repos}, indent=2))
    elif not repos:
        print("no graph with a profile in the cache: run kg bench build")
    for repo, row in repos.items():
        shares = ", ".join(f"{k} {v:.0%}" for k, v in row["classes"].items())
        print(
            f"{repo}: {row['graphs']} graphs, {row['items']} items, noise {row['noise']:.0%}; mean item share {shares}"
        )
    return 0


def cmd_curator(args) -> int:
    """``kg bench curator plan|decide|assemble``: the Curator on a bench set (evo_agents.kg.bench.curator)."""
    from evo_agents.kg.bench import curator, datasets
    from evo_agents.kg.bench.datasets import CODE_BENCHES

    work = Path(args.work)
    try:
        if args.curator_command == "plan":
            for bench in _benches(args.sets):
                plan = curator.write_plan(work, bench, bench in CODE_BENCHES)
                print(f"{bench}: {len(plan)} configs in {work / bench}: " + ", ".join(c["name"] for c in plan))
            return 0
        if args.curator_command == "decide":
            cache = datasets.cache_dir(args.cache)
            for bench in _benches(args.sets):
                record = curator.decide_from(work, cache, bench, skill=args.skill)
                if record["error"]:
                    print(f"{bench}: no valid decision: {record['error']}", file=sys.stderr)
                    return 1
                config = record["config"]
                print(
                    f"{bench}: strategy {config['strategy']['name']} "
                    f"({'made by the Curator' if config['strategy']['self_made'] else 'built-in'}), "
                    f"applies {json.dumps(config['applied'])}, unapplied {json.dumps(config['unapplied'])}, "
                    f"cost {record['cost_usd']} USD"
                )
            return 0
        out = Path(args.out)
        built = curator.from_work(
            work,
            _benches(args.sets),
            out.parent,
            curator=not args.defaults,
            note=args.note or "",
            sampling=args.sampling or "",
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(built, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"written to {out}")
        return 0
    except (datasets.BenchError, OSError, ValueError, KeyError) as exc:
        return _fail(f"{type(exc).__name__}: {exc}" if not isinstance(exc, datasets.BenchError) else str(exc), 1)


def _ratio(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"{value} is not within 0 to 1")
    return value


def _selection_args(parser) -> None:
    from evo_agents.kg.bench import graphs, run
    from evo_agents.kg.bench.tasks import PARTS

    parser.add_argument(
        "sets",
        nargs="*",
        help="code benches (default: all four), the non-code sets (musique, longmemeval, memoryagentbench, medical, "
        "literature, music, customers) or noncode for the seven (docs/kg-bench.md)",
    )
    parser.add_argument("--cache", help="bench cache, outside any repo (default $EVO_KG_BENCH_CACHE or ~/.cache/...)")
    parser.add_argument(
        "--part", action="append", choices=PARTS, help="only this part (repeatable; default tune and holdout)"
    )
    parser.add_argument("--sealed", action="store_true", help="also read the sealed test part")
    parser.add_argument("--sample", type=int, metavar="N", help="a fixed sample of N tasks per set, stratified by repo")
    parser.add_argument("--seed", type=int, default=0, help="seed of the sample (default 0)")
    parser.add_argument(
        "--max-hours",
        type=float,
        default=run.MAX_HOURS,
        help=f"sample a set whose graphs would take longer to build (default {run.MAX_HOURS:g}; 0: never)",
    )
    parser.add_argument(
        "--git-url",
        default=graphs.GITHUB_URL,
        help="where repos are cloned from, {repo} standing for OWNER/NAME (default GitHub)",
    )
    parser.add_argument(
        "--cache-gb",
        type=float,
        default=graphs.CAP_GB,
        help=f"graphs kept at most, the least recently used evicted past it (default {graphs.CAP_GB:g}; 0: no cap)",
    )
    parser.add_argument(
        "--reserve-gb",
        type=float,
        help=f"free disk a build leaves, else the run stops before it (default {graphs.RESERVE_GB:g}, a tenth of a "
        "smaller disk)",
    )
    parser.add_argument("--verbose", action="store_true", help="progress lines on stderr even with --json")
    parser.add_argument(
        "--strategy",
        metavar="NAME",
        help="corpus sets: a graph of its own built with this kg strategy applied, its entities step included",
    )
    parser.add_argument(
        "--entities",
        metavar="FILE",
        help="with --strategy: a json object of jobs, max_calls, max_tokens, max_cost set on its entities mapping",
    )


def _run_parser(sub) -> None:
    """`kg bench run`: ask the selected tasks on their graphs."""
    from evo_agents.kg import evaluate as ev
    from evo_agents.kg.bench import run

    go = sub.add_parser("run", help="ask every selected task on its graph, no model called, into a summary JSON")
    _selection_args(go)
    go.add_argument(
        "--out", action="append", required=True, metavar="PATH", help="where the summary JSON goes, one a --search"
    )
    go.add_argument(
        "--set", action="append", metavar="NAME=QUESTIONS", help="also a local `kg eval` set, such as evo-agents"
    )
    go.add_argument("--project", help="project of the local sets (default: from the cwd)")
    go.add_argument(
        "--settings",
        metavar="FILE",
        help="ask under these search settings (kg settings keys: semantic_weight, bm25_name, bm25_path, bm25_body)",
    )
    go.add_argument("--method", action="append", choices=ev.METHODS, help="method (repeatable; first is primary)")
    go.add_argument(
        "--limit",
        type=int,
        help="kg_search's limit (default 5 for swe-explore, 25 for the non-code sets, else 10)",
    )
    go.add_argument("--budget-tokens", type=int, default=run.DEFAULT_BUDGET_TOKENS, help="kg_context's budget")
    go.add_argument("--line-budget", type=int, default=ev.LINE_BUDGET, help="lines scored per answer (default 500)")
    go.add_argument(
        "--search",
        action="append",
        choices=run.SEARCHES,
        help="how kg_search ranks, as in kg eval (default graph); repeated, each on the same graphs into its --out",
    )
    go.add_argument("--prune", action="store_true", help="delete each graph and clone once asked, to spare the disk")
    go.add_argument(
        "--exclude-repo", action="append", metavar="OWNER/NAME", help="leave a repo out before sampling (repeatable)"
    )
    go.add_argument(
        "--like",
        metavar="SUMMARY",
        help="ask the tasks SUMMARY asked (its sample, whatever the part), so two runs pair on the same questions",
    )
    go.add_argument("--jobs", type=int, default=1, metavar="N", help="repos asked at once (default 1)")
    go.add_argument("--json", action="store_true", help="print the summary")
    go.set_defaults(func=cmd_run)


def _show_parser(sub) -> None:
    """`kg bench show`: print a summary and check it."""
    show = sub.add_parser("show", help="print a summary and check it")
    show.add_argument("summary")
    show.add_argument("--require", metavar="SET,...", help="sets that must have tasks (in --split)")
    show.add_argument("--split", help="the part the checks read")
    show.add_argument("--sealed", action="store_true", help="accept a summary with the test part")
    show.add_argument("--abstain-min", type=_ratio, metavar="R", help="least share abstained on without an answer")
    show.add_argument("--false-abstain-max", type=_ratio, metavar="R", help="most share abstained on with one")
    show.add_argument("--precision-min", type=_ratio, metavar="R", help="least mean line precision")
    show.add_argument("--arms", metavar="ARM,...", help="arms the summary must have run")
    show.add_argument("--cost-max", type=float, metavar="USD", help="most the run may have cost")
    show.add_argument(
        "--curator-agrees",
        type=int,
        metavar="N",
        help="fail unless N of the --require sets have the Curator's config within 2 SE of the best, "
        "and no Curator input is a file of holdout or test",
    )
    show.add_argument("--json", action="store_true", help="machine-readable output")
    show.set_defaults(func=cmd_show)


def _curator_parser(sub) -> None:
    """`kg bench curator plan|decide|assemble`."""
    cur = sub.add_parser(
        "curator", help="the Curator's kg_init run on a set: plan the configs, decide, assemble the file"
    )
    csub = cur.add_subparsers(dest="curator_command", required=True)
    for name, text in (
        ("plan", "write the configs to rank (and their settings files) under --work"),
        ("decide", "run the Curator on the set's profile and tune scores in --work, keep inputs and decision"),
        ("assemble", "join the runs and decisions in --work into the Curator file (curator.json)"),
    ):
        one = csub.add_parser(name, help=text)
        one.add_argument("sets", nargs="+", help="the sets (or noncode)")
        one.add_argument("--work", required=True, metavar="DIR", help="work directory, SET/plan.json and runs under it")
        one.set_defaults(func=cmd_curator)
        if name == "decide":
            one.add_argument("--cache", help="bench cache, outside any repo")
            one.add_argument("--skill", metavar="DIR", help="the curator skill (default $EVO_CURATOR_SKILL, ~/.claude)")
        if name == "assemble":
            one.add_argument("--out", required=True, metavar="PATH")
            one.add_argument("--defaults", action="store_true", help="rank for the release defaults: no Curator run")
            one.add_argument("--note", help="what the file ranked and how, kept in it")
            one.add_argument("--sampling", help="how the tasks were sampled, kept in it")


def register(ksub) -> None:
    bench = ksub.add_parser(
        "bench", help="ContextBench, SWE-Explore, LocBench and MuLocBench on graphs of their repos (docs/kg-bench.md)"
    )
    sub = bench.add_subparsers(dest="bench_command", required=True)

    fetch = sub.add_parser("fetch", help="download the sets at their pinned revisions into the bench cache")
    fetch.add_argument("sets", nargs="*", help="code benches (default: all four), non-code sets or noncode")
    fetch.add_argument("--cache", help="bench cache, outside any repo")
    fetch.add_argument("--from", dest="mirror", metavar="DIR", help="copy DIR/DATASET/FILE instead of downloading")
    fetch.add_argument("--force", action="store_true", help="download again what the cache has")
    fetch.set_defaults(func=cmd_fetch)

    build = sub.add_parser("build", help="build the graph of each repo and base commit the selected tasks need")
    _selection_args(build)
    build.set_defaults(func=cmd_build)

    _run_parser(sub)

    cmp = sub.add_parser("compare", help="pair two summaries task by task with a bootstrap; gate on the result")
    cmp.add_argument("before")
    cmp.add_argument("after")
    cmp.add_argument("--split", default="holdout", help="part to compare (default holdout)")
    cmp.add_argument("--gate", action="store_true", help="fail when a set's primary metric drops by over 2 SE")
    cmp.add_argument("--target", metavar="SET,...", help="sets that should improve beyond the 95%% interval")
    cmp.add_argument("--min-targets", type=int, metavar="N", help="how many --target sets must (default all)")
    cmp.add_argument("--p95-ms", type=float, metavar="MS", help="fail when kg_search's p95 after passes MS")
    cmp.add_argument("--tokens-drop", type=_ratio, metavar="R", help="fail unless tokens per question fall by R")
    cmp.add_argument("--json", action="store_true", help="machine-readable output")
    cmp.set_defaults(func=cmd_compare)

    mrg = sub.add_parser("merge", help="one summary from the summaries of several sets run apart (same search)")
    mrg.add_argument("--out", required=True, metavar="PATH")
    mrg.add_argument(
        "--ledger", metavar="PATH", help="spend of the API embeddings, to name where hybrid vectors came from"
    )
    mrg.add_argument("summaries", nargs="+")
    mrg.set_defaults(func=cmd_merge)

    prof = sub.add_parser("profile", help="the data profile of the cached bench graphs, by repo")
    prof.add_argument("--cache", help="bench cache, outside any repo")
    prof.add_argument("--json", action="store_true", help="machine-readable output")
    prof.set_defaults(func=cmd_profile)

    _show_parser(sub)

    _curator_parser(sub)
