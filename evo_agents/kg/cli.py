"""``evo-agents kg`` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evo_agents.kg.project import ProjectError, bind, read_bindings, resolve_project, unbind


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _project(args):
    try:
        return resolve_project(getattr(args, "project", None))
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _print_runs(results) -> None:
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        print(
            f"{mark} {r.source:<20} {r.items} item(s), {r.tombstones} tombstone(s), {r.removals} removed, "
            f"{r.errors} error(s), {r.rejected} rejected, {r.seconds}s"
        )
        for h in r.held:
            print(f"     held: {h['count']} removal(s) in {h['scope']}: {h['reason']}")
            print("           review, then run: evo-agents kg sync --accept-removals --source " + r.source)
        for issue in r.issues[:8]:
            print(f"     {issue}")
        if r.exception:
            print(f"     connector raised {r.exception}")
        for line in r.stderr_tail[-5:]:
            print(f"     stderr: {line}")


def _print_due(outcome) -> None:
    # Everything goes to stdout: under launchd both streams share schedule.log, and stdout is buffered.
    if outcome.error:
        print(f"FAIL project {outcome.project}: {outcome.error}")
        return
    if not outcome.due:
        print(f"project {outcome.project}: nothing due")
        return
    print(f"project {outcome.project}: {len(outcome.due)} source(s) due")
    _print_runs(outcome.runs)
    if outcome.build is not None:
        print(outcome.build.summary_line())
        for warning in outcome.build.warnings:
            print(f"warning: {warning}")


def _push(project=None) -> tuple[list, str | None]:
    """``--push``: send the runs the hub lacks, of ``project``, or without one of every project of projects.json the
    hub takes a push for. The push reports, and the error that stopped the push before any report, if one did."""
    from evo_agents.hub.client import Hub, HubError, load_credentials
    from evo_agents.hub.kg_cli import push
    from evo_agents.hub.kg_push import push_project

    try:
        credentials = load_credentials()
        hub = Hub(credentials.url, credentials.token)
        return ([push_project(hub, project)] if project is not None else push(hub, None, True)), None
    except HubError as exc:
        return [], str(exc)


def _push_json(reports: list, error: str | None) -> dict:
    return {
        "ok": error is None and all(r.ok for r in reports),
        "error": error,
        "projects": [r.to_json() for r in reports],
    }


def _print_push(reports: list, error: str | None) -> None:
    # stdout, like the rest of a scheduled run's output
    if error:
        print(f"FAIL push: {error}")
    for report in reports:
        print(("push " if report.ok else "FAIL push ") + report.summary_line())
        for line in report.errors:
            print(f"     {line}")


def _cmd_sync_due(args) -> int:
    from evo_agents.kg.ids import utc_now
    from evo_agents.kg.sync import sync_all_due, sync_due

    if args.source:
        print("error: --due picks the sources itself; drop --source", file=sys.stderr)
        return 2
    if args.all:
        if args.project or args.accept_removals:
            print("error: --all goes with --due, --build, --push and --json only", file=sys.stderr)
            return 2
        outcomes = sync_all_due(build=args.build)
        ok = all(o.ok for o in outcomes)
        pushed = _push() if args.push else None
        if pushed is not None:
            ok = ok and _push_json(*pushed)["ok"]
        if args.json:
            body = {"ok": ok, "projects": [o.to_json() for o in outcomes]}
            if pushed is not None:
                body["push"] = _push_json(*pushed)
            _print_json(body)
        else:
            print(f"kg sync --due --all at {utc_now()}: {len(outcomes)} project(s)")
            for outcome in outcomes:
                _print_due(outcome)
            if pushed is not None:
                _print_push(*pushed)
        return 0 if ok else 1

    project = _project(args)
    try:
        outcome = sync_due(project, build=args.build, accept=args.accept_removals)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pushed = _push(project) if args.push else None
    ok = outcome.ok and (pushed is None or _push_json(*pushed)["ok"])
    if args.json:
        body = outcome.to_json()
        if pushed is not None:
            body["push"] = _push_json(*pushed)
        _print_json(body)
    else:
        _print_due(outcome)
        if pushed is not None:
            _print_push(*pushed)
    return 0 if ok else 1


def cmd_sync(args) -> int:
    from evo_agents.kg.sync import sync_project

    if args.all and not args.due:
        print("error: --all needs --due; a full sync of every project is not offered", file=sys.stderr)
        return 2
    if args.due:
        return _cmd_sync_due(args)
    project = _project(args)
    results = sync_project(project, args.source or None, accept=args.accept_removals)
    ok = all(r.ok and not r.held for r in results)
    report = None
    if args.build:
        from evo_agents.kg.build import build_project

        report = build_project(project)
        ok = ok and report.ok
    pushed = _push(project) if args.push else None
    if pushed is not None:
        ok = ok and _push_json(*pushed)["ok"]
    if args.json:
        body = {"project": project.name, "runs": [r.to_json() for r in results]}
        if pushed is not None:
            body["push"] = _push_json(*pushed)
        _print_json(body)
    else:
        _print_runs(results)
        if report is not None:
            print(report.summary_line())
            for warning in report.warnings:
                print(f"warning: {warning}")
        if pushed is not None:
            _print_push(*pushed)
    return 0 if ok else 1


def cmd_status(args) -> int:
    from evo_agents.kg.status import project_status, render_status

    project = _project(args)
    status = project_status(project)
    if args.json:
        _print_json(status)
    else:
        print(render_status(status, brief=args.brief))
    return 0 if status["ok"] else 1


def cmd_bind(args) -> int:
    """Bind a directory to one project explicitly, list the bindings, or remove one. Bindings live in
    bound.json beside the projects.json index and win over the harness and the index."""
    if args.list or args.remove:
        if args.project or args.directory or (args.list and args.remove):
            print("error: --list and --remove DIR take no other arguments", file=sys.stderr)
            return 2
    if args.list:
        bindings = read_bindings()
        if args.json:
            _print_json(bindings)
        elif not bindings:
            print("no bindings")
        else:
            for directory, entry in sorted(bindings.items()):
                print(f"{directory}  ->  {entry.get('project')}  ({entry.get('harness_root')})")
        return 0
    if args.remove:
        removed = unbind(args.remove)
        if removed is None:
            print(f"error: {args.remove} has no binding (see evo-agents kg bind --list)", file=sys.stderr)
            return 1
        if args.json:
            _print_json({"removed": removed})
        else:
            print(f"unbound {removed}")
        return 0
    directory = Path(args.directory or Path.cwd()).expanduser().resolve()
    try:
        project = bind(directory, args.project)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json({"directory": str(directory), "project": project.name, "harness_root": str(project.harness.root)})
    else:
        print(f"bound {directory}  ->  {project.name}  ({project.harness.root})")
    return 0


def cmd_schedule(args) -> int:
    from evo_agents.kg import schedule

    if args.action == "print":
        sys.stdout.write(schedule.render(schedule.build_plist()))
        return 0
    try:
        if args.action == "install":
            path = schedule.install()
            plist = schedule.build_plist()
            print(f"installed {schedule.LABEL} from {path}")
            print(f"runs every {schedule.INTERVAL // 60} min: {' '.join(plist['ProgramArguments'])}")
            print(f"log: {plist['StandardOutPath']}")
        else:
            existed = schedule.uninstall()
            print(f"uninstalled {schedule.LABEL}" if existed else f"{schedule.LABEL} was not installed")
    except schedule.ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _hook_payload() -> dict:
    """The JSON object Claude Code writes to a hook's stdin, or {} when there is none."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cmd_hook_session_start(args) -> int:
    """SessionStart hook for Claude Code: a short note on the bound project, and the session label when the
    session already read from the graph (a resumed session). Only names and counts the pipeline derived;
    never source text, which would reach the model with system-reminder authority."""
    from evo_agents.kg.sessions import current_session_id, describe, read_session
    from evo_agents.kg.status import project_status, render_status

    try:
        project = resolve_project(None)
    except ProjectError:
        return 0  # not inside a project with a graph: say nothing
    note = render_status(project_status(project), brief=True)
    session = read_session(current_session_id(_hook_payload()))
    if session is not None:
        note += "\n" + describe(session)
    note += "\nUse the evo-kg tools (kg_search, kg_context, kg_node) before grepping for named things."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}}))
    return 0


