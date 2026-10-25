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

``proposal list`` lists the pending proposals of a problem or an opportunity that the member may decide, an intake's
among them (GET /v1/x/builder/{p}/proposals/waiting, ``--kind`` for one kind); a proposal an intake made has no thread
to answer in. ``proposal show ID`` prints one with the fields it sets and its lines of evidence, and ``proposal
approve|reject ID`` answers it (POST .../proposals/{id}/answer): approved, the hub makes the problem or the opportunity,
or its lines. The routes are those of the core plugin builder, which the project enables.

Starting, putting and answering need the writer role on the project; every member reads, and a reader decides no
proposal. Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness
around the current directory. ``--json`` prints what the hub answered (``list`` and ``proposal list`` an array).
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

from evo_agents.hub import intakes
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
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
PROPOSAL_KEYS = (
    "id",
    "project",
    "kind",
    "target",
    "change",
    "summary",
    "base_version",
    "state",
    "approver",
    "proposed_by",
    "proposed_at",
    "decided_by",
    "decided_at",
    "applied_version",
    "rollback_version",
    "error",
    "run_id",
    "link",
)
PROPOSAL_KINDS = ("problem", "opportunity")  # the kinds of proposal of @builder an intake makes, a writer's to decide
SHOWN_SUMMARY_CHARS = 70  # of a proposal's summary in a row of ``proposal list``


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/intakes{rest}"


def _proposals_path(project: str, rest: str = "") -> str:
    return f"/v1/x/builder/{quote(project, safe='')}/proposals{rest}"


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


# The proposals an intake made


def _makes(proposal: dict) -> str:
    """What a proposal of a problem or an opportunity makes: a new one, lines of evidence of one, or a change of one."""
    code = proposal["target"].get(proposal["kind"], "?")
    change = proposal["change"]
    if proposal["base_version"] == 0:
        return f"new {proposal['kind']} {code}"
    if change.get("evidence") and not change.get("set") and not change.get("unset"):
        return f"evidence for {code}"
    return f"change of {code}"


def _clip(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


@client_command
def cmd_proposal_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    kinds = (args.kind,) if args.kind else PROPOSAL_KINDS
    query = f"?kind={args.kind}" if args.kind else ""
    found = [p for p in hub.call("GET", _proposals_path(project, "/waiting" + query)) if p["kind"] in kinds]
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No proposal of {' or '.join(kinds)} waits for your answer in project {project}.")
        return 0
    rows = [
        (p["id"], _makes(p), p["proposed_by"], when(p["proposed_at"]), _clip(p["summary"], SHOWN_SUMMARY_CHARS))
        for p in found
    ]
    table(("ID", "MAKES", "BY", "PROPOSED", "SUMMARY"), rows)
    print("Answer each with `evo-agents hub intake proposal approve|reject ID`.")
    return 0


def _show_proposal(found: dict) -> None:
    print(f"Proposal {found['id']} of project {found['project']}: {_makes(found)}, {found['state'].replace('_', ' ')}")
    print(f"  {found['summary']}")
    print(f"  proposed by {found['proposed_by']} at {when(found['proposed_at'])} UTC")
    if found["decided_by"]:
        print(f"  decided by {found['decided_by']} at {when(found['decided_at'])} UTC")
    if found["error"]:
        print(f"  error: {found['error']}")
    for name, value in found["change"].get("set", {}).items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        print(f"  {name}: {shown}")
    for name in found["change"].get("unset", []):
        print(f"  {name}: (cleared)")
    for line in found["change"].get("evidence", []):
        print(f"  evidence: [{line['signal']}] {line['source_kind']} {line['source_uri']}: {line['excerpt']}")
    if found["link"]:
        print(f"  thread: {found['link']}")


@client_command
def cmd_proposal_show(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _proposals_path(project, f"/{args.id}"))
    if args.json:
        print_json(found)
        return 0
    _show_proposal(found)
    return 0


@client_command
def cmd_proposal_answer(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("POST", _proposals_path(project, f"/{args.id}/answer"), {"answer": args.answer})
    if args.json:
        print_json(found)
    elif found["state"] == "applied":
        print(f"Approved proposal {found['id']}: {_makes(found)} is made.")
    elif found["state"] == "rejected":
        print(f"Rejected proposal {found['id']}: nothing changed.")
    else:
        print(
            f"error: proposal {found['id']} was approved and the hub could not apply it: {found['error']}",
            file=sys.stderr,
        )
    return 0 if found["state"] != "failed" else 1


def _register_proposal(isub) -> None:
    proposal = isub.add_parser(
        "proposal", help="the proposals of a problem or an opportunity that wait for a writer: list, show, answer"
    )
    psub = proposal.add_subparsers(dest="proposal_command", required=True)

    listed = psub.add_parser("list", help="the pending proposals you may decide, newest first")
    listed.add_argument("--kind", choices=PROPOSAL_KINDS, help="those of this kind alone")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*PROPOSAL_KEYS, schema="BuilderProposal"))
    listed.set_defaults(func=cmd_proposal_list)

    shown = psub.add_parser("show", help="a proposal, with the fields it sets and its lines of evidence")
    shown.add_argument("id", type=int, metavar="ID", help="the proposal's id")
    shown.add_argument("--project", help=PROJECT_HELP)
    json_option(shown, returns_object(*PROPOSAL_KEYS, schema="BuilderProposal"))
    shown.set_defaults(func=cmd_proposal_show)

    for verb, says in (("approve", "make what it proposes"), ("reject", "refuse it; nothing changes")):
        answer = psub.add_parser(verb, help=f"answer a pending proposal: {says} (the writer role)")
        answer.add_argument("id", type=int, metavar="ID", help="the proposal's id")
        answer.add_argument("--project", help=PROJECT_HELP)
        json_option(answer, returns_object(*PROPOSAL_KEYS, schema="BuilderProposal"))
        answer.set_defaults(func=cmd_proposal_answer, answer=verb)


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

    _register_proposal(isub)
