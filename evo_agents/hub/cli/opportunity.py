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

The commands are classes of ``evo_agents.clikit`` on the bases of ``cli.ledger``, which ``cli.problem`` shares:
:class:`Opportunities` is the gateway to the routes.
"""

from __future__ import annotations

import argparse
from typing import ClassVar

from evo_agents.clikit import Arg, Group, UsageError, returns_array, returns_object
from evo_agents.hub import opportunities, problems
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
CODE = Arg("code", metavar="CODE", help="its code (O-<number>)")
THE_CODE = Arg("code", metavar="CODE", help="the opportunity's code (O-<number>)")
IF_REVISION = Arg("--if-revision", type=int, metavar="N", help="refuse unless the opportunity is at revision N")


class Opportunities(Ledger):
    """The opportunities of a project: GET and PUT /v1/projects/{p}/opportunities and the routes below it."""

    collection = "opportunities"

    def code(self, number: int) -> str:
        return opportunities.code(number)


class OpportunityCommand:
    """The noun and the gateway of every opportunity command (mixed in first)."""

    noun: ClassVar[str] = "opportunity"
    a_noun: ClassVar[str] = "an opportunity"
    ledger: ClassVar[type[Ledger]] = Opportunities


# Reading


class OpportunityList(OpportunityCommand, ListItems):
    name = "list"
    help = "the project's opportunities by rank"
    arguments = (
        Arg("--status", choices=opportunities.STATUSES, help="those of this status alone"),
        Arg(
            "--closing-within",
            type=int,
            metavar="DAYS",
            help="the open ones whose window ends within DAYS days (one already past included), nearest end first",
        ),
        PROJECT,
    )
    output = returns_array(*OPPORTUNITY_KEYS, schema="ProjectOpportunity")
    columns = ("CODE", "STATUS", "RANK", "OWNER", "WINDOW", "STATEMENT")
    plural = "opportunities"

    def query(self, args: argparse.Namespace) -> dict:
        return {"status": args.status or None, "closing_within": args.closing_within}

    def row(self, item: dict) -> tuple:
        return (
            item["code"],
            item["status"],
            item["rank"],
            item["owner"],
            opportunities.window_text(item),
            clip(item["statement"], SHOWN_STATEMENT_CHARS),
        )


class OpportunityShow(OpportunityCommand, ShowItem):
    name = "show"
    help = "an opportunity, with the problems it answers and its revisions"
    arguments = (
        CODE,
        Arg("--revision", type=int, metavar="N", help="an older revision (default: the newest)"),
        PROJECT,
    )
    output = returns_object(*OPPORTUNITY_KEYS, "evidence_count", "source_count", schema="ProjectOpportunityShown")

    def lines(self, item: dict) -> list[str]:
        written = f"  written by {item['created_by']} at {when(item['created_at'])} UTC"
        answers = ", ".join(item["answers"]) or "none"
        hidden = item.get("hidden_answers") or 0
        return [
            f"Opportunity {item['code']} of project {item['project']}, revision {item['revision']}",
            f"  statement: {item['statement']}",
            f"  beneficiary: {item['beneficiary']}",
            f"  leverage: {item['leverage']}",
            f"  window: {opportunities.window_text(item)}",
            f"  metric: {problems.metric_text(item['metric'])}",
            f"  rank: {item['rank']}",
            f"  owner: {item['owner']}",
            f"  status: {opportunities.status_text(item)}",
            written + (f": {item['note']}" if item.get("note") else ""),
            f"Answers: {answers}" + (f" (and {hidden} of projects you hold no grant on)" if hidden else ""),
            *self.evidence_line(item),
        ]


# Writing


class OpportunitySet(OpportunityCommand, SetItem):
    name = "set"
    help = "make an opportunity, or write an opportunity's next revision (the writer role on the project)"
    arguments = (
        Arg("code", metavar="CODE", nargs="?", help="the opportunity's code; none: a new opportunity"),
        Arg(
            "--statement",
            help=f"what the project could win (at most {opportunities.MAX_STATEMENT_CHARS} characters)",
        ),
        Arg("--beneficiary", help=f"who benefits (at most {opportunities.MAX_TEXT_CHARS} characters)"),
        Arg("--leverage", help=f"what makes it reachable now (at most {opportunities.MAX_TEXT_CHARS} characters)"),
        Arg("--window-until", metavar="DATE", help="the date after which it is gone (YYYY-MM-DD)"),
        Arg("--window-note", help=f"what the window is (at most {opportunities.MAX_TEXT_CHARS} characters)"),
        Arg("--no-window", action="store_true", help="clear the window"),
        Arg(
            "--answers",
            metavar="CODES",
            help=f"the problems it answers, comma separated: P-3 or lumen/P-3 (at most {opportunities.MAX_ANSWERS})",
        ),
        Arg("--no-answers", action="store_true", help="answer no problem"),
        Arg("--metric", metavar="KEY", help="a key of kind number of the project's registry of signals"),
        Arg("--aggregate", choices=problems.AGGREGATES, help="how the key's signals are summed up"),
        Arg("--window-days", type=int, metavar="N", help="the days they are summed up over (default 90)"),
        Arg("--tenant", help="the signals of this tenant alone"),
        Arg("--no-metric", action="store_true", help="clear the metric"),
        Arg("--rank", type=int, metavar="N", help="its place among the opportunities, 1 first"),
        Arg("--owner", metavar="LOGIN", help="a member with the writer role or above"),
        Arg("--reopen", action="store_true", help="open a closed opportunity again"),
        Arg("--note", help=f"why, for the history (at most {opportunities.MAX_NOTE_CHARS} characters)"),
        IF_REVISION,
        PROJECT,
    )
    output = returns_object(*OPPORTUNITY_KEYS, "changed", schema="ProjectOpportunityWritten")

    def check(self, args: argparse.Namespace) -> None:
        if args.no_metric and any(getattr(args, option) is not None for option in METRIC_OPTIONS):
            raise UsageError("--no-metric goes without the options of a metric")
        if args.no_window and (args.window_until is not None or args.window_note is not None):
            raise UsageError("--no-window goes without --window-until and --window-note")
        if args.no_answers and args.answers is not None:
            raise UsageError("--no-answers goes without --answers")

    @staticmethod
    def answers(args: argparse.Namespace, held: dict) -> list[str]:
        if args.no_answers:
            return []
        if args.answers is not None:
            return [code.strip() for code in args.answers.split(",") if code.strip()]
        return list(held.get("answers") or [])

    def body(self, args: argparse.Namespace, held: dict) -> dict:
        made = metric(args, held.get("metric"))
        body = {name: held.get(name) for name in WRITTEN_FIELDS if name != "answers"}
        body |= {"metric": made, "answers": self.answers(args, held)}
        body |= {name: getattr(args, name) for name in GIVEN_FIELDS if getattr(args, name) is not None}
        if args.no_window:
            body["window_until"] = body["window_note"] = None
        missing = [f"--{name}" for name in opportunities.TEXTS if not body.get(name)]
        if missing:
            raise UsageError(f"a new opportunity needs {', '.join(missing)}")
        if args.reopen:
            body["status"], body["close_reason"] = opportunities.OPEN, None
        body["status"] = body.get("status") or opportunities.OPEN
        body["note"] = args.note
        return {name: value for name, value in body.items() if value is not None}


class OpportunityClose(OpportunityCommand, CloseItem):
    name = "close"
    help = "close an opportunity as captured, missed or dropped (the writer role on the project)"
    arguments = (
        CODE,
        Arg("--status", required=True, choices=opportunities.CLOSED, help="captured, missed or dropped"),
        Arg("--reason", required=True, help=f"why (at most {opportunities.MAX_REASON_CHARS} characters)"),
        Arg("--note", help=f"for the history (at most {opportunities.MAX_NOTE_CHARS} characters)"),
        IF_REVISION,
        PROJECT,
    )
    output = returns_object(*OPPORTUNITY_KEYS, "changed", schema="ProjectOpportunityWritten")


# Evidence


class OpportunityEvidenceList(OpportunityCommand, ListEvidence):
    name = "list"
    help = "an opportunity's lines of evidence"
    arguments = (THE_CODE, PROJECT)
    output = returns_array(*EVIDENCE_KEYS, schema="OpportunityEvidence")


class OpportunityEvidenceAdd(OpportunityCommand, AddEvidence):
    name = "add"
    help = "add a line of evidence (the writer role on the project)"
    arguments = (
        THE_CODE,
        Arg("--source-kind", required=True, choices=problems.SOURCE_KINDS, help="where it was read"),
        Arg("--source-uri", required=True, metavar="URI", help="the source's URI or path"),
        Arg("--locator", help="where in the source: a message id, a time, a page"),
        Arg("--speaker", help="who spoke, when someone did"),
        Arg("--excerpt", required=True, help=f"what the source says (at most {problems.MAX_EXCERPT_CHARS} characters)"),
        Arg("--signal", required=True, choices=opportunities.SIGNALS, help="what the line shows"),
        Arg(
            "--label",
            required=True,
            choices=(*problems.EVIDENCE_LABELS, "personal"),
            help="public or internal; the hub refuses personal, which stays on your machine",
        ),
        PROJECT,
    )
    output = returns_object(*EVIDENCE_KEYS, schema="OpportunityEvidence")


class OpportunityEvidenceConfirm(OpportunityCommand, ConfirmEvidence):
    name = "confirm"
    help = "confirm a line of evidence (the writer role on the project)"
    arguments = (THE_CODE, Arg("evidence", metavar="EVIDENCE", help="the line's code (E-<number>)"), PROJECT)
    output = returns_object(*EVIDENCE_KEYS, schema="OpportunityEvidence")


OPPORTUNITY = Group(
    "opportunity",
    "the opportunities of a project: what it could win, for whom, and what makes it reachable",
    [
        OpportunityList(),
        OpportunityShow(),
        OpportunitySet(),
        OpportunityClose(),
        Group(
            "evidence",
            "the evidence of an opportunity: lines of sources that show it is there",
            [OpportunityEvidenceList(), OpportunityEvidenceAdd(), OpportunityEvidenceConfirm()],
        ),
    ],
)
