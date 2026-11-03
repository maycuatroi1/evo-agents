"""``evo-agents kg links``: the edge model M3 (``evo_agents.kg.links``), docs/kg-links.md.

``propose`` trains the model on a project's latest local build, chooses its threshold on hidden edges, scores the
candidate edges (the review queue's ambiguous and dangling names, resolved edges to a shared name, tests to code
files, and with ``--docs`` documents to code files) and lists those it proposes. ``--review FILE`` reads the review
queue from a file (``evo-agents hub kg review --json``); ``--hub`` reads it from the hub and sends each proposal there as a proposal
(``kg_propose``), at most ``--max``, which a writer or the Curator accepts or rejects. Nothing is written to a graph.

``eval`` measures the model on hidden edges of a project's graph and of a sample of the bench's tune graphs, and
writes a summary ``kg bench show`` checks (``evo_agents.kg.bench.links_eval``).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

DEFAULT_MAX = 50  # proposals sent to the hub in one call


def register(ksub) -> None:
    top = ksub.add_parser("links", help="edges the graph may lack, guessed by a graph model and only proposed")
    sub = top.add_subparsers(dest="links_command", required=True)

    prop = sub.add_parser("propose", help="score candidate edges of a project's graph and list or send proposals")
    prop.add_argument("--project", help="project (default: from the cwd)")
    prop.add_argument("--review", type=Path, help="the review queue as `evo-agents hub kg review --json` prints it")
    prop.add_argument("--hub", action="store_true", help="read the review queue from the hub and send the proposals")
    prop.add_argument("--max", type=int, default=DEFAULT_MAX, help="most proposals sent (default %(default)s)")
    prop.add_argument("--docs", action="store_true", help="also score documents against code files no name ties")
    prop.add_argument("--dry-run", action="store_true", help="with --hub: read the queue but send nothing")
    prop.add_argument("--out", type=Path, help="also write the proposals and the evaluation as JSON")
    prop.add_argument("--json", action="store_true", help="machine-readable output")
    _model_args(prop)
    prop.set_defaults(func=cmd_propose)

    ev = sub.add_parser("eval", help="precision and recall on hidden edges of a project's and the bench's graphs")
    ev.add_argument("--set", dest="local", action="append", default=[], metavar="NAME=PROJECT", help="a local graph")
    ev.add_argument("--bench", metavar="SET,...", default="", help="bench sets whose tune graphs are sampled")
    ev.add_argument("--graphs", type=int, default=4, help="tune graphs per bench set (default %(default)s)")
    ev.add_argument("--cache", help="bench cache, outside any repo")
    ev.add_argument("--out", type=Path, required=True)
    _model_args(ev)
    ev.set_defaults(func=cmd_eval)


def _model_args(parser) -> None:
    from evo_agents.kg import links

    parser.add_argument(
        "--model", choices=links.MODELS, default=links.MODELS[0], help="graph model (default %(default)s)"
    )
    parser.add_argument("--dim", type=int, default=links.DIM, help="embedding size (default %(default)s)")
    parser.add_argument("--epochs", type=int, default=links.EPOCHS, help="training epochs (default %(default)s)")
    parser.add_argument(
        "--precision", type=float, default=links.PRECISION, help="precision the threshold is chosen for"
    )
    parser.add_argument("--seed", type=int, default=links.SEED)
    parser.add_argument("--minutes", type=float, help="stop training a graph after this long")


def _options(args) -> dict:
    out = {"model": args.model, "dim": args.dim, "epochs": args.epochs, "seed": args.seed, "precision": args.precision}
    if args.minutes:
        out["deadline"] = time.monotonic() + args.minutes * 60
    return out


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _fail(text: str) -> int:
    print(f"evo-agents kg links: {text}", file=sys.stderr)
    return 1


def _open(project_name: str | None):
    from evo_agents.kg.project import ProjectError, resolve_project
    from evo_agents.kg.store import Store

    try:
        project = resolve_project(project_name)
    except ProjectError as exc:
        raise LookupError(str(exc)) from None
    store = Store.open_readonly(project)
    if store is None:
        raise LookupError(f"project {project.name} has no graph on this machine: run `evo-agents kg build` first")
    b = store.latest_ready()
    if b is None:
        store.close()
        raise LookupError(f"project {project.name} has no finished build")
    return project, store, b


def cmd_propose(args) -> int:
    from evo_agents.kg import links

    try:
        project, store, b = _open(args.project)
    except LookupError as exc:
        return _fail(str(exc))
    try:
        g = links.read(store, b)
        stored = store.review_items(b)
    finally:
        store.close()
    review = stored
    if args.review is not None:
        review = json.loads(args.review.read_text(encoding="utf-8")).get("items") or []
    elif args.hub:
        from evo_agents.hub.kg_links import read_review

        review = read_review(project.name)
    _say(f"{project.name}: {len(g.ids)} nodes, {len(g.parsed)} parsed edges; training {args.model}")
    try:
        model, numbers = links.evaluate(g, **_options(args))
    except links.LinksError as exc:
        return _fail(str(exc))
    threshold = numbers["threshold"]
    _say(
        f"hidden edges: precision {numbers['precision']}, recall {numbers['recall']} at threshold {threshold}"
        f" (auc {numbers['auc']}, {numbers['fit']['seconds']}s)"
    )
    if threshold is None:
        return _fail(f"no threshold reaches precision {args.precision} on hidden edges: nothing is proposed")
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
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    by_why: dict[str, int] = {}
    for p in proposed:
        by_why[p["why"]] = by_why.get(p["why"], 0) + 1
    print(f"{len(found)} candidates, {len(proposed)} proposals " + json.dumps(by_why, sort_keys=True))
    for p in proposed[:20]:
        print(f"{p['probability']:.2f}  {p['why']:<9} {p['src']} -{p['rel']}-> {p['dst']}")
    if args.hub:
        made = [s for s in sent if s.get("number")]
        print(
            f"sent {len(made)} of {len(sent)} to the hub as proposals"
            + (" (dry run)" if args.dry_run else "")
            + "; a writer or the Curator accepts or rejects each"
        )
        for s in sent:
            if s.get("error"):
                print(f"  not sent: {s['src']} -{s['rel']}-> {s['dst']}: {s['error']}")
    return 0


def cmd_eval(args) -> int:
    from evo_agents.kg import links
    from evo_agents.kg.bench import datasets, links_eval
    from evo_agents.kg.bench.datasets import BenchError

    options = _options(args)
    sets: dict[str, list[tuple[str, dict]]] = {}
    try:
        for entry in args.local:
            name, _, project_name = entry.partition("=")
            project, store, b = _open(project_name or name)
            store.close()
            _say(f"{name}: the latest build of project {project.name}")
            sets[name] = [(project.name, links_eval.graph_numbers(project.root / "graph.sqlite", options, _say))]
        benches = [s for s in args.bench.split(",") if s]
        cache = datasets.cache_dir(args.cache) if benches else None
        for bench in benches:
            found = links_eval.tune_graphs(cache, bench, args.graphs, args.seed)
            if not found:
                return _fail(f"{bench}: no tune graph is built in {cache}: `kg bench build {bench} --part tune`")
            _say(f"{bench}: {len(found)} tune graphs")
            sets[bench] = [(name, links_eval.graph_numbers(path, options, _say)) for name, path in found]
    except (LookupError, BenchError, links.LinksError) as exc:
        return _fail(str(exc))
    out = links_eval.summary(sets, options, args.seed, args.graphs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    from evo_agents.kg.bench import compare

    print("\n".join(compare.show_lines(out)))
    print(f"summary written to {args.out}")
    return 0
