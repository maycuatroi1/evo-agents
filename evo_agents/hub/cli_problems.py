"""``evo-agents hub problem list|show|set|close``: the problems of a project (``evo_agents.hub.problems``), over GET
and PUT /v1/projects/{p}/problems and the routes below it.

``list`` prints the project's problems by rank, those of one ``--status`` alone when given. ``show CODE`` prints a
problem (P-<number>) at its newest revision or at ``--revision``, with the goals that answer it and its revisions.
``set`` writes a problem's next revision: without CODE it makes a new problem under the project's next code, which
needs ``--statement``, ``--who``, ``--when`` and ``--workaround``; with CODE it changes the options given and keeps the
rest, at the revision the problem is at when it is read unless ``--if-revision`` says another. A metric is ``--metric
KEY`` with ``--aggregate``, and ``--window-days`` and ``--tenant`` optional; it has no target. ``--no-metric`` clears
it, and ``--reopen`` opens a solved or dropped problem again. ``close CODE --status solved|dropped --reason TEXT``
closes a problem.

Every member of the project reads its problems; writing one needs the writer role on the project. Every command finds
its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory.
``--json`` prints what the hub answered (``list`` an array). Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub import problems
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.mirror import harness_project, harness_root

EXIT_USAGE = 2
PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"
SHOWN_STATEMENT_CHARS = 60  # of a problem's statement in a row of list
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
PROBLEM_KEYS = (
    "project",
    "code",
    "number",
    "revision",
    "statement",
    "who",
    "when",
    "workaround",
    "metric",
    "rank",
    "owner",
    "status",
    "close_reason",
    "note",
    "created_by",
    "created_at",
)
METRIC_OPTIONS = {"metric": "key", "aggregate": "aggregate", "window_days": "window_days", "tenant": "tenant"}
NEEDED = ("metric", "aggregate")  # the options a metric cannot go without
WRITTEN_FIELDS = problems.FIELDS
GIVEN_FIELDS = ("statement", "who", "when", "workaround", "rank", "owner")  # the options that set one field each


def project_of(args) -> str:
    if args.project:
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(
            "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"
        ) from None
    return harness_project(root)


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/problems{rest}"


def _ref(ref: str) -> str:
    return "/" + quote(ref, safe="")


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


# Reading


def _clip(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _path(project, f"?status={args.status}" if args.status else ""))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no problems{' ' + args.status if args.status else ''}.")
        return 0
    rows = [
        (
            problem["code"],
            problem["status"],
            problem["rank"],
            problem["owner"],
            problems.metric_text(problem["metric"]),
            _clip(problem["statement"], SHOWN_STATEMENT_CHARS),
        )
        for problem in found
    ]
    table(("CODE", "STATUS", "RANK", "OWNER", "METRIC", "STATEMENT"), rows)
    return 0


def _show(problem: dict, revisions: list[dict]) -> None:
    print(f"Problem {problem['code']} of project {problem['project']}, revision {problem['revision']}")
    print(f"  statement: {problem['statement']}")
    print(f"  who: {problem['who']}")
    print(f"  when: {problem['when']}")
    print(f"  workaround: {problem['workaround']}")
    print(f"  metric: {problems.metric_text(problem['metric'])}")
    print(f"  rank: {problem['rank']}")
    print(f"  owner: {problem['owner']}")
    print(f"  status: {problems.status_text(problem)}")
    written = f"  written by {problem['created_by']} at {when(problem['created_at'])} UTC"
    print(written + (f": {problem['note']}" if problem.get("note") else ""))
    answering = problem.get("goals") or []
    print("Goals:" if answering else "Goals: none answers it yet")
    for goal in answering:
        print(f"  {goal['code']} ({goal['status']}): {_clip(goal['statement'], SHOWN_STATEMENT_CHARS)}")
    if revisions:
        print("Revisions:")
        rows = [
            (item["revision"], when(item["created_at"]), item["created_by"], ", ".join(item["changed"]) or "-")
            for item in revisions
        ]
        table(("REVISION", "WRITTEN (UTC)", "BY", "CHANGED"), rows)


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call(
        "GET", _path(project, _ref(args.code) + ("" if args.revision is None else f"?revision={args.revision}"))
    )
    if args.json:
        print_json(found)
        return 0
    _show(found, hub.call("GET", _path(project, _ref(args.code) + "/revisions")))
    return 0


# Writing


def _metric(args, held: dict | None) -> tuple[dict | None, str | None]:
    """The metric the options make of ``held`` (the problem's now, if any); (None, why) when they make none."""
    given = {part: value for option, part in METRIC_OPTIONS.items() if (value := getattr(args, option)) is not None}
    if args.no_metric:
        return None, None
    if not given:
        return held, None
    metric = {**(held or {}), **given}
    missing = [f"--{option.replace('_', '-')}" for option in NEEDED if METRIC_OPTIONS[option] not in metric]
    return (None, f"a metric needs {', '.join(missing)}") if missing else (metric, None)


def _body(args, held: dict) -> tuple[dict | None, str | None]:
    """The problem to write: ``held`` (its fields now, {} for a new problem) with the options given; (None, why) when
    the options do not make one. A field left out of the body is empty, or kept for its rank and owner."""
    metric, why = _metric(args, held.get("metric"))
    if why:
        return None, why
    body = {name: held.get(name) for name in WRITTEN_FIELDS} | {"metric": metric}
    body |= {name: getattr(args, name) for name in GIVEN_FIELDS if getattr(args, name) is not None}
    missing = [f"--{name}" for name in problems.TEXTS if not body[name]]
    if missing:
        return None, f"a new problem needs {', '.join(missing)}"
    if args.reopen:
        body["status"], body["close_reason"] = problems.OPEN, None
    body["status"] = body["status"] or problems.OPEN
    body["note"] = args.note
    return {name: value for name, value in body.items() if value is not None}, None


def _next_code(hub, project: str) -> str:
    numbers = [problem["number"] for problem in hub.call("GET", _path(project))]
    return problems.code(max(numbers, default=0) + 1)


def _said(written: dict, project: str) -> None:
    if not written["changed"]:
        print(
            f"Problem {written['code']} of project {project} is this already, at revision {written['revision']}: "
            "nothing written."
        )
    else:
        made = "Made" if written["revision"] == 1 else "Wrote"
        print(f"{made} problem {written['code']} of project {project}: revision {written['revision']}.")


@client_command
def cmd_set(args) -> int:
    if args.no_metric and any(getattr(args, option) is not None for option in METRIC_OPTIONS):
        return _usage("--no-metric goes without the options of a metric")
    hub, _ = signed_in()
    project = project_of(args)
    held, revision = {}, 0
    if args.code is not None:
        held = hub.call("GET", _path(project, _ref(args.code)))
        revision = held["revision"]
    elif args.reopen:
        return _usage("--reopen opens a problem the project has: name its code")
    body, why = _body(args, held)
    if why:
        return _usage(why)
    code = args.code or _next_code(hub, project)
    body["if_revision"] = revision if args.if_revision is None else args.if_revision
    written = hub.call("PUT", _path(project, _ref(code)), body)
    if args.json:
        print_json(written)
        return 0
    _said(written, project)
    return 0


@client_command
def cmd_close(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    body = {"status": args.status, "reason": args.reason}
    if args.note:
        body["note"] = args.note
    if args.if_revision is not None:
        body["if_revision"] = args.if_revision
    written = hub.call("POST", _path(project, _ref(args.code) + "/close"), body)
    if args.json:
        print_json(written)
        return 0
    print(f"Closed problem {written['code']} of project {project} ({args.status}): revision {written['revision']}.")
    return 0


# Registering


def _write_options(parser) -> None:
    most = problems.MAX_TEXT_CHARS
    parser.add_argument(
        "--statement", help=f"what hurts, before any solution (at most {problems.MAX_STATEMENT_CHARS} characters)"
    )
    parser.add_argument("--who", help=f"who is hurt (at most {most} characters)")
    parser.add_argument("--when", help=f"when it happens (at most {most} characters)")
    parser.add_argument("--workaround", help=f"how they cope today (at most {most} characters)")
    parser.add_argument("--metric", metavar="KEY", help="a key of kind number of the project's registry of signals")
    parser.add_argument("--aggregate", choices=problems.AGGREGATES, help="how the key's signals are summed up")
    parser.add_argument("--window-days", type=int, metavar="N", help="the days they are summed up over (default 90)")
    parser.add_argument("--tenant", help="the signals of this tenant alone")
    parser.add_argument("--no-metric", action="store_true", help="clear the metric")
    parser.add_argument("--rank", type=int, metavar="N", help="its place among the problems, 1 first")
    parser.add_argument("--owner", metavar="LOGIN", help="a member with the writer role or above")
    parser.add_argument("--reopen", action="store_true", help="open a solved or dropped problem again")


def register_problems(hsub) -> None:
    problem = hsub.add_parser("problem", help="the problems of a project: what hurts, who, when, and the workaround")
    psub = problem.add_subparsers(dest="problem_command", required=True)

    listed = psub.add_parser("list", help="the project's problems by rank")
    listed.add_argument("--status", choices=problems.STATUSES, help="those of this status alone")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*PROBLEM_KEYS, schema="ProjectProblem"))
    listed.set_defaults(func=cmd_list)

    shown = psub.add_parser("show", help="a problem, with the goals that answer it and its revisions")
    shown.add_argument("code", metavar="CODE", help="its code (P-<number>)")
    shown.add_argument("--revision", type=int, metavar="N", help="an older revision (default: the newest)")
    shown.add_argument("--project", help=PROJECT_HELP)
    json_option(shown, returns_object(*PROBLEM_KEYS, "goals", schema="ProjectProblemShown"))
    shown.set_defaults(func=cmd_show)

    written = psub.add_parser(
        "set", help="make a problem, or write a problem's next revision (the writer role on the project)"
    )
    written.add_argument("code", metavar="CODE", nargs="?", help="the problem's code; none: a new problem")
    _write_options(written)
    written.add_argument("--note", help=f"why, for the history (at most {problems.MAX_NOTE_CHARS} characters)")
    written.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the problem is at revision N")
    written.add_argument("--project", help=PROJECT_HELP)
    json_option(written, returns_object(*PROBLEM_KEYS, "changed", schema="ProjectProblemWritten"))
    written.set_defaults(func=cmd_set)

    closed = psub.add_parser("close", help="close a problem as solved or dropped (the writer role on the project)")
    closed.add_argument("code", metavar="CODE", help="its code (P-<number>)")
    closed.add_argument("--status", required=True, choices=problems.CLOSED, help="solved or dropped")
    closed.add_argument("--reason", required=True, help=f"why (at most {problems.MAX_REASON_CHARS} characters)")
    closed.add_argument("--note", help=f"for the history (at most {problems.MAX_NOTE_CHARS} characters)")
    closed.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the problem is at revision N")
    closed.add_argument("--project", help=PROJECT_HELP)
    json_option(closed, returns_object(*PROBLEM_KEYS, "changed", schema="ProjectProblemWritten"))
    closed.set_defaults(func=cmd_close)
