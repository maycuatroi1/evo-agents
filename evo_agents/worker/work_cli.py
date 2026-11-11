"""``evo-agents worker work set --title TEXT --goal TEXT --done-when TEXT``: the entry agent of an agent system, in a
chat run of a trace whose hand-off opened a work chat, names the work of that trace (``docs/workers.md``, Work chats;
``evo_agents.hub.server.work_cards``).

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker agent ask`` does, refuses outside a chat run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/work). The work card in the thread of the
person's message shows what it sets. A refusal (a run in no trace with a work chat, an agent that is not the system's
entry agent) is one ``error:`` line on stderr and exit status 1.
"""

from __future__ import annotations

import sys

COMMAND = "work set"
ROUTE = "work"
FIELDS = ("title", "goal", "done_when")


def cmd_work_set(args) -> int:
    from evo_agents.worker.chat_cli import chat_command

    return chat_command(_work_set)(args)


def payload(args) -> dict:
    """The body of ``work set``: the fields given."""
    return {name: getattr(args, name) for name in FIELDS if getattr(args, name, None)}


def _work_set(args) -> int:
    from evo_agents.worker import agent_ask_cli, chat_cli

    body = payload(args)
    if not body:
        print("error: give --title, --goal or --done-when", file=sys.stderr)
        return chat_cli.EXIT_USAGE
    _, _, run = agent_ask_cli.chat_run(COMMAND, "names the work of its trace")
    card = chat_cli.call(run, ROUTE, body)
    print(f"The work of trace #{card.get('trace_id')} is now: {card.get('title')} (status {card.get('status')}).")
    return 0


def register_work(wsub) -> None:
    """``evo-agents worker work set``, under the parser ``wsub`` of ``evo-agents worker``."""
    parser = wsub.add_parser(
        "work",
        help="inside a chat run of an agent system's entry agent: name the work of the trace",
        description="The work of the trace of this chat run, which its work chat and its card show.",
    )
    sub = parser.add_subparsers(dest="work_command", required=True)
    setter = sub.add_parser(
        "set",
        help="set the title, goal and done-when of the work its card shows",
        description="As the system's entry agent, once a hand-off opened the work chat of this trace: set what the "
        "work card shows. Give at least one flag; one not given keeps its value.",
    )
    setter.add_argument("--title", metavar="TEXT", help="a short name of the work, at most 200 characters")
    setter.add_argument("--goal", metavar="TEXT", help="what the work should reach")
    setter.add_argument("--done-when", dest="done_when", metavar="TEXT", help="when the work is finished")
    setter.set_defaults(func=cmd_work_set)
