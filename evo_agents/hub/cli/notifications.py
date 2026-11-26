"""``evo-agents hub notifications``: what the hub tells you, and the decisions that wait for your answer.

It lists your notifications, open decisions first, then newest first (GET /v1/me/notifications), with how many are
unread and how many decisions wait for your answer (GET /v1/me/notifications/count). ``--read all`` or ``--read
12,14`` marks them read instead (POST /v1/me/notifications/read) and lists nothing, so it takes none of the filters.
Notifications are a member's own, so ``--project`` only filters them and never comes from a harness. ``--json``
prints what the hub answered. The decisions themselves are ``evo-agents hub decision`` (``evo_agents.hub.cli.decision``).

The command is a class of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`MyNotifications`
is the gateway to the routes, :func:`read_body` turns ``--read`` into the body of the route that marks them read.
"""

from __future__ import annotations

import argparse
from typing import ClassVar
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Result, Table, Text, UsageError, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.decision import MAX_LIST, one_line
from evo_agents.hub.cli_client import call_object, when
from evo_agents.hub.client import Hub
from evo_agents.hub.runs import NOTIFICATION_KINDS

TITLE_CHARS = 60  # of a notification's title in the notification table
NOTIFICATION_LIST_KEYS = ("notifications", "total", "limit", "offset")
READ_KEYS = ("read", "unread")
QUERY_KEYS = {  # each option that filters the list, with the key of the query it sets
    "--unread": "unread",
    "--kind": "kind",
    "--project": "project",
    "--run": "run",
    "--search": "q",
    "--limit": "limit",
    "--offset": "offset",
}


class MyNotifications:
    """The signed-in member's notifications: GET /v1/me/notifications, .../count and POST .../read (Gateway)."""

    path = "/v1/me/notifications"

    def __init__(self, hub: Hub):
        self.hub = hub

    def list(self, query: list[tuple[str, object]]) -> dict:
        return call_object(self.hub, "GET", self.path + (f"?{urlencode(query)}" if query else ""))

    def count(self) -> dict:
        return call_object(self.hub, "GET", f"{self.path}/count")

    def read(self, body: dict) -> dict:
        return call_object(self.hub, "POST", f"{self.path}/read", body)


def given_filters(args: argparse.Namespace) -> list[tuple[str, object]]:
    """The filters given, as (flag, value): ``--unread`` when set, the others when not None."""
    given = (
        ("--unread", args.unread or None),
        ("--kind", args.kind),
        ("--project", args.project),
        ("--run", args.run),
        ("--search", args.search),
        ("--limit", args.limit),
        ("--offset", args.offset),
    )
    return [(flag, value) for flag, value in given if value is not None]


def list_query(args: argparse.Namespace) -> list[tuple[str, object]]:
    """The query of GET /v1/me/notifications for the filters given."""
    return [(QUERY_KEYS[flag], "true" if flag == "--unread" else value) for flag, value in given_filters(args)]


def read_body(which: str) -> dict | None:
    """``--read all`` or ``--read 12,14`` as the body of POST /v1/me/notifications/read; None when it is neither."""
    if which.strip() == "all":
        return {"all": True}
    parts = [part.strip() for part in which.split(",")]
    if not parts or not all(part.isascii() and part.isdigit() and int(part) > 0 for part in parts):
        return None
    return {"ids": list(dict.fromkeys(int(part) for part in parts))}


def kind_text(notification: dict) -> str:
    if notification["kind"] == "decision":
        state = f" ({notification['decision_state']})" if notification["decision_state"] else ""
        return f"decision #{notification['decision_id']}{state}"
    if notification["kind"] == "proposal":
        state = f" ({notification['proposal_state']})" if notification.get("proposal_state") else ""
        return f"proposal #{notification.get('proposal_id')}{state}"
    return notification["notice_kind"] or notification["kind"]


class Notifications(HubCommand):
    name = "notifications"
    help = "your notifications, open decisions first, then newest first; --read marks them read instead"
    arguments = (
        Arg("--unread", action="store_true", help="only those not read yet"),
        Arg("--kind", choices=NOTIFICATION_KINDS, help="only decisions, or only notices"),
        Arg("--project", help="only those of this project (default: every project's)"),
        Arg("--run", type=int, metavar="ID", help="only those of this run"),
        Arg("--search", metavar="WORDS", help="only those whose title or body holds these words"),
        Arg("--limit", type=int, choices=range(1, MAX_LIST + 1), metavar="N", help=f"1 to {MAX_LIST} (default 50)"),
        Arg("--offset", type=int, metavar="N", help="skip this many, for the next page"),
        Arg("--read", metavar="WHICH", help="mark notifications read and list none: all, or ids such as 12,14"),
    )
    output = returns_object(
        *NOTIFICATION_LIST_KEYS,
        schema="NotificationList",
        variants=[("--read", returns_object(*READ_KEYS, schema="ReadResult"))],
    )
    columns: ClassVar[tuple[str, ...]] = ("ID", "READ", "KIND", "PROJECT", "RUN", "TITLE", "WHEN (UTC)")

    def check(self, args: argparse.Namespace) -> None:
        if args.read is None:
            return
        filters = given_filters(args)
        if filters:
            raise UsageError(f"--read marks notifications read and lists none, so it does not go with {filters[0][0]}")
        if read_body(args.read) is None:
            raise UsageError(f"--read takes all, or notification ids such as 12,14, not {args.read!r}")

    def run(self, ctx: HubContext) -> Result:
        args, mine = ctx.args, MyNotifications(ctx.hub)
        if args.read is not None:
            result = mine.read(read_body(args.read) or {})
            return Result(
                result, Text(f"Marked {result['read']} notification(s) read; {result['unread']} left unread.")
            )
        listed = mine.list(list_query(args))
        if args.json:
            return Result(listed)
        rows = [self.row(notification) for notification in listed["notifications"]]
        return Result(listed, Views(Table(self.columns, rows), Text(*self.summary(listed, mine.count(), args))))

    @staticmethod
    def row(notification: dict) -> tuple:
        return (
            f"#{notification['id']}",
            "read" if notification["read_at"] else "unread",
            kind_text(notification),
            notification["project"] or "-",
            f"#{notification['run_id']}" if notification["run_id"] else "-",
            one_line(notification["title"], TITLE_CHARS),
            when(notification["created_at"]),
        )

    @staticmethod
    def summary(listed: dict, counts: dict, args: argparse.Namespace) -> list[str]:
        shown = len(listed["notifications"])
        which = "unread notification(s)" if args.unread else "notification(s)"
        proposals = counts.get("open_proposals") or 0
        waiting = f" and {proposals} proposal(s)" if proposals else ""
        lines = [
            f"{shown} of {listed['total']} {which}; {counts['unread']} unread in all, {counts['open_decisions']} "
            f"decision(s){waiting} waiting for your answer"
        ]
        if listed["offset"] + shown < listed["total"]:
            lines.append(f"More with --offset {listed['offset'] + shown}.")
        if counts["unread"]:
            lines.append("Mark them read with `evo-agents hub notifications --read all`, or --read with their ids.")
        return lines


NOTIFICATIONS = Notifications()
