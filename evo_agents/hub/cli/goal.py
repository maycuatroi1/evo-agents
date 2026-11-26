"""``evo-agents hub goal list|show|set|close``: the goals of a project (``evo_agents.hub.goals``), over GET and PUT
/v1/projects/{p}/goals and the routes below it.

``list`` prints the project's goals by rank, those of one ``--status`` alone when given. ``show REF`` prints a goal, by
its code (G-<number>) or alias, at its newest revision or at ``--revision``, with its revisions. ``set`` writes a goal's
next revision: without REF it makes a new goal under the project's next code, which needs ``--statement``; with REF it
changes the options given and keeps the rest, at the revision the goal is at when it is read unless ``--if-revision``
says another. A metric is ``--metric KEY`` with ``--aggregate``, ``--comparator`` and ``--target``, and
``--window-days`` and ``--tenant`` optional; on a goal that has one, an option given replaces its part alone.
``--problem P-<number>`` names the problem of the project the goal answers (``evo-agents hub problem``), one that is
not dropped, and ``--opportunity O-<number>`` the opportunity of the project it answers (``evo-agents hub
opportunity``); a goal answers a problem or an opportunity, so giving one replaces the other. ``--srs-document SLUG``
aims the goal at an SRS document of the project and ``--srs-heading`` limits it to the use cases under a heading of it;
with the metric ``srs.backlog.open``, which the hub counts so no signal is pushed, the goal is reached when every
approved use case of it is delivered. ``--no-metric``, ``--no-srs-document``, ``--no-srs-heading``, ``--no-deadline``,
``--no-canvas``, ``--no-problem`` and ``--no-opportunity`` clear those. ``close REF
--reason`` closes a goal: achieved, dropped or expired.

Every member of the project reads its goals; writing one needs the admin role on the project. Every command finds its
project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory.
``--json`` prints what the hub answered. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands): :class:`Goals` is the
gateway to the routes, :class:`GoalChange` the value object that turns the options of ``set`` into the goal to write.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import ClassVar

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Views, returns_array, returns_object
from evo_agents.hub import goals
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.cli_client import when
from evo_agents.hub.client import HubError

SHOWN_STATEMENT_CHARS = 60  # of a goal's statement in a row of list
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
GOAL_KEYS = (
    "project",
    "code",
    "number",
    "alias",
    "revision",
    "statement",
    "metric",
    "deadline",
    "canvas",
    "problem",
    "opportunity",
    "srs_document",
    "srs_heading",
    "rank",
    "owner",
    "status",
    "close_reason",
    "note",
    "created_by",
    "created_at",
)
METRIC_OPTIONS = {  # the part of a metric each option sets
    "metric": "key",
    "aggregate": "aggregate",
    "window_days": "window_days",
    "comparator": "comparator",
    "target": "target",
    "tenant": "tenant",
}
NEEDED = ("metric", "aggregate", "comparator", "target")  # the options a metric cannot go without
WRITTEN_FIELDS = (
    "statement",
    "metric",
    "deadline",
    "canvas",
    "problem",
    "opportunity",
    "srs_document",
    "srs_heading",
    "rank",
    "owner",
    "status",
    "close_reason",
)
GIVEN_FIELDS = (
    "statement",
    "deadline",
    "canvas",
    "problem",
    "opportunity",
    "srs_document",
    "srs_heading",
    "rank",
    "owner",
    "status",
)  # one field an option
CLEARED_FIELDS = (
    "deadline",
    "canvas",
    "problem",
    "opportunity",
    "srs_document",
    "srs_heading",
)  # each with its --no-<field>
SHOWN_FIELDS = ("deadline", "canvas", "problem", "opportunity", "srs_document", "srs_heading", "rank", "owner")
SHOWN_WHEN_SET = ("opportunity", "srs_document", "srs_heading")  # the lines of a goal of a problem stay as they were


class Goals(ProjectResource):
    """The goals of a project: GET and PUT /v1/projects/{p}/goals and the routes below it."""

    collection = "goals"

    def list(self, status: str | None = None) -> list[dict]:
        return self.call("GET", status=status)

    def get(self, ref: str, revision: int | None = None) -> dict:
        return self.call("GET", ref, revision=revision)

    def find(self, ref: str) -> dict | None:
        """The goal ``ref`` names; None when there is none."""
        try:
            return self.get(ref)
        except HubError as exc:
            if exc.status != 404:
                raise
            return None

    def revisions(self, ref: str) -> list[dict]:
        return self.call("GET", ref, "revisions")

    def next_code(self) -> str:
        return goals.code(max((goal["number"] for goal in self.list()), default=0) + 1)

    def put(self, ref: str, body: dict) -> dict:
        return self.call("PUT", ref, body=body)

    def close(self, ref: str, body: dict) -> dict:
        return self.call("POST", ref, "close", body=body)


def clip(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


def status_text(goal: dict) -> str:
    return goal["status"] + (f" ({goal['close_reason']})" if goal.get("close_reason") else "")


# Reading


class GoalList(HubCommand):
    name = "list"
    help = "the project's goals by rank"
    arguments = (Arg("--status", choices=goals.STATUSES, help="those of this status alone"), PROJECT)
    output = returns_array(*GOAL_KEYS, schema="ProjectGoal")
    columns: ClassVar[tuple[str, ...]] = ("CODE", "STATUS", "RANK", "OWNER", "DEADLINE", "METRIC", "STATEMENT")

    def run(self, ctx: HubContext) -> Result:
        status = ctx.args.status
        found = Goals.of(ctx).list(status)
        empty = f"Project {ctx.project} has no goals{' ' + status if status else ''}."
        return Result(found, Table(self.columns, [self.row(goal) for goal in found], empty))

    @staticmethod
    def row(goal: dict) -> tuple:
        return (
            goal["code"],
            status_text(goal),
            goal["rank"],
            goal["owner"],
            goal["deadline"] or "-",
            goals.metric_text(goal["metric"]),
            clip(goal["statement"], SHOWN_STATEMENT_CHARS),
        )


class GoalShow(HubCommand):
    name = "show"
    help = "a goal, with its revisions"
    arguments = (
        Arg("ref", metavar="REF", help="its code (G-<number>) or alias"),
        Arg("--revision", type=int, metavar="N", help="an older revision (default: the newest)"),
        PROJECT,
    )
    output = returns_object(*GOAL_KEYS, schema="ProjectGoal")

    def run(self, ctx: HubContext) -> Result:
        found = Goals.of(ctx).get(ctx.args.ref, ctx.args.revision)
        if ctx.args.json:
            return Result(found)
        return Result(found, self.view(found, Goals.of(ctx).revisions(ctx.args.ref)))

    @staticmethod
    def view(goal: dict, revisions: list[dict]) -> Views:
        alias = f" ({goal['alias']})" if goal.get("alias") else ""
        lines = [
            f"Goal {goal['code']}{alias} of project {goal['project']}, revision {goal['revision']}",
            f"  statement: {goal['statement']}",
            f"  metric: {goals.metric_text(goal['metric'])}",
        ]
        for name in SHOWN_FIELDS:
            value = goal.get(name)
            if not (name in SHOWN_WHEN_SET and value is None):
                lines.append(f"  {name}: {'-' if value is None else value}")
        lines.append(f"  status: {status_text(goal)}")
        written = f"  written by {goal['created_by']} at {when(goal['created_at'])} UTC"
        lines.append(written + (f": {goal['note']}" if goal.get("note") else ""))
        if not revisions:
            return Views(Text(*lines))
        rows = [
            (item["revision"], when(item["created_at"]), item["created_by"], ", ".join(item["changed"]) or "-")
            for item in revisions
        ]
        return Views(Text(*lines, "Revisions:"), Table(("REVISION", "WRITTEN (UTC)", "BY", "CHANGED"), rows))


# Writing


@dataclass(frozen=True)
class GoalChange:
    """What the options of ``goal set`` change on a goal (Value Object): checked alone with :meth:`conflict`, then
    applied to the goal as the hub holds it with :meth:`body`. Raises UsageError for options that make no goal."""

    args: argparse.Namespace

    def conflict(self) -> str | None:
        """Why options given together say two things; None when they do not."""
        args = self.args
        if any(getattr(args, f"no_{name}") and getattr(args, name) for name in CLEARED_FIELDS if name != "opportunity"):
            return "--no-deadline, --no-canvas and --no-problem each go without the option they clear"
        if args.no_opportunity and args.opportunity:
            return "--no-opportunity goes without --opportunity"
        if args.problem is not None and args.opportunity is not None:
            return "a goal answers a problem or an opportunity: give --problem or --opportunity, not both"
        if args.no_metric and any(getattr(args, option) is not None for option in METRIC_OPTIONS):
            return "--no-metric goes without the options of a metric"
        return None

    def metric(self, held: dict | None) -> dict | None:
        """The metric the options make of ``held``, the goal's now if any."""
        given = {
            part: value for option, part in METRIC_OPTIONS.items() if (value := getattr(self.args, option)) is not None
        }
        if self.args.no_metric:
            return None
        if not given:
            return held
        metric = {**(held or {}), **given}
        missing = [f"--{option.replace('_', '-')}" for option in NEEDED if METRIC_OPTIONS[option] not in metric]
        if missing:
            raise UsageError(f"a metric needs {', '.join(missing)}")
        return metric

    def body(self, held: dict) -> dict:
        """The goal to write: ``held`` (its fields now, {} for a new goal) with the options given. A field left out of
        the body is empty, or kept for its rank and owner."""
        args = self.args
        body = {name: held.get(name) for name in WRITTEN_FIELDS} | {"metric": self.metric(held.get("metric"))}
        body |= {name: getattr(args, name) for name in GIVEN_FIELDS if getattr(args, name) is not None}
        if args.problem is not None:
            body["opportunity"] = None  # a goal answers a problem or an opportunity: the one given replaces the other
        if args.opportunity is not None:
            body["problem"] = None
        body |= {name: None for name in CLEARED_FIELDS if getattr(args, f"no_{name}")}
        if args.no_srs_document and args.srs_heading is None:
            body["srs_heading"] = None  # a heading belongs to the document it is in
        if not body["statement"]:
            raise UsageError("a new goal needs --statement")
        if args.status is not None:
            body["close_reason"] = None  # --status is active or paused: a closed goal opens again
        body["status"] = body["status"] or goals.ACTIVE
        body["note"] = args.note
        return {name: value for name, value in body.items() if value is not None}


