"""`evo-agents kg rank`: train the learned order of kg_search's first results (evo_agents.kg.rank, docs/kg-bench.md)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def register(ksub) -> None:
    rank = ksub.add_parser("rank", help="the learned order of kg_search's first results (docs/kg-bench.md, section M1)")
    sub = rank.add_subparsers(dest="rank_command", required=True)

    train = sub.add_parser(
        "train",
        help="train the LambdaMART model on the bench's tune part, its repos' history, plan steps and clicks",
    )
    train.add_argument("--cache", help="bench cache, outside any repo")
    train.add_argument(
        "--set",
        dest="sets",
        action="append",
        metavar="NAME",
        help="bench sets of the labels (default: the tuned three)",
    )
    train.add_argument("--groups", type=Path, help="the rows of the labels, JSON lines: read when there, else written")
    train.add_argument("--no-git", action="store_true", help="no labels from the tune repos' history")
    train.add_argument("--per-graph", type=int, help="commits taken per tune graph (default 40)")
    train.add_argument("--min-words", type=int, help="fewest words of a commit message that is a label (default 4)")
    train.add_argument("--max-files", type=int, help="most files of a commit that is a label (default 8)")
    train.add_argument("--plan-questions", type=Path, help="a `kg eval-questions` set: its tune questions are labels")
    train.add_argument("--clicks", type=Path, help="a project's kg_search labels of the hub, JSON lines")
    train.add_argument("--project", help="project of --plan-questions and --clicks (default: from the cwd)")
    train.add_argument(
        "--rounds", default="100,200,300,600", help="numbers of trees cross-validated (default %(default)s)"
    )
    train.add_argument("--folds", type=int, default=5, help="folds over repos (default %(default)s)")
    train.add_argument("--out", type=Path, help="where the model goes (default: the package's kg/rank_model.txt)")
    train.add_argument("--report", type=Path, help="also write the cross-validation and feature importance as JSON")
    train.set_defaults(func=cmd_train)


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def cmd_train(args) -> int:
    from evo_agents.kg import rank
    from evo_agents.kg.bench import datasets, rank_train
    from evo_agents.kg.bench.tasks import load_tasks

    try:
        import lightgbm  # noqa: F401
    except ImportError:
        print("kg rank train needs LightGBM: pip install 'evo-ak[rank]'", file=sys.stderr)
        return 2
    try:
        cache = datasets.cache_dir(args.cache)
    except datasets.BenchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sets = tuple(args.sets or rank_train.TUNED_SETS)
    groups_path = args.groups or cache / "rank" / "groups.jsonl"
    if not groups_path.exists():
        labels = rank_train.bench_labels(cache, sets)
        if not args.no_git:
            tasks = [t for bench in sets for t in load_tasks(cache, bench)[0]]
            labels += rank_train.git_labels(
                cache,
                tasks,
                per_graph=args.per_graph or rank_train.PER_GRAPH,
                min_words=args.min_words or rank_train.MIN_WORDS,
                max_files=args.max_files or rank_train.MAX_FILES,
            )
        project = args.project
        if (args.plan_questions or args.clicks) and not project:
            from evo_agents.kg.project import resolve_project

            project = resolve_project(None).name
        if args.plan_questions:
            labels += rank_train.plan_labels(args.plan_questions, project)
        if args.clicks:
            labels += rank_train.click_labels(args.clicks, project)
        labels = rank_train.tune_only(labels)
        counts: dict[str, int] = {}
        for label in labels:
            counts[label.source] = counts.get(label.source, 0) + 1
        _say(f"labels: {json.dumps(counts, sort_keys=True)}")
        groups_path.parent.mkdir(parents=True, exist_ok=True)
        stats = rank_train.collect(cache, labels, groups_path, progress=_say)
        _say(f"rows: {json.dumps(dict(sorted(stats.items())))}")
    groups = rank_train.read_groups(groups_path)
    rounds = sorted({int(r) for r in str(args.rounds).split(",") if r.strip()})
    cv = rank_train.evaluate_cv(groups, rounds, k=args.folds)
    best = rank_train.best_rounds(cv, rounds)
    booster = rank_train.fit(groups, best)
    out = args.out or rank.MODEL_FILE
    size = rank_train.save(booster, out)
    found = rank_train.importance(booster)
    report = {"groups": len(groups), "rounds": best, "bytes": size, "cv": cv, "importance": found}
    if args.report:
        args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for name, by in cv.items():
        base, mine = by["base"], by[str(best)]
        _say(
            f"{name}: grade {base['grade']:.3f} -> {mine['grade']:.3f}, on lines {base['lines']:.3f} -> "
            f"{mine['lines']:.3f}, in a file {base['file']:.3f} -> {mine['file']:.3f}"
        )
    _say("importance: " + ", ".join(f"{n} {g:.3f}" for n, g, _ in found[:12]))
    print(f"{out}: {best} trees, {size} bytes, {len(groups)} groups")
    return 0
