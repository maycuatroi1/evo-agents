"""``kg bench compare``, ``merge``, ``profile`` and ``show``: what the summaries of runs say, and the data profile of
the cached graphs (docs/kg-bench.md)."""

from __future__ import annotations

import json
from pathlib import Path

from evo_agents.clikit import Arg, Result, Text, Views
from evo_agents.kg.bench import selection as sel
from evo_agents.kg.cli.base import JSON, Failed, Json, KgCommand, KgContext, Shown
from evo_agents.kg.cli.bench.base import CACHE, ratio


class BenchCompare(KgCommand):
    name = "compare"
    help = "pair two summaries task by task with a bootstrap; gate on the result"
    arguments = (
        Arg("before"),
        Arg("after"),
        Arg("--split", default="holdout", help="part to compare (default holdout)"),
        Arg("--gate", action="store_true", help="fail when a set's primary metric drops by over 2 SE"),
        Arg("--target", metavar="SET,...", help="sets that should improve beyond the 95%% interval"),
        Arg("--min-targets", type=int, metavar="N", help="how many --target sets must (default all)"),
        Arg("--p95-ms", type=float, metavar="MS", help="fail when kg_search's p95 after passes MS"),
        ratio("--tokens-drop", "fail unless tokens per question fall by R"),
        JSON,
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import compare
        from evo_agents.kg.bench.datasets import BenchError

        args = ctx.args
        try:
            verdict = compare.compare(
                compare.load_summary(args.before),
                compare.load_summary(args.after),
                split=args.split,
                gate=args.gate,
                targets=sel.names(args.target),
                min_targets=args.min_targets,
                p95_ms=args.p95_ms,
                tokens_drop=args.tokens_drop,
            )
        except BenchError as exc:
            raise Failed(str(exc), 2) from None
        data = verdict.to_json() if args.json else None
        view = Json(data, ascii=True) if args.json else Text("\n".join(verdict.lines()))
        return Shown(data, view, 0 if verdict.ok else 1)


class BenchMerge(KgCommand):
    name = "merge"
    help = "one summary from the summaries of several sets run apart (same search)"
    arguments = (
        Arg("--out", required=True, metavar="PATH"),
        Arg("--ledger", metavar="PATH", help="spend of the API embeddings, to name where hybrid vectors came from"),
        Arg("summaries", nargs="+"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import compare
        from evo_agents.kg.bench.datasets import BenchError

        args = ctx.args
        try:
            merged = compare.merge([compare.load_summary(p) for p in args.summaries], args.ledger)
        except (BenchError, OSError, ValueError) as exc:
            raise Failed(str(exc), 2) from None
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return Result(None, Text("\n".join(compare.show_lines(merged)), f"summary written to {out}"))


class BenchProfile(KgCommand):
    name = "profile"
    help = "the data profile of the cached bench graphs, by repo"
    arguments = (CACHE, JSON)

    def run(self, ctx: KgContext) -> Result:
        """The data profile of every graph of this extractor in the bench cache, summed by repo."""
        from evo_agents.kg.bench import datasets, graphs

        try:
            lay = graphs.layout(datasets.cache_dir(ctx.args.cache))
        except datasets.BenchError as exc:
            raise Failed(str(exc), 1) from None
        repos = sel.profile_repos(lay)
        lines = [
            f"{repo}: {row['graphs']} graphs, {row['items']} items, noise {row['noise']:.0%}; mean item share "
            + ", ".join(f"{k} {v:.0%}" for k, v in row["classes"].items())
            for repo, row in repos.items()
        ]
        if ctx.args.json:
            return Shown({"repos": repos}, Views(Json({"repos": repos}, ascii=True), Text(*lines)))
        if not repos:
            lines = ["no graph with a profile in the cache: run kg bench build"]
        return Shown({"repos": repos}, Text(*lines))


class BenchShow(KgCommand):
    name = "show"
    help = "print a summary and check it"
    arguments = (
        Arg("summary"),
        Arg("--require", metavar="SET,...", help="sets that must have tasks (in --split)"),
        Arg("--split", help="the part the checks read"),
        Arg("--sealed", action="store_true", help="accept a summary with the test part"),
        ratio("--abstain-min", "least share abstained on without an answer"),
        ratio("--false-abstain-max", "most share abstained on with one"),
        ratio("--precision-min", "least mean line precision"),
        Arg("--arms", metavar="ARM,...", help="arms the summary must have run"),
        Arg("--cost-max", type=float, metavar="USD", help="most the run may have cost"),
        Arg(
            "--curator-agrees",
            type=int,
            metavar="N",
            help="fail unless N of the --require sets have the Curator's config within 2 SE of the best, "
            "and no Curator input is a file of holdout or test",
        ),
        JSON,
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import compare
        from evo_agents.kg.bench.datasets import BenchError

        args = ctx.args
        try:
            summary = compare.load_summary(args.summary)
        except BenchError as exc:
            raise Failed(str(exc), 2) from None
        checks = compare.check(
            summary,
            require=sel.names(args.require),
            split=args.split,
            sealed=args.sealed,
            abstain_min=args.abstain_min,
            false_abstain_max=args.false_abstain_max,
            precision_min=args.precision_min,
            arms=sel.names(args.arms),
            cost_max=args.cost_max,
            curator_agrees=args.curator_agrees,
            base=Path(args.summary).parent,
        )
        ok = all(passed for _, passed, _ in checks)
        status = 0 if ok else 1
        if args.json:
            listed = [{"check": n, "ok": p, "detail": d} for n, p, d in checks]
            data = {"ok": ok, "checks": listed, "benches": summary.get("benches")}
            return Shown(data, Json(data, ascii=True), status)
        return Shown(
            None,
            Text(
                "\n".join(compare.show_lines(summary)),
                "\n".join(f"{'ok' if p else 'FAIL'}  {n}: {d}" for n, p, d in checks),
            ),
            status,
        )


COMPARE, MERGE, PROFILE, SHOW = BenchCompare(), BenchMerge(), BenchProfile(), BenchShow()
