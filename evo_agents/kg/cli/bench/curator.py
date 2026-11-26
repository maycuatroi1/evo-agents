"""``kg bench curator plan|decide|assemble``: the Curator's kg_init run on a bench set (evo_agents.kg.bench.curator)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result
from evo_agents.kg.bench import selection as sel
from evo_agents.kg.cli.base import Failed, KgCommand, KgContext, done
from evo_agents.kg.cli.bench.base import CACHE

ON_WORK = (
    Arg("sets", nargs="+", help="the sets (or noncode)"),
    Arg("--work", required=True, metavar="DIR", help="work directory, SET/plan.json and runs under it"),
)


def on_work(ctx: KgContext, step: Callable[[Path], int]) -> Result:
    """``step`` on the work directory of ``--work``; a failure of it is ``error: <message>`` and exit status 1."""
    from evo_agents.kg.bench import datasets

    try:
        return done(step(Path(ctx.args.work)))
    except (datasets.BenchError, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, datasets.BenchError) else f"{type(exc).__name__}: {exc}"
        raise Failed(message, 1) from None


class CuratorPlan(KgCommand):
    name = "plan"
    help = "write the configs to rank (and their settings files) under --work"
    arguments = ON_WORK

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import curator
        from evo_agents.kg.bench.datasets import CODE_BENCHES

        def step(work: Path) -> int:
            for bench in sel.benches(ctx.args.sets):
                plan = curator.write_plan(work, bench, bench in CODE_BENCHES)
                names = ", ".join(c["name"] for c in plan)
                print(f"{bench}: {len(plan)} configs in {work / bench}: " + names, file=ctx.out)
            return 0

        return on_work(ctx, step)


class CuratorDecide(KgCommand):
    name = "decide"
    help = "run the Curator on the set's profile and tune scores in --work, keep inputs and decision"
    arguments = (
        *ON_WORK,
        CACHE,
        Arg("--skill", metavar="DIR", help="the curator skill (default $EVO_CURATOR_SKILL, ~/.claude)"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import curator, datasets

        def step(work: Path) -> int:
            cache = datasets.cache_dir(ctx.args.cache)
            for bench in sel.benches(ctx.args.sets):
                record = curator.decide_from(work, cache, bench, skill=ctx.args.skill)
                if record["error"]:
                    print(f"{bench}: no valid decision: {record['error']}", file=sys.stderr)
                    return 1
                config = record["config"]
                print(
                    f"{bench}: strategy {config['strategy']['name']} "
                    f"({'made by the Curator' if config['strategy']['self_made'] else 'built-in'}), "
                    f"applies {json.dumps(config['applied'])}, unapplied {json.dumps(config['unapplied'])}, "
                    f"cost {record['cost_usd']} USD",
                    file=ctx.out,
                )
            return 0

        return on_work(ctx, step)


class CuratorAssemble(KgCommand):
    name = "assemble"
    help = "join the runs and decisions in --work into the Curator file (curator.json)"
    arguments = (
        *ON_WORK,
        Arg("--out", required=True, metavar="PATH"),
        Arg("--defaults", action="store_true", help="rank for the release defaults: no Curator run"),
        Arg("--note", help="what the file ranked and how, kept in it"),
        Arg("--sampling", help="how the tasks were sampled, kept in it"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import curator

        args = ctx.args

        def step(work: Path) -> int:
            out = Path(args.out)
            built = curator.from_work(
                work,
                sel.benches(args.sets),
                out.parent,
                curator=not args.defaults,
                note=args.note or "",
                sampling=args.sampling or "",
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(built, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            print(f"written to {out}", file=ctx.out)
            return 0

        return on_work(ctx, step)


CURATOR = Group(
    "curator",
    "the Curator's kg_init run on a set: plan the configs, decide, assemble the file",
    [CuratorPlan(), CuratorDecide(), CuratorAssemble()],
    dest="curator_command",
)
