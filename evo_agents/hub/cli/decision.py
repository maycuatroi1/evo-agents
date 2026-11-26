"""``evo-agents hub decision list|show|answer``: the decisions the agents of your plan runs and author runs ask you.

``decision list`` and ``decision show`` read a project's decisions (GET /v1/projects/{p}/decisions, .../{id}), as any
reader of the run may; a decision of a run that has put no plan (an author run before its plan, a local session)
has none, and shows its run alone.
``decision answer ID`` answers one (POST .../decisions/{id}/answer) with an option of the decision (``--option KEY``),
words of your own (``--text``, ``-`` reads them from stdin), or both; the member who dispatched the run may, and an
admin of its project (its log and a notice to the dispatcher say so), and the answer goes to the agent through the
run's inbox, or resumes a parked run on its worker. A credential request
(``evo_agents.hub.credential_requests``) also takes the value of the variable it asks for with ``--secret``, read from
stdin when it is not a terminal, else asked for without echo, never from the command line; the hub keeps it as your
secret and the answer says its name, never the value (an admin's, for another member's run, lent to that run alone).
An option marked waits leaves the decision open and tells the agent nothing; a decision of category wait is a wait of
the agent's for a time or an event, which the hub answers itself, and ``decision show`` says what it waits for, and for
any decision why the agent recommends its option, whether the act can be undone and how, and the refs of its evidence.
``docs/notifications.md`` describes decisions, their categories and their life. ``evo-agents hub notifications`` is in
``evo_agents.hub.cli.notifications``.

The commands find their project as ``hub run`` does: ``--project``, or ``hub.project`` in the harness.yaml around the
current directory. ``--json`` prints what the hub answered, with the keys declared next to the flag. Standard library
only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Decisions` is
the gateway to the routes, :class:`AnswerGiven` the value object that turns the options of ``answer`` into its body.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Views, returns_object
from evo_agents.hub import approval_heads, credential_requests
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.cli_client import call_object, when
from evo_agents.hub.plan_writes import checked_plan_id
from evo_agents.hub.runs import DECISION_STATES

QUESTION_CHARS = 60  # of a decision's question in the decision table
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
    "recommended_reason",
    "reversible",
    "undo",
    "evidence",
    "state",
    "owner",
    "asked_of",
    "answer_option",
    "answer_text",
    "answered_by",
    "answered_via",
    "answer_run_id",
    "asked_at",
    "answered_at",
    "delivered_at",
    "held_at",
    "wake_at",
    "wake_on",
    "waiting_on",
    "outputs",
    "credential_request",
    "approval",
    "label",
    "label_given",
    "channel_deliveries",
)
DECISION_LIST_KEYS = ("decisions", "total", "limit", "offset")
DECISION_OUTPUT = returns_object(*DECISION_KEYS, schema="Decision")


class Decisions(ProjectResource):
    """The decisions of a project: GET /v1/projects/{p}/decisions, .../{id} and POST .../{id}/answer."""

    collection = "decisions"

    def list(
        self,
        states: list[str] | None = None,
        run: int | None = None,
        plan: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """One page of the decisions, ``state`` repeated for each of ``states``."""
        given = {"state": states or [], "run_id": run, "plan_id": None if plan is None else checked_plan_id(plan)}
        given |= {"limit": limit, "offset": offset}
        query = urlencode({key: value for key, value in given.items() if value is not None}, doseq=True)
        return call_object(self.hub, "GET", self.path() + (f"?{query}" if query else ""))

    def get(self, decision_id: int) -> dict:
        return self.call("GET", decision_id)

    def answer(self, decision_id: int, body: dict) -> dict:
        return self.call("POST", decision_id, "answer", body=body)


def one_line(text: str | None, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters; ``-`` when there is none."""
    flat = " ".join((text or "").split()) or "-"
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def option_label(decision: dict, key: str | None) -> str | None:
    """The label of the decision's option ``key``; None when it has none."""
    return next((option["label"] for option in decision["options"] if option["key"] == key), None)


