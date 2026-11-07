"""``evo-agents worker agent ask HANDLE TEXT``: the agent of a chat run asks agent HANDLE, of another project, for work
or knowledge of its project (``chat_runs.ASK_COMMAND``; ``evo_agents.hub.server.agent_asks``).

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker chat`` does, refuses outside a chat run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/agent/asks). The hub queues the run of
HANDLE for the person at the origin of the chain, one hop further, and opens a wait of this run that the end of that
run answers: the command notes the wait in the run's file of decisions, as ``worker wait`` does, and says to end the
turn, so the run parks and holds no worker; the answer, or why the run asked failed, comes back in this session as the
message that answers the wait. A refusal (the person may not give HANDLE work, HANDLE has no pool, the hops) is one
``error:`` line on stderr and exit status 1.

The brief flags (``--goal``, ``--output``, ``--bounds``, ``--done-when``, ``--ref``) head the prompt of the run asked;
``--reason`` says why an agent of an agent system asks an agent off its interacts edges, which the hub refuses
without one. With none of them, the ask is as before. ``evo-agents worker agent forward --run RUN`` posts the last
answer of run RUN, of the same trace, verbatim into this run's thread (POST /v1/worker/runs/{id}/agent/forwards).
"""

from __future__ import annotations

import sys

COMMAND = "agent ask"
ROUTE = "agent/asks"
FORWARD_COMMAND = "agent forward"
FORWARD_ROUTE = "agent/forwards"
BRIEF = (
    "goal",
    "output",
    "bounds",
    "done_when",
    "refs",
    "reason",
)  # the optional fields of an ask, as the hub names them


def cmd_agent_ask(args) -> int:
    from evo_agents.worker.chat_cli import chat_command

    return chat_command(_ask)(args)


def chat_run(command: str, what: str):
    from evo_agents.worker import chat_cli

    run_id, home, record = chat_cli.running_run(command)
    kind = record.get("kind") or "step"
    if kind != "chat":
        article = "an" if kind[:1] in "aeiou" else "a"
        raise chat_cli.WorkerStateError(
            f"run {run_id} is {article} {kind} run: only the agent of a chat run {what} with `evo-agents worker "
            f"{command}`"
        )
    return run_id, home, chat_cli.AgentRun(run_id, kind, home.load_config(), home.load_token())


def ask_payload(args, handle: str, body: str) -> dict:
    """The body of the ask: the handle and text, and the brief fields and reason only when given."""
    payload: dict = {"handle": handle, "body": body}
    for name in BRIEF:
        value = getattr(args, name, None)
        if value:
            payload[name] = value
    return payload


def _ask(args) -> int:
    from evo_agents.worker import agent_cli, chat_cli

    run_id, home, run = chat_run(COMMAND, "asks an agent of another project")
    handle = args.handle.removeprefix("@").lower()
    answer = chat_cli.call(run, ROUTE, ask_payload(args, handle, chat_cli.text_of(args.text)))
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


def cmd_agent_forward(args) -> int:
    from evo_agents.worker.chat_cli import chat_command

    return chat_command(_forward)(args)


def _forward(args) -> int:
    from evo_agents.worker import chat_cli

    _, _, run = chat_run(FORWARD_COMMAND, "forwards an answer")
    answer = chat_cli.call(run, FORWARD_ROUTE, {"run_id": args.run})
    if answer.get("refusal"):
        print(f"error: run #{args.run} was not forwarded: {answer['refusal']}", file=sys.stderr)
        return chat_cli.EXIT_FAILED
    print(
        f"Forwarded the last answer of run #{args.run} verbatim into this thread (message #{answer.get('message_id')})."
    )
    return 0


def register_agent(wsub) -> None:
    """``evo-agents worker agent ask`` and ``agent read`` (``agent_read_cli``), under the parser ``wsub`` of
    ``evo-agents worker``."""
    from evo_agents.worker.agent_read_cli import register_read

    agent = wsub.add_parser("agent", help="inside a chat run: ask an agent of another project, or read that project")
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
    brief = ask.add_argument_group("brief", "heads the prompt of the run asked; give at least goal, output, done-when")
    brief.add_argument("--goal", help="what the agent asked should reach")
    brief.add_argument("--output", help="what its answer should hold")
    brief.add_argument("--bounds", help="what it should not do or touch")
    brief.add_argument("--done-when", dest="done_when", help="when its work is finished")
    brief.add_argument(
        "--ref",
        dest="refs",
        action="append",
        metavar="REF",
        help="a ref it starts from (repo:path, a run, a URL); repeat",
    )
    ask.add_argument(
        "--reason", help="why you ask an agent off your system's interacts edges; such an ask is refused without one"
    )
    ask.set_defaults(func=cmd_agent_ask)
    forward = asub.add_parser(
        "forward",
        help="post the last answer of run RUN of your trace verbatim into this run's thread",
        description="Post the last answer of run RUN, a run of the same trace of your agent system that has ended, "
        "verbatim into this run's thread, files and interactive answers included: it shows as written by RUN's agent "
        "and forwarded by you.",
    )
    forward.add_argument("--run", type=int, required=True, metavar="RUN", help="the run whose answer is forwarded")
    forward.set_defaults(func=cmd_agent_forward)
    register_read(asub)
