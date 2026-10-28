"""Graph subcommands: build, query, serve, eval and eval-questions. ``query``, ``serve`` and ``eval`` read the local
store or the hub (``--backend``)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def cmd_build(args) -> int:
    from evo_agents.kg.build import build_project
    from evo_agents.kg.cli import _print_json, _project
    from evo_agents.kg.status import code_line

    project = _project(args)
    report = build_project(project, verify=args.verify, cold=args.cold, force=args.force)
    if args.json:
        _print_json(report.to_json())
    else:
        print(report.summary_line())
        if report.verify is not None:
            v = report.verify
            state = "match" if v.get("match") else "MISMATCH"
            print(
                f"verify ({'cold memo' if v.get('cold') else 'warm memo'}): {state}"
                f" {v.get('rebuilt', '')[:23]} vs {v.get('expected', '')[:23]}"
            )
        for src in report.coverage.get("sources", []):
            refs = f"  refs {src.get('refs_resolved', 0)}/{src.get('refs', 0)}" if src.get("refs") else ""
            print(
                f"  {src['id']:<22} {src['items']:>6} items  mentions {src['resolved']}/{src['mentions']}"
                f" ({src['resolved_ratio']:.0%}), {src['ambiguous']} ambiguous, {src['dangling']} dangling{refs}"
            )
            if src.get("code"):
                print(f"    {code_line(src['code'])}")
        for warning in report.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
        for err in report.errors[:20]:
            print(f"  error: {err}", file=sys.stderr)
    return 0 if report.ok else 1


def cmd_semantic(args) -> int:
    """Show or set whether the project's graph is searched by meaning too, and with ``--index`` write its vectors."""
    from evo_agents.kg import semantic
    from evo_agents.kg.cli import _print_json, _project

    project = _project(args)
    try:
        settings = semantic.read_settings(project.root)
        if args.mode or args.model:
            settings = semantic.Settings(args.mode or settings.mode, args.model or settings.model)
            semantic.write_settings(project.root, settings)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    path = project.root / "graph.sqlite"
    indexed = None
    if args.index and path.exists():

        def progress(done: int, total: int) -> None:
            if not args.json:
                print(f"  embedded {done}/{total}", file=sys.stderr)

        indexed = semantic.index_file(path, settings, progress=progress)
    held = None
    if path.exists():
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
            held = semantic.info(db)
    out = {
        **settings.to_json(),
        "graph": None if held is None else {"model": held.model, "mode": held.mode, "digest": held.digest},
        "indexed": None
        if indexed is None
        else {"rows": indexed.rows, "embedded": indexed.embedded, "error": indexed.error},
    }
    if args.json:
        _print_json(out)
    else:
        print(f"semantic {settings.mode}, model {settings.model}")
        if indexed is not None:
            print(indexed.line())
        print(f"graph: {'no vectors' if held is None else f'vectors of {held.model}, mode {held.mode}'}")
    return 1 if indexed is not None and indexed.error else 0


def cmd_query(args) -> int:
    from evo_agents.kg.cli import _project
    from evo_agents.kg.serve import Session

    if args.backend == "local":
        session = Session(_project(args), sink=args.sink)
    else:
        from evo_agents.hub.kg_cli import open_session

        session = open_session(args.backend, args.project, args.sink)
    params = {}
    for pair in args.arg or []:
        key, _, value = pair.partition("=")
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value
    if args.text is not None:
        first = {"kg_node": "id", "kg_impact": "id", "kg_path": "from", "kg_more": "handle"}
        params.setdefault(first.get(args.tool, "query"), args.text)
    result = session.call(args.tool, params)
    if args.json:
        print(json.dumps(result.get("structuredContent", result), ensure_ascii=False, indent=2))
    else:
        for block in result.get("content", []):
            print(block.get("text", ""))
    return 1 if result.get("isError") else 0


def cmd_serve(args) -> int:
    from evo_agents.hub.kg_cli import open_session
    from evo_agents.kg.serve import serve_stdio

    return serve_stdio(session=open_session(args.backend, args.project, args.sink))


def _repo_overrides(pairs: list[str] | None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for pair in pairs or []:
        name, sep, path = pair.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"--repo takes NAME=PATH, not {pair!r}")
        found[name.strip()] = Path(path.strip()).expanduser().resolve()
    return found


