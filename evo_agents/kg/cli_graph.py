"""Graph subcommands: build, query, serve, profile, semantic, settings, eval and eval-questions. Graphs are built and read
on the hub only: ``build`` asks the hub for a build, the others read the hub's graph and settings, and no command reads
a graph store left on this machine."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def cmd_build(args) -> int:
    """``kg build``: ask the hub for a build of the project's graph and wait until it has finished. Graphs are built on
    the hub only (``evo_agents.hub.kg_hub_sync.HubSync.build``)."""
    from evo_agents.hub.client import HubError
    from evo_agents.hub.kg_hub_sync import BUILD_WAIT
    from evo_agents.kg.cli import print_json, project_of, syncer

    project = project_of(args)
    try:
        build = syncer(project).build(timeout=args.timeout or BUILD_WAIT)
    except HubError as exc:  # signing in, before the build is asked
        print(f"error: the hub: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print_json({"project": project.name, **build.to_json()})
    else:
        print(build.summary_line())
    return 0 if build.ok else 1


def hub_name(project: str | None) -> str:
    """The hub's name of ``project`` (a name, a harness path, or None for the project of the cwd); a bare name no harness
    here knows is taken as the hub's. Raises ProjectError."""
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        return resolve_project(project).name
    except ProjectError:
        if project and "/" not in project and project not in (".", ".."):
            return project
        raise


def hub_status(project: str | None, sink: str | None = None, timeout: float | None = None) -> tuple[str, dict]:
    """(text, status) of kg_status of ``project``'s graph as the hub answers it to ``sink`` (the agent session's by
    default). Raises ProjectError, and HubError when the hub refuses or does not answer within ``timeout``."""
    from urllib.parse import quote

    from evo_agents.hub.client import Hub, HubError, load_credentials

    name = hub_name(project)
    credentials = load_credentials()
    hub = Hub(credentials.url, credentials.token)
    body = {"arguments": {}, **({"sink": sink} if sink else {})}
    found = hub.call("POST", f"/v1/kg/{quote(name, safe='')}/tools/kg_status", body, timeout=timeout) or {}
    if found.get("isError"):
        text = " ".join(block.get("text", "") for block in found.get("content", []))
        raise HubError(text.removeprefix("error: ") or "the hub refused kg_status")
    text = "\n".join(block.get("text", "") for block in found.get("content", []) if block.get("type") == "text")
    return text, found.get("structuredContent") or {"project": name}


def _on_hub(args, command, **extra) -> int:
    """Run the ``evo-agents hub kg`` command ``command`` for the hub's name of ``args.project``."""
    from evo_agents.kg.project import ProjectError

    try:
        name = hub_name(args.project)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return command(argparse.Namespace(project=name, json=args.json, **extra))


def cmd_profile(args) -> int:
    """``kg profile``: the data profile of the project's newest graph on the hub (``evo-agents hub kg profile``)."""
    from evo_agents.hub.kg_profile_cli import cmd_profile as on_hub

    return _on_hub(args, on_hub, sink=args.sink)


def cmd_semantic(args) -> int:
    """``kg semantic``: whether the project's graph on the hub holds vectors (``evo-agents hub kg semantic``); with
    ``--mode`` or ``--model``, set them in the project's settings on the hub first. The hub embeds at its next build."""
    from evo_agents.hub.kg_semantic_cli import cmd_semantic as show
    from evo_agents.hub.kg_settings_cli import cmd_settings

    if args.mode or args.model:
        values = [f"semantic_mode={args.mode}"] * bool(args.mode) + [f"semantic_model={args.model}"] * bool(args.model)
        quiet = argparse.Namespace(project=args.project, json=True)  # the settings it printed would come twice
        code = _on_hub(
            quiet, _quietly(cmd_settings), set=values, undo=False, strategy=None, reason=args.reason, figures=None
        )
        if code:
            return code
    return _on_hub(args, show, sink=args.sink)


def _quietly(command):
    """``command`` with its stdout dropped, and its errors still on stderr."""
    import contextlib
    import io

    def run(args) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return command(args)

    return run


