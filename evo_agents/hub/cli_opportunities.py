"""``evo-agents hub opportunity list|show|set|close`` and ``opportunity evidence add|list|confirm``: the opportunities
of a project (``evo_agents.hub.opportunities``), over GET and PUT /v1/projects/{p}/opportunities and the routes below it.

``list`` prints the project's opportunities by rank, those of one ``--status`` alone when given. ``--closing-within
DAYS`` prints the open ones whose window ends within DAYS days of today (one already past included), the nearest end
first. ``show CODE`` prints an
opportunity (O-<number>) at its newest revision or at ``--revision``, with the problems it answers, how many problems
of other projects it answers that you hold no grant on, its lines of evidence counted, and its revisions.

``set`` without a code makes an opportunity and needs ``--statement``, ``--beneficiary`` and ``--leverage``; with CODE
it changes the options given and keeps the rest, at the revision the opportunity is at when it is read unless
``--if-revision`` says another. ``--window-until DATE`` and ``--window-note TEXT`` name the window, ``--no-window``
clears both. ``--answers P-3,lumen/P-2`` sets the problems it answers (a code of another project needs a grant of yours
on it), ``--no-answers`` clears them. A metric is ``--metric KEY`` with ``--aggregate``, and ``--window-days`` and
``--tenant`` optional; ``--no-metric`` clears it, and ``--reopen`` opens a closed opportunity again. ``close CODE
--status captured|missed|dropped --reason TEXT`` closes one.

``opportunity evidence add CODE`` adds a line of evidence (plan opportunity-intake): the kind and address of its source
(``--source-kind``, ``--source-uri``), where in it (``--locator``), who spoke (``--speaker``), an excerpt of at most 300
characters (``--excerpt``), the signal it shows (``--signal``) and its label (``--label`` public or internal; the hub
refuses personal). ``evidence list CODE`` prints the lines, ``evidence confirm CODE E-<n>`` confirms one.

Every member of the project reads its opportunities; writing one needs the writer role on the project. Every command
finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current
directory. ``--json`` prints what the hub answered (``list`` an array). Standard library only, like the rest of the
client.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub import opportunities, problems
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_problems import METRIC_OPTIONS, NEEDED, PROJECT_HELP, SHOWN_STATEMENT_CHARS, project_of
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
OPPORTUNITY_KEYS = (
    "project",
    "code",
    "number",
    "revision",
    "statement",
    "beneficiary",
    "leverage",
    "window_until",
    "window_note",
    "answers",
    "hidden_answers",
    "metric",
    "rank",
    "owner",
    "status",
    "close_reason",
    "note",
    "created_by",
    "created_at",
)
EVIDENCE_KEYS = (
    "project",
    "opportunity",
    "code",
    "number",
    "source_kind",
    "source_uri",
    "locator",
    "speaker",
    "excerpt",
    "signal",
    "label",
    "added_by",
    "added_at",
    "confirmed_by",
    "confirmed_at",
    "proposal_id",
)
GIVEN_FIELDS = ("statement", "beneficiary", "leverage", "window_until", "window_note", "rank", "owner")
WRITTEN_FIELDS = tuple(name for name in opportunities.FIELDS)


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/opportunities{rest}"


def _ref(ref: str) -> str:
    return "/" + quote(ref, safe="")


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _clip(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


# Reading


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    query = []
    if args.status:
        query.append(f"status={args.status}")
    if args.closing_within is not None:
        query.append(f"closing_within={args.closing_within}")
    found = hub.call("GET", _path(project, "?" + "&".join(query) if query else ""))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no opportunities{' ' + args.status if args.status else ''}.")
        return 0
    rows = [
        (
            item["code"],
            item["status"],
            item["rank"],
            item["owner"],
            opportunities.window_text(item),
            _clip(item["statement"], SHOWN_STATEMENT_CHARS),
        )
        for item in found
    ]
    table(("CODE", "STATUS", "RANK", "OWNER", "WINDOW", "STATEMENT"), rows)
    return 0


def _show(item: dict, revisions: list[dict]) -> None:
    print(f"Opportunity {item['code']} of project {item['project']}, revision {item['revision']}")
    print(f"  statement: {item['statement']}")
    print(f"  beneficiary: {item['beneficiary']}")
    print(f"  leverage: {item['leverage']}")
    print(f"  window: {opportunities.window_text(item)}")
    print(f"  metric: {problems.metric_text(item['metric'])}")
    print(f"  rank: {item['rank']}")
    print(f"  owner: {item['owner']}")
    print(f"  status: {opportunities.status_text(item)}")
    written = f"  written by {item['created_by']} at {when(item['created_at'])} UTC"
    print(written + (f": {item['note']}" if item.get("note") else ""))
    answers = ", ".join(item["answers"]) or "none"
    hidden = item.get("hidden_answers") or 0
    print(f"Answers: {answers}" + (f" (and {hidden} of projects you hold no grant on)" if hidden else ""))
    if item.get("evidence_count"):
        print(f"Evidence: {item['evidence_count']} lines from {item['source_count']} independent sources")
    if revisions:
        print("Revisions:")
        rows = [
            (entry["revision"], when(entry["created_at"]), entry["created_by"], ", ".join(entry["changed"]) or "-")
            for entry in revisions
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


@client_command
def cmd_evidence_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _path(project, _ref(args.code) + "/evidence"))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Opportunity {args.code} of project {project} has no evidence.")
        return 0
    rows = [
        (
            line["code"],
            line["signal"],
            line["label"],
            f"{line['source_kind']} {_clip(line['source_uri'], 40)}",
            line["speaker"] or "-",
            "confirmed by " + line["confirmed_by"] if line["confirmed_by"] else "not confirmed",
            _clip(line["excerpt"], SHOWN_STATEMENT_CHARS),
        )
        for line in found
    ]
    table(("CODE", "SIGNAL", "LABEL", "SOURCE", "SPEAKER", "STATE", "EXCERPT"), rows)
    return 0


@client_command
def cmd_evidence_add(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    body = {
        "source_kind": args.source_kind,
        "source_uri": args.source_uri,
        "excerpt": args.excerpt,
        "signal": args.signal,
        "label": args.label,
    }
    body |= {name: value for name in ("locator", "speaker") if (value := getattr(args, name)) is not None}
    written = hub.call("POST", _path(project, _ref(args.code) + "/evidence"), body)
    if args.json:
        print_json(written)
        return 0
    print(f"Added evidence {written['code']} to opportunity {written['opportunity']} of project {project}.")
    return 0


@client_command
def cmd_evidence_confirm(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    written = hub.call("POST", _path(project, f"{_ref(args.code)}/evidence{_ref(args.evidence)}/confirm"))
    if args.json:
        print_json(written)
        return 0
    print(f"Confirmed evidence {written['code']} of opportunity {written['opportunity']} of project {project}.")
    return 0


# Writing


def _metric(args, held: dict | None) -> tuple[dict | None, str | None]:
    """The metric the options make of ``held`` (the opportunity's now, if any); (None, why) when they make none."""
    given = {part: value for option, part in METRIC_OPTIONS.items() if (value := getattr(args, option)) is not None}
    if args.no_metric:
        return None, None
    if not given:
        return held, None
    metric = {**(held or {}), **given}
    missing = [f"--{option.replace('_', '-')}" for option in NEEDED if METRIC_OPTIONS[option] not in metric]
    return (None, f"a metric needs {', '.join(missing)}") if missing else (metric, None)


def _answers(args, held: dict) -> list[str]:
    if args.no_answers:
        return []
    if args.answers is not None:
        return [code.strip() for code in args.answers.split(",") if code.strip()]
    return list(held.get("answers") or [])


def _body(args, held: dict) -> tuple[dict | None, str | None]:
    """The opportunity to write: ``held`` (its fields now, {} for a new one) with the options given; (None, why) when
    the options do not make one."""
    metric, why = _metric(args, held.get("metric"))
    if why:
        return None, why
    body = {name: held.get(name) for name in WRITTEN_FIELDS if name != "answers"}
    body |= {"metric": metric, "answers": _answers(args, held)}
    body |= {name: getattr(args, name) for name in GIVEN_FIELDS if getattr(args, name) is not None}
    if args.no_window:
        body["window_until"] = body["window_note"] = None
    missing = [f"--{name}" for name in opportunities.TEXTS if not body.get(name)]
    if missing:
        return None, f"a new opportunity needs {', '.join(missing)}"
    if args.reopen:
        body["status"], body["close_reason"] = opportunities.OPEN, None
    body["status"] = body.get("status") or opportunities.OPEN
    body["note"] = args.note
    return {name: value for name, value in body.items() if value is not None}, None


def _next_code(hub, project: str) -> str:
    numbers = [item["number"] for item in hub.call("GET", _path(project))]
    return opportunities.code(max(numbers, default=0) + 1)


def _said(written: dict, project: str) -> None:
    if not written["changed"]:
        print(
            f"Opportunity {written['code']} of project {project} is this already, at revision {written['revision']}: "
            "nothing written."
        )
    else:
        made = "Made" if written["revision"] == 1 else "Wrote"
        print(f"{made} opportunity {written['code']} of project {project}: revision {written['revision']}.")


@client_command
def cmd_set(args) -> int:
    if args.no_metric and any(getattr(args, option) is not None for option in METRIC_OPTIONS):
        return _usage("--no-metric goes without the options of a metric")
    if args.no_window and (args.window_until is not None or args.window_note is not None):
        return _usage("--no-window goes without --window-until and --window-note")
    if args.no_answers and args.answers is not None:
        return _usage("--no-answers goes without --answers")
    hub, _ = signed_in()
    project = project_of(args)
    held, revision = {}, 0
    if args.code is not None:
        held = hub.call("GET", _path(project, _ref(args.code)))
        revision = held["revision"]
    elif args.reopen:
        return _usage("--reopen opens an opportunity the project has: name its code")
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
    print(f"Closed opportunity {written['code']} of project {project} ({args.status}): revision {written['revision']}.")
    return 0


# Registering


def _write_options(parser) -> None:
    most = opportunities.MAX_TEXT_CHARS
    parser.add_argument(
        "--statement", help=f"what the project could win (at most {opportunities.MAX_STATEMENT_CHARS} characters)"
    )
    parser.add_argument("--beneficiary", help=f"who benefits (at most {most} characters)")
    parser.add_argument("--leverage", help=f"what makes it reachable now (at most {most} characters)")
    parser.add_argument("--window-until", metavar="DATE", help="the date after which it is gone (YYYY-MM-DD)")
    parser.add_argument("--window-note", help=f"what the window is (at most {most} characters)")
    parser.add_argument("--no-window", action="store_true", help="clear the window")
    parser.add_argument(
        "--answers",
        metavar="CODES",
        help=f"the problems it answers, comma separated: P-3 or lumen/P-3 (at most {opportunities.MAX_ANSWERS})",
    )
    parser.add_argument("--no-answers", action="store_true", help="answer no problem")
    parser.add_argument("--metric", metavar="KEY", help="a key of kind number of the project's registry of signals")
    parser.add_argument("--aggregate", choices=problems.AGGREGATES, help="how the key's signals are summed up")
    parser.add_argument("--window-days", type=int, metavar="N", help="the days they are summed up over (default 90)")
    parser.add_argument("--tenant", help="the signals of this tenant alone")
    parser.add_argument("--no-metric", action="store_true", help="clear the metric")
    parser.add_argument("--rank", type=int, metavar="N", help="its place among the opportunities, 1 first")
    parser.add_argument("--owner", metavar="LOGIN", help="a member with the writer role or above")
    parser.add_argument("--reopen", action="store_true", help="open a closed opportunity again")


def register_opportunities(hsub) -> None:
    opportunity = hsub.add_parser(
        "opportunity", help="the opportunities of a project: what it could win, for whom, and what makes it reachable"
    )
    osub = opportunity.add_subparsers(dest="opportunity_command", required=True)

    listed = osub.add_parser("list", help="the project's opportunities by rank")
    listed.add_argument("--status", choices=opportunities.STATUSES, help="those of this status alone")
    listed.add_argument(
        "--closing-within",
        type=int,
        metavar="DAYS",
        help="the open ones whose window ends within DAYS days (one already past included), nearest end first",
    )
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*OPPORTUNITY_KEYS, schema="ProjectOpportunity"))
    listed.set_defaults(func=cmd_list)

    shown = osub.add_parser("show", help="an opportunity, with the problems it answers and its revisions")
    shown.add_argument("code", metavar="CODE", help="its code (O-<number>)")
    shown.add_argument("--revision", type=int, metavar="N", help="an older revision (default: the newest)")
    shown.add_argument("--project", help=PROJECT_HELP)
    json_option(
        shown,
        returns_object(*OPPORTUNITY_KEYS, "evidence_count", "source_count", schema="ProjectOpportunityShown"),
    )
    shown.set_defaults(func=cmd_show)

    written = osub.add_parser(
        "set", help="make an opportunity, or write an opportunity's next revision (the writer role on the project)"
    )
    written.add_argument("code", metavar="CODE", nargs="?", help="the opportunity's code; none: a new opportunity")
    _write_options(written)
    written.add_argument("--note", help=f"why, for the history (at most {opportunities.MAX_NOTE_CHARS} characters)")
    written.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the opportunity is at revision N")
    written.add_argument("--project", help=PROJECT_HELP)
    json_option(written, returns_object(*OPPORTUNITY_KEYS, "changed", schema="ProjectOpportunityWritten"))
    written.set_defaults(func=cmd_set)

    closed = osub.add_parser(
        "close", help="close an opportunity as captured, missed or dropped (the writer role on the project)"
    )
    closed.add_argument("code", metavar="CODE", help="its code (O-<number>)")
    closed.add_argument("--status", required=True, choices=opportunities.CLOSED, help="captured, missed or dropped")
    closed.add_argument("--reason", required=True, help=f"why (at most {opportunities.MAX_REASON_CHARS} characters)")
    closed.add_argument("--note", help=f"for the history (at most {opportunities.MAX_NOTE_CHARS} characters)")
    closed.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the opportunity is at revision N")
    closed.add_argument("--project", help=PROJECT_HELP)
    json_option(closed, returns_object(*OPPORTUNITY_KEYS, "changed", schema="ProjectOpportunityWritten"))
    closed.set_defaults(func=cmd_close)

    evidence = osub.add_parser(
        "evidence", help="the evidence of an opportunity: lines of sources that show it is there"
    )
    esub = evidence.add_subparsers(dest="evidence_command", required=True)

    seen = esub.add_parser("list", help="an opportunity's lines of evidence")
    seen.add_argument("code", metavar="CODE", help="the opportunity's code (O-<number>)")
    seen.add_argument("--project", help=PROJECT_HELP)
    json_option(seen, returns_array(*EVIDENCE_KEYS, schema="OpportunityEvidence"))
    seen.set_defaults(func=cmd_evidence_list)

    added = esub.add_parser("add", help="add a line of evidence (the writer role on the project)")
    added.add_argument("code", metavar="CODE", help="the opportunity's code (O-<number>)")
    added.add_argument("--source-kind", required=True, choices=problems.SOURCE_KINDS, help="where it was read")
    added.add_argument("--source-uri", required=True, metavar="URI", help="the source's URI or path")
    added.add_argument("--locator", help="where in the source: a message id, a time, a page")
    added.add_argument("--speaker", help="who spoke, when someone did")
    added.add_argument(
        "--excerpt", required=True, help=f"what the source says (at most {problems.MAX_EXCERPT_CHARS} characters)"
    )
    added.add_argument("--signal", required=True, choices=opportunities.SIGNALS, help="what the line shows")
    added.add_argument(
        "--label",
        required=True,
        choices=(*problems.EVIDENCE_LABELS, "personal"),
        help="public or internal; the hub refuses personal, which stays on your machine",
    )
    added.add_argument("--project", help=PROJECT_HELP)
    json_option(added, returns_object(*EVIDENCE_KEYS, schema="OpportunityEvidence"))
    added.set_defaults(func=cmd_evidence_add)

    confirmed = esub.add_parser("confirm", help="confirm a line of evidence (the writer role on the project)")
    confirmed.add_argument("code", metavar="CODE", help="the opportunity's code (O-<number>)")
    confirmed.add_argument("evidence", metavar="EVIDENCE", help="the line's code (E-<number>)")
    confirmed.add_argument("--project", help=PROJECT_HELP)
    json_option(confirmed, returns_object(*EVIDENCE_KEYS, schema="OpportunityEvidence"))
    confirmed.set_defaults(func=cmd_evidence_confirm)