def cmd_eval(args) -> int:
    from evo_agents.hub.kg_cli import open_session
    from evo_agents.kg.evaluate import METHODS, evaluate, load_set, project_checkouts, render

    try:
        overrides = _repo_overrides(args.repo)
        question_set = load_set(args.questions)
    except ValueError as exc:  # QuestionError is one
        print(f"error: {exc}", file=sys.stderr)
        return 2
    questions = question_set.part(args.split)
    if not questions:
        print(f"error: no question of part {args.split}", file=sys.stderr)
        return 2
    if args.type:
        questions = [q for q in questions if q.type in args.type]
        if not questions:
            print(f"error: no question of type {', '.join(args.type)}", file=sys.stderr)
            return 2

    def session_for(project):
        session = open_session(args.backend, project or args.project, args.sink)
        if args.search != "graph":
            if not hasattr(session, "semantic"):
                raise ValueError("--search bm25 or hybrid reads a graph on this machine: use --backend local")
            session.semantic = args.search == "hybrid"
        return session

    report = evaluate(
        questions,
        session_for=session_for,
        checkouts_for=lambda project: project_checkouts(project or args.project, overrides),
        methods=[m for m in METHODS if m in args.method] if args.method else METHODS,
        limit=args.limit,
        budget_tokens=args.budget_tokens,
        grep_ref=args.grep_ref,
        question_set=question_set,
        split=args.split,
    )
    report.settings.update(backend=args.backend, sink=args.sink, search=args.search)
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    # A method that failed on every question it ran says the set or the backend is wrong, not the ranking.
    broken = [r for r in report.rows if r["type"] == "all" and r["n"] and r["errors"] == r["n"]]
    return 1 if broken else 0


def cmd_eval_questions(args) -> int:
    from evo_agents.hub.client import Hub, HubError, load_credentials
    from evo_agents.hub.kg_eval import plan_questions
    from evo_agents.kg.evaluate import project_checkouts
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        overrides = _repo_overrides(args.repo)
        name = resolve_project(args.project).name
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProjectError as exc:
        if not args.project or "/" in args.project:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        name = args.project  # a hub project with no harness on this machine: give its checkouts with --repo
    try:
        credentials = load_credentials()
        questions, skipped = plan_questions(
            Hub(credentials.url, credentials.token),
            name,
            project_checkouts(args.project, overrides),
            plans=args.plan or (),
            text=args.text,
            max_files=args.max_files,
        )
    except HubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for question in questions:
        sys.stdout.write(json.dumps(question, ensure_ascii=False) + "\n")
    print(f"{len(questions)} question(s) from the plans of {name}; {len(skipped)} step(s) left out", file=sys.stderr)
    for why in skipped:
        print(f"  left out {why}", file=sys.stderr)
    return 0


