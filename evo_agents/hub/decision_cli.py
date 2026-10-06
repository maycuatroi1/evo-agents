"""``evo-agents hub decision`` and ``evo-agents hub notifications``: the decisions the agents of your plan runs ask you,
and what the hub tells you.

``decision list`` and ``decision show`` read a project's decisions (GET /v1/projects/{p}/decisions, .../{id}), as any
reader of the run's plan may. ``decision answer ID`` answers one (POST .../decisions/{id}/answer) with an option of
the decision (``--option KEY``), words of your own (``--text``, ``-`` reads them from stdin), or both; only the member
who dispatched the run may, and the answer goes to the agent through the run's inbox, or resumes a parked run on its
worker. ``docs/notifications.md`` describes decisions, their categories and their life.

``notifications`` lists your notifications, open decisions first, then newest first (GET /v1/me/notifications), with
how many are unread and how many decisions wait for your answer (GET /v1/me/notifications/count). ``--read all`` or
``--read 12,14`` marks them read instead (POST /v1/me/notifications/read) and lists nothing, so it takes none of the
filters. Notifications are a member's own, so ``--project`` only filters them and never comes from a harness.

The decision commands find their project as ``hub run`` does: ``--project``, or ``hub.project`` in the harness.yaml
around the current directory. ``--json`` prints what the hub answered, with the keys declared next to the flag.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

from evo_agents.hub.cli_client import _client_command, _print_json, _project_path, _signed_in, _table, _when
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.plan_cli import _plan_id, _project
from evo_agents.hub.runs import DECISION_STATES, NOTIFICATION_KINDS

EXIT_USAGE = 2
QUESTION_CHARS = 60  # of a decision's question in the decision table
TITLE_CHARS = 60  # of a notification's title in the notification table
MAX_LIST = 200  # the most decisions or notifications the hub answers at once

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
DECISION_KEYS = (
    "id",
    "project",
    "run_id",
    "run_state",
    "plan_id",
    "step_key",
    "category",
    "question",
    "context",
    "options",
    "recommended",
    "state",
    "owner",
    "answer_option",
    "answer_text",
    "answered_by",
    "answer_run_id",
    "asked_at",
    "answered_at",
    "delivered_at",
)
DECISION_LIST_KEYS = ("decisions", "total", "limit", "offset")
NOTIFICATION_LIST_KEYS = ("notifications", "total", "limit", "offset")
READ_KEYS = ("read", "unread")
DECISION = returns_object(*DECISION_KEYS, schema="Decision")


def _decisions_path(project: str, decision_id: int | None = None, action: str | None = None) -> str:
    path = f"{_project_path(project)}/decisions"
    if decision_id is not None:
        path += f"/{decision_id}"
    return path + (f"/{action}" if action else "")


def _one_line(text: str | None, limit: int) -> str:
    flat = " ".join((text or "").split()) or "-"
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _with_project(args, project: str) -> str:
    """The --project to repeat in a command printed for the person, when the harness did not give the project."""
    return f" --project {project}" if args.project else ""


def _usage_error(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _option_label(decision: dict, key: str | None) -> str | None:
    return next((option["label"] for option in decision["options"] if option["key"] == key), None)


# Decisions


@_client_command
def cmd_list(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    query: list[tuple[str, object]] = [("state", state) for state in args.state or ()]
    if args.run is not None:
        query.append(("run_id", args.run))
    if args.plan is not None:
        query.append(("plan_id", _plan_id(args.plan)))
    query += [(key, value) for key, value in (("limit", args.limit), ("offset", args.offset)) if value is not None]
    listed = hub.call("GET", _decisions_path(project) + (f"?{urlencode(query)}" if query else ""))
    if args.json:
        _print_json(listed)
        return 0
    rows = [
        (
            f"#{decision['id']}",
            decision["state"],
            decision["category"],
            f"#{decision['run_id']}",
            decision["plan_id"],
            decision["step_key"] or "-",
            decision["owner"],
            _one_line(decision["question"], QUESTION_CHARS),
            _when(decision["asked_at"]),
        )
        for decision in listed["decisions"]
    ]
    if rows:
        _table(("DECISION", "STATE", "CATEGORY", "RUN", "PLAN", "STEP", "OWNER", "QUESTION", "ASKED (UTC)"), rows)
    shown = len(rows)
    print(f"{shown} of {listed['total']} decision(s) of project {project}")
    if listed["offset"] + shown < listed["total"]:
        print(f"More with --offset {listed['offset'] + shown}.")
    if any(decision["state"] == "open" for decision in listed["decisions"]):
        where = _with_project(args, project)
        print(
            f"Read one with `evo-agents hub decision show ID{where}`, and answer it with "
            f"`evo-agents hub decision answer ID --option KEY{where}`."
        )
    return 0


def _answer_lines(decision: dict) -> list[tuple[str, str]]:
    if decision["state"] != "answered":
        return []
    lines = [("answered", f"by {decision['answered_by']} at {_when(decision['answered_at'])} UTC")]
    if decision["answer_option"]:
        label = _option_label(decision, decision["answer_option"])
        lines.append(("option", decision["answer_option"] + (f", {label}" if label else "")))
    if decision["answer_text"]:
        lines.append(("text", decision["answer_text"]))
    inbox = decision["answer_run_id"]
    if inbox is not None and inbox != decision["run_id"]:
        lines.append(("resumed", f"as run #{inbox}, whose inbox took the answer"))
    delivered = decision["delivered_at"]
    lines.append(("delivered", f"to the agent at {_when(delivered)} UTC" if delivered else "not to the agent yet"))
    return lines


def _print_fields(lines: list[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in lines)
    for label, text in lines:
        first, *rest = str(text).splitlines() or [""]
        print(f"  {label:<{width}}  {first}".rstrip())
        for line in rest:
            print(f"  {'':<{width}}  {line}".rstrip())


def _print_block(title: str, text: str) -> None:
    print(f"{title}:")
    for line in text.splitlines() or [""]:
        print(f"  {line}".rstrip())


def _print_options(decision: dict) -> None:
    print("Options:")
    width = max(len(option["key"]) for option in decision["options"])
    for option in decision["options"]:
        marks = [mark for mark, on in (("recommended", option["recommended"]),) if on]
        if decision["answer_option"] == option["key"]:
            marks.append("chosen")
        label = option["label"] + (f" ({', '.join(marks)})" if marks else "")
        print(f"  {option['key']:<{width}}  {label}")
        for line in (option["description"] or "").splitlines():
            print(f"  {'':<{width}}  {line}".rstrip())


@_client_command
def cmd_show(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    decision = hub.call("GET", _decisions_path(project, args.decision))
    if args.json:
        _print_json(decision)
        return 0
    print(f"Decision #{decision['id']}: {decision['category']}, {decision['state']}")
    step = f", step {decision['step_key']}" if decision["step_key"] else ""
    lines = [
        ("run", f"#{decision['run_id']} ({decision['run_state']}) of plan {decision['plan_id']}{step}"),
        ("owner", f"{decision['owner']}, who alone answers it"),
        ("asked", f"{_when(decision['asked_at'])} UTC"),
        *_answer_lines(decision),
    ]
    _print_fields(lines)
    _print_block("Question", decision["question"])
    if decision["context"]:
        _print_block("Context", decision["context"])
    _print_options(decision)
    if decision["state"] == "open":
        print(
            f"Answer it with `evo-agents hub decision answer {decision['id']} --option KEY"
            f"{_with_project(args, project)}`; --text adds words of your own, or stands for an option."
        )
    return 0


@_client_command
def cmd_answer(args) -> int:
    if args.option is None and args.text is None:
        return _usage_error("an answer names an option with --option KEY, gives words with --text TEXT, or both")
    text = sys.stdin.read() if args.text == "-" else args.text
    hub, _ = _signed_in()
    project = _project(args)
    body = {key: value for key, value in (("option", args.option), ("text", text)) if value is not None}
    decision = hub.call("POST", _decisions_path(project, args.decision, "answer"), body)
    if args.json:
        _print_json(decision)
        return 0
    given = []
    if decision["answer_option"]:
        label = _option_label(decision, decision["answer_option"])
        given.append(f"option {decision['answer_option']}" + (f" ({label})" if label else ""))
    if decision["answer_text"]:
        given.append("your words")
    print(f"Answered decision #{decision['id']} with {' and '.join(given)}.")
    inbox = decision["answer_run_id"]
    if inbox is not None and inbox != decision["run_id"]:
        print(
            f"Run #{decision['run_id']} was parked: the hub queued run #{inbox} on the same worker to resume it in its "
            "session, and the answer waits in its inbox."
        )
    else:
        print(f"The answer went to the inbox of run #{decision['run_id']}; its worker hands it to the agent.")
    return 0


# Notifications


def _read_body(which: str) -> dict | None:
    """``--read all`` or ``--read 12,14`` as the body of POST /v1/me/notifications/read; None when it is neither."""
    if which.strip() == "all":
        return {"all": True}
    parts = [part.strip() for part in which.split(",")]
    if not parts or not all(part.isascii() and part.isdigit() and int(part) > 0 for part in parts):
        return None
    return {"ids": list(dict.fromkeys(int(part) for part in parts))}


def _kind(notification: dict) -> str:
    if notification["kind"] == "decision":
        state = f" ({notification['decision_state']})" if notification["decision_state"] else ""
        return f"decision #{notification['decision_id']}{state}"
    return notification["notice_kind"] or notification["kind"]


@_client_command
def cmd_notifications(args) -> int:
    if args.read is not None:
        given = (
            ("--unread", args.unread or None),
            ("--kind", args.kind),
            ("--project", args.project),
            ("--limit", args.limit),
            ("--offset", args.offset),
        )
        filters = [flag for flag, value in given if value is not None]
        if filters:
            return _usage_error(f"--read marks notifications read and lists none, so it does not go with {filters[0]}")
        body = _read_body(args.read)
        if body is None:
            return _usage_error(f"--read takes all, or notification ids such as 12,14, not {args.read!r}")
        hub, _ = _signed_in()
        result = hub.call("POST", "/v1/me/notifications/read", body)
        if args.json:
            _print_json(result)
            return 0
        print(f"Marked {result['read']} notification(s) read; {result['unread']} left unread.")
        return 0
    query: list[tuple[str, object]] = [("unread", "true")] if args.unread else []
    given = (("kind", args.kind), ("project", args.project), ("limit", args.limit), ("offset", args.offset))
    query += [(key, value) for key, value in given if value is not None]
    hub, _ = _signed_in()
    listed = hub.call("GET", "/v1/me/notifications" + (f"?{urlencode(query)}" if query else ""))
    if args.json:
        _print_json(listed)
        return 0
    rows = [
        (
            f"#{notification['id']}",
            "read" if notification["read_at"] else "unread",
            _kind(notification),
            notification["project"] or "-",
            f"#{notification['run_id']}" if notification["run_id"] else "-",
            _one_line(notification["title"], TITLE_CHARS),
            _when(notification["created_at"]),
        )
        for notification in listed["notifications"]
    ]
    if rows:
        _table(("ID", "READ", "KIND", "PROJECT", "RUN", "TITLE", "WHEN (UTC)"), rows)
    counts = hub.call("GET", "/v1/me/notifications/count")
    shown = len(rows)
    which = "unread notification(s)" if args.unread else "notification(s)"
    print(
        f"{shown} of {listed['total']} {which}; {counts['unread']} unread in all, {counts['open_decisions']} "
        "decision(s) waiting for your answer"
    )
    if listed["offset"] + shown < listed["total"]:
        print(f"More with --offset {listed['offset'] + shown}.")
    if counts["unread"]:
        print("Mark them read with `evo-agents hub notifications --read all`, or --read with their ids.")
    return 0


def register_decisions(hsub) -> None:
    decision = hsub.add_parser("decision", help="the decisions the agents of your plan runs ask you, and your answers")
    dsub = decision.add_subparsers(dest="decision_command", required=True)
    project_help = "hub project (default: hub.project in the harness.yaml around the current directory)"
    decision_help = "the decision's id, as `hub decision list` and the notification show it"

    def with_project(parser) -> None:
        parser.add_argument("--project", help=project_help)

    def with_decision(parser) -> None:
        parser.add_argument("decision", metavar="DECISION", type=int, help=decision_help)

    listed = dsub.add_parser("list", help="the decisions of a project, newest first, of the plans you may read")
    with_project(listed)
    listed.add_argument(
        "--state",
        action="append",
        choices=DECISION_STATES,
        metavar="STATE",
        help=f"only decisions in this state, one of {', '.join(DECISION_STATES)}; repeat it for several",
    )
    listed.add_argument("--run", type=int, metavar="RUN", help="only the decisions of this run")
    listed.add_argument("--plan", help="only the decisions of this plan")
    listed.add_argument(
        "--limit", type=int, choices=range(1, MAX_LIST + 1), metavar="N", help=f"1 to {MAX_LIST} (default 50)"
    )
    listed.add_argument("--offset", type=int, metavar="N", help="skip this many decisions, for the next page")
    json_option(listed, returns_object(*DECISION_LIST_KEYS, schema="DecisionList"))
    listed.set_defaults(func=cmd_list)

    show = dsub.add_parser("show", help="one decision: its question, context, options and answer")
    with_decision(show)
    with_project(show)
    json_option(show, DECISION)
    show.set_defaults(func=cmd_show)

    answer = dsub.add_parser(
        "answer",
        help="answer an open decision of a run you dispatched, with an option, words of your own, or both; the "
        "answer goes to the agent, and a parked run resumes",
    )
    with_decision(answer)
    answer.add_argument("--option", metavar="KEY", help="the key of one of the decision's options")
    answer.add_argument("--text", metavar="TEXT", help="your own words, at most 4 KiB; - reads them from stdin")
    with_project(answer)
    json_option(answer, DECISION)
    answer.set_defaults(func=cmd_answer)

    notifications = hsub.add_parser(
        "notifications",
        help="your notifications, open decisions first, then newest first; --read marks them read instead",
    )
    notifications.add_argument("--unread", action="store_true", help="only those not read yet")
    notifications.add_argument("--kind", choices=NOTIFICATION_KINDS, help="only decisions, or only notices")
    notifications.add_argument("--project", help="only those of this project (default: every project's)")
    notifications.add_argument(
        "--limit", type=int, choices=range(1, MAX_LIST + 1), metavar="N", help=f"1 to {MAX_LIST} (default 50)"
    )
    notifications.add_argument("--offset", type=int, metavar="N", help="skip this many, for the next page")
    notifications.add_argument(
        "--read",
        metavar="WHICH",
        help="mark notifications read and list none: all, or ids such as 12,14",
    )
    json_option(
        notifications,
        returns_object(
            *NOTIFICATION_LIST_KEYS,
            schema="NotificationList",
            variants=[("--read", returns_object(*READ_KEYS, schema="ReadResult"))],
        ),
    )
    notifications.set_defaults(func=cmd_notifications)
