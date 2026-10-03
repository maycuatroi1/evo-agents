"""``evo-agents kg`` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evo_agents.kg.project import ProjectError, resolve_project


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


def _cmd_sync_due(args) -> int:
    from evo_agents.kg.ids import utc_now
    from evo_agents.kg.sync import sync_all_due, sync_due

    if args.source:
        print("error: --due picks the sources itself; drop --source", file=sys.stderr)
        return 2
    if args.all:
        if args.project or args.accept_removals:
            print("error: --all goes with --due, --build and --json only", file=sys.stderr)
            return 2
        outcomes = sync_all_due(build=args.build)
        ok = all(o.ok for o in outcomes)
        if args.json:
            _print_json({"ok": ok, "projects": [o.to_json() for o in outcomes]})
        else:
            print(f"kg sync --due --all at {utc_now()}: {len(outcomes)} project(s)")
            for outcome in outcomes:
                _print_due(outcome)
        return 0 if ok else 1

    project = _project(args)
    try:
        outcome = sync_due(project, build=args.build, accept=args.accept_removals)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(outcome.to_json())
    else:
        _print_due(outcome)
    return 0 if outcome.ok else 1


def cmd_sync(args) -> int:
    from evo_agents.kg.sync import sync_project

    if args.all and not args.due:
        print("error: --all needs --due; a full sync of every project is not offered", file=sys.stderr)
        return 2
    if args.due:
        return _cmd_sync_due(args)
    project = _project(args)
    results = sync_project(project, args.source or None, accept=args.accept_removals)
    if args.json:
        _print_json({"project": project.name, "runs": [r.to_json() for r in results]})
    else:
        _print_runs(results)
    if args.build:
        from evo_agents.kg.build import build_project

        report = build_project(project)
        if not args.json:
            print(report.summary_line())
        if not report.ok:
            return 1
    return 0 if all(r.ok and not r.held for r in results) else 1


def cmd_status(args) -> int:
    from evo_agents.kg.status import project_status, render_status

    project = _project(args)
    status = project_status(project)
    if args.json:
        _print_json(status)
    else:
        print(render_status(status, brief=args.brief))
    return 0 if status["ok"] else 1


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


def cmd_hook_session_start(args) -> int:
    """SessionStart hook for Claude Code: a short note on the bound project. Only names and counts the
    pipeline derived; never source text, which would reach the model with system-reminder authority."""
    from evo_agents.kg.status import project_status, render_status

    try:
        project = resolve_project(None)
    except ProjectError:
        return 0  # not inside a project with a graph: say nothing
    note = render_status(project_status(project), brief=True)
    note += "\nUse the evo-kg tools (kg_search, kg_context, kg_node) before grepping for named things."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}}))
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
    sync.set_defaults(func=cmd_sync)

    schedule = ksub.add_parser("schedule", help="hourly LaunchAgent running kg sync --due --all --build (macOS)")
    schedule.add_argument(
        "action", choices=["print", "install", "uninstall"], help="print the plist, or load or unload it"
    )
    schedule.set_defaults(func=cmd_schedule)

    status = with_project(ksub.add_parser("status", help="coverage, freshness and held deletions per source"))
    status.add_argument("--brief", action="store_true", help="a few lines, for session hooks")
    status.set_defaults(func=cmd_status)

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

    from evo_agents.kg import cli_graph

    cli_graph.register(ksub, with_project)
