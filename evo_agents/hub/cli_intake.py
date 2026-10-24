"""``evo-agents hub intake start|put|show|list``: the intakes of a project (plan problem-intake, ``evo_agents.hub.intakes``),
over the routes under /v1/projects/{p}/intakes.

``start --source KIND:LABEL[:NAME]`` (once for each source) opens intake I-<n>: the kinds and labels of what the session
will read, with a short name for each, never its content. ``put CODE FILE`` sends the result as an ``evo-intake/1``
document (FILE, or ``-`` for standard input): candidate problems with their evidence, promises and questions; an
``evo-intake/2`` document adds candidate opportunities (``intake2.schema.json``). The hub
checks it against ``evo_agents/harness/schemas/intake.schema.json`` and refuses with 422, naming each reason and
writing nothing, a candidate with one independent source that nobody confirmed. A result makes a pending proposal of
@builder for each candidate, and a task of the plugin tasks for each promise when the project enabled it. ``show CODE``
prints one intake, ``list`` the project's, those of one ``--status`` alone when given.

Starting and putting need the writer role on the project; every member reads. Every command finds its project with
``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory. ``--json`` prints
what the hub answered (``list`` an array). Standard library only, like the rest of the client.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

from evo_agents.hub import intakes
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.cli_problems import PROJECT_HELP, project_of
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
INTAKE_KEYS = (
    "project",
    "code",
    "number",
    "status",
    "requested_by",
    "sources",
    "note",
    "created_at",
    "put_by",
    "done_at",
    "candidates",
    "promises",
    "questions",
    "opportunities",
    "outcome",
)


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/intakes{rest}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _source(text: str) -> dict:
    """A source as ``KIND:LABEL[:NAME]`` gives it; ValueError naming what is wrong."""
    kind, _, rest = text.partition(":")
    label, _, name = rest.partition(":")
    if kind not in intakes.SOURCE_KINDS or label not in intakes.SOURCE_LABELS:
        raise ValueError(
            f"a source is KIND:LABEL[:NAME] with a kind of {', '.join(intakes.SOURCE_KINDS)} and a label of "
            f"{', '.join(intakes.SOURCE_LABELS)}, not {text!r}"
        )
    return {"kind": kind, "label": label, **({"name": name} if name else {})}


def _show(found: dict) -> None:
    print(f"Intake {found['code']} of project {found['project']}: {found['status']}, asked by {found['requested_by']}")
    for source in found["sources"]:
        print(f"  source: {intakes.source_text(source)}")
    if found["note"]:
        print(f"  note: {found['note']}")
    outcome = found["outcome"]
    if outcome is None:
        return
    print(
        f"Put by {found['put_by']}: {found['candidates']} candidates, {found['promises']} promises, "
        f"{found['questions']} questions"
        + (f", {found['opportunities']} opportunities" if found.get("opportunities") else "")
    )
    for proposal in outcome["proposals"]:
        what = "new problem" if proposal["kind"] == "new_problem" else "evidence for"
        print(f"  proposal {proposal['proposal_id']}: {what} {proposal['problem']} ({proposal['lines']} lines)")
    for proposal in outcome.get("opportunity_proposals") or []:
        what = "new opportunity" if proposal["kind"] == "new_opportunity" else "evidence for"
        print(f"  proposal {proposal['proposal_id']}: {what} {proposal['opportunity']} ({proposal['lines']} lines)")
    for task in outcome["tasks"]:
        due = f", due {task['due_on']}" if task["due_on"] else ""
        print(f"  task {task['task']}: promise {task['promise']}, assigned to {task['assignee']}{due}")
    if outcome["tasks_skipped"]:
        print(f"  {outcome['tasks_skipped']}")
    for question in outcome["questions"]:
        print(f"  question: {question.get('text', '')}")


@client_command
def cmd_start(args) -> int:
    try:
        sources = [_source(text) for text in args.source]
    except ValueError as exc:
        return _usage(str(exc))
    hub, _ = signed_in()
    project = project_of(args)
    body = {"sources": sources, **({"note": args.note} if args.note else {})}
    made = hub.call("POST", _path(project), body)
    if args.json:
        print_json(made)
        return 0
    print(
        f"Started intake {made['code']} of project {project}: put its result with `hub intake put {made['code']} FILE`."
    )
    return 0


@client_command
def cmd_put(args) -> int:
    try:
        text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        document = json.loads(text)
    except (OSError, ValueError) as exc:
        return _usage(f"cannot read an evo-intake/1 document from {args.file}: {exc}")
    hub, _ = signed_in()
    project = project_of(args)
    done = hub.call("PUT", _path(project, "/" + quote(args.code, safe="")), document)
    if args.json:
        print_json(done)
        return 0
    _show(done)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _path(project, "/" + quote(args.code, safe="")))
    if args.json:
        print_json(found)
        return 0
    _show(found)
    return 0


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _path(project, f"?status={args.status}" if args.status else ""))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no intakes{' ' + args.status if args.status else ''}.")
        return 0
    rows = [
        (
            intake["code"],
            intake["status"],
            intake["requested_by"],
            len(intake["sources"]),
            "-" if intake["candidates"] is None else intake["candidates"],
            "-" if intake["promises"] is None else intake["promises"],
        )
        for intake in found
    ]
    table(("CODE", "STATUS", "ASKED BY", "SOURCES", "CANDIDATES", "PROMISES"), rows)
    return 0


def register_intake(hsub) -> None:
    intake = hsub.add_parser("intake", help="an intake: one reading of vague context, its problems and its promises")
    isub = intake.add_subparsers(dest="intake_command", required=True)

    started = isub.add_parser("start", help="open an intake from the sources a session will read (the writer role)")
    started.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="KIND:LABEL[:NAME]",
        help=f"a source: its kind ({', '.join(intakes.SOURCE_KINDS)}), its label "
        f"({', '.join(intakes.SOURCE_LABELS)}) and a short name; once for each, never content",
    )
    started.add_argument("--note", help="why you ask for it")
    started.add_argument("--project", help=PROJECT_HELP)
    json_option(started, returns_object(*INTAKE_KEYS, schema="Intake"))
    started.set_defaults(func=cmd_start)

    put = isub.add_parser(
        "put", help="put the evo-intake/1 or evo-intake/2 result of an intake, once (the writer role)"
    )
    put.add_argument("code", metavar="CODE", help="the intake's code (I-<number>)")
    put.add_argument(
        "file", metavar="FILE", help="the evo-intake/1 or evo-intake/2 JSON document, or - for standard input"
    )
    put.add_argument("--project", help=PROJECT_HELP)
    json_option(put, returns_object(*INTAKE_KEYS, schema="Intake"))
    put.set_defaults(func=cmd_put)

    shown = isub.add_parser("show", help="an intake, with what the hub made of its result")
    shown.add_argument("code", metavar="CODE", help="the intake's code (I-<number>)")
    shown.add_argument("--project", help=PROJECT_HELP)
    json_option(shown, returns_object(*INTAKE_KEYS, schema="Intake"))
    shown.set_defaults(func=cmd_show)

    listed = isub.add_parser("list", help="the project's intakes, newest first")
    listed.add_argument("--status", choices=intakes.STATUSES, help="those of this status alone")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*INTAKE_KEYS, schema="Intake"))
    listed.set_defaults(func=cmd_list)
