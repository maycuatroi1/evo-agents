"""``evo-agents worker playbook add|helpful|harmful|retire``: the deltas of the Curator's playbook of a project
(``evo_agents.hub.playbook``), which the agent of a review run writes; the playbook is never written anew.

- ``add --text TEXT --evidence SPEC [--evidence SPEC ...] [--lens LENS] [--until-proposal N]``: a new entry, a lesson
  the playbook lacks, with its evidence (as ``worker finding`` takes it), for one lens or, without ``--lens``, every
  lens; with ``--until-proposal``, it holds until proposal N lands, and the hub retires it once that change is merged
  (without it, the hub reads ``until proposal #N`` or ``#N lands`` in the text);
- ``helpful ID [--evidence SPEC ...] [--note TEXT]`` and ``harmful ID [...]``: one more on the counter of entry ID
  (``pb-3`` or ``3``) when it helped or misled the run; once each way for a run;
- ``retire ID --note TEXT``: the entry no longer holds; it stays, out of the prompt.

Each reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker finding`` does, refuses outside a review run of
this worker, calls the hub with the worker's token (POST /v1/worker/runs/{id}/playbook,
``evo_agents.hub.server.playbook``) and prints one line with the entry and its counters. A failure is one ``error:``
line on stderr and exit status 1; without the worker extra, status 2. TEXT ``-`` reads stdin.
"""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.hub import playbook
from evo_agents.hub.api import WorkerRun
from evo_agents.hub.review import LENSES
from evo_agents.worker.agent_input import evidence_items
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext

REVIEW = ("review",)
SPEC = "session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ or code:REPO:PATH[:LINE]; repeat it"
ENTRY = Arg("entry", metavar="ID", help="the entry, pb-N or N")


def _send(ctx: AgentRunContext, op: str, body: dict) -> dict:
    """POST the delta ``body`` for the review run of EVO_RUN_ID; the hub's entry."""
    ctx.check()
    run_id = ctx.run_id
    return ctx.with_hub(lambda hub: WorkerRun(hub, run_id).call("POST", "playbook", body={"op": op, **body}))


def _text(value: str | None, what: str, limit: int) -> str | None:
    if value is None:
        return None
    return playbook.clean_text(sys.stdin.read() if value == "-" else value, what, limit)


def _said(entry: dict, what: str) -> Result:
    said = (
        f"Playbook entry {entry.get('ref')} {what}: helpful {entry.get('helpful')}, harmful {entry.get('harmful')}: "
        f"{entry.get('text')}"
    )
    return Result(entry, Text(said))


class PlaybookAdd(AgentCommand):
    name = "add"
    words = "playbook add"
    kinds = REVIEW
    problems = (playbook.PlaybookProblem,)
    hub_failed = EXIT_FAILED
    help = "a new entry: a lesson the playbook lacks, with its evidence"
    arguments = (
        Arg("--text", required=True, help=f"the lesson, at most {playbook.MAX_TEXT_CHARS} characters (- for stdin)"),
        Arg("--evidence", action="append", required=True, metavar="SPEC", help=SPEC),
        Arg("--lens", choices=list(LENSES), help="the lens it is for (default: every lens)"),
        Arg(
            "--until-proposal",
            type=int,
            metavar="N",
            help="the proposal it holds until: the hub retires it once that proposal's change is merged",
        ),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        body = {"text": _text(args.text, "text", playbook.MAX_TEXT_CHARS), "evidence": evidence_items(args.evidence)}
        if args.lens:
            body["lens"] = args.lens
        if args.until_proposal is not None:
            body["until_proposal_id"] = args.until_proposal
        return _said(_send(ctx, "add", body), "added")


def _tag(ctx: AgentRunContext) -> Result:
    """One more on the counter ``args.op`` of the entry."""
    args = ctx.args
    body: dict = {"entry_id": playbook.parse_ref(args.entry), "evidence": evidence_items(args.evidence or [])}
    note = _text(args.note, "note", playbook.MAX_NOTE_CHARS)
    if note:
        body["note"] = note
    return _said(_send(ctx, args.op, body), f"counted {args.op}")


TAG_ARGUMENTS = (
    ENTRY,
    Arg("--evidence", action="append", metavar="SPEC", help=SPEC),
    Arg("--note", help="why, in a line (- for stdin)"),
)


class PlaybookHelpful(AgentCommand):
    name = "helpful"
    words = "playbook helpful"
    kinds = REVIEW
    problems = (playbook.PlaybookProblem,)
    hub_failed = EXIT_FAILED
    help = "one more on the counter of an entry that helped this run; once a run"
    arguments = TAG_ARGUMENTS

    def add_arguments(self, parser) -> None:
        parser.set_defaults(op="helpful")

    def run(self, ctx: AgentRunContext) -> Result:
        return _tag(ctx)


class PlaybookHarmful(AgentCommand):
    name = "harmful"
    words = "playbook harmful"
    kinds = REVIEW
    problems = (playbook.PlaybookProblem,)
    hub_failed = EXIT_FAILED
    help = "one more on the counter of an entry that misled this run; once a run"
    arguments = TAG_ARGUMENTS

    def add_arguments(self, parser) -> None:
        parser.set_defaults(op="harmful")

    def run(self, ctx: AgentRunContext) -> Result:
        return _tag(ctx)


class PlaybookRetire(AgentCommand):
    name = "retire"
    words = "playbook retire"
    kinds = REVIEW
    problems = (playbook.PlaybookProblem,)
    hub_failed = EXIT_FAILED
    help = "an entry that no longer holds: it leaves the prompt and stays listed"
    arguments = (ENTRY, Arg("--note", required=True, help="why it no longer holds (- for stdin)"))

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        body = {"entry_id": playbook.parse_ref(args.entry), "note": _text(args.note, "note", playbook.MAX_NOTE_CHARS)}
        return _said(_send(ctx, "retire", body), "retired")


PLAYBOOK = Group(
    "playbook",
    "inside a review run: change the Curator's playbook by one delta",
    [PlaybookAdd(), PlaybookHelpful(), PlaybookHarmful(), PlaybookRetire()],
)
