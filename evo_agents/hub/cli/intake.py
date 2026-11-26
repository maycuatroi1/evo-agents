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

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands): :class:`Intakes` is the
gateway to the intakes of a project, :class:`Proposals` to the proposals of the core plugin builder.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from evo_agents.clikit import Arg, Group, Result, Stderr, Table, Text, UsageError, Views, returns_array, returns_object
from evo_agents.hub import intakes
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when

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
    "opened_run_id",
    "outcome",
    "link",
)
PROPOSAL_KINDS = ("problem", "opportunity")  # the kinds of proposal of @builder an intake makes, a writer's to decide
SHOWN_SUMMARY_CHARS = 70  # of a proposal's summary in a row of ``proposal list``
INTAKE_OUTPUT = returns_object(*INTAKE_KEYS, schema="Intake")
PROPOSAL = returns_object(*PROPOSAL_KEYS, schema="BuilderProposal")
CODE = Arg("code", metavar="CODE", help="the intake's code (I-<number>)")
PROPOSAL_ID = Arg("id", type=int, metavar="ID", help="the proposal's id")


class Intakes(ProjectResource):
    """The intakes of a project: /v1/projects/{p}/intakes and the routes below it (Gateway)."""

    collection = "intakes"

    def start(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def put(self, code: str, document: Any) -> dict:
        return self.call("PUT", code, body=document)

    def get(self, code: str) -> dict:
        return self.call("GET", code)

    def list(self, status: str | None) -> list[dict]:
        return self.call("GET", status=status)


class Proposals(ProjectResource):
    """The proposals of the core plugin builder in a project: /v1/x/builder/{p}/proposals (Gateway)."""

    collection = "proposals"

    def path(self, *parts: Any, **query: Any) -> str:
        tail = "".join(f"/{quote(str(part), safe='')}" for part in parts)
        given = {name: value for name, value in query.items() if value is not None}
        return f"/v1/x/builder/{quote(self.project, safe='')}/proposals{tail}" + (
            f"?{urlencode(given)}" if given else ""
        )

    def waiting(self, kind: str | None) -> list[dict]:
        return self.call("GET", "waiting", kind=kind)

    def get(self, id_: int) -> dict:
        return self.call("GET", id_)

    def answer(self, id_: int, answer: str) -> dict:
        return self.call("POST", id_, "answer", body={"answer": answer})


# Intakes


def source_of(text: str) -> dict:
    """A source as ``KIND:LABEL[:NAME]`` gives it; UsageError naming what is wrong."""
    kind, _, rest = text.partition(":")
    label, _, name = rest.partition(":")
    if kind not in intakes.SOURCE_KINDS or label not in intakes.SOURCE_LABELS:
        raise UsageError(
            f"a source is KIND:LABEL[:NAME] with a kind of {', '.join(intakes.SOURCE_KINDS)} and a label of "
            f"{', '.join(intakes.SOURCE_LABELS)}, not {text!r}"
        )
    return {"kind": kind, "label": label, **({"name": name} if name else {})}


def outcome_lines(outcome: dict) -> list[str]:
    """What the hub made of an intake's result: its proposals, its tasks and its questions."""
    lines = []
    for proposal in outcome["proposals"]:
        what = "new problem" if proposal["kind"] == "new_problem" else "evidence for"
        lines.append(f"  proposal {proposal['proposal_id']}: {what} {proposal['problem']} ({proposal['lines']} lines)")
    for proposal in outcome.get("opportunity_proposals") or []:
        what = "new opportunity" if proposal["kind"] == "new_opportunity" else "evidence for"
        lines.append(
            f"  proposal {proposal['proposal_id']}: {what} {proposal['opportunity']} ({proposal['lines']} lines)"
        )
    for task in outcome["tasks"]:
        due = f", due {task['due_on']}" if task["due_on"] else ""
        lines.append(f"  task {task['task']}: promise {task['promise']}, assigned to {task['assignee']}{due}")
    if outcome["tasks_skipped"]:
        lines.append(f"  {outcome['tasks_skipped']}")
    return lines + [f"  question: {question.get('text', '')}" for question in outcome["questions"]]


def intake_lines(found: dict) -> list[str]:
    lines = [
        f"Intake {found['code']} of project {found['project']}: {found['status']}, asked by {found['requested_by']}"
    ]
    lines += [f"  source: {intakes.source_text(source)}" for source in found["sources"]]
    if found["note"]:
        lines.append(f"  note: {found['note']}")
    outcome = found["outcome"]
    if outcome is None:
        return lines
    lines.append(
        f"Put by {found['put_by']}: {found['candidates']} candidates, {found['promises']} promises, "
        f"{found['questions']} questions"
        + (f", {found['opportunities']} opportunities" if found.get("opportunities") else "")
    )
    return lines + outcome_lines(outcome)


class IntakeStart(HubCommand):
    name = "start"
    help = "open an intake from the sources a session will read (the writer role)"
    arguments = (
        Arg(
            "--source",
            action="append",
            required=True,
            metavar="KIND:LABEL[:NAME]",
            help=f"a source: its kind ({', '.join(intakes.SOURCE_KINDS)}), its label "
            f"({', '.join(intakes.SOURCE_LABELS)}) and a short name; once for each, never content",
        ),
        Arg("--note", help="why you ask for it"),
        PROJECT,
    )
    output = INTAKE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        sources = [source_of(text) for text in args.source]
        made = Intakes.of(ctx).start({"sources": sources, **({"note": args.note} if args.note else {})})
        said = (
            f"Started intake {made['code']} of project {ctx.project}: "
            f"put its result with `hub intake put {made['code']} FILE`."
        )
        return Result(made, Text(said))


class IntakePut(HubCommand):
    name = "put"
    help = "put the evo-intake/1 or evo-intake/2 result of an intake, once (the writer role)"
    arguments = (
        CODE,
        Arg("file", metavar="FILE", help="the evo-intake/1 or evo-intake/2 JSON document, or - for standard input"),
        PROJECT,
    )
    output = INTAKE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        try:
            if args.file == "-":
                text = sys.stdin.read()
            else:
                with open(args.file, encoding="utf-8") as handle:
                    text = handle.read()
            document = json.loads(text)
        except (OSError, ValueError) as exc:
            raise UsageError(f"cannot read an evo-intake/1 document from {args.file}: {exc}") from None
        done = Intakes.of(ctx).put(args.code, document)
        return Result(done, Text(*intake_lines(done)))


class IntakeShow(HubCommand):
    name = "show"
    help = "an intake, with what the hub made of its result"
    arguments = (CODE, PROJECT)
    output = INTAKE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Intakes.of(ctx).get(ctx.args.code)
        return Result(found, Text(*intake_lines(found)))


class IntakeList(HubCommand):
    name = "list"
    help = "the project's intakes, newest first"
    arguments = (Arg("--status", choices=intakes.STATUSES, help="those of this status alone"), PROJECT)
    output = returns_array(*INTAKE_KEYS, schema="Intake")

    def run(self, ctx: HubContext) -> Result:
        status = ctx.args.status
        found = Intakes.of(ctx).list(status)
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
        header = ("CODE", "STATUS", "ASKED BY", "SOURCES", "CANDIDATES", "PROMISES")
        empty = f"Project {ctx.project} has no intakes{' ' + status if status else ''}."
        return Result(found, Table(header, rows, empty))


# The proposals an intake made


def makes(proposal: dict) -> str:
    """What a proposal of a problem or an opportunity makes: a new one, lines of evidence of one, or a change of one."""
    code = proposal["target"].get(proposal["kind"], "?")
    change = proposal["change"]
    if proposal["base_version"] == 0:
        return f"new {proposal['kind']} {code}"
    if change.get("evidence") and not change.get("set") and not change.get("unset"):
        return f"evidence for {code}"
    return f"change of {code}"


def clipped(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


def proposal_lines(found: dict) -> list[str]:
    lines = [
        f"Proposal {found['id']} of project {found['project']}: {makes(found)}, {found['state'].replace('_', ' ')}",
        f"  {found['summary']}",
        f"  proposed by {found['proposed_by']} at {when(found['proposed_at'])} UTC",
    ]
    if found["decided_by"]:
        lines.append(f"  decided by {found['decided_by']} at {when(found['decided_at'])} UTC")
    if found["error"]:
        lines.append(f"  error: {found['error']}")
    for name, value in found["change"].get("set", {}).items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"  {name}: {shown}")
    lines += [f"  {name}: (cleared)" for name in found["change"].get("unset", [])]
    lines += [
        f"  evidence: [{line['signal']}] {line['source_kind']} {line['source_uri']}: {line['excerpt']}"
        for line in found["change"].get("evidence", [])
    ]
    if found["link"]:
        lines.append(f"  thread: {found['link']}")
    return lines


class ProposalList(HubCommand):
    name = "list"
    help = "the pending proposals you may decide, newest first"
    arguments = (Arg("--kind", choices=PROPOSAL_KINDS, help="those of this kind alone"), PROJECT)
    output = returns_array(*PROPOSAL_KEYS, schema="BuilderProposal")

    def run(self, ctx: HubContext) -> Result:
        kind = ctx.args.kind
        kinds = (kind,) if kind else PROPOSAL_KINDS
        found = [p for p in Proposals.of(ctx).waiting(kind) if p["kind"] in kinds]
        if not found:
            return Result(
                found, Text(f"No proposal of {' or '.join(kinds)} waits for your answer in project {ctx.project}.")
            )
        rows = [
            (p["id"], makes(p), p["proposed_by"], when(p["proposed_at"]), clipped(p["summary"], SHOWN_SUMMARY_CHARS))
            for p in found
        ]
        return Result(
            found,
            Views(
                Table(("ID", "MAKES", "BY", "PROPOSED", "SUMMARY"), rows),
                Text("Answer each with `evo-agents hub intake proposal approve|reject ID`."),
            ),
        )


class ProposalShow(HubCommand):
    name = "show"
    help = "a proposal, with the fields it sets and its lines of evidence"
    arguments = (PROPOSAL_ID, PROJECT)
    output = PROPOSAL

    def run(self, ctx: HubContext) -> Result:
        found = Proposals.of(ctx).get(ctx.args.id)
        return Result(found, Text(*proposal_lines(found)))


class ProposalAnswer(HubCommand):
    """``proposal approve|reject ID``: one class, a subclass for each answer."""

    answer: ClassVar[str]
    arguments = (PROPOSAL_ID, PROJECT)
    output = PROPOSAL

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.set_defaults(answer=self.answer)

    def run(self, ctx: HubContext) -> Result:
        found = Proposals.of(ctx).answer(ctx.args.id, ctx.args.answer)
        if found["state"] == "applied":
            return Result(found, Text(f"Approved proposal {found['id']}: {makes(found)} is made."))
        if found["state"] == "rejected":
            return Result(found, Text(f"Rejected proposal {found['id']}: nothing changed."))
        said = f"error: proposal {found['id']} was approved and the hub could not apply it: {found['error']}"
        return Result(found, Stderr(Text(said)), 0 if found["state"] != "failed" else 1)


class ProposalApprove(ProposalAnswer):
    name = answer = "approve"
    help = "answer a pending proposal: make what it proposes (the writer role)"


class ProposalReject(ProposalAnswer):
    name = answer = "reject"
    help = "answer a pending proposal: refuse it; nothing changes (the writer role)"


INTAKE = Group(
    "intake",
    "an intake: one reading of vague context, its problems and its promises",
    [
        IntakeStart(),
        IntakePut(),
        IntakeShow(),
        IntakeList(),
        Group(
            "proposal",
            "the proposals of a problem or an opportunity that wait for a writer: list, show, answer",
            [ProposalList(), ProposalShow(), ProposalApprove(), ProposalReject()],
        ),
    ],
)