def _bounded(low: int, high: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{value} is not within {low} to {high}")
        return value

    return parse


def _register_eval(ksub, with_project) -> None:
    from evo_agents.kg import evaluate as ev

    run = with_project(
        ksub.add_parser(
            "eval", help="hit@5, Recall@k, MRR and tokens per hit of kg_search, kg_context and grep on a question set"
        )
    )
    run.add_argument("questions", help="JSONL file, one question a line (docs/kg-eval.md)")
    run.add_argument(
        "--split",
        choices=[ev.ALL_TYPES, *ev.SPLITS],
        default=ev.ALL_TYPES,
        help="only the questions of this part of the set, by the hash of their id (default all; tune ranking on tune)",
    )
    run.add_argument("--method", action="append", choices=ev.METHODS, help="only this method (repeatable)")
    run.add_argument("--type", action="append", help="only questions of this type (repeatable)")
    run.add_argument(
        "--limit",
        type=_bounded(1, ev.MAX_LIMIT),
        default=ev.DEFAULT_LIMIT,
        help=f"kg_search's limit (default {ev.DEFAULT_LIMIT})",
    )
    run.add_argument(
        "--budget-tokens",
        type=_bounded(ev.MIN_BUDGET_TOKENS, ev.MAX_BUDGET_TOKENS),
        default=ev.DEFAULT_BUDGET_TOKENS,
        help=f"kg_context's budget and where grep's output is cut (default {ev.DEFAULT_BUDGET_TOKENS})",
    )
    run.add_argument(
        "--repo", action="append", metavar="NAME=PATH", help="checkout of repo NAME for grep (default: the harness's)"
    )
    run.add_argument("--grep-ref", metavar="REF", help="grep the tree of REF (e.g. origin/main), not the working tree")
    run.add_argument("--sink", default="cli", help="sink whose clearance applies (default: cli)")
    run.add_argument(
        "--backend",
        choices=["auto", "local", "hub"],
        default="local",
        help="where the graph is read: this machine's store (default), the hub, or the hub when it has the project",
    )
    run.add_argument(
        "--search",
        choices=["graph", "bm25", "hybrid"],
        default="graph",
        help="how kg_search ranks: as the graph's semantic mode says (default), by words only, or by words and meaning"
        " fused (needs the graph's vectors; docs/kg-semantic.md)",
    )
    run.set_defaults(func=cmd_eval)

    build = ksub.add_parser(
        "eval-questions", help="print a question set for `kg eval` from the done steps of the hub's plans (JSONL)"
    )
    build.add_argument("--project", help="hub project (default: the project of the current directory)")
    build.add_argument("--plan", action="append", help="only this plan (repeatable)")
    build.add_argument("--text", choices=ev.PLAN_TEXTS, default="title", help="what of a step asks (default: title)")
    build.add_argument(
        "--max-files",
        type=_bounded(1, 1000),
        default=ev.DEFAULT_MAX_FILES,
        help=f"leave out a step whose commits change more files (default {ev.DEFAULT_MAX_FILES})",
    )
    build.add_argument(
        "--repo", action="append", metavar="NAME=PATH", help="checkout of repo NAME (default: the harness's)"
    )
    build.set_defaults(func=cmd_eval_questions)


def register(ksub, with_project) -> None:
    build = with_project(ksub.add_parser("build", help="build the graph from the corpus"))
    build.add_argument("--verify", action="store_true", help="rebuild into an empty store and compare (P1)")
    build.add_argument("--cold", action="store_true", help="with --verify, also start from an empty memo")
    build.add_argument(
        "--force", action="store_true", help="build even when the corpus and the config are those of the latest build"
    )
    build.set_defaults(func=cmd_build)

    query = with_project(ksub.add_parser("query", help="call a kg tool from the shell"))
    query.add_argument(
        "tool", choices=["kg_search", "kg_context", "kg_node", "kg_impact", "kg_path", "kg_status", "kg_more"]
    )
    query.add_argument("text", nargs="?", help="query, node id (kg_path: from) or handle")
    query.add_argument("--arg", action="append", help="extra tool argument key=value (JSON values allowed)")
    query.add_argument("--sink", default="cli", help="sink whose clearance applies (default: cli)")
    query.add_argument(
        "--backend",
        choices=["auto", "local", "hub"],
        default="local",
        help="where the graph is read: this machine's store (default), the hub, or the hub when it has the project",
    )
    query.set_defaults(func=cmd_query)

    serve = ksub.add_parser("serve", help="MCP server over stdio, bound to one project")
    serve.add_argument("--project", help="project name or harness path (default: from the session directory)")
    serve.add_argument("--sink", default="claude-code@anthropic", help="sink profile whose clearance applies")
    serve.add_argument(
        "--backend",
        choices=["auto", "local", "hub"],
        default="auto",
        help="auto (default): the hub when signed in and it has a graph of the project, else this machine's store",
    )
    serve.set_defaults(func=cmd_serve)

    _register_eval(ksub, with_project)

    sem = with_project(
        ksub.add_parser("semantic", help="show or set whether kg_search ranks by meaning too (docs/kg-semantic.md)")
    )
    sem.add_argument(
        "--mode", choices=["off", "index", "on"], help="off: words only; index: write vectors; on: use them"
    )
    sem.add_argument("--model", help="embedding model (default BAAI/bge-m3)")
    sem.add_argument("--index", action="store_true", help="write the vectors into the graph now (downloads the model)")
    sem.set_defaults(func=cmd_semantic)