NOTE = Arg("--note", help="why, for the history (at most 1000 characters)")
IF_REVISION = Arg("--if-revision", type=int, metavar="N", help="refuse unless the goal is at revision N")
WRITE_OPTIONS = (
    Arg("--statement", help="what the project is for, in one statement (at most 2000 characters)"),
    Arg("--metric", metavar="KEY", help="a key of kind number of the project's registry of signals"),
    Arg("--aggregate", choices=goals.AGGREGATES, help="how the key's signals are summed up"),
    Arg("--window-days", type=int, metavar="N", help="the days they are summed up over (default 90)"),
    Arg("--comparator", choices=goals.COMPARATORS, help="how the value compares with the target"),
    Arg("--target", type=float, metavar="X", help="the value the goal reaches"),
    Arg("--tenant", help="the signals of this tenant alone"),
    Arg("--no-metric", action="store_true", help="clear the metric"),
    Arg("--deadline", metavar="YYYY-MM-DD", help="when the goal is due"),
    Arg("--no-deadline", action="store_true", help="clear the deadline"),
    Arg("--canvas", metavar="SLUG", help="the canvas whose product the goal drives"),
    Arg("--no-canvas", action="store_true", help="clear the canvas"),
    Arg("--problem", metavar="P-N", help="the problem of the project the goal answers, not dropped"),
    Arg("--no-problem", action="store_true", help="clear the problem"),
    Arg("--opportunity", metavar="O-N", help="the opportunity of the project the goal answers, not dropped"),
    Arg("--no-opportunity", action="store_true", help="clear the opportunity"),
    Arg(
        "--srs-document",
        metavar="SLUG",
        help="the SRS document of the project whose approved use cases the goal delivers; with metric "
        "srs.backlog.open, the hub counts the use cases not delivered yet",
    ),
    Arg("--no-srs-document", action="store_true", help="clear the SRS document and its heading"),
    Arg("--srs-heading", metavar="HEADING", help="limit the goal to the use cases under this heading"),
    Arg("--no-srs-heading", action="store_true", help="clear the heading"),
    Arg("--rank", type=int, metavar="N", help="its place among the goals, 1 first"),
    Arg("--owner", metavar="LOGIN", help="a member with the writer role or above"),
    Arg("--status", choices=(goals.ACTIVE, goals.PAUSED), help="active or paused"),
)