def cmd_hook_post_tool(args) -> int:
    """PostToolUse hook for the evo-kg tools: join the labels of the result into the session label. Prints
    nothing to stdout and never blocks: any error becomes a note on stderr and exit code 0."""
    from evo_agents.kg.sessions import post_tool

    try:
        post_tool(json.loads(sys.stdin.read()))
    except Exception as exc:  # a hook must never break the session
        print(f"evo-kg post-tool hook skipped: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
    return 0


def cmd_hook_pre_search(args) -> int:
    """PreToolUse hook for Grep, Glob and rg or grep in Bash: ids of graph nodes named like the search. Sets
    no permissionDecision, since "allow" would skip the user's permission prompt; prints nothing when unsure."""
    from evo_agents.kg.search_hint import pre_search

    note = pre_search(sys.stdin.read(), only=args.only, sink=args.sink)
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}}))
    return 0


def cmd_connector_test(args) -> int:
    from evo_agents.kg.protocol.conformance import run_conformance
    from evo_agents.kg.protocol.runner import ConnectorError, credential_env

    harness = None
    env: dict = {}
    if args.command:
        command = args.command[1:] if args.command[0] == "--" else args.command
        source = {
            "id": args.id,
            "connector": "exec",
            "command": command,
            "config": json.loads(args.config) if args.config else {},
        }
    elif args.builtin:
        source = {"id": args.id, "connector": args.builtin, **(json.loads(args.config) if args.config else {})}
    elif args.source:
        project = _project(args)
        harness = project.harness
        source = project.source(args.source)
        try:
            env = credential_env(source)
        except ConnectorError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        print("error: give a command after --, --builtin NAME, or --source ID", file=sys.stderr)
        return 2
    if args.label:
        source["label"] = {"level": args.label}
    report = run_conformance(
        source,
        harness=harness,
        env=env,
        golden=Path(args.golden) if args.golden else None,
        update_golden=args.update_golden,
    )
    if args.json:
        _print_json(report.to_json())
    else:
        for check in report.checks:
            print(f"{'PASS' if check.ok else 'FAIL'} {check.name}")
            for line in check.details:
                print(f"     {line}")
    return 0 if report.ok else 1


