"""``evo-agents hub problem list|show|set|close`` and ``problem evidence add|list|confirm``: the problems of a project
(``evo_agents.hub.problems``), over GET and PUT /v1/projects/{p}/problems and the routes below it.

``problem evidence add CODE`` adds a line of evidence to a problem (plan problem-intake): the kind and address of its
source (``--source-kind``, ``--source-uri``), where in it (``--locator``), who spoke (``--speaker``), an excerpt of at
most 300 characters (``--excerpt``), the signal it shows (``--signal``) and its label (``--label`` public or internal;
the hub refuses personal). ``evidence list CODE`` prints a problem's lines, ``evidence confirm CODE E-<n>`` confirms
one. ``show`` counts the lines and the independent sources they come from.

``list`` prints the project's problems by rank, those of one ``--status`` alone when given. ``show CODE`` prints a
problem (P-<number>) at its newest revision or at ``--revision``, with the goals that answer it, the claims of the
canvases that link it, the plans that name it, and its revisions.
``set`` writes a problem's next revision: without CODE it makes a new problem under the project's next code, which
needs ``--statement``, ``--who``, ``--when`` and ``--workaround``; with CODE it changes the options given and keeps the
rest, at the revision the problem is at when it is read unless ``--if-revision`` says another. A metric is ``--metric
KEY`` with ``--aggregate``, and ``--window-days`` and ``--tenant`` optional; it has no target. ``--no-metric`` clears
it, and ``--reopen`` opens a solved or dropped problem again. ``close CODE --status solved|dropped --reason TEXT``
closes a problem.

Every member of the project reads its problems; writing one needs the writer role on the project. Every command finds
its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory.
``--json`` prints what the hub answered (``list`` an array). Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` on the bases of ``cli.ledger``, which ``cli.opportunity`` shares:
:class:`Problems` is the gateway to the routes.
"""

from __future__ import annotations

import argparse
from typing import ClassVar