class GoalSet(HubCommand):
    name = "set"
    help = "make a goal, or write a goal's next revision (the admin role on the project)"
    arguments = (
        Arg("ref", metavar="REF", nargs="?", help="the goal's code or alias; none: a new goal"),
        *WRITE_OPTIONS,
        NOTE,
        IF_REVISION,
        PROJECT,
    )
    output = returns_object(*GOAL_KEYS, "changed", schema="ProjectGoalWritten")

    def check(self, args: argparse.Namespace) -> None:
        why = GoalChange(args).conflict()
        if why:
            raise UsageError(why)

    def run(self, ctx: HubContext) -> Result:
        resource = Goals.of(ctx)
        held = resource.find(ctx.args.ref) if ctx.args.ref is not None else None
        body = GoalChange(ctx.args).body(held or {})
        ref = held["code"] if held else ctx.args.ref or resource.next_code()
        body["if_revision"] = (
            (held["revision"] if held else 0) if ctx.args.if_revision is None else ctx.args.if_revision
        )
        written = resource.put(ref, body)
        return Result(written, Text(self.said(written, ctx.project)))

    @staticmethod
    def said(written: dict, project: str) -> str:
        if not written["changed"]:
            return (
                f"Goal {written['code']} of project {project} is this already, at revision {written['revision']}: "
                "nothing written."
            )
        made = "Made" if written["revision"] == 1 else "Wrote"
        return f"{made} goal {written['code']} of project {project}: revision {written['revision']}."


class GoalClose(HubCommand):
    name = "close"
    help = "close a goal for a reason (the admin role on the project)"
    arguments = (
        Arg("ref", metavar="REF", help="its code (G-<number>) or alias"),
        Arg("--reason", required=True, choices=goals.CLOSE_REASONS, help="why it is closed"),
        NOTE,
        IF_REVISION,
        PROJECT,
    )
    output = returns_object(*GOAL_KEYS, "changed", schema="ProjectGoalWritten")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"reason": args.reason, "note": args.note or None, "if_revision": args.if_revision}
        written = Goals.of(ctx).close(args.ref, {name: value for name, value in body.items() if value is not None})
        said = (
            f"Closed goal {written['code']} of project {ctx.project} ({args.reason}): revision {written['revision']}."
        )
        return Result(written, Text(said))


GOAL = Group(
    "goal",
    "the goals of a project: a statement, a metric and a target",
    [GoalList(), GoalShow(), GoalSet(), GoalClose()],
)