DECISION_ARG = Arg(
    "decision",
    metavar="DECISION",
    type=int,
    help="the decision's id, as `hub decision list` and the notification show it",
)


# Reading


class DecisionList(HubCommand):
    name = "list"
    help = "the decisions of a project, newest first, of the plans you may read"
    arguments = (
        PROJECT,
        Arg(
            "--state",
            action="append",
            choices=DECISION_STATES,
            metavar="STATE",
            help=f"only decisions in this state, one of {', '.join(DECISION_STATES)}; repeat it for several",
        ),
        Arg("--run", type=int, metavar="RUN", help="only the decisions of this run"),
        Arg("--plan", help="only the decisions of this plan"),
        Arg("--limit", type=int, choices=range(1, MAX_LIST + 1), metavar="N", help=f"1 to {MAX_LIST} (default 50)"),
        Arg("--offset", type=int, metavar="N", help="skip this many decisions, for the next page"),
    )
    output = returns_object(*DECISION_LIST_KEYS, schema="DecisionList")
    columns: ClassVar[tuple[str, ...]] = (
        "DECISION",
        "STATE",
        "CATEGORY",
        "RUN",
        "PLAN",
        "STEP",
        "ANSWERER",
        "QUESTION",
        "ASKED (UTC)",
    )

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        listed = Decisions.of(ctx).list(args.state, args.run, args.plan, args.limit, args.offset)
        if args.json:
            return Result(listed)
        rows = [self.row(decision) for decision in listed["decisions"]]
        shown = len(rows)
        lines = [f"{shown} of {listed['total']} decision(s) of project {ctx.project}"]
        if listed["offset"] + shown < listed["total"]:
            lines.append(f"More with --offset {listed['offset'] + shown}.")
        if any(decision["state"] == "open" for decision in listed["decisions"]):
            where = ctx.with_project()
            lines.append(
                f"Read one with `evo-agents hub decision show ID{where}`, and answer it with "
                f"`evo-agents hub decision answer ID --option KEY{where}`."
            )
        return Result(listed, Views(Table(self.columns, rows), Text(*lines)))

    @staticmethod
    def row(decision: dict) -> tuple:
        return (
            f"#{decision['id']}",
            decision["state"],
            decision["category"],
            f"#{decision['run_id']}",
            decision["plan_id"] or "-",
            decision["step_key"] or "-",
            decision.get("asked_of") or decision["owner"],
            one_line(decision["question"], QUESTION_CHARS),
            when(decision["asked_at"]),
        )


def answer_lines(decision: dict) -> list[tuple[str, str]]:
    """Who answered a decision, how and with what, and whether the agent has the answer yet."""
    if decision["state"] != "answered":
        return []
    who = decision["answered_by"] or "the hub, as what it waited for came"
    via = decision.get("answered_via")  # a hub before schema 0259 has none
    through = f" through {via}" if via else ""
    lines = [("answered", f"by {who}{through} at {when(decision['answered_at'])} UTC")]
    if decision["answer_option"]:
        label = option_label(decision, decision["answer_option"])
        lines.append(("option", decision["answer_option"] + (f", {label}" if label else "")))
    elif decision["answer_text"] and decision["answered_by"]:
        lines.append(("option", f"none: a free answer in words{through}"))
    if decision["answer_text"]:
        lines.append(("text", decision["answer_text"]))
    inbox = decision["answer_run_id"]
    if inbox is not None and inbox != decision["run_id"]:
        lines.append(("resumed", f"as run #{inbox}, whose inbox took the answer"))
    delivered = decision["delivered_at"]
    lines.append(("delivered", f"to the agent at {when(delivered)} UTC" if delivered else "not to the agent yet"))
    return lines


