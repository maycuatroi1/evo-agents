"""Graph subcommands: build, query, serve. ``query`` and ``serve`` read the local store or the hub (``--backend``)."""

from __future__ import annotations

import json
import sys


def cmd_build(args) -> int:
    from evo_agents.kg.build import build_project
    from evo_agents.kg.cli import _print_json, _project
    from evo_agents.kg.status import code_line

    project = _project(args)
    report = build_project(project, verify=args.verify, cold=args.cold)
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


def register(ksub, with_project) -> None:
    build = with_project(ksub.add_parser("build", help="build the graph from the corpus"))
    build.add_argument("--verify", action="store_true", help="rebuild into an empty store and compare (P1)")
    build.add_argument("--cold", action="store_true", help="with --verify, also start from an empty memo")
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
