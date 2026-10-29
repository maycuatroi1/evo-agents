"""``evo-agents kg bench``: fetch, build, run, merge, compare and show the code benches (docs/kg-bench.md)."""

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
    from evo_agents.kg.bench.datasets import CODE_BENCHES, BenchError

    names = given or list(CODE_BENCHES)
    unknown = [n for n in names if n not in CODE_BENCHES]
    if unknown:
        raise BenchError(f"no code bench {', '.join(unknown)}: one of {', '.join(CODE_BENCHES)}")
    return names


def _options(args):
    from evo_agents.kg.bench import run
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
        jobs=getattr(args, "jobs", 1),
        progress=None if getattr(args, "json", False) and not args.verbose else run.progress_to_stderr,
    )


def cmd_fetch(args) -> int:
    from evo_agents.kg.bench import datasets

    try:
        cache = datasets.cache_dir(args.cache)
        for bench in _benches(args.sets):
            got = datasets.fetch(bench, cache, mirror=Path(args.mirror) if args.mirror else None, force=args.force)
            size = sum(f["bytes"] for f in got["files"])
            left = ", ".join(f"{n} {why}" for why, n in sorted(got["skipped"].items())) or "none"
            print(f"{bench}: {got['tasks']} tasks, {size / 1e6:.1f} MB in {cache}; left out: {left}")
    except datasets.BenchError as exc:
        return _fail(str(exc), 1)
    return 0


def cmd_build(args) -> int:
    from evo_agents.kg.bench import datasets, run

    try:
        cache = datasets.cache_dir(args.cache)
        options = _options(args)
        failed = 0
        for bench in _benches(args.sets):
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
    from evo_agents.kg.bench import compare, datasets, run
    from evo_agents.kg.evaluate import load_set, project_checkouts
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        options = _options(args)
        outs = [Path(p) for p in args.out]
        if len(outs) != len(options.searches):
            raise datasets.BenchError(f"{len(options.searches)} searches need as many --out, not {len(outs)}")
        local = _local_sets(args.set)
        benches = [] if local and not args.sets else _benches(args.sets)
        cache = datasets.cache_dir(args.cache) if benches else None
        parts: dict[str, dict] = {s: {} for s in options.searches}
        records: dict[str, list[dict]] = {s: [] for s in options.searches}
        for bench in benches:
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
    )
    ok = all(passed for _, passed, _ in checks)
    if args.json:
        listed = [{"check": n, "ok": p, "detail": d} for n, p, d in checks]
        print(json.dumps({"ok": ok, "checks": listed, "benches": summary.get("benches")}, indent=2))
    else:
        print("\n".join(compare.show_lines(summary)))
        print("\n".join(f"{'ok' if p else 'FAIL'}  {n}: {d}" for n, p, d in checks))
    return 0 if ok else 1


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

    parser.add_argument("sets", nargs="*", help="code benches (default: all four; docs/kg-bench.md)")
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
    parser.add_argument("--verbose", action="store_true", help="progress lines on stderr even with --json")


def register(ksub) -> None:
    from evo_agents.kg import evaluate as ev
    from evo_agents.kg.bench import run

    bench = ksub.add_parser(
        "bench", help="ContextBench, SWE-Explore, LocBench and MuLocBench on graphs of their repos (docs/kg-bench.md)"
    )
    sub = bench.add_subparsers(dest="bench_command", required=True)

    fetch = sub.add_parser("fetch", help="download the sets at their pinned revisions into the bench cache")
    fetch.add_argument("sets", nargs="*", help="code benches (default: all four)")
    fetch.add_argument("--cache", help="bench cache, outside any repo")
    fetch.add_argument("--from", dest="mirror", metavar="DIR", help="copy DIR/DATASET/FILE instead of downloading")
    fetch.add_argument("--force", action="store_true", help="download again what the cache has")
    fetch.set_defaults(func=cmd_fetch)

    build = sub.add_parser("build", help="build the graph of each repo and base commit the selected tasks need")
    _selection_args(build)
    build.set_defaults(func=cmd_build)

    go = sub.add_parser("run", help="ask every selected task on its graph, no model called, into a summary JSON")
    _selection_args(go)
    go.add_argument(
        "--out", action="append", required=True, metavar="PATH", help="where the summary JSON goes, one a --search"
    )
    go.add_argument(
        "--set", action="append", metavar="NAME=QUESTIONS", help="also a local `kg eval` set, such as evo-agents"
    )
    go.add_argument("--project", help="project of the local sets (default: from the cwd)")
    go.add_argument("--method", action="append", choices=ev.METHODS, help="method (repeatable; first is primary)")
    go.add_argument("--limit", type=int, help="kg_search's limit (default 5 for swe-explore, else 10)")
    go.add_argument("--budget-tokens", type=int, default=run.DEFAULT_BUDGET_TOKENS, help="kg_context's budget")
    go.add_argument("--line-budget", type=int, default=ev.LINE_BUDGET, help="lines scored per answer (default 500)")
    go.add_argument(
        "--search",
        action="append",
        choices=run.SEARCHES,
        help="how kg_search ranks, as in kg eval (default graph); repeated, each on the same graphs into its --out",
    )
    go.add_argument("--prune", action="store_true", help="delete each graph and clone once asked, to spare the disk")
    go.add_argument("--jobs", type=int, default=1, metavar="N", help="repos asked at once (default 1)")
    go.add_argument("--json", action="store_true", help="print the summary")
    go.set_defaults(func=cmd_run)

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
    show.add_argument("--json", action="store_true", help="machine-readable output")
    show.set_defaults(func=cmd_show)