def wait_lines(decision: dict) -> list[tuple[str, str]]:
    """What a wait waits for, and when the person asked chose an option that leaves a decision open."""
    lines = []
    if decision.get("wake_on"):
        on = decision["wake_on"]
        contains = (
            f" whose payload contains {json.dumps(on['contains'], ensure_ascii=False)}" if on.get("contains") else ""
        )
        lines.append(("wakes on", f"an event {on['kind']} of the project{contains}"))
    if decision.get("wake_at"):
        lines.append(("wakes at", f"{when(decision['wake_at'])} UTC at the latest"))
    if decision.get("held_at") and decision["state"] == "open":
        lines.append(("held", f"at {when(decision['held_at'])} UTC: not yet, the run waits for a later answer"))
    return lines


def field_lines(fields: list[tuple[str, str]]) -> list[str]:
    """``  label  text`` lines, the labels in one column, a text of several lines under its first."""
    width = max(len(label) for label, _ in fields)
    lines = []
    for label, text in fields:
        first, *rest = str(text).splitlines() or [""]
        lines.append(f"  {label:<{width}}  {first}".rstrip())
        lines += [f"  {'':<{width}}  {line}".rstrip() for line in rest]
    return lines


def block_lines(title: str, text: str) -> list[str]:
    return [f"{title}:", *(f"  {line}".rstrip() for line in text.splitlines() or [""])]


def option_lines(decision: dict) -> list[str]:
    lines = ["Options:"]
    width = max(len(option["key"]) for option in decision["options"])
    for option in decision["options"]:
        flags = (("recommended", option["recommended"]), ("leaves it open", option.get("waits")))
        marks = [mark for mark, on in flags if on]
        if decision["answer_option"] == option["key"]:
            marks.append("chosen")
        label = option["label"] + (f" ({', '.join(marks)})" if marks else "")
        lines.append(f"  {option['key']:<{width}}  {label}")
        lines += [f"  {'':<{width}}  {line}".rstrip() for line in (option["description"] or "").splitlines()]
    return lines


def reversible_lines(decision: dict) -> list[tuple[str, str]]:
    """Whether the act a decision asks about can be undone, and how, when the agent said."""
    reversible = decision.get("reversible")
    if reversible is None:
        return []
    if not reversible:
        return [("reversible", "no: it cannot be undone")]
    return [("reversible", "yes" + (f": {decision['undo']}" if decision.get("undo") else ""))]


def grounds_lines(decision: dict) -> list[str]:
    """Why the agent recommends its option, and the refs of what the question rests on."""
    lines = []
    if decision.get("recommended_reason"):
        lines += block_lines(f"Why {decision['recommended']}", decision["recommended_reason"])
    if decision.get("evidence"):
        lines += block_lines("Evidence", "\n".join(decision["evidence"]))
    return lines


def request_lines(decision: dict) -> list[tuple[str, str]]:
    """What a credential request asks for: a variable's value and the secret that would keep it, the owner's hub
    account."""
    request = decision.get("credential_request")
    if not request:
        return []
    lines = []
    if request.get("env_var"):
        lines.append(("asks for", f"the value of {request['env_var']}, kept as your secret {request['secret_name']}"))
    if request.get("hub_account"):
        lines.append(("asks for", "your hub account: the run runs as you, secrets, tokens and workers aside"))
    return lines


def approval_lines(decision: dict) -> list[tuple[str, str]]:
    """What a merge approval holds for: the request, its head and its scope."""
    approval = decision.get("approval")
    if not approval:
        return []
    pull = f" of {approval['pull']}" if approval.get("pull") else ""
    scope = f", scope {approval_heads.short(approval['scope'])}" if approval.get("scope") else ""
    return [("holds for", f"head {approval_heads.short(approval['head'])}{pull}{scope}")]


def label_lines(decision: dict) -> list[tuple[str, str]]:
    """The decision's effective label, and whether the agent gave it; a hub before schema 0265 shows none."""
    label = decision.get("label")
    if not label:
        return []
    return [("label", label + ("" if decision.get("label_given") else ", the project's default"))]


