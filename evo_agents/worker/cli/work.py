"""``evo-agents worker work set --title TEXT --goal TEXT --done-when TEXT --outcome met|not_met --left TEXT``: the
entry agent of an agent system, in a chat run of a trace whose hand-off opened a work chat, names the work of that trace
and, before its final answer, records its outcome against the done-when with what is still to do
(``docs/workers.md``, Work chats; ``evo_agents.hub.server.work_cards``).

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker agent ask`` does, refuses outside a chat run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/work). The work card in the thread of the
person's message shows what it sets. A refusal (a run in no trace with a work chat, an agent that is not the system's
entry agent) is one ``error:`` line on stderr and exit status 1.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Text, UsageError
from evo_agents.worker.cli.agent import chat_failed, chat_only
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext

ROUTE = "work"
FIELDS = ("title", "goal", "done_when", "outcome", "left")
OUTCOMES = ("met", "not_met")  # evo_agents.hub.server.work_status.OUTCOMES


def payload(args) -> dict:
    """The body of ``work set``: the fields given."""
    return {name: getattr(args, name) for name in FIELDS if getattr(args, name, None)}


class WorkSet(AgentCommand):
    name = "set"
    words = "work set"
    help = "set the title, goal, done-when and outcome of the work its card shows"
    kinds = None  # any run's agent reaches it; ``chat_only`` refuses another kind than chat
    parser_options = {
        "description": "As the system's entry agent, once a hand-off opened the work chat of this trace: set what "
        "the work card shows. Give at least one flag; one not given keeps its value. Before the final answer, record "
        "--outcome met or not_met against the done-when, with --left naming what is still to do when not met; an "
        "outcome given without --left clears what was left."
    }
    arguments = (
        Arg("--title", metavar="TEXT", help="a short name of the work, at most 200 characters"),
        Arg("--goal", metavar="TEXT", help="what the work should reach"),
        Arg("--done-when", dest="done_when", metavar="TEXT", help="when the work is finished"),
        Arg("--outcome", choices=OUTCOMES, help="whether the work reached its done-when: met or not_met"),
        Arg("--left", metavar="TEXT", help="what is still to do when the done-when is not met"),
    )

    def check(self, args: argparse.Namespace) -> None:
        if not payload(args):
            raise UsageError("give --title, --goal, --done-when, --outcome or --left")

    def run(self, ctx: AgentRunContext) -> Result:
        chat_only(ctx, "names the work of its trace")
        card = ctx.in_chats(ROUTE, payload(ctx.args))
        outcome = f", outcome {card['outcome']}" if card.get("outcome") else ""
        return Result(
            card,
            Text(
                f"The work of trace #{card.get('trace_id')} is now: {card.get('title')} (status "
                f"{card.get('status')}{outcome})."
            ),
        )

    def failed(self, exc: Exception) -> int | None:
        return chat_failed(exc)


WORK_SET = WorkSet()
WORK = Group(
    "work",
    "inside a chat run of an agent system's entry agent: name the work of the trace",
    [WORK_SET],
    description="The work of the trace of this chat run, which its work chat and its card show.",
)