def cmd_settings(args) -> int:
    """``kg settings``: show the project's KG settings on the hub, set some (``--set KEY=VALUE``, a reason with
    ``--reason``) or put back the last change (``--undo``), as ``evo-agents hub kg settings`` does."""
    from evo_agents.hub.kg_settings_cli import cmd_settings as on_hub

    return _on_hub(args, on_hub, set=args.set, undo=args.undo, strategy=None, reason=args.reason, figures=args.figures)


def cmd_init(args) -> int:
    """``kg init``: ask the hub for a kg_init run of the project, in which the Curator configures its graph from its
    data profile (``evo-agents hub kg init``); the run, or the one already in flight."""
    from evo_agents.hub.client import Hub, HubError, load_credentials
    from evo_agents.hub.kg_init_cli import line_of, path_of
    from evo_agents.kg.cli import print_json, project_of

    project = project_of(args)
    try:
        credentials = load_credentials()
        found = Hub(credentials.url, credentials.token).call("POST", path_of(project.name), {})
    except HubError as exc:
        print(f"error: the hub: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print_json(found)
    else:
        print(f"kg init run of project {project.name}: {line_of(found or {})}")
    return 0


def cmd_strategy(args) -> int:
    """List the strategies, show one, or apply one (``--dry-run`` says what it would change)."""
    from evo_agents.kg import strategy as kg_strategy
    from evo_agents.kg.cli import print_json, project_of

    project = None
    if args.action == "apply" or args.project:
        project = project_of(args)
    else:  # list and show work anywhere; a project the cwd belongs to adds its own strategies
        from evo_agents.kg.project import ProjectError, resolve_project

        try:
            project = resolve_project(None)
        except ProjectError:
            pass
    root = project.harness.root if project is not None else None
    try:
        if args.action == "list":
            found = kg_strategy.available(root)
            if args.json:
                print_json([s.to_json() for s in found.values()])
            else:
                print(kg_strategy.render_list(found))
            return 0
        if not args.target:
            raise kg_strategy.StrategyError(f"{args.action} needs a strategy name or file")
        chosen = kg_strategy.find(args.target, root)
        if args.action == "show":
            if args.json:
                print_json(chosen.to_json())
            else:
                print(f"{chosen.name} ({chosen.origin}): {chosen.description}")
                print(yaml_dump(chosen.to_json()))
            return 0
        found = kg_strategy.plan(chosen, project) if args.dry_run else kg_strategy.apply(chosen, project)
    except kg_strategy.StrategyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = {**found.to_json(), "applied": not args.dry_run}
    if args.json:
        print_json(out)
        return 1 if found.problems else 0
    for problem in found.problems:
        print(f"cannot apply: {problem}")
    verb = "would change" if args.dry_run else "changed"
    for key, change in found.changed.items():
        print(f"{verb} {key}: {change['from']} -> {change['to']}")
    if out["adds_identifiers"] or out["adds_ontology"]:
        print(f"{verb} knowledge: identifiers {out['adds_identifiers']}, ontology {out['adds_ontology']}")
    if found.needs_rebuild:
        print(f"takes effect with the next build: {', '.join(found.needs_rebuild)}")
    return 1 if found.problems else 0


def yaml_dump(data) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()


QUERY_TOOLS = ["kg_search", "kg_context", "kg_node", "kg_impact", "kg_path", "kg_status", "kg_more"]


def query_tool(args) -> None:
    """Resolve ``kg query``'s first argument: a kg_ tool, a short name of one (search, node...), or with no second
    argument the text of kg_search, so ``kg query SnapshotCoordinator`` searches; an unknown tool before a text is
    refused as argparse refuses an invalid choice."""
    if args.tool in QUERY_TOOLS:
        return
    if f"kg_{args.tool}" in QUERY_TOOLS:
        args.tool = f"kg_{args.tool}"
    elif args.text is None:
        args.tool, args.text = "kg_search", args.tool
    else:
        choices = ", ".join(map(repr, QUERY_TOOLS))
        args.query_parser.error(f"argument tool: invalid choice: {args.tool!r} (choose from {choices})")


def cmd_query(args) -> int:
    from evo_agents.hub.kg_cli import open_session

    query_tool(args)
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
    if getattr(args, "graph", None):  # a named graph of the project on the hub (docs/kg.md, Named graphs)
        params["graph"] = args.graph
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

    report = evaluate(
        questions,
        session_for=lambda project: open_session(args.backend, project or args.project, args.sink),
        checkouts_for=lambda project: project_checkouts(project or args.project, overrides),
        methods=[m for m in METHODS if m in args.method] if args.method else METHODS,
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
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    # A method that failed on every question it ran says the set or the backend is wrong, not the ranking.
    broken = [r for r in report.rows if r["type"] == "all" and r["n"] and r["errors"] == r["n"]]
    return 1 if broken else 0


def cmd_session_questions(args) -> int:
    from evo_agents.hub.kg_eval_sessions import session_questions
    from evo_agents.hub.transcripts import claude_roots
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        if args.plan:
            raise ValueError("--plan picks plans, which --from-sessions does not read")
        overrides = _repo_overrides(args.repo)
        project = resolve_project(args.project)
    except (ValueError, ProjectError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    roots = [Path(path).expanduser() for path in args.transcripts] if args.transcripts else claude_roots()
    found = session_questions(project, roots, since=time.time() - args.days * 86400, checkouts=overrides)
    for question in found.questions:
        sys.stdout.write(json.dumps(question, ensure_ascii=False) + "\n")
    print(
        f"{len(found.questions)} question(s) from {found.transcripts} transcript(s) of {project.name} in the last"
        f" {args.days} day(s); of {found.searches} search(es) of its repos, {found.no_identifier} named no identifier"
        f" and {found.unanswered} opened no file of the repo after",
        file=sys.stderr,
    )
    for repo, count in found.per_repo():
        print(f"  {repo}: {count}", file=sys.stderr)
    return 0


def cmd_eval_questions(args) -> int:
    if args.from_sessions:
        return cmd_session_questions(args)
    from evo_agents.hub.client import Hub, HubError, load_credentials
    from evo_agents.hub.kg_eval import plan_questions
    from evo_agents.kg.evaluate import DEFAULT_MAX_FILES, project_checkouts
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
            max_files=args.max_files or DEFAULT_MAX_FILES,
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
        "--line-budget",
        type=_bounded(1, 100_000),
        default=ev.LINE_BUDGET,
        help=f"lines a method may return and be scored on, for questions with `lines` (default {ev.LINE_BUDGET})",
    )
    run.add_argument(
        "--acc", action="store_true", help="also print Acc@k by file and by function when the set has no `lines`"
    )
    run.add_argument(
        "--repo", action="append", metavar="NAME=PATH", help="checkout of repo NAME for grep (default: the harness's)"
    )
    run.add_argument("--grep-ref", metavar="REF", help="grep the tree of REF (e.g. origin/main), not the working tree")
    run.add_argument("--sink", default="cli", help="sink whose clearance applies (default: cli)")
    run.add_argument(
        "--backend",
        choices=["auto", "hub"],
        default="hub",
        help="where the graph is read: the hub; auto is the same, kept for old scripts",
    )
    run.add_argument(
        "--scope",
        choices=["all", "repo"],
        default="all",
        help="what kg_search ranks: the whole graph (default), or only the question's repo, as an agent inside it",
    )
    run.set_defaults(func=cmd_eval)

    build = ksub.add_parser(
        "eval-questions",
        help="print a question set for `kg eval` from the done steps of the hub's plans, or from how agents searched"
        " the code in this harness's sessions (JSONL)",
    )
    build.add_argument("--project", help="hub project (default: the project of the current directory)")
    build.add_argument("--plan", action="append", help="only this plan (repeatable)")
    build.add_argument("--text", choices=ev.PLAN_TEXTS, default="title", help="what of a step asks (default: title)")
    build.add_argument(
        "--max-files",
        type=_bounded(1, 1000),
        help=f"leave out a step that changes more files (default {ev.DEFAULT_MAX_FILES})",
    )
    build.add_argument(
        "--repo", action="append", metavar="NAME=PATH", help="checkout of repo NAME (default: the harness's)"
    )
    build.add_argument(
        "--from-sessions",
        action="store_true",
        help="questions from the code searches in the Claude Code transcripts of the harness, not from the plans",
    )
    build.add_argument(
        "--days",
        type=_bounded(1, 3650),
        default=ev.DEFAULT_SESSION_DAYS,
        help=f"with --from-sessions: tool calls of the last N days (default {ev.DEFAULT_SESSION_DAYS})",
    )
    build.add_argument(
        "--transcripts",
        action="append",
        metavar="DIR",
        help="with --from-sessions: a Claude Code projects directory to read (repeatable; default: this machine's)",
    )
    build.set_defaults(func=cmd_eval_questions)


def register(ksub, with_project) -> None:
    build = with_project(
        ksub.add_parser(
            "build", help="ask the hub for a build of the project's graph and wait for it (the writer role)"
        )
    )
    build.add_argument("--timeout", type=float, help="seconds to wait at most (default: 1800)")
    build.set_defaults(func=cmd_build)

    profile = with_project(
        ksub.add_parser("profile", help="the data profile of the graph: what the corpus is made of, counts and shapes")
    )
    profile.add_argument("--sink", help="sink whose clearance applies (default: the agent session's)")
    profile.set_defaults(func=cmd_profile)

    query = with_project(ksub.add_parser("query", help="call a kg tool from the shell"))
    query.add_argument(
        "tool",
        metavar="tool",
        help=f"one of {', '.join(QUERY_TOOLS)}, or its short name (search, node...); "
        "alone, any other word is the text of kg_search",
    )
    query.add_argument("text", nargs="?", help="query, node id (kg_path: from) or handle")
    query.add_argument("--arg", action="append", help="extra tool argument key=value (JSON values allowed)")
    query.add_argument("--sink", default="cli", help="sink whose clearance applies (default: cli)")
    query.add_argument(
        "--graph",
        metavar="NAME",
        help="only the sources of this named graph of the project (`hub kg graph list`; needs the hub); "
        "default: every source",
    )
    query.add_argument(
        "--backend",
        choices=["auto", "hub"],
        default="hub",
        help="where the graph is read: the hub; auto is the same, kept for old scripts",
    )
    query.set_defaults(func=cmd_query, query_parser=query)

    serve = ksub.add_parser("serve", help="MCP server over stdio, bound to one project")
    serve.add_argument("--project", help="project name or harness path (default: from the session directory)")
    serve.add_argument("--sink", default="claude-code@anthropic", help="sink profile whose clearance applies")
    serve.add_argument(
        "--backend",
        choices=["auto", "hub"],
        default="auto",
        help="where the graph is read: the hub; auto (default) is the same, kept for the .mcp.json files that name it",
    )
    serve.set_defaults(func=cmd_serve)

    _register_eval(ksub, with_project)

    sem = with_project(
        ksub.add_parser(
            "semantic",
            help="show or set whether kg_search on the hub ranks by meaning too (docs/kg-semantic.md)",
        )
    )
    sem.add_argument(
        "--mode",
        choices=["off", "index", "on"],
        help="off: words only; index: the hub's builds write vectors; on: and kg_search uses them",
    )
    sem.add_argument("--model", help="embedding model the hub's builds use (default BAAI/bge-m3)")
    sem.add_argument("--reason", help="why, kept with the change of the settings")
    sem.add_argument("--sink", help="sink whose clearance applies (default: the agent session's)")
    sem.set_defaults(func=cmd_semantic)

    st = with_project(
        ksub.add_parser("settings", help="show, set (--set KEY=VALUE) or undo the project's KG settings on the hub")
    )
    st.add_argument("--set", action="append", metavar="KEY=VALUE", help="set a key (repeatable)")
    st.add_argument("--reason", help="why, kept with the change")
    st.add_argument("--figures", help="json object of the figures that justify the change, kept with it")
    st.add_argument("--undo", action="store_true", help="put back what the last change replaced")
    st.set_defaults(func=cmd_settings)

    ini = with_project(
        ksub.add_parser("init", help="ask the hub's Curator to configure the project's graph from its data profile")
    )
    ini.set_defaults(func=cmd_init)

    sg = with_project(
        ksub.add_parser("strategy", help="list, show or apply a KG strategy for a kind of data (docs/kg-strategies.md)")
    )
    sg.add_argument("action", choices=["list", "show", "apply"])
    sg.add_argument("target", nargs="?", help="a strategy name, or a strategy file (apply, show)")
    sg.add_argument("--dry-run", action="store_true", help="apply: say what would change and change nothing")
    sg.set_defaults(func=cmd_strategy)