def channel_text(entry: dict) -> str:
    """Where a decision's message stands on one channel (``channel_deliveries``)."""
    state = entry["state"]
    if state == "sent":
        text = f"sent as {entry['external_id']}" if entry.get("external_id") else "sent"
        if entry.get("sent_at"):
            text += f" at {when(entry['sent_at'])} UTC"
        if entry.get("reason"):
            text += f"; {entry['reason']}"
    elif state == "not_sent":
        text = f"not sent: {entry.get('reason') or 'no reason given'}"
    elif state == "failed":
        text = f"failed after {entry.get('attempts', 0)} tries: {entry.get('error') or 'no error given'}"
    else:
        text = "pending" + (f", last error: {entry['error']}" if entry.get("error") else "")
    edit = entry.get("edit")
    if edit:
        text += f"; edit {edit['state']}" + (f": {edit['error']}" if edit.get("error") else "")
    return text


def channel_lines(decision: dict) -> list[tuple[str, str]]:
    """One line for each channel of ``channel_deliveries``; a hub before schema 0265 has none."""
    return [(entry["channel"], channel_text(entry)) for entry in decision.get("channel_deliveries") or []]


def run_line(decision: dict) -> str:
    """The run of a decision, with its plan and step, or the run alone when it has put no plan."""
    run = f"#{decision['run_id']} ({decision['run_state']})"
    if decision["plan_id"] is None:
        return f"{run}, no plan"
    step = f", step {decision['step_key']}" if decision["step_key"] else ""
    return f"{run} of plan {decision['plan_id']}{step}"


class DecisionShow(HubCommand):
    name = "show"
    help = "one decision: its question, context, options and answer"
    arguments = (DECISION_ARG, PROJECT)
    output = DECISION_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        decision = Decisions.of(ctx).get(ctx.args.decision)
        if ctx.args.json:
            return Result(decision)
        return Result(decision, Text(*self.lines(decision, ctx.with_project())))

    @staticmethod
    def lines(decision: dict, where: str) -> list[str]:
        fields = [
            ("run", run_line(decision)),
            ("owner", decision["owner"]),
            ("answerer", f"{decision.get('asked_of') or decision['owner']}, who answers it"),
            ("asked", f"{when(decision['asked_at'])} UTC"),
            *label_lines(decision),
            *channel_lines(decision),
            *request_lines(decision),
            *approval_lines(decision),
            *reversible_lines(decision),
            *wait_lines(decision),
            *answer_lines(decision),
        ]
        lines = [f"Decision #{decision['id']}: {decision['category']}, {decision['state']}", *field_lines(fields)]
        lines += block_lines("Question", decision["question"])
        if decision["context"]:
            lines += block_lines("Context", decision["context"])
        lines += option_lines(decision) + grounds_lines(decision)
        if decision["state"] == "open":
            asks_value = (decision.get("credential_request") or {}).get("env_var")
            secret = " (--secret pastes the value it asks for)" if asks_value else ""
            lines.append(
                f"Answer it with `evo-agents hub decision answer {decision['id']} --option KEY"
                f"{where}`; --text adds words of your own, or stands for an option{secret}."
            )
        return lines


# Answering


