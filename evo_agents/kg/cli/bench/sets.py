"""``kg bench fetch``, ``kg bench build`` and ``kg bench run``: the sets into the bench cache, the graphs of their
tasks, and the tasks asked on those graphs into a summary (docs/kg-bench.md)."""

from __future__ import annotations

import json
from pathlib import Path

from evo_agents.clikit import Arg, Result, Text
from evo_agents.kg import evaluate as ev
from evo_agents.kg.bench import run
from evo_agents.kg.bench import selection as sel
from evo_agents.kg.cli.base import Failed, Json, KgCommand, KgContext, done
from evo_agents.kg.cli.bench.base import CACHE, SELECTION


class BenchFetch(KgCommand):
    name = "fetch"
    help = "download the sets at their pinned revisions into the bench cache"
    arguments = (
        Arg("sets", nargs="*", help="code benches (default: all four), non-code sets or noncode"),
        CACHE,
        Arg("--from", dest="mirror", metavar="DIR", help="copy DIR/DATASET/FILE instead of downloading"),
        Arg("--force", action="store_true", help="download again what the cache has"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import corpus, datasets, memory

        args = ctx.args
        try:
            cache = datasets.cache_dir(args.cache)
            for bench in sel.benches(args.sets):
                fetch = (
                    memory.fetch
                    if memory.is_memory(bench)
                    else corpus.fetch
                    if corpus.is_corpus(bench)
                    else datasets.fetch
                )
                got = fetch(bench, cache, mirror=Path(args.mirror) if args.mirror else None, force=args.force)
                size = sum(f["bytes"] for f in got["files"])
                left = ", ".join(f"{n} {why}" for why, n in sorted(got["skipped"].items())) or "none"
                print(f"{bench}: {got['tasks']} tasks, {size / 1e6:.1f} MB in {cache}; left out: {left}", file=ctx.out)
        except datasets.BenchError as exc:
            raise Failed(str(exc), 1) from None
        return done()


def built_line(bench: str, done_: list, tasks: int, what: str) -> str:
    built = sum(1 for b in done_ if not b.reused and not b.error)
    reused = sum(1 for b in done_ if b.reused)
    return f"{bench}: {len(done_)} graphs for {tasks} {what}: {built} built, {reused} reused"


class BenchBuild(KgCommand):
    name = "build"
    help = "build the graph of each repo and base commit the selected tasks need"
    arguments = SELECTION

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import corpus, datasets, memory

        args = ctx.args
        try:
            cache = datasets.cache_dir(args.cache)
            options = sel.options_of(args)
            graph = sel.graph_of(args)
            failed = 0
            for bench in sel.benches(args.sets):
                if graph is not None and not corpus.is_corpus(bench):
                    raise datasets.BenchError(f"--strategy builds the graphs of corpus sets, not of {bench}")
                if corpus.is_corpus(bench):
                    one = corpus.build_only(cache, bench, sel.memory_options(options), graph)
                    failed += bool(one.error)
                    said = "reused" if one.reused else one.error or f"built in {one.seconds}s"
                    print(f"{bench}: 1 graph: " + said, file=ctx.out)
                    continue
                if memory.is_memory(bench):
                    cases, built = memory.build_memory(cache, bench, sel.memory_options(options))
                    failed += sum(1 for b in built if b.error)
                    print(built_line(bench, built, len(cases), "cases"), file=ctx.out)
                    continue
                selection, built = run.build_bench(cache, bench, options)
                failed += sum(1 for b in built if b.error)
                print(built_line(bench, built, len(selection.tasks), "tasks"), file=ctx.out)
        except datasets.BenchError as exc:
            raise Failed(str(exc), 1) from None
        return done(1 if failed else 0)


class BenchRun(KgCommand):
    name = "run"
    help = "ask every selected task on its graph, no model called, into a summary JSON"
    arguments = (
        *SELECTION,
        Arg(
            "--out", action="append", required=True, metavar="PATH", help="where the summary JSON goes, one a --search"
        ),
        Arg("--set", action="append", metavar="NAME=QUESTIONS", help="also a local `kg eval` set, such as evo-agents"),
        Arg("--project", help="project of the local sets (default: from the cwd)"),
        Arg(
            "--settings",
            metavar="FILE",
            help="ask under these search settings (kg settings keys: semantic_weight, bm25_name, bm25_path, bm25_body, walk, entity_code, entity_words)",
        ),
        Arg("--method", action="append", choices=ev.METHODS, help="method (repeatable; first is primary)"),
        Arg(
            "--limit", type=int, help="kg_search's limit (default 5 for swe-explore, 25 for the non-code sets, else 10)"
        ),
        Arg("--budget-tokens", type=int, default=run.DEFAULT_BUDGET_TOKENS, help="kg_context's budget"),
        Arg("--line-budget", type=int, default=ev.LINE_BUDGET, help="lines scored per answer (default 500)"),
        Arg(
            "--search",
            action="append",
            choices=run.SEARCHES,
            help="how kg_search ranks, as in kg eval (default graph); repeated, each on the same graphs into its --out",
        ),
        Arg("--prune", action="store_true", help="delete each graph and clone once asked, to spare the disk"),
        Arg(
            "--exclude-repo",
            action="append",
            metavar="OWNER/NAME",
            help="leave a repo out before sampling (repeatable)",
        ),
        Arg(
            "--like",
            metavar="SUMMARY",
            help="ask the tasks SUMMARY asked (its sample, whatever the part), so two runs pair on the same questions",
        ),
        Arg("--jobs", type=int, default=1, metavar="N", help="repos asked at once (default 1)"),
        Arg("--json", action="store_true", help="print the summary"),
    )

    def summaries(self, args, outs: list[Path]) -> list[dict]:
        """The summary of each search the run asks."""
        from evo_agents.kg import settings as kg_settings
        from evo_agents.kg.bench import datasets

        options = sel.options_of(args)
        graph = sel.graph_of(args)
        if len(outs) != len(options.searches):
            raise datasets.BenchError(f"{len(options.searches)} searches need as many --out, not {len(outs)}")
        local = sel.local_sets(args.set)
        benches = [] if local and not args.sets else sel.benches(args.sets)
        cache = datasets.cache_dir(args.cache) if benches else None
        asked = sel.Asked(options.searches)
        with kg_settings.overriding(options.tuning):
            for bench in benches:
                assert cache is not None  # a bench asked means its cache was named
                sel.ask_bench(asked, cache, bench, options, graph)
            sel.ask_local(asked, local, args.project, options)
        found = [run.summary(asked.parts[s], asked.records[s], options, s) for s in options.searches]
        for summary in found:
            sel.extraction_cost(summary, graph)
        return found

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import compare, datasets
        from evo_agents.kg.project import ProjectError

        args = ctx.args
        outs = [Path(p) for p in args.out]
        try:
            summaries = self.summaries(args, outs)
        except (datasets.BenchError, ProjectError, ValueError) as exc:
            raise Failed(str(exc), 1) from None
        for out, summary in zip(outs, summaries, strict=True):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            if args.json:
                Json(summary).write(ctx.out)
            else:
                Text("\n".join(compare.show_lines(summary)), f"summary written to {out}").write(ctx.out)
        return done()


FETCH, BUILD, RUN = BenchFetch(), BenchBuild(), BenchRun()
