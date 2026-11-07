"""``evo-agents worker escalate --question TEXT``: the agent of a chat run, an agent of an agent system, asks the
system's Orchestrator (``evo_agents.hub.server.orchestrator``; ``docs/workers.md``, Escalation).

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker agent ask`` does, refuses outside a chat run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/escalate). The hub opens a thread to the
Orchestrator that orchestrates the agent (else the one of its escalates edge) in the system's ops chat, at the hop of
this run: an escalation adds no hop and no budget refuses it. It opens a wait of this run that the Orchestrator's
answer wakes, or the end of the run of the agent it gives the work to: the command notes the wait in the run's file of
decisions, as ``worker wait`` does, and says to end the turn, so the run parks and holds no worker. A refusal (no
Orchestrator, the Orchestrator takes no work) is one ``error:`` line on stderr and exit status 1.

The brief flags (``--goal``, ``--output``, ``--bounds``, ``--done-when``, ``--ref``) head the Orchestrator's thread, as
they head the prompt of ``agent ask``.
"""

from __future__ import annotations

import sys

COMMAND = "escalate"
ROUTE = "escalate"
BRIEF = ("goal", "output", "bounds", "done_when", "refs")  # the optional fields of an escalation, as the hub names them


def cmd_escalate(args) -> int:
    from evo_agents.worker.chat_cli import chat_command

    return chat_command(_escalate)(args)


def payload(args, question: str) -> dict:
    """The body of the escalation: the question, and the brief fields only when given."""
    found: dict = {"question": question}
    for name in BRIEF:
        value = getattr(args, name, None)
        if value:
            found[name] = value
    return found


def _escalate(args) -> int:
    from evo_agents.worker import agent_ask_cli, agent_cli, chat_cli

    run_id, home, run = agent_ask_cli.chat_run(COMMAND, "escalates")
    answer = chat_cli.call(run, ROUTE, payload(args, chat_cli.text_of(args.question)))
    if answer.get("refusal"):
        print(f"error: the escalation was refused: {answer['refusal']}", file=sys.stderr)
        return chat_cli.EXIT_FAILED
    decision_id = agent_cli.note_decision(home, run_id, answer.get("decision_id"))
    print(
        f"Escalated to the Orchestrator: its run is #{answer.get('run_id')} (thread #{answer.get('thread_id')}), and "
        f"wait #{decision_id} of this run is open until it answers. End your turn now: the run waits without the agent "
        f"and holds no worker, and this session goes on with a message naming decision #{decision_id} when the answer "
        "comes."
    )
    return 0


def register_escalate(wsub) -> None:
    """``evo-agents worker escalate``, under the parser ``wsub`` of ``evo-agents worker``."""
    parser = wsub.add_parser(
        "escalate",
        help="inside a chat run of an agent system: ask the system's Orchestrator; ends in a wait for its answer",
        description="Ask the Orchestrator of your agent system what you cannot settle yourself. It answers, gives the "
        "work to another agent of the system, or asks the owner; this run waits for that: end your turn after it.",
    )
    parser.add_argument(
        "--question", required=True, metavar="TEXT", help="what you ask, all it needs, at most 16 KiB (- for stdin)"
    )
    brief = parser.add_argument_group("brief", "heads the Orchestrator's thread")
    brief.add_argument("--goal", help="what the answer should reach")
    brief.add_argument("--output", help="what the answer should hold")
    brief.add_argument("--bounds", help="what it should not do or touch")
    brief.add_argument("--done-when", dest="done_when", help="when the work is finished")
    brief.add_argument(
        "--ref", dest="refs", action="append", metavar="REF", help="a ref it starts from (repo:path, a run, a URL)"
    )
    parser.set_defaults(func=cmd_escalate)
