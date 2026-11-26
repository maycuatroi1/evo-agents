"""``evo-agents worker curator answer|merge``: what the agent of a proposal's thread does on its own for the owner
(``evo_agents.hub.server.curator.service.threads``; the skill answering-curator-proposals of the evo-hub plugin says how
it decides).

- ``answer PROPOSAL accept|reject|defer --note TEXT [--days N]``: answer the proposal of the thread for the owner, an
  admin of the project, as their answer on the web does: accepting one of a tier the charter plans makes the Curator's
  plan. ``--days`` goes with defer (7 by default).
- ``merge CHANGE``: merge, for the owner, the pull request of a change of the thread's proposal that the hub left open,
  at the head its Judge passed, once the agent has read its diff there. The hub merges it only when the Judge passed
  it, its head did not move, every sign of its files is one of a protected path, it is open into the default branch
  and mergeable, K5 lets it and the night shift is not paused.

- ``drop --reason TEXT [--note TEXT]``: inside the Builder of a Curator's change (a plan run of the Curator's plan),
  close that change before its pull request opens, for the member who dispatched the run, an admin of the project:
  the way to end a change its measurements or its owner's decision say not to land, instead of a commit that reverts
  it. The reason, one line, becomes the change's; the note (``-`` reads stdin), what the run measured, is kept whole
  in the ledger line of the drop (POST /v1/worker/runs/{id}/curator/drop, ``changes.routes.drop_change``).

It reads the run from EVO_RUN_ID like ``worker chat``; ``answer`` and ``merge`` are ``ChatCommand``s, for a chat run
only, ``drop`` for a plan run only. The hub refuses what is outside the frame (another proposal than the thread's, an owner
who is no admin, a proposal answered already, a change the merge does not allow, a plan run of no Curator's plan, a
change with a pull request already), with the reason, and the command prints it on stderr and exits 1. A success is
one line; for ``answer`` and ``merge`` the hub posts the ``Decision:`` line in the thread. TEXT ``-`` reads stdin. No
``--json``: this module may not import the hub's contract.
"""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext, ChatCommand, failure
from evo_agents.worker.home import WorkerStateError

ACTIONS = ("accept", "reject", "defer")  # review.ANSWERS, which the hub checks


def _text(value: str) -> str:
    return sys.stdin.read().strip() if value == "-" else value


class CuratorAnswer(ChatCommand):
    name = "answer"
    words = "chat curator answer"
    help = "accept, reject or defer the proposal of the thread for its owner"
    arguments = (
        Arg("proposal", type=int, metavar="PROPOSAL", help="the proposal's id, the thread's"),
        Arg("action", choices=ACTIONS, help="the answer"),
        Arg("--note", required=True, metavar="TEXT", help="why, in one line (- for stdin)"),
        Arg("--days", type=int, metavar="N", help="with defer: how many days, 1 to 90 (7 by default)"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        body = {"proposal_id": args.proposal, "action": args.action, "note": _text(args.note)}
        if args.days is not None:
            body["defer_days"] = args.days
        answer = ctx.in_chats("curator/answer", body)
        proposal = answer["proposal"]
        change = proposal.get("change") or {}
        made = f"; change #{change['id']} is {change['state']}" if change else ""
        said = f"Proposal #{proposal['id']} is {proposal['state']}{made}; the Decision line is message #{answer['message_id']}."
        return Result(answer, Text(said))


class CuratorMerge(ChatCommand):
    name = "merge"
    words = "chat curator merge"
    help = "merge the pull request of the thread's change the hub left open, for its owner, inside the frame"
    arguments = (Arg("change", type=int, metavar="CHANGE", help="the change's id, as the thread names it"),)

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        merged = ctx.in_chats("curator/merge", {"change_id": ctx.args.change})
        change = merged["change"]
        said = (
            f"Change #{change['id']} is {change['state']}: pull request {change['pr_url']} merged as "
            f"{change['merge_sha']}; the Decision line is message #{merged['message_id']}."
        )
        return Result(merged, Text(said))


class CuratorDrop(AgentCommand):
    name = "drop"
    words = "curator drop"
    kinds = None  # any run of this worker; one of another kind than plan is refused below, in its own words
    help = "inside the Builder of a Curator's change: close the change before its pull request opens, saying why"
    arguments = (
        Arg("--reason", required=True, metavar="TEXT", help="why the change is dropped, in one line"),
        Arg(
            "--note",
            metavar="TEXT",
            help="what the run measured, kept whole in the ledger line of the drop (- for stdin)",
        ),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        if ctx.kind != "plan":
            raise WorkerStateError(
                f"run {ctx.run_id} is a {ctx.kind} run: `evo-agents worker curator drop` is for the Builder of a "
                "Curator's change, a plan run of the Curator's plan"
            )
        body = {"reason": args.reason}
        if args.note is not None:
            body["note"] = _text(args.note)
        change = ctx.in_chats("curator/drop", body)
        return Result(change, Text(f"Change #{change['id']} is {change['state']}: {change['reason']}"))

    def failed(self, exc: Exception) -> int | None:
        return failure(exc, "chat", hub_failed=EXIT_FAILED)  # as the other commands of a run's agent in chats


CURATOR = Group(
    "curator",
    "inside a proposal's thread: answer the Curator's proposal, or merge its change, for its owner; inside a "
    "Curator's Builder: drop its change",
    [CuratorAnswer(), CuratorMerge(), CuratorDrop()],
)
