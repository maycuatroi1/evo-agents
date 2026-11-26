"""``evo-agents kg links``: the edge model M3 (``evo_agents.kg.links``), docs/kg-links.md.

``propose`` trains the model on a build made in memory of the project's corpus on this machine, chooses its threshold on hidden edges, scores the
candidate edges (the review queue's ambiguous and dangling names, resolved edges to a shared name, tests to code
files, and with ``--docs`` documents to code files) and lists those it proposes. ``--review FILE`` reads the review
queue from a file (``evo-agents hub kg review --json``); ``--hub`` reads it from the hub and sends each proposal there as a proposal
(``kg_propose``), at most ``--max``, which a writer or the Curator accepts or rejects. Nothing is written to a graph.

``eval`` measures the model on hidden edges of such a build and of a sample of the bench's tune graphs, and
writes a summary ``kg bench show`` checks (``evo_agents.kg.bench.links_eval``).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import cast

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.kg import links
from evo_agents.kg.cli.base import JSON, Failed, Json, KgCommand, KgContext, Shown

DEFAULT_MAX = 50  # proposals sent to the hub in one call

MODEL_ARGS = (
    Arg("--model", choices=links.MODELS, default=links.MODELS[0], help="graph model (default %(default)s)"),
    Arg("--dim", type=int, default=links.DIM, help="embedding size (default %(default)s)"),
    Arg("--epochs", type=int, default=links.EPOCHS, help="training epochs (default %(default)s)"),
    Arg("--precision", type=float, default=links.PRECISION, help="precision the threshold is chosen for"),
    Arg("--seed", type=int, default=links.SEED),
    Arg("--minutes", type=float, help="stop training a graph after this long"),
)


def model_options(args) -> dict:
    out = {"model": args.model, "dim": args.dim, "epochs": args.epochs, "seed": args.seed, "precision": args.precision}
    if args.minutes:
        out["deadline"] = time.monotonic() + args.minutes * 60
    return out


def say(text: str) -> None:
    """A line of progress, on stderr as it happens."""
    print(text, file=sys.stderr, flush=True)


def links_failed(text: str) -> Failed:
    return Failed(text, 1, prefix="evo-agents kg links: ")


def open_build(project_name: str | None):
    """The project, the view of a build of it made in memory (``build.build_rows``) and that build's id: nothing is
    written to a graph."""
    from evo_agents.kg.bench import rows_file
    from evo_agents.kg.bench.datasets import BenchError
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        project = resolve_project(project_name)
        snap = rows_file.local_snapshot(project)
    except (ProjectError, BenchError) as exc:
        raise links_failed(str(exc)) from None
    view = snap.view()
    return project, view, cast(int, view.latest_ready())  # a build made in memory is ready


def proposal_lines(proposed: list[dict], found: list, sent: list[dict], args) -> list[str]:
    """What a person reads of the proposals, and of what was sent to the hub with ``--hub``."""
    by_why: dict[str, int] = {}
    for p in proposed:
        by_why[p["why"]] = by_why.get(p["why"], 0) + 1
    lines = [f"{len(found)} candidates, {len(proposed)} proposals " + json.dumps(by_why, sort_keys=True)]
    lines += [f"{p['probability']:.2f}  {p['why']:<9} {p['src']} -{p['rel']}-> {p['dst']}" for p in proposed[:20]]
    if args.hub:
        made = [s for s in sent if s.get("number")]
        lines.append(
            f"sent {len(made)} of {len(sent)} to the hub as proposals"
            + (" (dry run)" if args.dry_run else "")
            + "; a writer or the Curator accepts or rejects each"
        )
        lines += [f"  not sent: {s['src']} -{s['rel']}-> {s['dst']}: {s['error']}" for s in sent if s.get("error")]
    return lines


class LinksPropose(KgCommand):
    name = "propose"
    help = "score candidate edges of a project's graph and list or send proposals"
    arguments = (
        Arg("--project", help="project (default: from the cwd)"),
        Arg("--review", type=Path, help="the review queue as `evo-agents hub kg review --json` prints it"),
        Arg("--hub", action="store_true", help="read the review queue from the hub and send the proposals"),
        Arg("--max", type=int, default=DEFAULT_MAX, help="most proposals sent (default %(default)s)"),
        Arg("--docs", action="store_true", help="also score documents against code files no name ties"),
        Arg("--dry-run", action="store_true", help="with --hub: read the queue but send nothing"),
        Arg("--out", type=Path, help="also write the proposals and the evaluation as JSON"),
        JSON,
        *MODEL_ARGS,
    )

    def run(self, ctx: KgContext) -> Result:
        args = ctx.args
        project, store, b = open_build(args.project)
        g = links.read(store, b)
        review = store.review_items(b)
        if args.review is not None:
            review = json.loads(args.review.read_text(encoding="utf-8")).get("items") or []
        elif args.hub:
            from evo_agents.hub.kg_links import read_review

            review = read_review(project.name)
        say(f"{project.name}: {len(g.ids)} nodes, {len(g.parsed)} parsed edges; training {args.model}")
        try:
            model, numbers = links.evaluate(g, **model_options(args))
        except links.LinksError as exc:
            raise links_failed(str(exc)) from exc
        threshold = numbers["threshold"]
        say(
            f"hidden edges: precision {numbers['precision']}, recall {numbers['recall']} at threshold {threshold}"
            f" (auc {numbers['auc']}, {numbers['fit']['seconds']}s)"
        )
        if threshold is None:
            raise links_failed(f"no threshold reaches precision {args.precision} on hidden edges: nothing is proposed")
        found = links.candidates(g, review, model=model, docs=args.docs)
        proposed = links.proposals(links.score(model, g, found), threshold)
        sent: list[dict] = []
        if args.hub:
            from evo_agents.hub.kg_links import send

            sent = send(project.name, proposed[: args.max], dry_run=args.dry_run)
        result = {
            "project": project.name,
            "build": b,
            "evaluation": numbers,
            "candidates": len(found),
            "proposals": proposed,
            "sent": sent,
        }
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        if args.json:
            return Shown(result, Json(result, indent=1))
        return Shown(result, Text(*proposal_lines(proposed, found, sent, args)))


class LinksEval(KgCommand):
    name = "eval"
    help = "precision and recall on hidden edges of a project's and the bench's graphs"
    arguments = (
        Arg("--set", dest="local", action="append", default=[], metavar="NAME=PROJECT", help="a local graph"),
        Arg("--bench", metavar="SET,...", default="", help="bench sets whose tune graphs are sampled"),
        Arg("--graphs", type=int, default=4, help="tune graphs per bench set (default %(default)s)"),
        Arg("--cache", help="bench cache, outside any repo"),
        Arg("--out", type=Path, required=True),
        *MODEL_ARGS,
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.bench import compare, datasets, links_eval, rows_file
        from evo_agents.kg.bench.datasets import BenchError

        args = ctx.args
        options = model_options(args)
        sets: dict[str, list[tuple[str, dict]]] = {}
        try:
            for entry in args.local:
                name, _, project_name = entry.partition("=")
                project, view, _ = open_build(project_name or name)
                say(f"{name}: a build in memory of project {project.name}")
                sets[name] = [(project.name, links_eval.graph_numbers(project.name, view, options, say))]
            benches = [s for s in args.bench.split(",") if s]
            cache = datasets.cache_dir(args.cache) if benches else None
            for bench in benches:
                assert cache is not None  # a bench asked means its cache was named
                found = links_eval.tune_graphs(cache, bench, args.graphs, args.seed)
                if not found:
                    raise links_failed(
                        f"{bench}: no tune graph is built in {cache}: `kg bench build {bench} --part tune`"
                    )
                say(f"{bench}: {len(found)} tune graphs")
                sets[bench] = [
                    (name, links_eval.graph_numbers(name, rows_file.snapshot_of(path).view(), options, say))
                    for name, path in found
                ]
        except (BenchError, links.LinksError) as exc:
            raise links_failed(str(exc)) from exc
        out = links_eval.summary(sets, options, args.seed, args.graphs)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return Result(None, Text("\n".join(compare.show_lines(out)), f"summary written to {args.out}"))


LINKS = Group(
    "links", "edges the graph may lack, guessed by a graph model and only proposed", [LinksPropose(), LinksEval()]
)
