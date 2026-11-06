"""``evo-agents worker agent ask HANDLE TEXT``: the agent of a chat run asks agent HANDLE, of another project, for work
or knowledge of its project (``chat_runs.ASK_COMMAND``; ``evo_agents.hub.server.agent_asks``).

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker chat`` does, refuses outside a chat run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/agent/asks). The hub queues the run of
HANDLE for the person at the origin of the chain, one hop further, and opens a wait of this run that the end of that
run answers: the command notes the wait in the run's file of decisions, as ``worker wait`` does, and says to end the
turn, so the run parks and holds no worker; the answer, or why the run asked failed, comes back in this session as the
message that answers the wait. A refusal (the person may not give HANDLE work, HANDLE has no pool, the hops) is one
``error:`` line on stderr and exit status 1.
"""

from __future__ import annotations

import sys

COMMAND = "agent ask"
ROUTE = "agent/asks"


def cmd_agent_ask(args) -> int:
    from evo_agents.worker.chat_cli import chat_command

    return chat_command(_ask)(args)


def _ask(args) -> int:
    from evo_agents.worker import agent_cli, chat_cli

    run_id, home, record = chat_cli.running_run(COMMAND)
    kind = record.get("kind") or "step"
    if kind != "chat":
        article = "an" if kind[:1] in "aeiou" else "a"
        raise chat_cli.WorkerStateError(
            f"run {run_id} is {article} {kind} run: only the agent of a chat run asks an agent of another project "
            f"with `evo-agents worker {COMMAND}`"
        )
    run = chat_cli.AgentRun(run_id, kind, home.load_config(), home.load_token())
    handle = args.handle.removeprefix("@").lower()
    answer = chat_cli.call(run, ROUTE, {"handle": handle, "body": chat_cli.text_of(args.text)})
    if answer.get("refusal"):
        print(f"error: @{handle} took no work: {answer['refusal']}", file=sys.stderr)
        return chat_cli.EXIT_FAILED
    decision_id = agent_cli.note_decision(home, run_id, answer.get("decision_id"))
    print(
        f"Asked @{handle}: its run is #{answer.get('run_id')}, and wait #{decision_id} of this run is open until it "
        "ends. End your turn once no work that does not depend on the answer is left: the run waits without the agent "
        f"and holds no worker, and this session goes on with a message naming decision #{decision_id} when the "
        "answer comes."
    )
    return 0


def register_agent(wsub) -> None:
    """``evo-agents worker agent ask``, under the parser ``wsub`` of ``evo-agents worker``."""
    agent = wsub.add_parser("agent", help="inside a chat run: ask an agent of another project")
    asub = agent.add_subparsers(dest="agent_command", required=True)
    ask = asub.add_parser(
        "ask",
        help="ask agent HANDLE of another project for work or knowledge of its project; ends in a wait for its answer",
        description="Ask agent HANDLE, of another project, for work or knowledge this run cannot reach. Its run works "
        "for the person at the origin of the chain, one hop further; this run waits for its answer: end your turn "
        "after it.",
    )
    ask.add_argument("handle", metavar="HANDLE", help="the agent asked, @ left out or not")
    ask.add_argument("text", metavar="TEXT", help="what you ask, all the agent needs, at most 16 KiB (- for stdin)")
    ask.set_defaults(func=cmd_agent_ask)
