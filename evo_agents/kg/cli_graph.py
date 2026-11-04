"""Graph subcommands: build, query, serve, eval and eval-questions. ``query``, ``serve`` and ``eval`` read the local
store or the hub (``--backend``)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
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


def cmd_profile(args) -> int:
    """Print the data profile of the project's newest graph (``evo_agents.kg.profile``)."""
    from evo_agents.kg import profile as kg_profile
    from evo_agents.kg.cli import _print_json, _project
    from evo_agents.kg.store import StaleSchema, Store

    project = _project(args)
    try:
        store = Store.open_readonly(project)
    except StaleSchema as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if store is None:
        print(f"error: project {project.name} has no graph yet: run evo-agents kg build", file=sys.stderr)
        return 1
    try:
        summary = store.summary()
    finally:
        store.close()
    found = kg_profile.of_coverage(summary.get("coverage")) if summary.get("ready") else None
    if found is None:
        print("error: the newest build holds no profile: run evo-agents kg build --force", file=sys.stderr)
        return 1
    if args.json:
        _print_json({"project": project.name, "build_id": summary["build_id"], "profile": found})
    else:
        print(f"project {project.name}, build {summary['build_id']}")
        print(kg_profile.render(found))
    return 0


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
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            held = semantic.info(db)
        finally:
            db.close()
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


def cmd_settings(args) -> int:
    """Show the project's KG settings, set some (``--set KEY=VALUE``, a reason with ``--reason``) or put back the last
    change (``--undo``). A key that needs a build says so."""
    from evo_agents.kg import settings as kg_settings
    from evo_agents.kg.cli import _print_json, _project

    project = _project(args)
    try:
        before = kg_settings.read(project.root)
        after = before
        if args.undo:
            after = kg_settings.undo(project.root)
            if after is None:
                raise kg_settings.SettingsError("nothing to undo: the settings were never changed")
        elif args.set:
            changes = {}
            for item in args.set:
                key, sep, text = item.partition("=")
                if not sep:
                    raise kg_settings.SettingsError(f"--set {item!r}: write KEY=VALUE")
                changes[key.strip()] = kg_settings.parse_value(key.strip(), text)
            after = kg_settings.write(
                project.root, kg_settings.of(changes, before), reason=args.reason, figures=_figures(args.figures)
            )
    except (kg_settings.SettingsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = {
        "project": project.name,
        "settings": after.to_json(),
        "changed": kg_settings.diff(before, after),
        "needs_rebuild": kg_settings.rebuild_keys(before, after),
    }
    if args.json:
        _print_json(out)
    else:
        for key, value in after.to_json().items():
            mark = "" if value == getattr(kg_settings.DEFAULT, key) else "  (set)"
            print(f"{key} = {value}{mark}")
        if out["needs_rebuild"]:
            print(f"takes effect with the next build: {', '.join(out['needs_rebuild'])}")
    return 0


def _figures(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        found = json.loads(text)
    except ValueError:
        raise ValueError("--figures must be a json object") from None
    if not isinstance(found, dict):
        raise ValueError("--figures must be a json object")
    return found


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
        line_budget=args.line_budget,
        acc=args.acc,
        scope=args.scope,
    )
    report.settings.update(backend=args.backend, sink=args.sink, search=args.search, scope=args.scope)
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


def cmd_commit_questions(args) -> int:
    """``kg eval-questions --from-commits``: the starter set out of the commits the local graph holds."""
    from evo_agents.kg import commit_labels, eval_commits
    from evo_agents.kg.cli import _project
    from evo_agents.kg.store import StaleSchema, Store

    project = _project(args)
    try:
        store = Store.open_readonly(project)
    except StaleSchema as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if store is None or store.latest_ready() is None:
        print(f"error: project {project.name} has no graph yet: run evo-agents kg build", file=sys.stderr)
        return 1
    try:
        found = eval_commits.commit_questions(
            store,
            min_words=args.min_words,
            max_files=args.max_files or commit_labels.MAX_FILES,
            limit=args.limit,
        )
    finally:
        store.close()
    for question in found.questions:
        sys.stdout.write(json.dumps(question, ensure_ascii=False) + "\n")
    print(
        f"{len(found.questions)} question(s) from {found.commits} commit(s) of {project.name}",
        file=sys.stderr,
    )
    for why, count in sorted(found.left_out.items()):
        print(f"  left out {count}: {why}", file=sys.stderr)
    return 0 if found.questions else 1


def cmd_eval_questions(args) -> int:
    if args.from_sessions:
        return cmd_session_questions(args)
    if args.from_commits:
        return cmd_commit_questions(args)
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
        help=f"leave out a step (default {ev.DEFAULT_MAX_FILES}) or, with --from-commits, a commit (default 8) that"
        " changes more files",
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
        "--from-commits",
        action="store_true",
        help="questions from the commits of the graph's git history: a commit's message asks, the files it changed"
        " that the graph holds answer; needs no session and no plan",
    )
    build.add_argument(
        "--min-words",
        type=_bounded(1, 100),
        default=4,
        help="with --from-commits: leave out a commit whose message has fewer words (default 4)",
    )
    build.add_argument(
        "--limit",
        type=_bounded(1, 5000),
        default=300,
        help="with --from-commits: most questions (default 300, the same ones every time)",
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
    build = with_project(ksub.add_parser("build", help="build the graph from the corpus"))
    build.add_argument("--verify", action="store_true", help="rebuild into an empty store and compare (P1)")
    build.add_argument("--cold", action="store_true", help="with --verify, also start from an empty memo")
    build.add_argument(
        "--force", action="store_true", help="build even when the corpus and the config are those of the latest build"
    )
    build.set_defaults(func=cmd_build)

    profile = with_project(
        ksub.add_parser("profile", help="the data profile of the graph: what the corpus is made of, counts and shapes")
    )
    profile.set_defaults(func=cmd_profile)

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

    st = with_project(
        ksub.add_parser("settings", help="show, set (--set KEY=VALUE) or undo the project's KG settings (kg.settings)")
    )
    st.add_argument("--set", action="append", metavar="KEY=VALUE", help="set a key (repeatable)")
    st.add_argument("--reason", help="why, kept with the change")
    st.add_argument("--figures", help="json object of the figures that justify the change, kept with it")
    st.add_argument("--undo", action="store_true", help="put back what the last change replaced")
    st.set_defaults(func=cmd_settings)