def register(sub) -> None:
    kg = sub.add_parser("kg", help="multi-source knowledge graph")
    ksub = kg.add_subparsers(dest="command_name", required=True)

    def with_project(p):
        p.add_argument("--project", help="project name or harness path (default: from the cwd)")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    sync = with_project(ksub.add_parser("sync", help="run connectors into the corpus"))
    sync.add_argument("--source", action="append", help="only this source (repeatable)")
    sync.add_argument("--accept-removals", action="store_true", help="apply deletions a guard held back")
    sync.add_argument(
        "--build", action="store_true", help="build the graph after syncing (with --due: only if a source ran)"
    )
    sync.add_argument(
        "--due", action="store_true", help="only sources whose refresh interval has passed since their last ok run"
    )
    sync.add_argument(
        "--all", action="store_true", help="with --due: every project in projects.json whose harness still exists"
    )
    sync.add_argument(
        "--push",
        action="store_true",
        help="then send the runs the hub lacks (`hub kg push`); with --all, of every project the hub takes",
    )
    sync.set_defaults(func=cmd_sync)

    schedule = ksub.add_parser(
        "schedule", help="hourly LaunchAgent running kg sync --due --all --build, and --push when signed in (macOS)"
    )
    schedule.add_argument(
        "action", choices=["print", "install", "uninstall"], help="print the plist, or load or unload it"
    )
    schedule.set_defaults(func=cmd_schedule)

    status = with_project(ksub.add_parser("status", help="coverage, freshness and held deletions per source"))
    status.add_argument("--brief", action="store_true", help="a few lines, for session hooks")
    status.set_defaults(func=cmd_status)

    binding = ksub.add_parser("bind", help="bind a directory and everything below it to one project")
    binding.add_argument("directory", nargs="?", help="directory to bind (default: the cwd)")
    binding.add_argument(
        "--project", help="project name or harness path (default: the project the directory resolves to now)"
    )
    binding.add_argument("--list", action="store_true", help="print every binding")
    binding.add_argument("--remove", metavar="DIR", help="delete the binding of DIR")
    binding.add_argument("--json", action="store_true", help="machine-readable output")
    binding.set_defaults(func=cmd_bind)

    connector = ksub.add_parser("connector", help="connector tooling")
    csub = connector.add_subparsers(dest="connector_command", required=True)
    test = with_project(csub.add_parser("test", help="conformance test for a connector"))
    test.add_argument("--id", default="test", help="source id for an ad hoc connector (default: test)")
    test.add_argument("--builtin", help="test a builtin connector, e.g. git")
    test.add_argument("--source", help="test a source declared in the project's knowledge.yaml")
    test.add_argument("--config", help="JSON config for an ad hoc connector")
    test.add_argument("--label", help="declared label level, to check the connector never lowers it")
    test.add_argument("--golden", help="golden summary file to compare against")
    test.add_argument("--update-golden", action="store_true", help="rewrite the golden file")
    test.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    test.set_defaults(func=cmd_connector_test)

    hook = ksub.add_parser("hook", help="Claude Code hook entry points")
    hsub = hook.add_subparsers(dest="hook_name", required=True)
    hsub.add_parser("session-start", help="print the SessionStart note").set_defaults(func=cmd_hook_session_start)
    hsub.add_parser("post-tool", help="add an evo-kg result to the session label").set_defaults(func=cmd_hook_post_tool)
    pre_search = hsub.add_parser("pre-search", help="PreToolUse: graph ids named like a Grep, Glob, rg or grep")
    pre_search.add_argument(
        "--only", choices=["rg", "grep"], help="for Bash, answer only when this command leads (one handler per if)"
    )
    pre_search.add_argument("--sink", default="claude-code@anthropic", help="sink profile whose clearance applies")
    pre_search.set_defaults(func=cmd_hook_pre_search)

    from evo_agents.kg import cli_graph

    cli_graph.register(ksub, with_project)
