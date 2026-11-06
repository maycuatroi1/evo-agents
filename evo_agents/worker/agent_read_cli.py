"""``evo-agents worker agent read PROJECT TOOL [TEXT]``: the agent of a chat run reads the knowledge graph or the
memories of another project that the person at the origin of the chain reads (``chat_runs.READ_COMMAND``;
``evo_agents.hub.server.agent_reads``).

TOOL is kg-search, kg-context, memory-search or memory-get, as the evo-hub tool of the same name reads its own project.
It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker chat`` does, refuses outside a chat run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/agent/reads). It prints what the hub lets
into the run: the items the hub sink of the run's project clears, then one line on how many were left out and why.
A refusal (no grant on PROJECT, the run's own project) is one ``error:`` line on stderr and exit status 1.
"""

from __future__ import annotations

import json

from evo_agents.worker.agent_cli import json_option, returns_object  # the hub CLI's, as agent_cli borrows them

COMMAND = "agent read"
ROUTE = "agent/reads"
TOOLS = ("kg-search", "kg-context", "memory-search", "memory-get")
READ_KEYS = ("project", "tool", "text", "shown", "withheld", "why")


def cmd_agent_read(args) -> int:
    from evo_agents.worker.chat_cli import chat_command

    return chat_command(_read)(args)


def _body(args) -> dict:
    from evo_agents.worker import chat_cli

    tool = args.tool.replace("-", "_")
    body: dict = {"project": args.project, "tool": tool, "limit": args.limit}
    if tool == "memory_get":
        if not args.text or not args.text.isdigit():
            raise chat_cli.WorkerStateError("memory-get takes the memory's id as TEXT")
        body["memory_id"] = int(args.text)
    elif tool == "kg_context" and args.ids:
        body["ids"] = list(args.ids)
    elif not args.text:
        raise chat_cli.WorkerStateError(f"{args.tool} takes TEXT, what to look for")
    else:
        body["query"] = args.text
    return body


def read_json(answer: dict) -> dict:
    """What ``--json`` prints of the hub's answer: its READ_KEYS, each null when the hub left it out."""
    return {key: answer.get(key) for key in READ_KEYS}


def _read(args) -> int:
    from evo_agents.worker import chat_cli

    run_id, home, record = chat_cli.running_run(COMMAND)
    kind = record.get("kind") or "step"
    if kind != "chat":
        article = "an" if kind[:1] in "aeiou" else "a"
        raise chat_cli.WorkerStateError(
            f"run {run_id} is {article} {kind} run: only the agent of a chat run reads another project with "
            f"`evo-agents worker {COMMAND}`"
        )
    run = chat_cli.AgentRun(run_id, kind, home.load_config(), home.load_token())
    answer = chat_cli.call(run, ROUTE, _body(args))
    if args.json:
        print(json.dumps(read_json(answer), ensure_ascii=False))
    else:
        print(answer.get("text") or "")
    return 0


def register_read(asub) -> None:
    """``evo-agents worker agent read``, under the subparsers ``asub`` of ``evo-agents worker agent``."""
    read = asub.add_parser(
        "read",
        help="read the knowledge graph or the memories of another project, within what this run's project lets in",
        description="Read the knowledge graph (kg-search, kg-context) or the memories (memory-search, memory-get) of "
        "another project that the person at the origin of the chain reads. The hub leaves out what the hub sink of "
        "this run's project does not clear and says how many items it left out and why.",
    )
    read.add_argument("project", metavar="PROJECT", help="the project read, another than this run's")
    read.add_argument("tool", metavar="TOOL", choices=TOOLS, help="kg-search, kg-context, memory-search or memory-get")
    read.add_argument("text", metavar="TEXT", nargs="?", help="what to look for; for memory-get, the memory's id")
    read.add_argument("--id", dest="ids", action="append", help="kg-context: a node id, in place of TEXT (repeatable)")
    read.add_argument("--limit", type=int, default=10, help="kg-search, memory-search: at most this many (1 to 20)")
    json_option(read, returns_object(*READ_KEYS), help="the hub's answer as JSON")
    read.set_defaults(func=cmd_agent_read)