@dataclass(frozen=True)
class AnswerGiven:
    """What the options of ``decision answer`` say (Value Object): checked alone with :meth:`conflict`, then made the
    body of POST .../answer with :meth:`body`."""

    args: argparse.Namespace

    def conflict(self) -> str | None:
        """Why the options make no answer; None when they make one."""
        args = self.args
        if args.option is None and args.text is None and not args.secret:
            return "an answer names an option with --option KEY, gives words with --text TEXT, or both"
        if args.secret and args.text == "-":
            return "--secret reads the value from stdin: give your words with --text TEXT, not -"
        if args.secret and args.option is not None and not credential_requests.gives_value(args.option):
            return f"--secret goes with option {credential_requests.VALUE} or {credential_requests.BOTH}, or with none"
        return None

    def text(self) -> str | None:
        """The words of the answer: ``--text``, or stdin for ``--text -``."""
        return sys.stdin.read() if self.args.text == "-" else self.args.text

    def secret(self) -> str:
        """The value a credential request asks for, read from stdin or asked for; UsageError when it is refused."""
        from evo_agents.hub.cli_secrets import Refused, check_value, read_value

        decision = self.args.decision
        try:
            value = read_value(
                f"of decision {decision}",
                prompt=f"Value decision #{decision} asks for (not shown): ",
                missing="no value: pipe it on stdin, such as `pbpaste | evo-agents hub decision answer ID --secret`, "
                "or run the command in a terminal to be asked for it",
            )
            check_value("env", value)
        except Refused as exc:
            raise UsageError(str(exc)) from None
        return value

    def body(self, text: str | None, secret: str | None) -> dict:
        given = (("option", self.args.option), ("text", text), ("secret", secret))
        return {key: value for key, value in given if value is not None}


class DecisionAnswer(HubCommand):
    name = "answer"
    help = (
        "answer an open decision of a run you dispatched, with an option, words of your own, or both; the "
        "answer goes to the agent, and a parked run resumes"
    )
    arguments = (
        DECISION_ARG,
        Arg("--option", metavar="KEY", help="the key of one of the decision's options"),
        Arg("--text", metavar="TEXT", help="your own words, at most 4 KiB; - reads them from stdin"),
        Arg(
            "--secret",
            action="store_true",
            help="a credential request: paste the value of the variable it asks for, read from stdin, or asked for "
            "without echo in a terminal; the hub keeps it as your secret, never shows it again",
        ),
        PROJECT,
    )
    output = DECISION_OUTPUT

    def check(self, args: argparse.Namespace) -> None:
        why = AnswerGiven(args).conflict()
        if why:
            raise UsageError(why)

    def run(self, ctx: HubContext) -> Result:
        given = AnswerGiven(ctx.args)
        text = given.text()
        decisions = Decisions.of(ctx)  # signed in, and the project found, before the value is asked for
        secret = given.secret() if ctx.args.secret else None
        decision = decisions.answer(ctx.args.decision, given.body(text, secret))
        if ctx.args.json:
            return Result(decision)
        return Result(decision, Text(*self.said(decision, ctx.args)))

    @staticmethod
    def said(decision: dict, args: argparse.Namespace) -> list[str]:
        if decision["state"] == "open":  # an option that waits: not yet
            return [
                f"Decision #{decision['id']} stays open: the agent hears nothing, and run #{decision['run_id']} waits "
                "for your answer with another option."
            ]
        given = []
        if decision["answer_option"]:
            label = option_label(decision, decision["answer_option"])
            given.append(f"option {decision['answer_option']}" + (f" ({label})" if label else ""))
        if args.text is not None:
            given.append("your words")
        lines = [f"Answered decision #{decision['id']} with {' and '.join(given)}."]
        gave = args.secret or credential_requests.grants_account(decision["answer_option"])
        if decision.get("credential_request") and gave and decision["answer_text"]:
            lines.append(decision["answer_text"].rsplit("\n\n", 1)[-1])  # what the run was given, by name, not value
        inbox = decision["answer_run_id"]
        if inbox is not None and inbox != decision["run_id"]:
            lines.append(
                f"Run #{decision['run_id']} was parked: the hub queued run #{inbox} on the same worker to resume it in "
                "its session, and the answer waits in its inbox."
            )
        else:
            lines.append(
                f"The answer went to the inbox of run #{decision['run_id']}; its worker hands it to the agent."
            )
        return lines


DECISION = Group(
    "decision",
    "the decisions the agents of your plan runs and author runs ask you, and your answers",
    [DecisionList(), DecisionShow(), DecisionAnswer()],
)
