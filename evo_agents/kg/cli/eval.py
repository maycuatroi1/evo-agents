"""``evo-agents kg eval``: hit@5, Recall@k, MRR and tokens per hit of kg_search, kg_context and grep on a question
set, the graph read on the hub (``evo_agents.kg.evaluate``, docs/kg-eval.md)."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.kg import evaluate as ev
from evo_agents.kg.cli.base import WITH_PROJECT, Failed, KgCommand, KgContext, bounded, repo_overrides, shown


class KgEval(KgCommand):
    name = "eval"
    help = "hit@5, Recall@k, MRR and tokens per hit of kg_search, kg_context and grep on a question set"
    arguments = (
        *WITH_PROJECT,
        Arg("questions", help="JSONL file, one question a line (docs/kg-eval.md)"),
        Arg(
            "--split",
            choices=[ev.ALL_TYPES, *ev.SPLITS],
            default=ev.ALL_TYPES,
            help="only the questions of this part of the set, by the hash of their id (default all; tune ranking on tune)",
        ),
        Arg("--method", action="append", choices=ev.METHODS, help="only this method (repeatable)"),
        Arg("--type", action="append", help="only questions of this type (repeatable)"),
        Arg(
            "--limit",
            type=bounded(1, ev.MAX_LIMIT),
            default=ev.DEFAULT_LIMIT,
            help=f"kg_search's limit (default {ev.DEFAULT_LIMIT})",
        ),
        Arg(
            "--budget-tokens",
            type=bounded(ev.MIN_BUDGET_TOKENS, ev.MAX_BUDGET_TOKENS),
            default=ev.DEFAULT_BUDGET_TOKENS,
            help=f"kg_context's budget and where grep's output is cut (default {ev.DEFAULT_BUDGET_TOKENS})",
        ),
        Arg(
            "--line-budget",
            type=bounded(1, 100_000),
            default=ev.LINE_BUDGET,
            help=f"lines a method may return and be scored on, for questions with `lines` (default {ev.LINE_BUDGET})",
        ),
        Arg("--acc", action="store_true", help="also print Acc@k by file and by function when the set has no `lines`"),
        Arg(
            "--repo",
            action="append",
            metavar="NAME=PATH",
            help="checkout of repo NAME for grep (default: the harness's)",
        ),
        Arg("--grep-ref", metavar="REF", help="grep the tree of REF (e.g. origin/main), not the working tree"),
        Arg("--sink", default="cli", help="sink whose clearance applies (default: cli)"),
        Arg(
            "--backend",
            choices=["auto", "hub"],
            default="hub",
            help="where the graph is read: the hub; auto is the same, kept for old scripts",
        ),
        Arg(
            "--scope",
            choices=["all", "repo"],
            default="all",
            help="what kg_search ranks: the whole graph (default), or only the question's repo, as an agent inside it",
        ),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_remote import open_session

        args = ctx.args
        try:
            overrides = repo_overrides(args.repo)
            question_set = ev.load_set(args.questions)
        except ValueError as exc:  # QuestionError is one
            raise Failed(str(exc), 2) from exc
        questions = question_set.part(args.split)
        if not questions:
            raise Failed(f"no question of part {args.split}", 2)
        if args.type:
            questions = [q for q in questions if q.type in args.type]
            if not questions:
                raise Failed(f"no question of type {', '.join(args.type)}", 2)
        report = ev.evaluate(
            questions,
            session_for=lambda project: open_session(args.backend, project or args.project, args.sink),
            checkouts_for=lambda project: ev.project_checkouts(project or args.project, overrides),
            methods=[m for m in ev.METHODS if m in args.method] if args.method else ev.METHODS,
            limit=args.limit,
            budget_tokens=args.budget_tokens,
            grep_ref=args.grep_ref,
            question_set=question_set,
            split=args.split,
            line_budget=args.line_budget,
            acc=args.acc,
            scope=args.scope,
        )
        report.settings.update(backend=args.backend, sink=args.sink, scope=args.scope)
        # A method that failed on every question it ran says the set or the backend is wrong, not the ranking.
        broken = [r for r in report.rows if r["type"] == "all" and r["n"] and r["errors"] == r["n"]]
        text = Text() if args.json else Text(ev.render(report))
        return shown(ctx, report.to_json() if args.json else None, text, 1 if broken else 0)


EVAL = KgEval()