from evo_agents.clikit import Arg, Group, UsageError, returns_array, returns_object
from evo_agents.hub import problems
from evo_agents.hub.cli.base import PROJECT, when
from evo_agents.hub.cli.ledger import (
    METRIC_OPTIONS,
    SHOWN_STATEMENT_CHARS,
    AddEvidence,
    CloseItem,
    ConfirmEvidence,
    Ledger,
    ListEvidence,
    ListItems,
    SetItem,
    ShowItem,
    clip,
    metric,
)

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
EVIDENCE_KEYS = (
    "project",
    "problem",
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
WRITTEN_FIELDS = problems.FIELDS
GIVEN_FIELDS = ("statement", "who", "when", "workaround", "rank", "owner")  # the options that set one field each
CODE = Arg("code", metavar="CODE", help="its code (P-<number>)")
THE_CODE = Arg("code", metavar="CODE", help="the problem's code (P-<number>)")
IF_REVISION = Arg("--if-revision", type=int, metavar="N", help="refuse unless the problem is at revision N")


class Problems(Ledger):
    """The problems of a project: GET and PUT /v1/projects/{p}/problems and the routes below it."""

    collection = "problems"

    def code(self, number: int) -> str:
        return problems.code(number)


class ProblemCommand:
    """The noun and the gateway of every problem command (mixed in first)."""

    noun: ClassVar[str] = "problem"
    a_noun: ClassVar[str] = "a problem"
    ledger: ClassVar[type[Ledger]] = Problems


# Reading


class ProblemList(ProblemCommand, ListItems):
    name = "list"
    help = "the project's problems by rank"
    arguments = (Arg("--status", choices=problems.STATUSES, help="those of this status alone"), PROJECT)
    output = returns_array(*PROBLEM_KEYS, schema="ProjectProblem")
    columns = ("CODE", "STATUS", "RANK", "OWNER", "METRIC", "STATEMENT")
    plural = "problems"

    def row(self, item: dict) -> tuple:
        return (
            item["code"],
            item["status"],
            item["rank"],
            item["owner"],
            problems.metric_text(item["metric"]),
            clip(item["statement"], SHOWN_STATEMENT_CHARS),
        )


class ProblemShow(ProblemCommand, ShowItem):
    name = "show"
    help = "a problem, with the goals that answer it and its revisions"
    arguments = (
        CODE,
        Arg("--revision", type=int, metavar="N", help="an older revision (default: the newest)"),
        PROJECT,
    )
    output = returns_object(
        *PROBLEM_KEYS,
        "goals",
        "claims",
        "plans",
        "evidence_count",
        "source_count",
        schema="ProjectProblemShown",
    )

    def lines(self, item: dict) -> list[str]:
        written = f"  written by {item['created_by']} at {when(item['created_at'])} UTC"
        return [
            f"Problem {item['code']} of project {item['project']}, revision {item['revision']}",
            f"  statement: {item['statement']}",
            f"  who: {item['who']}",
            f"  when: {item['when']}",
            f"  workaround: {item['workaround']}",
            f"  metric: {problems.metric_text(item['metric'])}",
            f"  rank: {item['rank']}",
            f"  owner: {item['owner']}",
            f"  status: {problems.status_text(item)}",
            written + (f": {item['note']}" if item.get("note") else ""),
            *self.links(item),
            *self.evidence_line(item),
        ]

    @staticmethod
    def links(item: dict) -> list[str]:
        """The goals that answer the problem, the claims that link it and the plans that name it."""
        answering = item.get("goals") or []
        lines = ["Goals:" if answering else "Goals: none answers it yet"]
        lines += [
            f"  {goal['code']} ({goal['status']}): {clip(goal['statement'], SHOWN_STATEMENT_CHARS)}"
            for goal in answering
        ]
        claims = item.get("claims") or []
        lines.append("Claims:" if claims else "Claims: none links it yet")
        for claim in claims:
            where = f"{claim['canvas']}, {claim['cell']}, {claim['status']}"
            lines.append(f"  {claim['code']} ({where}): {clip(claim['text'], SHOWN_STATEMENT_CHARS)}")
        plans = item.get("plans") or []
        lines.append("Plans:" if plans else "Plans: none names it yet")
        for plan in plans:
            done = f"{plan['area']}, {plan['steps_done']}/{plan['steps_total']} steps done"
            title = f": {clip(plan['title'], SHOWN_STATEMENT_CHARS)}" if plan.get("title") else ""
            lines.append(f"  {plan['plan_id']} ({done}){title}")
        return lines


# Writing


class ProblemSet(ProblemCommand, SetItem):
    name = "set"
    help = "make a problem, or write a problem's next revision (the writer role on the project)"
    arguments = (
        Arg("code", metavar="CODE", nargs="?", help="the problem's code; none: a new problem"),
        Arg("--statement", help=f"what hurts, before any solution (at most {problems.MAX_STATEMENT_CHARS} characters)"),
        Arg("--who", help=f"who is hurt (at most {problems.MAX_TEXT_CHARS} characters)"),
        Arg("--when", help=f"when it happens (at most {problems.MAX_TEXT_CHARS} characters)"),
        Arg("--workaround", help=f"how they cope today (at most {problems.MAX_TEXT_CHARS} characters)"),
        Arg("--metric", metavar="KEY", help="a key of kind number of the project's registry of signals"),
        Arg("--aggregate", choices=problems.AGGREGATES, help="how the key's signals are summed up"),
        Arg("--window-days", type=int, metavar="N", help="the days they are summed up over (default 90)"),
        Arg("--tenant", help="the signals of this tenant alone"),
        Arg("--no-metric", action="store_true", help="clear the metric"),
        Arg("--rank", type=int, metavar="N", help="its place among the problems, 1 first"),
        Arg("--owner", metavar="LOGIN", help="a member with the writer role or above"),
        Arg("--reopen", action="store_true", help="open a solved or dropped problem again"),
        Arg("--note", help=f"why, for the history (at most {problems.MAX_NOTE_CHARS} characters)"),
        IF_REVISION,
        PROJECT,
    )
    output = returns_object(*PROBLEM_KEYS, "changed", schema="ProjectProblemWritten")

    def check(self, args: argparse.Namespace) -> None:
        if args.no_metric and any(getattr(args, option) is not None for option in METRIC_OPTIONS):
            raise UsageError("--no-metric goes without the options of a metric")

    def body(self, args: argparse.Namespace, held: dict) -> dict:
        """A field left out of the body is empty, or kept for its rank and owner."""
        body = {name: held.get(name) for name in WRITTEN_FIELDS} | {"metric": metric(args, held.get("metric"))}
        body |= {name: getattr(args, name) for name in GIVEN_FIELDS if getattr(args, name) is not None}
        missing = [f"--{name}" for name in problems.TEXTS if not body[name]]
        if missing:
            raise UsageError(f"a new problem needs {', '.join(missing)}")
        if args.reopen:
            body["status"], body["close_reason"] = problems.OPEN, None
        body["status"] = body["status"] or problems.OPEN
        body["note"] = args.note
        return {name: value for name, value in body.items() if value is not None}


class ProblemClose(ProblemCommand, CloseItem):
    name = "close"
    help = "close a problem as solved or dropped (the writer role on the project)"
    arguments = (
        CODE,
        Arg("--status", required=True, choices=problems.CLOSED, help="solved or dropped"),
        Arg("--reason", required=True, help=f"why (at most {problems.MAX_REASON_CHARS} characters)"),
        Arg("--note", help=f"for the history (at most {problems.MAX_NOTE_CHARS} characters)"),
        IF_REVISION,
        PROJECT,
    )
    output = returns_object(*PROBLEM_KEYS, "changed", schema="ProjectProblemWritten")


# Evidence


class ProblemEvidenceList(ProblemCommand, ListEvidence):
    name = "list"
    help = "a problem's lines of evidence"
    arguments = (THE_CODE, PROJECT)
    output = returns_array(*EVIDENCE_KEYS, schema="ProblemEvidence")


class ProblemEvidenceAdd(ProblemCommand, AddEvidence):
    name = "add"
    help = "add a line of evidence to a problem (the writer role on the project)"
    arguments = (
        THE_CODE,
        Arg("--source-kind", required=True, choices=problems.SOURCE_KINDS, help="where it was read"),
        Arg("--source-uri", required=True, metavar="URI", help="the source's URI or path"),
        Arg("--locator", help="where in the source: a message id, a time, a page"),
        Arg("--speaker", help="who spoke, when someone did"),
        Arg("--excerpt", required=True, help=f"what the source says (at most {problems.MAX_EXCERPT_CHARS} characters)"),
        Arg("--signal", required=True, choices=problems.SIGNALS, help="what the line shows"),
        Arg(
            "--label",
            required=True,
            choices=(*problems.EVIDENCE_LABELS, "personal"),
            help="public or internal; the hub refuses personal, which stays on your machine",
        ),
        PROJECT,
    )
    output = returns_object(*EVIDENCE_KEYS, schema="ProblemEvidence")


class ProblemEvidenceConfirm(ProblemCommand, ConfirmEvidence):
    name = "confirm"
    help = "confirm a line of evidence (the writer role on the project)"
    arguments = (THE_CODE, Arg("evidence", metavar="EVIDENCE", help="the line's code (E-<number>)"), PROJECT)
    output = returns_object(*EVIDENCE_KEYS, schema="ProblemEvidence")


PROBLEM = Group(
    "problem",
    "the problems of a project: what hurts, who, when, and the workaround",
    [
        ProblemList(),
        ProblemShow(),
        ProblemSet(),
        ProblemClose(),
        Group(
            "evidence",
            "the evidence of a problem: lines of sources that show it hurts",
            [ProblemEvidenceList(), ProblemEvidenceAdd(), ProblemEvidenceConfirm()],
        ),
    ],
)
